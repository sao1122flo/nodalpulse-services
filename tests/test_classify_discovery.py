"""Unit tests for the B3 theme classifier's pure guardrail logic (G1/G3)."""

from nodalpulse.workers.classify_discovery import (
    _build_system,
    _parse_matches,
    _verbatim_snippet,
)

_THEMES = [
    {"id": "1", "key": "roe", "label": "Return on Equity", "definition": "ROE disputes."},
    {"id": "2", "key": "bess", "label": "Battery Storage", "definition": "Storage resources."},
]


# ── G1: evidence must be a verbatim substring of the description ──────────────


def test_verbatim_accepts_exact_substring():
    desc = "Revisions to Attachment F to Update the Return on Equity to be effective 6/30/2026"
    assert _verbatim_snippet("Update the Return on Equity", desc) == "Update the Return on Equity"


def test_verbatim_is_case_and_whitespace_insensitive():
    desc = "Annual   Formula  Rate  Update of Duke Energy"
    # different spacing/case but same words → still a real substring, accepted
    assert _verbatim_snippet("annual formula rate update", desc) is not None


def test_verbatim_rejects_paraphrase():
    desc = "Application for market-based rate authority for a storage project"
    # plausible paraphrase that is NOT in the text → dropped to None (no fake cita)
    assert _verbatim_snippet("this filing concerns battery energy storage", desc) is None


def test_verbatim_rejects_too_short_or_empty():
    desc = "Some filing about storage and rates"
    assert _verbatim_snippet("rates", desc) is None  # < 8 chars
    assert _verbatim_snippet("", desc) is None
    assert _verbatim_snippet(None, desc) is None


# ── parsing robustness ───────────────────────────────────────────────────────


def test_parse_plain_json():
    raw = '{"matches": [{"theme_key": "roe", "evidence": "Return on Equity"}]}'
    out = _parse_matches(raw)
    assert out == [{"theme_key": "roe", "evidence": "Return on Equity"}]


def test_parse_strips_markdown_fences():
    raw = '```json\n{"matches": [{"theme_key": "bess", "evidence": "Energy Storage"}]}\n```'
    assert _parse_matches(raw)[0]["theme_key"] == "bess"


def test_parse_garbage_returns_empty():
    assert _parse_matches("not json at all") == []
    assert _parse_matches("") == []
    assert _parse_matches('{"unexpected": true}') == []


# ── system prompt carries the taxonomy ───────────────────────────────────────


def test_build_system_lists_every_theme_key_and_caps():
    sys = _build_system(_THEMES)
    assert "roe:" in sys and "bess:" in sys
    assert "at most 3" in sys.lower()
    assert "verbatim" in sys.lower()
