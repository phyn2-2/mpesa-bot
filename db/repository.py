"""
mpesa-bot/db/repository.py

All database reads and writes live here.
No SQL is written anywhere else in the codebase.

Design rules enforced here:
- PRAGMA foreign_keys = ON on every connection (does not persist)
- Atomic processing: insert transaction + mark processed = one transaction
- Dedup hash uses structured serialisation, not string concatenation
- process_attempts is always incremented on failure (never overwritten)
- All date arithmetic uses UTC+3 offset for Nairobi (EAT)
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from config import DB_PATH

# ------------------------------------------------------------------ #
# Connection                                                          #
# ------------------------------------------------------------------ #

@contextmanager
def _connect():
    """
    Open a connection, set required PRAGMAs, yield, commit or rollback.

    foreign_keys MUST be set per-connection — it does not persist.
    WAL mode is already set by schema.sql, but setting it here is
    idempotent and makes this function self-contained.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Schema init                                                         #
# ------------------------------------------------------------------ #

def init_db(schema_path: str = "db/schema.sql") -> None:
    """
    Run schema.sql against the database.
    Safe to call on every startup — all statements use IF NOT EXISTS.
    """
    with open(schema_path) as f:
        sql = f.read()
    with _connect() as conn:
        conn.executescript(sql)


# ------------------------------------------------------------------ #
# Data classes                                                        #
# ------------------------------------------------------------------ #

@dataclass
class RawEvent:
    id: int
    user_id: str
    raw_text: str
    source: str
    dedup_hash: str
    received_at: str
    processed: int
    process_attempts: int
    process_error: Optional[str]


@dataclass
class Transaction:
    id: int
    raw_event_id: int
    amount: int
    type: str
    code: Optional[str]
    counterparty: Optional[str]
    confidence: str
    transaction_at: Optional[str]
    created_at: str


# ------------------------------------------------------------------ #
# Dedup hash                                                          #
# ------------------------------------------------------------------ #

def make_dedup_hash(user_id: str, raw_text: str) -> str:
    """
    SHA-256 of a structured JSON payload.

    Why JSON and not concatenation:
      user_id="abc" + raw_text="defghi"  →  "abcdefghi"
      user_id="abcdef" + raw_text="ghi"  →  "abcdefghi"
    Same string, different events, same hash → wrong dedup.

    json.dumps with sort_keys guarantees a stable, unambiguous payload.
    """
    payload = json.dumps(
        {"user_id": user_id, "text": raw_text},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ #
# raw_events writes                                                   #
# ------------------------------------------------------------------ #

def insert_raw_event(
    user_id: str,
    raw_text: str,
    source: str,
) -> Optional[int]:
    """
    Write a raw event. Returns the new row id, or None if duplicate.

    source must be 'manual' or 'auto' — enforced by DB CHECK constraint.
    trust_level is NOT stored: it is fully determined by source and
    should be derived in application code, not duplicated in the DB.

    Duplicate detection is global (no time window). For financial data,
    the same raw_text from the same user is always the same event,
    regardless of when it arrives.
    """
    dedup_hash = make_dedup_hash(user_id, raw_text)
    try:
        with _connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO raw_events (user_id, raw_text, source, dedup_hash)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, raw_text, source, dedup_hash),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError as e:
        # Only swallow duplicate hash collisions (dedup).
        # Re-raise CHECK constraint failures (bad source value, etc.)
        # so callers are not silently misled about bad input data.
        if "dedup_hash" in str(e):
            return None
        raise


# ------------------------------------------------------------------ #
# Atomic processing                                                   #
# ------------------------------------------------------------------ #

