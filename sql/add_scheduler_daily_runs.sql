-- Persistent scheduler state: one row per CT calendar date.
-- Prevents missed daily runs when the scheduler process restarts.
--
-- crawl_enqueued_at NULL  => crawl jobs not yet enqueued for that date
-- brief_enqueued_at NULL  => brief jobs not yet enqueued for that date

CREATE TABLE IF NOT EXISTS scheduler_daily_runs (
  run_date            DATE        PRIMARY KEY,
  crawl_enqueued_at   TIMESTAMPTZ,
  brief_enqueued_at   TIMESTAMPTZ
);
