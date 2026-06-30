-- NodalPulse services migration
-- Adds pipeline-only tables to the existing Railway Postgres database.
--
-- SAFE: does NOT touch users, sessions, accounts, verifications,
--       user_profiles, entitlements, subscriptions, briefs, health_checks.
-- All statements use IF NOT EXISTS / ON CONFLICT DO NOTHING.

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ─────────────────────────────────────────────
-- Sources (crawl registry)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sources (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  slug       TEXT UNIQUE NOT NULL,
  label      TEXT        NOT NULL,
  base_url   TEXT        NOT NULL,
  is_active  BOOLEAN     NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO sources (slug, label, base_url) VALUES
  ('puct',       'PUCT Interchange',                          'https://interchange.puc.texas.gov'),
  ('ercot-nprr', 'ERCOT Protocol Revision (NPRR/PGRR/MPRR)', 'https://www.ercot.com/mktrules/nprrs'),
  ('ercot-mn',   'ERCOT Market Notices',                      'https://www.ercot.com/services/comm/mkt_notices'),
  ('ferc',       'FERC eTariff / eLibrary',                   'https://elibrary.ferc.gov'),
  ('tlo',        'Texas Legislature Online',                  'https://capitol.texas.gov'),
  ('caiso',      'CAISO Regulatory Filings',                  'https://www.caiso.com/legal-regulatory/regulatory-filings-orders/filings'),
  ('cpuc',       'CPUC Document Search',                      'https://docs.cpuc.ca.gov'),
  ('pjm',        'PJM FERC Filings',                          'https://elibrary.ferc.gov'),
  ('imm',        'PJM IMM (Monitoring Analytics)',            'https://www.monitoringanalytics.com/filings'),
  ('njbpu',      'NJ Board of Public Utilities',              'https://publicaccess.bpu.state.nj.us'),
  ('mdpsc',      'MD Public Service Commission',              'https://webpscxb.pscmaryland.com')
ON CONFLICT (slug) DO NOTHING;

-- ─────────────────────────────────────────────
-- Filings (raw documents from crawlers)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS filings (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  source_id   UUID        NOT NULL REFERENCES sources(id),
  external_id TEXT        NOT NULL,
  doc_type    TEXT        NOT NULL,
  title       TEXT        NOT NULL,
  filer       TEXT,
  filed_at    TIMESTAMPTZ NOT NULL,
  r2_key      TEXT,
  file_ext    TEXT,
  source_url  TEXT,
  metadata    JSONB       NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS filings_source_filed_at_idx ON filings (source_id, filed_at DESC);
CREATE INDEX IF NOT EXISTS filings_filed_at_idx        ON filings (filed_at DESC);
CREATE INDEX IF NOT EXISTS filings_created_at_idx      ON filings (created_at DESC);
CREATE INDEX IF NOT EXISTS filings_doc_type_idx        ON filings (doc_type);

-- ─────────────────────────────────────────────
-- Extractions (structured LLM output per filing)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS extractions (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  filing_id     UUID        NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
  schema_ver    TEXT        NOT NULL,
  model         TEXT        NOT NULL,
  prompt_ver    TEXT        NOT NULL,
  payload       JSONB       NOT NULL,
  haiku_verdict TEXT,
  haiku_model   TEXT,
  extracted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (filing_id, schema_ver, prompt_ver)
);

CREATE INDEX IF NOT EXISTS extractions_filing_id_idx    ON extractions (filing_id);
CREATE INDEX IF NOT EXISTS extractions_extracted_at_idx ON extractions (extracted_at DESC);

-- ─────────────────────────────────────────────
-- Job queue
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS jobs (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  kind         TEXT        NOT NULL,
  payload      JSONB       NOT NULL DEFAULT '{}',
  priority     INT         NOT NULL DEFAULT 0,
  run_after    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  attempts     INT         NOT NULL DEFAULT 0,
  max_attempts INT         NOT NULL DEFAULT 3,
  locked_by    TEXT,
  locked_until TIMESTAMPTZ,
  status       TEXT        NOT NULL DEFAULT 'pending',
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS jobs_pending_idx ON jobs (kind, priority DESC, run_after ASC)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS jobs_status_idx  ON jobs (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS job_results (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  job_id      UUID    NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  attempt     INT     NOT NULL,
  success     BOOLEAN NOT NULL,
  output      JSONB   NOT NULL DEFAULT '{}',
  duration_ms INT,
  finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS job_results_job_id_idx ON job_results (job_id);

-- ─────────────────────────────────────────────
-- Eval runs (brief quality gate)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS eval_runs (
  id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  run_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  model            TEXT         NOT NULL,
  prompt_ver       TEXT         NOT NULL,
  taxonomy_ver     TEXT         NOT NULL,
  golden_set_size  INT          NOT NULL,
  results          JSONB        NOT NULL,
  overall_accuracy NUMERIC(5,4),
  passed           BOOLEAN      NOT NULL,
  failed_tags      TEXT[]       NOT NULL DEFAULT '{}',
  triggered_alert  BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS eval_runs_run_at_idx ON eval_runs (run_at DESC);

-- ─────────────────────────────────────────────
-- updated_at trigger for jobs
-- ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'jobs_updated_at' AND tgrelid = 'jobs'::regclass
  ) THEN
    CREATE TRIGGER jobs_updated_at
      BEFORE UPDATE ON jobs
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END;
$$;
