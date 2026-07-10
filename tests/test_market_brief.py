"""Tests for the never-empty quiet-day email (build_market_brief_html).

The email templates format dates with strftime "%-d", which is only valid on
Linux/macOS (prod + CI), not Windows — so these render-level tests skip on
win32. The routing that decides market-brief vs plain-quiet lives in
compose_brief.handle_compose_brief and is exercised against the DB.
"""

import sys
from datetime import date

import pytest

from nodalpulse.email.templates import build_market_brief_html, build_quiet_day_html

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="strftime %-d unsupported on Windows; templates render on Linux CI/prod",
)

_SALIENCE = [
    {
        "market": "FERC",
        "docket_key": "EL26-67",
        "headline": "FERC conditionally waives affiliate transaction restrictions for VEPCO",
    },
    {
        "market": "PUCT",
        "docket_key": "59818",
        "headline": "PUCT denies joint extension request in docket 58851",
    },
]
_DISCOVERY = [
    {
        "accession": "20260709-5001",
        "description": "Motion to Intervene of Hecate Energy LLC under EL26-70.",
        "filer_names": ["Hecate Energy LLC"],
        "docket_numbers": ["EL26-70"],
        "filed_at": "2026-07-09",
        "doc_type": "motion",
        "matched_on": "filer_name",
    },
]


def _mb(**kw):
    base = dict(
        brief_date=date(2026, 7, 10),
        app_url="https://nodalpulse.com",
        unsubscribe_url="https://nodalpulse.com/unsubscribe/abc",
        tracked_count=7,
        corpus_count=41,
        salience_items=_SALIENCE,
        discovery_hits=_DISCOVERY,
        record_url="https://nodalpulse.com/record/ferc/2026-07-09",
    )
    base.update(kw)
    return build_market_brief_html(**base)


def test_market_brief_carries_salience_and_mentions():
    """The whole point: a quiet day is never an empty email — it surfaces signal."""
    html = _mb()
    assert "Quiet in your matters" in html
    assert "7 tracked matters" in html
    assert "MARKET HIGHLIGHTS" in html
    assert "VEPCO" in html  # FERC salience headline
    assert "denies joint extension" in html  # PUCT salience headline
    assert "Mentions of your entities" in html
    assert "Hecate Energy LLC" in html


def test_widen_nudge_is_honest_about_the_wider_corpus():
    html = _mb(corpus_count=41)
    assert "41 filings landed in this window" in html
    assert "Widen your filters" in html


def test_widen_nudge_suppressed_when_wider_corpus_empty():
    # corpus_count=0 → nothing to widen toward, so no misleading nudge.
    html = _mb(corpus_count=0)
    assert "Widen your filters" not in html
    assert "landed in this window" not in html


def test_singular_matter_wording():
    html = _mb(tracked_count=1)
    assert "1 tracked matter " in html  # trailing space → not "matters"


def test_quiet_day_reports_the_true_corpus_count():
    # Regression: the personalized path used to pass the *filtered* count (0),
    # rendering the false "full corpus had 0 filings". It now passes true corpus.
    qd = build_quiet_day_html(
        brief_date=date(2026, 7, 10),
        corpus_count=41,
        app_url="https://nodalpulse.com",
        unsubscribe_url="https://nodalpulse.com/unsubscribe/abc",
    )
    assert "had 41 filings" in qd
