"""Job handler for compose-brief queue jobs.

Personalization status (as of Prompt 3 — 2026-05-18):

IMPLEMENTED predicates — wired into get_filings_for_brief_user():
  * markets (saved_search.query.markets)  → source_id/sources.slug filter
  * text    (saved_search.query.text)      → ILIKE on title + filer (no tsvector)
  * dockets (tracked_docket_ids)           → fragile string join via docket_number
  * zones   (user_profiles.tracked_tags)   → filer-name lookup (zone_lookup.py)

DEFERRED — visible noops, logged via bundle.log_noops():
  * Role-based filtering (user_profiles.market_roles)
    → Requires per-filing role tags. Tagger upgrade in Prompt 3.5.
  * Tag-based filtering (saved_search.query.tags)
    → Same blocker: no tag column on filings. Prompt 3.5.
  * Full-text indexed search (tsvector / GIN index)
    → ILIKE only for now; index + upgrade in Prompt 3.6.

When personalization is active (has_implementable_predicates=True):
  - Only filings matching at least one predicate enter the brief pool.
  - Zero matches → quiet-day path. No global backfill.

When personalization is inactive (skipped onboarding / no predicates set):
  - Global query (current behaviour): all non-irrelevant filings in window.
  - Brief HTML includes "Add filters" banner.
"""

import json
import logging
import os
import re
import unicodedata

import httpx
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from nodalpulse.db.briefs import (
    check_eval_gate,
    get_filings_for_brief,
    get_filings_for_brief_user,
    get_last_brief_date,
    get_user_for_brief,
    insert_brief,
    mark_brief_sent,
)
from nodalpulse.email.brevo import send_email
from nodalpulse.email.templates import (
    build_brief_html,
    build_brief_text,
    build_maintenance_html,
    build_quiet_day_html,
)
from nodalpulse.llm.client import compose as llm_compose
from nodalpulse.llm.taxonomy import TEXAS_ELECTRICITY_TAXONOMY
from nodalpulse.saved_search_predicate import PredicateBundle, build_predicate_bundle
from nodalpulse.settings import settings
from nodalpulse.storage import r2
from nodalpulse.zone_lookup import ilike_patterns_for_zones

logger = logging.getLogger(__name__)

_CHICAGO = ZoneInfo("America/Chicago")

COMPOSER_MODEL = "claude-sonnet-4-6"
COMPOSER_VERSION = "1.0"
PROMPT_VER = "1.0"

# Strict citation regex — hallucinated citations that don't match are dropped.
_CITATION_RE = re.compile(
    r"\[(ERCOT|PUCT|FERC|TLO|ERCOT-NPRR|ERCOT-MN)[^\]]+, p\.\d+ ¶\d+\]"
)

_COMPOSE_SYSTEM = """\
You are NodalPulse's brief composer. You write 2-line summaries of regulatory filings
for energy-industry professionals.

Hard rules:
- You are given structured Filing records with claims and citations.
- You may ONLY write summaries that paraphrase the claims provided.
- Every summary must end with the citation exactly as given in the input record.
- You may not introduce new facts, parties, dates, or numbers not in the input.
- You may not editorialize ("notably," "interestingly," "concerning").
- Voice: dry, precise, present-tense, active voice. No hedging.
- Length: max 280 characters total (summary text + citation).
- If an input record has no claims, write "Filing summary unavailable; see source." + citation.
- You MUST render ALL input filings — every filing_id must appear in the output.\
"""

_COMPOSE_SYSTEM_FULL = _COMPOSE_SYSTEM + "\n\n" + TEXAS_ELECTRICITY_TAXONOMY


# ── scoring ───────────────────────────────────────────────────────────────────

