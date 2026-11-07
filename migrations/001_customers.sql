-- Existing customer table. Safe to re-run. Does not alter or delete rows.
CREATE TABLE IF NOT EXISTS customers (
    id              SERIAL PRIMARY KEY,
    name            TEXT   NOT NULL,
    phone           TEXT   NOT NULL,
    account         TEXT   NOT NULL,
    profile_name    TEXT   NOT NULL,
    monthly_amount  REAL   NOT NULL DEFAULT 0,
    start_date      TEXT   NOT NULL,
    payment_status  TEXT   NOT NULL DEFAULT 'Payment pending',
    created_at      TEXT   NOT NULL DEFAULT TO_CHAR(CURRENT_DATE, 'YYYY-MM-DD')
);
