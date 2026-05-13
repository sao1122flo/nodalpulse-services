-- Add idempotency_key to jobs for deduplication of admin-triggered enqueues.
-- Apply once against the Railway database:
--   railway run psql $DATABASE_URL -f sql/add_idempotency_key.sql

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key_uidx
    ON jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
