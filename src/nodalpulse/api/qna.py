"""Q&A endpoint: POST /qna

Scopes retrieval to the user's predicate bundle (tracked dockets + saved searches
+ markets). Uses structured extraction payload only — no R2 text retrieval (V1).
Caches the filing context block (user message) via cache_control: ephemeral.
Rate-limits via llm_calls COUNT against a daily CT window.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from nodalpulse.api.auth import verify_bearer
from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.llm.client import compute_cost, tracked_messages_create
from nodalpulse.saved_search_predicate import build_predicate_bundle
from nodalpulse.zone_lookup import ilike_patterns_for_zones

logger = logging.getLogger(__name__)

_QNA_MAX_FILINGS = 15
_QNA_MAX_TOKENS = 800
_QNA_COST_WARN_THRESHOLD = 0.10
_QNA_MODEL = "claude-sonnet-4-6"
_QNA_PROMPT_VERSION = "1.0"

_QNA_SYSTEM = """\
You are NodalPulse's Q&A assistant for Texas energy regulation (PUCT, ERCOT, FERC).

Rules:
- Answer ONLY from the filing context provided. Do not add facts from training data.
- Cite the exact filing_id values from context when referencing information.
- If the answer is not in the context, say: "I don't see that in your tracked filings."
- Be concise, direct, and dry in tone. No hedging or editorializing.
- If deadlines or dates are mentioned, quote them exactly from the filing data.\
"""

_QNA_TOOL = {
    "name": "answer_question",
    "description": "Provide an answer to the question with filing citations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "Answer to the question. Max 500 words.",
            },
            "citations": {
                "type": "array",
                "description": "Filing IDs cited in the answer.",
                "items": {
                    "type": "object",
                    "properties": {
                        "filing_id": {"type": "string"},
                        "relevance_note": {
                            "type": "string",
                            "maxLength": 120,
                            "description": "One-line note on why this filing is relevant.",
                        },
                        "snippet": {
                            "type": "string",
                            "maxLength": 300,
                            "description": "Verbatim sentence or short passage from the filing context that directly supports this citation. Quote exactly as it appears in the context.",
                        },
                        "page_number": {
                            "type": "integer",
                            "description": "Page number where the cited text appears, if visible in the context (e.g. 'Page 3' or 'p.3'). Omit if not available.",
                        },
                    },
                    "required": ["filing_id", "relevance_note"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["answer", "citations"],
        "additionalProperties": False,
    },
}


class QnaRequest(BaseModel):
    user_id: str
    question: str
    conversation_id: str | None = None
    limit_per_day: int = 0  # 0 = blocked; passed by web layer from getEntitlements


def _build_filing_context(filings: list[dict]) -> str:
    """Serialize structured extraction payload into a cacheable context block."""
    lines = ["=== FILING CONTEXT (your tracked filings) ===\n"]
    for f in filings:
        lines.append(f"--- Filing {f['filing_id']} ---")
        lines.append(f"Title: {f['title']}")
        lines.append(f"Source: {f['source_slug'].upper()}")
        if f.get("docket_number"):
            lines.append(f"Docket: {f['docket_number']}")
        if f.get("filer"):
            lines.append(f"Filer: {f['filer']}")
        if f.get("filed_at"):
            lines.append(f"Filed: {f['filed_at']}")
        p = f.get("payload") or {}
        if p.get("summary"):
            lines.append(f"Summary: {p['summary']}")
        if p.get("relief_requested"):
            lines.append(f"Relief requested: {p['relief_requested']}")
        if p.get("outcome"):
            lines.append(f"Outcome: {p['outcome']}")
        kps = (p.get("key_points") or [])[:4]
        if kps:
            lines.append("Key points:")
            for kp in kps:
                lines.append(f"  - {kp}")
        parties = (p.get("parties") or [])[:6]
        if parties:
            lines.append(f"Parties: {', '.join(parties)}")
        deadlines = (p.get("deadlines") or [])[:3]
        for d in deadlines:
            if isinstance(d, dict) and d.get("date"):
                lines.append(f"Deadline: {d['date']} — {d.get('description', '')}")
        if p.get("effective_date"):
            lines.append(f"Effective date: {p['effective_date']}")
        lines.append("")
    lines.append("=== END CONTEXT ===")
    return "\n".join(lines)


async def _get_qna_usage_today(user_id: str) -> int:
    """Count Q&A calls today in America/Chicago time."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT COUNT(*) FROM llm_calls
                WHERE pipeline_stage = 'qna'
                  AND user_id = CAST(:uid AS uuid)
                  AND created_at >= date_trunc('day', now() AT TIME ZONE 'America/Chicago')
                    AT TIME ZONE 'America/Chicago'
            """),
            {"uid": user_id},
        )
        return int(result.scalar_one())


async def handle_qna(body: QnaRequest) -> JSONResponse:
    """Core Q&A handler — called from the FastAPI route."""
    # ── Entitlement gate ──────────────────────────────────────────────────────
    if body.limit_per_day == 0:
        return JSONResponse(
            {
                "error": "qa_not_available",
                "message": "Q&A is not available on your current plan.",
                "upgrade_url": "/pricing?return=%2Fchat",
            },
            status_code=403,
        )

    # ── Daily rate limit ──────────────────────────────────────────────────────
    used_today = await _get_qna_usage_today(body.user_id)
    if used_today >= body.limit_per_day:
        # Compute next CT midnight in UTC
        from zoneinfo import ZoneInfo
        ct = ZoneInfo("America/Chicago")
        now_ct = datetime.now(ct)
        next_midnight_ct = (now_ct + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        resets_at = next_midnight_ct.astimezone(UTC).isoformat()

        return JSONResponse(
            {
                "error": "rate_limit",
                "limit": body.limit_per_day,
                "used": used_today,
                "resets_at": resets_at,
            },
            status_code=429,
        )

    # ── Predicate bundle ──────────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        profile_result = await session.execute(
            text("""
                SELECT
                    COALESCE(tracked_docket_ids::text[], '{}') AS profile_ids,
                    COALESCE(tracked_tags, '[]'::jsonb)        AS tracked_tags,
                    COALESCE(market_roles, '{}')               AS market_roles
                FROM user_profiles
                WHERE user_id = CAST(:uid AS uuid)
            """),
            {"uid": body.user_id},
        )
        profile = profile_result.mappings().first()
        profile_docket_ids: list[str] = list(profile["profile_ids"]) if profile else []
        tracked_tags: list[str] = list(profile["tracked_tags"]) if profile else []
        market_roles: list[str] = list(profile["market_roles"]) if profile else []

        junc_result = await session.execute(
            text("SELECT docket_id::text FROM user_dockets WHERE user_id = CAST(:uid AS uuid)"),
            {"uid": body.user_id},
        )
        junction_ids = [r[0] for r in junc_result.fetchall() if r[0]]

        ss_result = await session.execute(
            text("""
                SELECT id::text AS id, query
                FROM saved_searches
                WHERE user_id = CAST(:uid AS uuid)
            """),
            {"uid": body.user_id},
        )
        saved_searches = [{"id": r["id"], "query": r["query"]} for r in ss_result.mappings().all()]

    tracked_docket_uuids = list({*profile_docket_ids, *junction_ids})
    zone_patterns = ilike_patterns_for_zones(tracked_tags)

    bundle = build_predicate_bundle(
        saved_searches=saved_searches,
        tracked_docket_uuids=tracked_docket_uuids,
        zone_filer_patterns=zone_patterns,
        market_roles=market_roles,
    )

    if not bundle.has_implementable_predicates:
        return JSONResponse(
            {
                "error": "no_predicates",
                "message": (
                    "Q&A scopes to your tracked dockets and saved searches. "
                    "Set up at least one to start."
                ),
                "actions": [
                    {"label": "Track a docket", "href": "/dockets"},
                    {"label": "Create a saved search", "href": "/dashboard"},
                ],
            },
            status_code=422,
        )

    # ── Retrieve candidate filings ────────────────────────────────────────────
    where_clause, params = bundle.build_where_clause()
    params["since"] = datetime.now(UTC) - timedelta(days=30)
    params["limit"] = _QNA_MAX_FILINGS

    sql_query = f"""
        SELECT
            f.id::text                  AS filing_id,
            f.title,
            f.filer,
            f.filed_at,
            f.source_url,
            s.slug                      AS source_slug,
            d.external_id               AS docket_number,
            e.payload
        FROM filings f
        JOIN sources s ON s.id = f.source_id
        LEFT JOIN dockets d ON d.id = f.docket_id
        LEFT JOIN extractions e ON e.filing_id = f.id
        WHERE f.created_at >= :since
          AND e.haiku_verdict IS DISTINCT FROM 'irrelevant'
          AND ({where_clause})
        ORDER BY f.filed_at DESC
        LIMIT :limit
    """  # noqa: S608

    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql_query), params)
        rows = result.mappings().fetchall()

    if not rows:
        return JSONResponse(
            {
                "error": "no_filings",
                "message": "No recent filings match your tracked context. Try broadening your saved searches.",
            },
            status_code=422,
        )

    filings = [
        {
            "filing_id": r["filing_id"],
            "title": r["title"],
            "filer": r["filer"],
            "filed_at": r["filed_at"].isoformat()[:10] if r["filed_at"] else None,
            "source_url": r["source_url"],
            "source_slug": r["source_slug"],
            "docket_number": r["docket_number"],
            "payload": r["payload"] or {},
        }
        for r in rows
    ]

    # Index for citation resolution
    filing_index = {f["filing_id"]: f for f in filings}

    # ── Build LLM prompt ──────────────────────────────────────────────────────
    conversation_id = body.conversation_id or str(uuid.uuid4())
    filing_context = _build_filing_context(filings)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": filing_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": f"Question: {body.question}",
                },
            ],
        }
    ]

    # ── LLM call ──────────────────────────────────────────────────────────────
    response = await tracked_messages_create(
        pipeline_stage="qna",
        user_id=body.user_id,
        prompt_version=_QNA_PROMPT_VERSION,
        model=_QNA_MODEL,
        max_tokens=_QNA_MAX_TOKENS,
        system=[{"type": "text", "text": _QNA_SYSTEM}],
        messages=messages,
        tools=[_QNA_TOOL],
        tool_choice={"type": "tool", "name": "answer_question"},
        # Write conversation_id via extra metadata — tracked_messages_create
        # uses fire-and-forget for the insert; we handle conversation_id below
    )

    # Extract tool output
    answer = ""
    raw_citations: list[dict] = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "answer_question":
            answer = block.input.get("answer", "")
            raw_citations = block.input.get("citations", [])
            break

    if not answer:
        answer = "Unable to generate an answer. Please try again."

    # ── Persist conversation_id on the llm_calls row (best-effort) ───────────
    # tracked_messages_create fires the insert asynchronously without conversation_id.
    # We patch the most-recent matching row for this user/stage/request_id.
    request_id = getattr(response, "id", None)
    if request_id:
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        UPDATE llm_calls
                        SET conversation_id = CAST(:conv_id AS uuid)
                        WHERE request_id = :request_id
                          AND user_id = CAST(:uid AS uuid)
                          AND pipeline_stage = 'qna'
                    """),
                    {
                        "conv_id": conversation_id,
                        "request_id": request_id,
                        "uid": body.user_id,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning("Failed to patch conversation_id on llm_calls: %s", exc)

    # ── Cost guard ───────────────────────────────────────────────────────────
    cost = compute_cost(response.usage, _QNA_MODEL)
    if float(cost) > _QNA_COST_WARN_THRESHOLD:
        logger.warning(
            "qna cost_warn user=%s cost=%.4f tokens_in=%d tokens_out=%d",
            body.user_id, float(cost),
            response.usage.input_tokens, response.usage.output_tokens,
        )

    # ── Resolve citations ────────────────────────────────────────────────────
    citations = []
    for c in raw_citations:
        fid = c.get("filing_id", "")
        f = filing_index.get(fid)
        if not f:
            continue
        citations.append({
            "filing_id": fid,
            "title": f["title"],
            "source_url": f["source_url"],
            "docket_number": f["docket_number"],
            "relevance_note": c.get("relevance_note", ""),
            "snippet": c.get("snippet") or None,
            "page_number": c.get("page_number") or None,
        })

    u = response.usage
    logger.info(
        "qna user=%s conv=%s in=%d out=%d cache_read=%d cache_write=%d cost=%.4f",
        body.user_id, conversation_id,
        u.input_tokens, u.output_tokens,
        getattr(u, "cache_read_input_tokens", 0),
        getattr(u, "cache_creation_input_tokens", 0),
        float(cost),
    )

    return JSONResponse({
        "answer": answer,
        "citations": citations,
        "conversation_id": conversation_id,
        "tokens_used": {
            "input": u.input_tokens,
            "output": u.output_tokens,
            "cache_read": getattr(u, "cache_read_input_tokens", 0),
            "cache_creation": getattr(u, "cache_creation_input_tokens", 0),
        },
        "cost_estimate": float(cost),
        "used_today": used_today + 1,
        "limit_per_day": body.limit_per_day,
    })