def _score_filing(filing: dict, today: date, predicate_match_count: int = 0) -> int:
    score = 0

    payload = filing.get("extraction_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    doc_type = filing.get("doc_type", "")
    if "order" in doc_type:
        score += 20

    verdict = filing.get("haiku_verdict", "uncertain")
    if verdict == "relevant":
        score += 10
    elif verdict == "uncertain":
        score += 5

    # Effective date urgency
    effective_date = payload.get("effective_date")
    if effective_date:
        try:
            eff = date.fromisoformat(str(effective_date)[:10])
            days_away = (eff - today).days
            if 0 <= days_away <= 7:
                score += 60
            elif 0 <= days_away <= 30:
                score += 40
        except ValueError:
            pass

    # Comment/action deadlines
    for deadline in payload.get("deadlines") or []:
        d_str = deadline.get("date") if isinstance(deadline, dict) else None
        if d_str:
            try:
                d = date.fromisoformat(str(d_str)[:10])
                if 0 <= (d - today).days <= 7:
                    score += 60
                    break
            except ValueError:
                pass

    # Personalization boost — each additional matched predicate adds weight
    score += predicate_match_count * 10

    return score


# ── helpers ───────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(text)).replace("\xa0", " "),
    ).strip()


def _parse_payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def _build_citation(payload: dict, filing: dict) -> str:
    """Construct a canonical [SOURCE ID, p.N ¶N] citation from available data."""
    metadata = filing.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    doc_type = filing.get("doc_type", "puct-filing")

    if doc_type == "ercot-nprr":
        identifier = (
            metadata.get("nprr_number")
            or payload.get("docket_number")
            or filing["filing_id"][:8]
        )
        return f"[ERCOT {identifier}, p.1 ¶1]"

    if doc_type == "ercot-mn":
        identifier = (
            metadata.get("notice_id")
            or payload.get("docket_number")
            or filing["filing_id"][:8]
        )
        return f"[ERCOT-MN {identifier}, p.1 ¶1]"

    # PUCT (default)
    control = (
        payload.get("docket_number")
        or metadata.get("control_number")
        or filing["filing_id"][:8]
    )
    return f"[PUCT {control}, p.1 ¶1]"


def _build_composer_input(entry: dict) -> dict:
    f = entry["filing"]
    payload = _parse_payload(f.get("extraction_payload"))

    citation = _build_citation(payload, f)

    claims = []
    for field in ("summary", "relief_requested", "outcome"):
        val = payload.get(field)
        if val:
            claims.append(_normalize(val))
    for kp in (payload.get("key_points") or [])[:3]:
        claims.append(_normalize(kp))

    filed = f["filed_at"]
    filed_str = filed.isoformat()[:10] if hasattr(filed, "isoformat") else str(filed)[:10]

    return {
        "filing_id": f["filing_id"],
        "title": f["title"],
        "doc_type": f.get("doc_type", ""),
        "filed_at": filed_str,
        "claims": claims[:5],
        "citation": citation,
    }


# ── main handler ──────────────────────────────────────────────────────────────

