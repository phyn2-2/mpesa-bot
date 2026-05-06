"""
mpesa-bot/ingestion/ingest.py

Ingestion layer: the entry point for all incoming messages.

Responsibilities:
  1. Validate input before touching the database
  2. Normalise raw_text (strip whitespace) — same SMS pasted with a
     trailing newline must not produce a different dedup hash
  3. Write to raw_events (store first, always)
  4. Return a typed IngestResult so callers know exactly what happened

Does NOT:
  - Call the processing pipeline (single responsibility)
  - Store trust_level (derived from source in config, not persisted)
  - Make decisions about what to do with the result

The handler (bot/handlers.py) receives IngestResult and decides
whether to trigger the pipeline based on status.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import sqlite3

from config import MAX_RAW_TEXT_LENGTH, VALID_SOURCES, trust_level_for
from db.repository import insert_raw_event


# ------------------------------------------------------------------ #
# Result type                                                         #
# ------------------------------------------------------------------ #

class IngestStatus(str, Enum):
    ACCEPTED   = "accepted"    # written to raw_events, pipeline should run
    DUPLICATE  = "duplicate"   # already seen, safe to ignore
    INVALID    = "invalid"     # bad input, do not retry
    DB_ERROR   = "db_error"    # unexpected DB failure, may retry


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    raw_event_id: Optional[int]  # set only when status=ACCEPTED
    reason: Optional[str]        # human-readable explanation for non-ACCEPTED results

    # Convenience predicates so callers don't compare strings
    @property
    def accepted(self) -> bool:
        return self.status == IngestStatus.ACCEPTED

    @property
    def should_pipeline(self) -> bool:
        """True when the pipeline should be triggered for this event."""
        return self.status == IngestStatus.ACCEPTED


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def ingest(
    user_id: str,
    raw_text: str,
    source: str,
) -> IngestResult:
    """
    Ingest an incoming M-Pesa SMS message.

    Parameters
    ----------
    user_id : str
        Telegram chat ID of the message sender.
    raw_text : str
        The raw SMS text, as received. Will be stripped of leading/
        trailing whitespace before storage. Stripping is intentional:
        a user pasting the same SMS with an accidental trailing newline
        should not produce a new event.
    source : str
        'manual' (user typed/pasted) or 'auto' (MacroDroid forwarded).

    Returns
    -------
    IngestResult
        Always returned, never raises. Callers check .status.
    """

    # ── Step 1: validate source before any DB work ────────────────────
    # Fail fast on bad input. No point opening a DB connection for
    # data we know is wrong.
    if source not in VALID_SOURCES:
        return IngestResult(
            status=IngestStatus.INVALID,
            raw_event_id=None,
            reason=f"Unknown source {source!r}. Expected one of: {sorted(VALID_SOURCES)}",
        )

    # ── Step 2: validate and normalise raw_text ───────────────────────
    if not user_id or not user_id.strip():
        return IngestResult(
            status=IngestStatus.INVALID,
            raw_event_id=None,
            reason="user_id is empty",
        )

    normalised = raw_text.strip()

    if not normalised:
        return IngestResult(
            status=IngestStatus.INVALID,
            raw_event_id=None,
            reason="raw_text is empty after stripping whitespace",
        )

    if len(normalised) > MAX_RAW_TEXT_LENGTH:
        return IngestResult(
            status=IngestStatus.INVALID,
            raw_event_id=None,
            reason=(
                f"raw_text length {len(normalised)} exceeds maximum "
                f"{MAX_RAW_TEXT_LENGTH}. Not a valid M-Pesa SMS."
            ),
        )

    # ── Step 3: write to raw_events ───────────────────────────────────
    # insert_raw_event returns None on duplicate hash (idempotent).
    # It re-raises IntegrityError on constraint violations other than
    # dedup (e.g. bad source CHECK) — those are caught below as DB_ERROR
    # since they indicate a programming bug, not a user action.
    try:
        raw_event_id = insert_raw_event(
            user_id=user_id,
            raw_text=normalised,
            source=source,
        )
    except sqlite3.IntegrityError as e:
        # This path should not be reachable if source validation above
        # is correct, but we guard it anyway.
        return IngestResult(
            status=IngestStatus.DB_ERROR,
            raw_event_id=None,
            reason=f"Unexpected DB constraint error: {e}",
        )
    except Exception as e:
        return IngestResult(
            status=IngestStatus.DB_ERROR,
            raw_event_id=None,
            reason=f"DB error: {e}",
        )

    if raw_event_id is None:
        return IngestResult(
            status=IngestStatus.DUPLICATE,
            raw_event_id=None,
            reason="Message already ingested (duplicate hash)",
        )

    return IngestResult(
        status=IngestStatus.ACCEPTED,
        raw_event_id=raw_event_id,
        reason=None,
    )
