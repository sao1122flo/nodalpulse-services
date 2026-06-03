-- market_events — non-document calendar deadline rows for services pipeline.
--
-- Ownership: SERVICES-ONLY.
--   Writer: crawl_pjm_calendar (workers/crawl_pjm_calendar.py)
--   Reader: compose_brief (workers/compose_brief.py)
--   NOT managed by drizzle-kit. NOT referenced in nodalpulse-web/db/schema.
--   Adding a drizzle schema entry would incorrectly transfer ownership to the
--   web team and risk snapshot drift (#52 lesson).
--
-- Applied 2026-06-03 via:
--   node scripts/apply-sql.mjs drizzle/market_events.sql
--   (nodalpulse-web/drizzle/market_events.sql is a stub pointing here)
--
-- To re-apply from services context (Railway):
--   railway run --service nodalpulse-services psql "$DATABASE_URL" -f sql/add_market_events.sql

CREATE TABLE IF NOT EXISTS market_events (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  source         text        NOT NULL,
  jurisdiction   text        NOT NULL DEFAULT 'PJM-FERC',
  event_type     text        NOT NULL,
  title          text        NOT NULL,
  event_date     date        NOT NULL,
  estimated      boolean     NOT NULL DEFAULT false,
  related_docket text,
  source_url     text,
  external_id    text        UNIQUE,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_market_events_jurisdiction_date
  ON market_events (jurisdiction, event_date);

CREATE INDEX IF NOT EXISTS idx_market_events_event_date
  ON market_events (event_date DESC);
