-- P5: Add conversation_id to llm_calls for Q&A session tracking.
-- Apply via: python scripts/apply_sql.py sql/add_llm_calls_conversation_id.sql
-- OR via web: node scripts/run-p5-migration.mjs

ALTER TABLE llm_calls
ADD COLUMN IF NOT EXISTS conversation_id uuid;

CREATE INDEX IF NOT EXISTS idx_llm_calls_conversation
ON llm_calls (conversation_id)
WHERE conversation_id IS NOT NULL;
