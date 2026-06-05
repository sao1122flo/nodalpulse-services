-- Migration: add public lead-capture table
-- Owned by services (written by /public/lead, never touched by drizzle/web).
-- Apply via: psql $DATABASE_URL -f sql/add_leads.sql

CREATE TABLE IF NOT EXISTS leads (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email        TEXT        NOT NULL,
  name         TEXT        NOT NULL,
  title        TEXT        NOT NULL,  -- job title / cargo
  market       TEXT,                  -- market slug from the record page (puct, caiso, ferc…)
  record_date  DATE,                  -- date of the record page that triggered capture
  source_url   TEXT,                  -- the /record/… URL they visited
  captured_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (email)
);

CREATE INDEX IF NOT EXISTS leads_captured_at_idx ON leads (captured_at DESC);