async def handle_compose_brief(payload: dict) -> dict:
    user_id = payload["user_id"]
    brief_date = date.fromisoformat(payload["brief_date"])

    logger.info("compose-brief user=%s date=%s", user_id, brief_date)

    unsubscribe_url = f"{settings.app_url}/unsubscribe/{user_id}"

    # Entitlement check at generation time (not at cron-enqueue time)
    user = await get_user_for_brief(user_id)
    if not user:
        logger.info("No active entitlement for %s — skipping", user_id)
        return {"user_id": user_id, "status": "skipped", "reason": "no_entitlement"}

    # Eval gate — if last eval failed, send maintenance notice
    eval_ok = await check_eval_gate()
    if not eval_ok:
        logger.warning("Eval gate failed — maintenance notice to %s", user["email"])
        html = build_maintenance_html(
            brief_date=brief_date,
            unsubscribe_url=unsubscribe_url,
        )
        text = (
            f"NodalPulse pipeline maintenance {brief_date}. "
            "Check https://nodalpulse.com/status"
        )
        await send_email(
            to_email=user["email"],
            to_name=user.get("name"),
            subject=f"NodalPulse · Pipeline maintenance · {brief_date.strftime('%b %-d')}",
            html_content=html,
            text_content=text,
            unsubscribe_url=unsubscribe_url,
        )
        return {"user_id": user_id, "status": "maintenance"}

    # Window: last_brief_date+1 → brief_date (handles Fri→Mon 3-day gap correctly)
    last_date = await get_last_brief_date(user_id)
    if last_date:
        window_since = datetime.combine(
            last_date + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=UTC)
    else:
        window_since = datetime.combine(
            brief_date - timedelta(days=settings.max_lookback_days),
            datetime.min.time(),
        ).replace(tzinfo=UTC)
    window_until = datetime.combine(
        brief_date + timedelta(days=1), datetime.min.time()
    ).replace(tzinfo=UTC)

    # ── Personalization ────────────────────────────────────────────────────────

    zone_patterns = ilike_patterns_for_zones(
        user.get("tracked_tags") or []
    )
    bundle: PredicateBundle = build_predicate_bundle(
        saved_searches=user.get("saved_searches") or [],
        tracked_docket_uuids=user.get("tracked_docket_ids") or [],
        zone_filer_patterns=zone_patterns,
        market_roles=user.get("market_roles") or [],
    )
    bundle.log_noops()

    filters_active = bundle.has_implementable_predicates

    if filters_active:
        logger.info(
            "compose-brief personalized: user=%s markets=%s dockets=%d text=%d zones=%d",
            user_id,
            bundle.market_slugs,
            len(bundle.tracked_docket_uuids),
            len(bundle.text_ilike_patterns),
            len(bundle.zone_filer_patterns),
        )
        filings = await get_filings_for_brief_user(window_since, window_until, bundle)
        total_corpus = len(filings)

        if not filings:
            logger.info(
                "compose-brief quiet-day (zero predicate matches) user=%s", user_id
            )
            html = build_quiet_day_html(
                brief_date=brief_date,
                corpus_count=0,
                app_url=settings.app_url,
                unsubscribe_url=unsubscribe_url,
            )
            text_body = (
                f"Quiet day {brief_date}. "
                "0 items match your filters. "
                f"https://nodalpulse.com/digest/{brief_date.isoformat()}"
            )
            await send_email(
                to_email=user["email"],
                to_name=user.get("name"),
                subject=f"NodalPulse · Quiet day · {brief_date.strftime('%b %-d')}",
                html_content=html,
                text_content=text_body,
                unsubscribe_url=unsubscribe_url,
            )
            return {
                "user_id": user_id,
                "status": "quiet_day",
                "corpus_count": 0,
                "reason": "no_predicate_matches",
            }
    else:
        # Global fallback — no implementable predicates (skipped onboarding or
        # role-only context). Shows "Add filters" banner in the email.
        logger.info(
            "compose-brief global-fallback (no implementable predicates) user=%s",
            user_id,
        )
        filings = await get_filings_for_brief(window_since, window_until)
        total_corpus = len(filings)

        if not filings:
            html = build_quiet_day_html(
                brief_date=brief_date,
                corpus_count=0,
                app_url=settings.app_url,
                unsubscribe_url=unsubscribe_url,
            )
            text_body = (
                f"Quiet day {brief_date}. "
                "No filings in window. "
                f"https://nodalpulse.com/digest/{brief_date.isoformat()}"
            )
            await send_email(
                to_email=user["email"],
                to_name=user.get("name"),
                subject=f"NodalPulse · Quiet day · {brief_date.strftime('%b %-d')}",
                html_content=html,
                text_content=text_body,
                unsubscribe_url=unsubscribe_url,
            )
            return {
                "user_id": user_id,
                "status": "quiet_day",
                "corpus_count": 0,
                "reason": "empty_corpus",
            }

    # Role filtering: if the user has market_roles AND a filing has role_tags,
    # only include the filing when the two sets intersect. Filings without
    # role_tags (older extractions) pass through unconditionally.
    user_roles: set[str] = set(user.get("market_roles") or [])
    if user_roles:
        def _role_match(f: dict) -> bool:
            tags: list[str] = (f.get("payload") or {}).get("role_tags") or []
            return not tags or bool(user_roles.intersection(tags))
        before_role = len(filings)
        filings = [f for f in filings if _role_match(f)]
        if len(filings) < before_role:
            logger.info(
                "compose-brief role-filter user=%s kept=%d dropped=%d",
                user_id, len(filings), before_role - len(filings),
            )

    # Score, rank, cap at 25
    today = brief_date
    scored = sorted(
        [
            {
                "filing": f,
                "score": _score_filing(
                    f, today, int(f.get("predicate_match_count") or 0)
                ),
            }
            for f in filings
        ],
        key=lambda x: x["score"],
        reverse=True,
    )[:25]

    # R2 existence check — drop filings whose source objects are missing
    valid_entries = []
    for entry in scored:
        r2_key = entry["filing"].get("r2_key")
        if r2_key and not r2.exists(r2_key):
            logger.warning(
                "R2 key missing — dropping filing %s", entry["filing"]["filing_id"]
            )
            continue
        valid_entries.append(entry)

    if not valid_entries:
        logger.warning("All items failed R2 check for user %s", user_id)
        return {"user_id": user_id, "status": "skipped", "reason": "all_r2_missing"}

    # Section assignment: top 3 high-scoring → top_of_mind; rest → what_changed
    top_of_mind = [e for e in valid_entries[:3] if e["score"] >= 20]
    what_changed = [e for e in valid_entries if e not in top_of_mind]
    all_sections_ordered = top_of_mind + what_changed

    # Build composer inputs
    composer_inputs = [_build_composer_input(e) for e in all_sections_ordered]
    n_expected = len(composer_inputs)

    user_prompt = (
        f"Compose brief items for {n_expected} filing(s) for {brief_date}. "
        f"Render ALL {n_expected} filings.\n\n"
        + json.dumps(composer_inputs, indent=2)
    )

    # LLM compose — tool_choice forces structured output
    composed = await llm_compose(
        _COMPOSE_SYSTEM_FULL, user_prompt, model=COMPOSER_MODEL, user_id=user_id
    )
    logger.info("compose: expected %d items, got %d", n_expected, len(composed))

    # Count parity check — retry once with explicit filing_id list
    if len(composed) != n_expected:
        logger.warning(
            "Count mismatch %d→%d for user %s — retrying with explicit list",
            n_expected,
            len(composed),
            user_id,
        )
        expected_ids = [c["filing_id"] for c in composer_inputs]
        retry_prompt = (
            f"You MUST render ALL {n_expected} filings. "
            f"Your previous response had {len(composed)} items. "
            f"Required filing_ids: {expected_ids}\n\n"
            + user_prompt
        )
        composed = await llm_compose(
            _COMPOSE_SYSTEM_FULL, retry_prompt, model=COMPOSER_MODEL, user_id=user_id
        )

    composed_by_id = {c["filing_id"]: c for c in composed}

    # Citation validation — drop items whose citation doesn't match the strict regex
    sections: dict[str, list[dict]] = {"top_of_mind": [], "what_changed": []}
    valid_filing_ids: list[str] = []
    citation_count = 0
    fallback_items: list[dict] = []

    def _process_entry(entry: dict, section_key: str) -> None:
        nonlocal citation_count
        f = entry["filing"]
        fid = f["filing_id"]
        item = composed_by_id.get(fid)
        if not item:
            return
        citation = item.get("citation", "")
        if not _CITATION_RE.search(citation):
            logger.warning("Bad citation for %s: %r — dropping", fid, citation)
            p = _parse_payload(f.get("extraction_payload"))
            fallback_items.append({
                "filing_id": fid,
                "title": f["title"],
                "summary": p.get("summary", "Filing summary unavailable; see source."),
                "citation": _build_citation(p, f),
                "doc_type": f.get("doc_type", ""),
                "source_url": f.get("source_url", ""),
            })
            return
        sections[section_key].append({
            "filing_id": fid,
            "title": f["title"],
            "summary": item["summary"],
            "citation": citation,
            "doc_type": f.get("doc_type", ""),
            "source_url": f.get("source_url", ""),
        })
        valid_filing_ids.append(fid)
        citation_count += 1

    for entry in top_of_mind:
        _process_entry(entry, "top_of_mind")
    for entry in what_changed:
        _process_entry(entry, "what_changed")

    item_count = sum(len(v) for v in sections.values())

    if item_count == 0:
        logger.warning("All citations failed for user %s — sending fallback brief", user_id)
        for fb in fallback_items[:10]:
            sections["what_changed"].append(fb)
            valid_filing_ids.append(fb["filing_id"])
        item_count = len(sections["what_changed"])

    if item_count == 0:
        return {"user_id": user_id, "status": "skipped", "reason": "no_valid_items"}

    # Email subject
    top_item = sections["top_of_mind"][0] if sections["top_of_mind"] else (
        sections["what_changed"][0] if sections["what_changed"] else None
    )
    if top_item and item_count > 1:
        subject = (
            f"{top_item['title'][:60]} · "
            f"{item_count - 1} more item{'s' if item_count - 1 != 1 else ''}"
        )
    elif top_item:
        subject = top_item["title"][:80]
    else:
        subject = f"NodalPulse · {brief_date.strftime('%b %-d')} · {item_count} items"

    generated_at = datetime.now(UTC)

    # Build HTML + plain text
    html = build_brief_html(
        brief_date=brief_date,
        sections=sections,
        generated_at=generated_at,
        composer_version=COMPOSER_VERSION,
        app_url=settings.app_url,
        unsubscribe_url=unsubscribe_url,
        eval_ok=eval_ok,
        item_count=item_count,
        filters_active=filters_active,
    )
    text_content = build_brief_text(
        brief_date=brief_date,
        sections=sections,
        app_url=settings.app_url,
        unsubscribe_url=unsubscribe_url,
        composer_version=COMPOSER_VERSION,
    )

    # Upload HTML + text to R2
    date_path = brief_date.strftime("%Y/%m/%d")
    html_key = f"briefs/{user_id}/{date_path}/brief.html"
    txt_key = f"briefs/{user_id}/{date_path}/brief.txt"
    r2.upload(html_key, html.encode("utf-8"), "text/html; charset=utf-8")
    r2.upload(txt_key, text_content.encode("utf-8"), "text/plain; charset=utf-8")

    # Persist brief row
    brief_id = await insert_brief(
        user_id=user_id,
        brief_date=brief_date,
        model=COMPOSER_MODEL,
        prompt_ver=PROMPT_VER,
        html_r2_key=html_key,
        txt_r2_key=txt_key,
        filing_ids=valid_filing_ids,
        citation_count=citation_count,
        send_status="pending",
    )

    # Send via Brevo
    msg_id = await send_email(
        to_email=user["email"],
        to_name=user.get("name"),
        subject=subject,
        html_content=html,
        text_content=text_content,
        unsubscribe_url=unsubscribe_url,
    )

    if msg_id:
        await mark_brief_sent(brief_id)
        logger.info(
            "Brief sent user=%s email=%s items=%d citations=%d filters=%s msg_id=%s",
            user_id,
            user["email"],
            item_count,
            citation_count,
            "active" if filters_active else "global",
            msg_id,
        )
        if settings.better_stack_heartbeat_brief_url:
            try:
                async with httpx.AsyncClient() as client:
                    await client.get(settings.better_stack_heartbeat_brief_url, timeout=5.0)
                logger.info("Better Stack brief heartbeat fired user=%s", user_id)
            except Exception:
                logger.warning("Better Stack brief heartbeat failed user=%s — ignoring", user_id)
    else:
        logger.error("Brevo send failed for user %s", user_id)

    return {
        "user_id": user_id,
        "brief_id": brief_id,
        "status": "sent" if msg_id else "send_failed",
        "item_count": item_count,
        "citation_count": citation_count,
        "corpus_count": total_corpus,
        "filters_active": filters_active,
    }
