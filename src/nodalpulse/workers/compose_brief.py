"""Job handler for compose-brief queue jobs.

Personalization status (as of Prompt 3 — 2026-05-18):

IMPLEMENTED predicates — wired into get_filings_for_brief_user():
  * markets (saved_search.query.markets)  → source_id/sources.slug filter
  * text    (saved_search.query.text)      → ILIKE on title + filer (no tsvector)
  * dockets (tracked_docket_ids)           → FK join via filings.docket_id
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
from collections import defaultdict
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

BRIEF_ITEM_CAP = 25
TOP_OF_MIND_COUNT = 5
PER_DOCKET_CEILING = 12

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


# ── per-docket allocation ─────────────────────────────────────────────────────

def _deadline_badge_info(payload: dict, brief_date: date) -> dict:
    """Return nearest upcoming deadline/effective_date within 30 days for badge rendering."""
    result: dict = {"nearest_deadline_date": None, "nearest_effective_date": None}
    eff = payload.get("effective_date")
    if eff:
        try:
            eff_d = date.fromisoformat(str(eff)[:10])
            if 0 <= (eff_d - brief_date).days <= 30:
                result["nearest_effective_date"] = eff_d.isoformat()
        except ValueError:
            pass
    soonest = None
    for dl in payload.get("deadlines") or []:
        d_str = dl.get("date") if isinstance(dl, dict) else None
        if d_str:
            try:
                d = date.fromisoformat(str(d_str)[:10])
                if 0 <= (d - brief_date).days <= 30:
                    if soonest is None or d < soonest:
                        soonest = d
            except ValueError:
                pass
    if soonest:
        result["nearest_deadline_date"] = soonest.isoformat()
    return result


def allocate_brief(
    candidates: list[dict],
    tracked_docket_uuids: list[str],
    brief_date: date,
) -> dict:
    """Allocate candidates into top_of_mind + per-docket sections.

    Returns:
        {
          "top_of_mind": [{"filing": f, "score": s}, ...],
          "docket_sections": [
              {"docket_id": str, "external_id": str|None,
               "items": [{"filing": f, "score": s}],
               "pool_total": int, "section_score": int},
              ...
          ]
        }
    TOP_OF_MIND: top TOP_OF_MIND_COUNT globally, regardless of docket.
    Body: remaining slots distributed by per-docket floor+bonus, capped at
    PER_DOCKET_CEILING per docket (overridden to remaining_slots when only
    one active docket, to avoid underutilising the brief cap).
    Overflow path (more active dockets than body slots): top N by best score
    each get 1 slot.
    """
    if not candidates:
        return {"top_of_mind": [], "docket_sections": []}

    scored = [
        {
            "filing": f,
            "score": _score_filing(f, brief_date, int(f.get("predicate_match_count") or 0)),
        }
        for f in candidates
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)

    top_of_mind = scored[:TOP_OF_MIND_COUNT]
    tom_ids = {e["filing"]["filing_id"] for e in top_of_mind}
    remaining = [e for e in scored if e["filing"]["filing_id"] not in tom_ids]

    docket_pools: dict[str, list] = defaultdict(list)
    for entry in remaining:
        docket_id = entry["filing"].get("docket_id")
        if docket_id:
            docket_pools[docket_id].append(entry)

    active_dockets = [d for d in tracked_docket_uuids if docket_pools.get(d)]
    n_active = len(active_dockets)
    remaining_slots = BRIEF_ITEM_CAP - len(top_of_mind)
    effective_ceiling = remaining_slots if n_active == 1 else PER_DOCKET_CEILING

    allocated: dict[str, list] = defaultdict(list)

    if n_active > 0:
        if n_active <= remaining_slots:
            for d in active_dockets:
                allocated[d].append(docket_pools[d][0])
            cursors = {d: 1 for d in active_dockets}
            for _ in range(remaining_slots - n_active):
                best_d, best_s = None, -1
                for d in active_dockets:
                    if len(allocated[d]) >= effective_ceiling:
                        continue
                    idx = cursors[d]
                    if idx >= len(docket_pools[d]):
                        continue
                    s = docket_pools[d][idx]["score"]
                    if s > best_s:
                        best_s, best_d = s, d
                if best_d is None:
                    break
                allocated[best_d].append(docket_pools[best_d][cursors[best_d]])
                cursors[best_d] += 1
        else:
            by_best = sorted(
                active_dockets,
                key=lambda d: docket_pools[d][0]["score"],
                reverse=True,
            )
            for d in by_best[:remaining_slots]:
                allocated[d].append(docket_pools[d][0])

    docket_sections = []
    for d in allocated:
        tom_count = sum(1 for e in top_of_mind if e["filing"].get("docket_id") == d)
        ext_id = allocated[d][0]["filing"].get("docket_external_id") if allocated[d] else None
        docket_sections.append({
            "docket_id": d,
            "external_id": ext_id,
            "items": list(allocated[d]),
            "pool_total": len(docket_pools[d]) + tom_count,
            "section_score": max(e["score"] for e in allocated[d]),
        })
    docket_sections.sort(key=lambda s: s["section_score"], reverse=True)

    return {"top_of_mind": top_of_mind, "docket_sections": docket_sections}


def _build_subject(top_item: dict | None, item_count: int, brief_date: date) -> str:
    if top_item and item_count > 1:
        rest = item_count - 1
        return (
            f"{top_item['title'][:60]} · "
            f"{rest} more item{'s' if rest != 1 else ''}"
        )
    if top_item:
        return top_item["title"][:80]
    month = brief_date.strftime("%b")
    return f"NodalPulse · {month} {brief_date.day} · {item_count} items"


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


# ── #16 hallucination filter ─────────────────────────────────────────────────

def _is_hallucinated_summary(summary: str) -> bool:
    return summary.lower().startswith("filing summary unavailable")


def _has_claims(filing: dict) -> bool:
    payload = _parse_payload(filing.get("extraction_payload"))
    return bool(
        payload.get("summary")
        or payload.get("relief_requested")
        or payload.get("outcome")
        or (payload.get("key_points") or [])
    )


def filter_no_claims(filings: list[dict]) -> list[dict]:
    """Pre-allocate safety net: drop candidates whose extraction has no claims.

    The LLM is instructed to write 'Filing summary unavailable' when claims=[].
    Filtering here prevents wasting compose tokens and eliminates that path.
    Logs a warning if >50% are dropped — that signals an upstream extraction issue.
    """
    before = len(filings)
    result = [f for f in filings if _has_claims(f)]
    dropped = before - len(result)
    if dropped:
        logger.info("filter_no_claims: dropped %d/%d zero-claim candidates", dropped, before)
        if before > 0 and dropped / before > 0.5:
            logger.warning(
                "filter_no_claims: >50%% zero-claim (%d/%d) — upstream extraction quality issue",
                dropped,
                before,
            )
    return result


# ── #17 dedup ─────────────────────────────────────────────────────────────────

def dedup_candidates(filings: list[dict]) -> list[dict]:
    """Dedup by (title, docket_id). filer is always empty for PUCT crawler.

    Keeps the richest extraction (by JSON payload size). Ties broken by
    filed_at DESC (most recently filed wins). Explicit sort makes this
    order-independent from upstream query ordering.
    Filings with no title are kept as-is via a per-filing fallback path.
    """
    seen: dict[tuple, dict] = {}
    no_title: list[dict] = []

    def _filed_at_key(f: dict) -> str:
        v = f.get("filed_at")
        return v.isoformat() if v is not None and hasattr(v, "isoformat") else ""

    sorted_filings = sorted(filings, key=_filed_at_key, reverse=True)

    for f in sorted_filings:
        title = (f.get("title") or "").strip().lower()
        if not title:
            no_title.append(f)
            continue
        key = (title, f.get("docket_id"))
        existing = seen.get(key)
        if existing is None:
            seen[key] = f
        else:
            new_richness = len(json.dumps(f.get("extraction_payload") or {}))
            old_richness = len(json.dumps(existing.get("extraction_payload") or {}))
            if new_richness > old_richness:
                seen[key] = f

    result = list(seen.values()) + no_title
    dropped = len(sorted_filings) - len(no_title) - len(seen)
    if dropped:
        logger.info(
            "dedup_candidates: %d→%d (dropped %d duplicates)",
            len(filings),
            len(result),
            dropped,
        )
    return result


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
            app_url=settings.app_url,
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

    # ── Dedup + pre-allocate hallucination safety net ────────────────────────
    filings = dedup_candidates(filings)
    filings = filter_no_claims(filings)

    # ── Allocation ────────────────────────────────────────────────────────────
    # Per-docket path: user has tracked dockets + personalization active.
    # Flat path: global fallback or market/text/zone-only (no tracked dockets).
    use_docket_sections = filters_active and bool(bundle.tracked_docket_uuids)

    if use_docket_sections:
        allocated = allocate_brief(filings, bundle.tracked_docket_uuids, brief_date)
        # Tag each entry with its render destination before R2 check
        for e in allocated["top_of_mind"]:
            e["_dest"] = "top_of_mind"
        for sec in allocated["docket_sections"]:
            for e in sec["items"]:
                e["_dest"] = f"docket:{sec['docket_id']}"
        all_entries = [
            *allocated["top_of_mind"],
            *(e for sec in allocated["docket_sections"] for e in sec["items"]),
        ]
    else:
        scored_flat = sorted(
            [
                {
                    "filing": f,
                    "score": _score_filing(
                        f, brief_date, int(f.get("predicate_match_count") or 0)
                    ),
                }
                for f in filings
            ],
            key=lambda x: x["score"],
            reverse=True,
        )[:BRIEF_ITEM_CAP]
        tom_flat = [e for e in scored_flat[:TOP_OF_MIND_COUNT] if e["score"] >= 20]
        wc_flat = [e for e in scored_flat if e not in tom_flat]
        for e in tom_flat:
            e["_dest"] = "top_of_mind"
        for e in wc_flat:
            e["_dest"] = "what_changed"
        all_entries = tom_flat + wc_flat
        allocated = None

    # R2 existence check — drop filings whose source objects are missing
    r2_valid_ids: set[str] = set()
    for entry in all_entries:
        r2_key = entry["filing"].get("r2_key")
        if r2_key and not r2.exists(r2_key):
            logger.warning(
                "R2 key missing — dropping filing %s", entry["filing"]["filing_id"]
            )
            continue
        r2_valid_ids.add(entry["filing"]["filing_id"])

    all_sections_ordered = [e for e in all_entries if e["filing"]["filing_id"] in r2_valid_ids]

    if not all_sections_ordered:
        logger.warning("All items failed R2 check for user %s", user_id)
        return {"user_id": user_id, "status": "skipped", "reason": "all_r2_missing"}

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

    # Citation validation — route validated items to their destination sections
    sections: dict[str, list[dict]] = {"top_of_mind": [], "what_changed": []}
    docket_items_out: dict[str, list[dict]] = defaultdict(list)
    valid_filing_ids: list[str] = []
    citation_count = 0
    hallucination_drop_count = 0
    fallback_items: list[dict] = []

    def _process_entry(entry: dict) -> None:
        nonlocal citation_count, hallucination_drop_count
        f = entry["filing"]
        fid = f["filing_id"]
        dest = entry.get("_dest", "what_changed")
        item_data = composed_by_id.get(fid)
        if not item_data:
            return
        citation = item_data.get("citation", "")
        p = _parse_payload(f.get("extraction_payload"))
        if not _CITATION_RE.search(citation):
            logger.warning("Bad citation for %s: %r — dropping", fid, citation)
            fallback_items.append({
                "filing_id": fid,
                "title": f["title"],
                "summary": p.get("summary", "Filing summary unavailable; see source."),
                "citation": _build_citation(p, f),
                "doc_type": f.get("doc_type", ""),
                "source_url": f.get("source_url", ""),
                **_deadline_badge_info(p, brief_date),
            })
            return
        summary = item_data["summary"]
        if _is_hallucinated_summary(summary):
            hallucination_drop_count += 1
            logger.warning("Hallucinated summary for filing %s — dropping", fid)
            return
        item_dict = {
            "filing_id": fid,
            "title": f["title"],
            "summary": summary,
            "citation": citation,
            "doc_type": f.get("doc_type", ""),
            "source_url": f.get("source_url", ""),
            **_deadline_badge_info(p, brief_date),
        }
        if dest == "top_of_mind":
            sections["top_of_mind"].append(item_dict)
        elif dest.startswith("docket:"):
            docket_items_out[dest[len("docket:"):]].append(item_dict)
        else:
            sections["what_changed"].append(item_dict)
        valid_filing_ids.append(fid)
        citation_count += 1

    for entry in all_sections_ordered:
        _process_entry(entry)

    if hallucination_drop_count > 0:
        logger.info(
            "compose: dropped %d hallucinated summaries for user=%s",
            hallucination_drop_count,
            user_id,
        )
        if n_expected > 0 and hallucination_drop_count / n_expected > 0.5:
            logger.warning(
                "compose: >50%% hallucinated summaries (%d/%d) — investigate compose prompt for user=%s",
                hallucination_drop_count,
                n_expected,
                user_id,
            )

    # Build final docket sections list (preserves allocate_brief order)
    final_docket_sections: list[dict] = []
    if use_docket_sections and allocated:
        for sec in allocated["docket_sections"]:
            items = docket_items_out.get(sec["docket_id"], [])
            if items:
                final_docket_sections.append({
                    "external_id": sec["external_id"] or sec["docket_id"][:8],
                    "pool_total": sec["pool_total"],
                    "items": items,
                })

    item_count = (
        len(sections["top_of_mind"])
        + len(sections["what_changed"])
        + sum(len(s["items"]) for s in final_docket_sections)
    )

    if item_count == 0:
        logger.warning("All citations failed for user %s — sending fallback brief", user_id)
        for fb in fallback_items[:10]:
            fid = fb["filing_id"]
            dest = next(
                (e.get("_dest", "what_changed") for e in all_sections_ordered
                 if e["filing"]["filing_id"] == fid),
                "what_changed",
            )
            if dest == "top_of_mind":
                sections["top_of_mind"].append(fb)
            elif dest.startswith("docket:") and use_docket_sections and allocated:
                did = dest[len("docket:"):]
                # Find or create the section in final_docket_sections
                sec_match = next((s for s in final_docket_sections if
                                  s["external_id"] == next(
                                      (x["external_id"] for x in allocated["docket_sections"]
                                       if x["docket_id"] == did), None)), None)
                if sec_match:
                    sec_match["items"].append(fb)
                else:
                    ext = next(
                        (x["external_id"] or did[:8] for x in allocated["docket_sections"]
                         if x["docket_id"] == did), did[:8]
                    )
                    final_docket_sections.append({"external_id": ext, "pool_total": 1, "items": [fb]})
            else:
                sections["what_changed"].append(fb)
            valid_filing_ids.append(fid)
        item_count = (
            len(sections["top_of_mind"])
            + len(sections["what_changed"])
            + sum(len(s["items"]) for s in final_docket_sections)
        )

    if item_count == 0:
        return {"user_id": user_id, "status": "skipped", "reason": "no_valid_items"}

    # Email subject
    first_tom = sections["top_of_mind"][0] if sections["top_of_mind"] else None
    first_docket_item = (
        final_docket_sections[0]["items"][0]
        if final_docket_sections and final_docket_sections[0]["items"]
        else None
    )
    top_item = first_tom or first_docket_item or (
        sections["what_changed"][0] if sections["what_changed"] else None
    )
    subject = _build_subject(top_item, item_count, brief_date)

    generated_at = datetime.now(UTC)

    # Build HTML + plain text
    html = build_brief_html(
        brief_date=brief_date,
        sections=sections,
        docket_sections=final_docket_sections,
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
        docket_sections=final_docket_sections,
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
