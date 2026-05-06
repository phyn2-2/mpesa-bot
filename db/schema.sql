-- ============================================================
-- mpesa-bot/db/schema.sql
-- SQLite — run once to initialise, safe to re-run (IF NOT EXISTS)
--
-- PRAGMA notes:
--   journal_mode=WAL  → persists on the database file, set here once
--   foreign_keys=ON   → per-connection only, NEVER set here —
--                       must be set in repository.py on every connection
-- ============================================================

PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------
-- raw_events
-- Every incoming message is written here FIRST, before any
-- processing. Nothing is ever deleted. This is the recovery
-- point — if parsing breaks, raw_text lets you reprocess.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_events (
    id               INTEGER PRIMARY KEY,
    user_id          TEXT    NOT NULL,
    raw_text         TEXT    NOT NULL,
    source           TEXT    NOT NULL
                             CHECK(source IN ('manual', 'auto')),
    dedup_hash       TEXT    NOT NULL UNIQUE,
    received_at      TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    processed        INTEGER NOT NULL DEFAULT 0
                             CHECK(processed IN (0, 1)),
    process_attempts INTEGER NOT NULL DEFAULT 0,
    process_error    TEXT
);

-- ------------------------------------------------------------
-- transactions
-- Written ONLY after the parent raw_events row exists.
-- Foreign key is enforced at the DB level (with PRAGMA on).
--
-- user_id is NOT stored here. Derive it by joining raw_events.
-- Two copies with no enforced consistency = silent corruption.
--
-- transaction_at is NULLABLE: some M-Pesa SMS types do not
-- include a parseable timestamp. Insights layer falls back to
-- raw_events.received_at when this is NULL.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY,
    raw_event_id   INTEGER NOT NULL REFERENCES raw_events(id),
    amount         INTEGER NOT NULL CHECK(amount > 0),
    type           TEXT    NOT NULL
                           CHECK(type IN (
                               'send', 'receive', 'paybill',
                               'buy_goods', 'airtime', 'unclassified'
                           )),
    code           TEXT,
    counterparty   TEXT,
    confidence     TEXT    NOT NULL
                           CHECK(confidence IN ('HIGH', 'MEDIUM', 'LOW', 'FAILED')),
    transaction_at TEXT,
    created_at     TEXT    NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ------------------------------------------------------------
-- Indexes
-- ------------------------------------------------------------

-- One transaction per M-Pesa code.
-- Partial: two NULL codes are allowed (some SMS types have none).
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_code
    ON transactions(code) WHERE code IS NOT NULL;

-- Work queue: pending events. Partial stays small as rows flip to 1.
CREATE INDEX IF NOT EXISTS idx_raw_unprocessed
    ON raw_events(received_at) WHERE processed = 0;

-- Insights: filter events by user then time.
CREATE INDEX IF NOT EXISTS idx_raw_user_time
    ON raw_events(user_id, received_at);

-- JOIN acceleration: transactions → raw_events.
CREATE INDEX IF NOT EXISTS idx_txn_event
    ON transactions(raw_event_id);

-- Time-range scans (today/7-day). Partial: NULLs never match range queries.
CREATE INDEX IF NOT EXISTS idx_txn_time
    ON transactions(transaction_at) WHERE transaction_at IS NOT NULL;
