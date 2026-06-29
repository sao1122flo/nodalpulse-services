"""Theme classification over the FERC discovery firehose (B3).

One cheap Haiku pass per new discovery_feed row vs the shared theme taxonomy —
GLOBAL (not per-user), so cost is O(filings) regardless of subscriber count.
Per-user "untracked" filtering and theme subscription happen at read time (web).

Guardrails:
  G1 — evidence_snippet is persisted ONLY when it is a verbatim substring of the
       description; paraphrased "quotes" are dropped to NULL (never shown as a cita).
  G3 — at most _MAX_THEMES_PER_FILING strong matches per filing.
"""

import json
import logging
import re

from nodalpulse.db.discovery import (
    get_active_themes,
    get_unclassified_discovery,
    save_discovery_matches,
)
from nodalpulse.llm.client import classify

logger = logging.getLogger(__name__)

_MAX_THEMES_PER_FILING = 3  # G3
_PROMPT_VER = "1.0"
_MIN_EVIDENCE_LEN = 8


def _build_system(themes: list[dict]) -> str:
    lines = [
        "You classify FERC electric-utility filings by regulatory theme.",
        "",
        "Themes — match ONLY when the filing is clearly and substantively about the theme:",
    ]
    for t in themes:
        lines.append(f"- {t['key']}: {t['definition']}")
    lines += [
        "",
        "Rules:",
        "- Match only themes that are a clear, central subject of THIS filing. When in doubt, do NOT match.",
        f"- Return at most {_MAX_THEMES_PER_FILING} themes; prefer 0-1. Use 2-3 only when the filing is genuinely multi-topic.",
        "- Generic procedural filings (notice, motion to intervene, certificate of service, "
        "extension of time, routine compliance with no substantive subject) → return no matches.",
        '- For each matched theme, provide "evidence": a SHORT quote (<=140 chars) copied '
        "VERBATIM from the filing description that shows the match. Do NOT paraphrase or invent. "
        "If no verbatim phrase from the description supports the theme, omit that theme.",
        "",
        "Respond with JSON only, no markdown fences:",
        '{"matches": [{"theme_key": "<key>", "evidence": "<verbatim quote from the description>"}]}',
        'If nothing clearly matches: {"matches": []}',
    ]
    return "\n".join(lines)


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _verbatim_snippet(evidence: str | None, description: str) -> str | None:
    """Return the evidence only if it is a verbatim substring of description (G1)."""
    e = _norm(evidence)
    if e and len(e) >= _MIN_EVIDENCE_LEN and e in _norm(description):
        return (evidence or "").strip()[:200]
    return None


def _parse_matches(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data.get("matches", []) if isinstance(data, dict) else []


async def classify_new_discovery_items(limit: int = 500) -> dict:
    """Classify unthemed discovery_feed rows; populate discovery_matches.

    Returns a small summary dict. Safe to call repeatedly — only touches rows
    with themed_at IS NULL, and marks each row themed afterward.
    """
    themes = await get_active_themes()
    if not themes:
        logger.warning("classify_new_discovery_items: no active themes, skipping")
        return {"classified": 0, "matched": 0, "no_verbatim": 0, "themes": 0}

    by_key = {t["key"]: t["id"] for t in themes}
    system = _build_system(themes)
    rows = await get_unclassified_discovery(limit)

    classified = matched = no_verbatim = 0
    for row in rows:
        desc = row.get("description") or ""
        dockets = ", ".join(row.get("docket_numbers") or [])
        user = f"Filing description: {desc}\nDoc type: {row.get('doc_type')}\nDockets: {dockets}"
        try:
            raw = await classify(system=system, user=user, prompt_version=_PROMPT_VER)
        except Exception:
            logger.exception("classify_discovery: LLM failed for %s", row.get("accession"))
            continue  # leave themed_at NULL → retried next sweep

        seen: set[str] = set()
        out: list[tuple[str, str | None]] = []
        for m in _parse_matches(raw):
            key = (m.get("theme_key") or "").strip()
            if key not in by_key or key in seen:
                continue
            seen.add(key)
            snippet = _verbatim_snippet(m.get("evidence"), desc)
            if snippet is None:
                no_verbatim += 1
            out.append((by_key[key], snippet))
            if len(out) >= _MAX_THEMES_PER_FILING:
                break

        await save_discovery_matches(row["id"], out)
        classified += 1
        matched += len(out)

    result = {
        "classified": classified,
        "matched": matched,
        "no_verbatim": no_verbatim,
        "themes": len(themes),
    }
    logger.info("classify_new_discovery_items: %s", result)
    return result
