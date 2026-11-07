-- Delivery ledger for owner reminders. One row per customer, expiry, and milestone.
-- Safe to re-run. Does not alter or delete customer rows.
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id              SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL,
    expiry_date     TEXT    NOT NULL,
    milestone       TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT reminder_deliveries_status_chk CHECK (status IN ('sending', 'sent', 'failed')),
    CONSTRAINT reminder_deliveries_unique UNIQUE (customer_id, expiry_date, milestone)
);
