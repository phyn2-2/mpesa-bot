"""
mpesa-bot/processing/pipeline.py

Orchestrates the processing of a stored raw event into a transaction.

Responsibility:
  Given a raw_event_id → fetch → extract → classify → score → persist.
  Return a typed PipelineResult describing exactly what happened.

Does NOT:
  - Accept raw text directly (enforces store-first: only process
    what is already persisted in raw_events)
  - Send Telegram messages (handlers.py does that)
  - Make retry decisions (callers decide based on PipelineResult.status)

PROCESSING FLOW
  1. Fetch raw event by ID          → NOT_FOUND if missing
  2. Already processed?             → return early (idempotent)
  3. Attempts >= MAX?               → MAX_ATTEMPTS_REACHED, abandon
  4. Extract fields from raw_text
  5. Classify raw_text
  6. Score (amount, code, type)
  7. FAILED confidence?             → PARSE_FAILED, mark processed,
                                       do not retry (terminal state)
  8. complete_processing()          → SUCCESS or DUPLICATE
  9. Unexpected exception           → TRANSIENT_ERROR, increment
                                       attempts, leave in queue for retry

TERMINAL vs RETRIABLE
  Terminal (processed=1, no retry):
    SUCCESS, PARSE_FAILED, DUPLICATE, MAX_ATTEMPTS_REACHED, NOT_FOUND
  Retriable (processed=0, attempt counter incremented):
    TRANSIENT_ERROR

LOW confidence (unclassified + amount):
  Inserted into transactions with type='unclassified', confidence='LOW'.
  Money moved — we record it even if we can't classify it.
  Handler notes low confidence in the Telegram response.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import MAX_PROCESS_ATTEMPTS
from db.repository import (
    complete_processing,
    get_raw_event_by_id,
    record_processing_failure,
)
from processing.classify import classify
from processing.extract  import extract_all
from processing.score    import FAILED as SCORE_FAILED, score


# ------------------------------------------------------------------ #
# Result type                                                         #
# ------------------------------------------------------------------ #

class PipelineStatus(str, Enum):
    SUCCESS              = "success"
    PARSE_FAILED         = "parse_failed"
    DUPLICATE            = "duplicate"
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    TRANSIENT_ERROR      = "transient_error"
    NOT_FOUND            = "not_found"
    ALREADY_PROCESSED    = "already_processed"


@dataclass(frozen=True)
class PipelineResult:
    """
    Describes the outcome of processing one raw event.

    Fields are populated based on how far processing got:
    - transaction_id: set only on SUCCESS
    - type_, confidence, amount: set when extraction/classification ran
    - reason: human-readable detail for non-SUCCESS outcomes

    Handlers use this to format the Telegram response.
    """
    status:         PipelineStatus
    raw_event_id:   int
    transaction_id: Optional[int]   = None
    type_:          Optional[str]   = None
    confidence:     Optional[str]   = None
    amount:         Optional[int]   = None
    reason:         Optional[str]   = None

    @property
    def succeeded(self) -> bool:
        return self.status == PipelineStatus.SUCCESS

    @property
    def is_terminal(self) -> bool:
        """
        True when this event should not be retried.
        Callers can gate retry logic on this property rather than
        comparing status strings directly.
        """
        return self.status not in (
            PipelineStatus.TRANSIENT_ERROR,
        )


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def run(raw_event_id: int) -> PipelineResult:
    """
    Process a single raw event end-to-end.

    Parameters
    ----------
    raw_event_id : int
        Primary key of an existing raw_events row.

    Returns
    -------
    PipelineResult
        Always returned, never raises.
        Check .status or .succeeded for outcome.
    """

    # ── Step 1: fetch raw event ───────────────────────────────────────
    try:
        event = get_raw_event_by_id(raw_event_id)
    except Exception as e:
        # DB unavailable — transient, leave unprocessed for retry
        return PipelineResult(
            status=PipelineStatus.TRANSIENT_ERROR,
            raw_event_id=raw_event_id,
            reason=f"DB fetch error: {e}",
        )

    if event is None:
        return PipelineResult(
            status=PipelineStatus.NOT_FOUND,
            raw_event_id=raw_event_id,
            reason=f"raw_event_id={raw_event_id} does not exist",
        )

    # ── Step 2: already processed? ────────────────────────────────────
    # Idempotent guard. complete_processing() would handle a duplicate
    # code via IntegrityError anyway, but an explicit check here gives
    # a cleaner status and avoids running extract/classify needlessly.
    if event.processed:
        return PipelineResult(
            status=PipelineStatus.ALREADY_PROCESSED,
            raw_event_id=raw_event_id,
            reason="Event already marked processed",
        )

    # ── Step 3: retry limit ───────────────────────────────────────────
    if event.process_attempts >= MAX_PROCESS_ATTEMPTS:
        try:
            record_processing_failure(
                raw_event_id,
                f"abandoned after {event.process_attempts} attempts: "
                f"{event.process_error or 'unknown error'}",
            )
            # Force-mark processed so it leaves the work queue.
            # We do this by calling complete_processing with no
            # transaction data — but complete_processing requires amount.
            # Instead, write the abandon directly.
            _mark_abandoned(raw_event_id, event.process_attempts)
        except Exception:
            pass  # best effort; event stays in queue if this fails
        return PipelineResult(
            status=PipelineStatus.MAX_ATTEMPTS_REACHED,
            raw_event_id=raw_event_id,
            reason=(
                f"Abandoned after {event.process_attempts} attempts. "
                f"Last error: {event.process_error or 'unknown'}"
            ),
        )

    # ── Steps 4–6: extract, classify, score ──────────────────────────
    try:
        fields     = extract_all(event.raw_text)
        type_      = classify(event.raw_text)
        confidence = score(fields.amount, fields.code, type_)
    except Exception as e:
        record_processing_failure(raw_event_id, f"extract/classify error: {e}")
        return PipelineResult(
            status=PipelineStatus.TRANSIENT_ERROR,
            raw_event_id=raw_event_id,
            reason=f"extract/classify error: {e}",
        )

    # ── Step 7: FAILED confidence → terminal parse failure ───────────
    # No amount was extracted. This SMS will never yield a valid
    # transaction. Mark processed so it leaves the work queue.
    # It remains visible via /unknown through process_error field.
    if confidence == SCORE_FAILED:
        try:
            _mark_parse_failed(raw_event_id, fields.raw_amount_str)
        except Exception as e:
            record_processing_failure(raw_event_id, f"mark_failed error: {e}")
        return PipelineResult(
            status=PipelineStatus.PARSE_FAILED,
            raw_event_id=raw_event_id,
            type_=type_,
            confidence=confidence,
            reason=(
                "No amount extracted. "
                f"raw_amount_str={fields.raw_amount_str!r}"
            ),
        )

    # ── Step 8: persist transaction ───────────────────────────────────
    try:
        txn_id = complete_processing(
            raw_event_id=raw_event_id,
            amount=fields.amount,
            type_=type_,
            confidence=confidence,
            code=fields.code,
            counterparty=fields.counterparty,
            transaction_at=fields.transaction_at,
        )
    except Exception as e:
        record_processing_failure(raw_event_id, f"DB write error: {e}")
        return PipelineResult(
            status=PipelineStatus.TRANSIENT_ERROR,
            raw_event_id=raw_event_id,
            type_=type_,
            confidence=confidence,
            amount=fields.amount,
            reason=f"DB write error: {e}",
        )

    # complete_processing returns None on duplicate code
    if txn_id is None:
        return PipelineResult(
            status=PipelineStatus.DUPLICATE,
            raw_event_id=raw_event_id,
            type_=type_,
            confidence=confidence,
            amount=fields.amount,
            reason="Transaction code already exists (duplicate)",
        )

    return PipelineResult(
        status=PipelineStatus.SUCCESS,
        raw_event_id=raw_event_id,
        transaction_id=txn_id,
        type_=type_,
        confidence=confidence,
        amount=fields.amount,
    )


# ------------------------------------------------------------------ #
# Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _mark_parse_failed(raw_event_id: int, raw_amount_str: Optional[str]) -> None:
    """
    Mark a raw event as processed with a parse_failed error.

    Uses repository internals to write directly — there is no
    public complete_processing path for zero-amount events
    (the DB CHECK constraint would reject amount=0 anyway).
    """
    from db import repository
    with repository._connect() as conn:
        conn.execute(
            """
            UPDATE raw_events
            SET processed = 1,
                process_error = ?
            WHERE id = ?
            """,
            (
                f"parse_failed: no_amount"
                + (f" (raw={raw_amount_str!r})" if raw_amount_str else ""),
                raw_event_id,
            ),
        )


def _mark_abandoned(raw_event_id: int, attempts: int) -> None:
    """
    Mark a raw event as processed after exhausting retries.
    Removes it from the work queue while preserving it for audit.
    """
    from db import repository
    with repository._connect() as conn:
        conn.execute(
            """
            UPDATE raw_events
            SET processed = 1,
                process_error = ?
            WHERE id = ?
            """,
            (f"abandoned after {attempts} attempts", raw_event_id),
        )