def complete_processing(
    raw_event_id: int,
    amount: int,
    type_: str,
    confidence: str,
    code: Optional[str] = None,
    counterparty: Optional[str] = None,
    transaction_at: Optional[str] = None,
) -> Optional[int]:
    """
    Insert a transaction AND mark the raw event processed in one transaction.

    This is the critical atomic operation. The two operations must never
    be split across separate connections:

      WRONG:
        insert_transaction(...)   ← succeeds
        crash
        mark_processed(...)       ← never runs
        → event reprocessed on restart, hits unique constraint "by accident"

      RIGHT (this function):
        BEGIN
          INSERT INTO transactions ...
          UPDATE raw_events SET processed=1 ...
        COMMIT  ← both succeed or both roll back

    Returns the new transaction id, or None if the code is a duplicate
    (in which case the raw event is still marked processed to avoid
    infinite retry on a known duplicate).
    """
    with _connect() as conn:
        txn_id: Optional[int] = None
        try:
            cursor = conn.execute(
                """
                INSERT INTO transactions
                    (raw_event_id, amount, type, code, counterparty,
                     confidence, transaction_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (raw_event_id, amount, type_, code, counterparty,
                 confidence, transaction_at),
            )
            txn_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate transaction code. Mark processed so we stop retrying.
            conn.execute(
                """
                UPDATE raw_events
                SET processed = 1, process_error = 'duplicate_code'
                WHERE id = ?
                """,
                (raw_event_id,),
            )
            return None

        conn.execute(
            "UPDATE raw_events SET processed = 1, process_error = NULL WHERE id = ?",
            (raw_event_id,),
        )
        return txn_id


def record_processing_failure(raw_event_id: int, error: str) -> None:
    """
    Increment attempt counter and store error. Does NOT mark processed=1.
    The event stays in the work queue for retry.

    process_attempts lets the caller enforce a retry limit:
        event = get_raw_event(id)
        if event.process_attempts >= MAX_ATTEMPTS:
            abandon(event)
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE raw_events
            SET process_attempts = process_attempts + 1,
                process_error = ?
            WHERE id = ?
            """,
            (error, raw_event_id),
        )


# ------------------------------------------------------------------ #
# raw_events reads                                                    #
# ------------------------------------------------------------------ #

def get_unprocessed_events(limit: int = 50) -> list[RawEvent]:
    """
    Return pending events ordered oldest-first.
    Limit prevents unbounded work on a large backlog.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM raw_events
            WHERE processed = 0
            ORDER BY received_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [RawEvent(**dict(row)) for row in rows]


def get_failed_events(user_id: str, limit: int = 20) -> list[RawEvent]:
    """
    Events that failed parsing and have not been resolved.
    Used by the /unknown Telegram command.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM raw_events
            WHERE user_id = ?
              AND processed = 0
              AND process_error IS NOT NULL
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [RawEvent(**dict(row)) for row in rows]


# ------------------------------------------------------------------ #
# Insights reads                                                      #
# ------------------------------------------------------------------ #

# EAT offset applied to all date arithmetic.
# SQLite's 'now' is UTC. Kenya is UTC+3.
# Using '+3 hours' converts the boundary correctly:
#   date('now', '+3 hours') = today in Nairobi
# This means "today" resets at midnight Nairobi time, not midnight UTC.
_EAT_OFFSET = "+3 hours"


def get_spend_today(user_id: str) -> int:
    """Total outgoing spend for today (Nairobi calendar day), KES."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN raw_events r ON r.id = t.raw_event_id
            WHERE r.user_id = :user_id
              AND t.type IN ('send', 'paybill', 'buy_goods', 'airtime')
              AND t.confidence IN ('HIGH', 'MEDIUM')
              AND date(
                    COALESCE(t.transaction_at, r.received_at),
                    :offset
                  ) = date('now', :offset)
            """,
            {"user_id": user_id, "offset": _EAT_OFFSET},
        ).fetchone()
    return row["total"]


def get_spend_yesterday(user_id: str) -> int:
    """Total outgoing spend for yesterday (Nairobi calendar day), KES."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN raw_events r ON r.id = t.raw_event_id
            WHERE r.user_id = :user_id
              AND t.type IN ('send', 'paybill', 'buy_goods', 'airtime')
              AND t.confidence IN ('HIGH', 'MEDIUM')
              AND date(
                    COALESCE(t.transaction_at, r.received_at),
                    :offset
                  ) = date('now', :offset, '-1 day')
            """,
            {"user_id": user_id, "offset": _EAT_OFFSET},
        ).fetchone()
    return row["total"]


def get_spend_last_7_days(user_id: str) -> int:
    """Total outgoing spend for the last 7 Nairobi calendar days, KES."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN raw_events r ON r.id = t.raw_event_id
            WHERE r.user_id = :user_id
              AND t.type IN ('send', 'paybill', 'buy_goods', 'airtime')
              AND t.confidence IN ('HIGH', 'MEDIUM')
              AND date(
                    COALESCE(t.transaction_at, r.received_at),
                    :offset
                  ) >= date('now', :offset, '-6 days')
            """,
            {"user_id": user_id, "offset": _EAT_OFFSET},
        ).fetchone()
    return row["total"]


def get_raw_event_by_id(raw_event_id: int) -> Optional[RawEvent]:
    """
    Fetch a single raw event by primary key.

    Added for pipeline.py: the pipeline works from IDs (not raw text)
    to enforce store-first. It fetches the event here, then processes it.

    Returns None if the ID does not exist — pipeline treats this as
    a programming bug (NOT_FOUND status) not a retriable error.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM raw_events WHERE id = ?",
            (raw_event_id,),
        ).fetchone()
    if row is None:
        return None
    return RawEvent(**dict(row))


def get_latest_transaction(user_id: str) -> Optional[Transaction]:
    """Most recent transaction for a user, regardless of type."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT t.*
            FROM transactions t
            JOIN raw_events r ON r.id = t.raw_event_id
            WHERE r.user_id = ?
            ORDER BY COALESCE(t.transaction_at, r.received_at) DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return Transaction(**dict(row))
