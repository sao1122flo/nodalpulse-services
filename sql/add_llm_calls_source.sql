-- WS-B (B4): real `source` column on llm_calls for cost attribution.
-- Values: 'app' (in-app Q&A), 'connector' (MCP ask_the_record), 'pipeline'
-- (extraction / brief / discovery / everything else). Derived from pipeline_stage,
-- which is KEPT (finer-grained). Additive + idempotent.
-- Applied via: python scripts/apply_sql.py sql/add_llm_calls_source.sql
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS source text;

UPDATE llm_calls
SET source = CASE
  WHEN pipeline_stage = 'connector' THEN 'connector'
  WHEN pipeline_stage = 'qna'       THEN 'app'
  ELSE 'pipeline'
END
WHERE source IS NULL;

CREATE INDEX IF NOT EXISTS idx_llm_calls_user_source_created
  ON llm_calls (user_id, source, created_at DESC);
