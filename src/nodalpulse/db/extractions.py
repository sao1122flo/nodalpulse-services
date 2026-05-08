"""DB operations for extractions."""

import json
import logging

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def get_filing(filing_id: str) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT id::text, r2_key, file_ext, doc_type, title "
                "FROM filings WHERE id = CAST(:id AS uuid)"
            ),
            {"id": filing_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def insert_extraction(
    *,
    filing_id: str,
    schema_ver: str,
    model: str,
    prompt_ver: str,
    payload: dict,
    haiku_verdict: str,
    haiku_model: str,
) -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO extractions (
                    filing_id, schema_ver, model, prompt_ver,
                    payload, haiku_verdict, haiku_model
                ) VALUES (
                    CAST(:filing_id AS uuid), :schema_ver, :model, :prompt_ver,
                    CAST(:payload AS jsonb), :haiku_verdict, :haiku_model
                )
                ON CONFLICT (filing_id, schema_ver, prompt_ver) DO UPDATE SET
                    payload       = EXCLUDED.payload,
                    haiku_verdict = EXCLUDED.haiku_verdict,
                    haiku_model   = EXCLUDED.haiku_model,
                    extracted_at  = NOW()
                RETURNING id::text
            """),
            {
                "filing_id": filing_id,
                "schema_ver": schema_ver,
                "model": model,
                "prompt_ver": prompt_ver,
                "payload": json.dumps(payload),
                "haiku_verdict": haiku_verdict,
                "haiku_model": haiku_model,
            },
        )
        extraction_id = result.scalar_one()
        await session.commit()
        return extraction_id
