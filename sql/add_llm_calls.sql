-- LLM call observability table.
-- Applied via: python scripts/apply_sql.py sql/add_llm_calls.sql
-- Uses uuid_generate_v4() consistent with services_schema.sql (uuid-ossp already enabled).

CREATE TABLE IF NOT EXISTS llm_calls (
  id                          uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
  created_at                  timestamptz NOT NULL DEFAULT NOW(),
  model                       text        NOT NULL,
  pipeline_stage              text        NOT NULL,
  input_tokens                integer     NOT NULL DEFAULT 0,
  output_tokens               integer     NOT NULL DEFAULT 0,
  cache_read_input_tokens     integer     NOT NULL DEFAULT 0,
  cache_creation_input_tokens integer     NOT NULL DEFAULT 0,
  cost_usd_estimate           numeric(12,6) NOT NULL DEFAULT 0,
  pricing_version             text        NOT NULL,
  latency_ms                  integer,
  request_id                  text,
  prompt_version              text,
  environment                 text        NOT NULL,
  filing_id                   uuid        REFERENCES filings(id) ON DELETE SET NULL,
  user_id                     uuid        REFERENCES users(id)   ON DELETE SET NULL,
  brief_id                    uuid        REFERENCES briefs(id)  ON DELETE SET NULL,
  error                       text
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_created_at ON llm_calls (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_stage_day  ON llm_calls (pipeline_stage, created_at DESC);
