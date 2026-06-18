-- Market salience cache (issue #128).
-- Stores the top-3 dockets driving each market this week.
-- Computed daily by SQL heuristic (no LLM). Headline added in STEP 2 (Haiku).
-- UPSERT-safe: re-running daily overwrites scores while preserving headline/headline_at.

CREATE TABLE IF NOT EXISTS market_salience (
  id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
  market          TEXT        NOT NULL,           -- 'FERC', 'PUCT', 'ERCOT', 'CAISO', 'PJM', 'CPUC'
  week_start      DATE        NOT NULL,           -- Monday of the current ISO week
  rank            INT         NOT NULL CHECK (rank BETWEEN 1 AND 3),
  docket_key      TEXT        NOT NULL,           -- docket number / external_id
  docket_title    TEXT,                           -- title from dockets table (null for FERC)
  score           NUMERIC     NOT NULL,
  filings_count   INT         NOT NULL DEFAULT 0,
  distinct_filers INT         NOT NULL DEFAULT 0,
  max_doc_weight  INT         NOT NULL DEFAULT 0,
  headline        TEXT,                           -- Haiku one-liner (STEP 2)
  headline_at     TIMESTAMPTZ,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (market, week_start, rank)
);

CREATE INDEX IF NOT EXISTS market_salience_market_week_idx
  ON market_salience (market, week_start DESC);
