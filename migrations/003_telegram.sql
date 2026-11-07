-- Idempotency and confirmation state for the owner bot.
-- Safe to re-run. Does not alter or delete customer rows.
CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id   BIGINT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS telegram_workflows (
    id           TEXT PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    kind         TEXT   NOT NULL,
    payload      TEXT   NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    consumed_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS telegram_workflows_owner_idx
    ON telegram_workflows (owner_id, consumed_at, expires_at);
