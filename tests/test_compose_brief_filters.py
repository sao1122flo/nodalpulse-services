"""Unit tests for #16 (hallucination filter) and #17 (dedup) compose_brief helpers.

Tests cover:
  #16 — _is_hallucinated_summary: exact match, case variants, false positives
  #16 — _has_claims: all claim-field combinations
  #16 — filter_no_claims: drops zero-claim candidates, warns at >50%
  #17 — dedup_candidates: (title, docket_id) key, richness tiebreaker,
         filed_at-order tiebreak, cross-docket same-title safety,
         empty-title pass-through, 38× crawler fanout case
"""

import logging
from datetime import date

import pytest

from nodalpulse.workers.compose_brief import (
    _has_claims,
    _is_hallucinated_summary,
    dedup_candidates,
    filter_no_claims,
)

D1 = "aaaa0000-0000-0000-0000-000000000001"
D2 = "bbbb0000-0000-0000-0000-000000000002"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _filing(filing_id: str, title: str = "Test Filing", docket_id: str | None = D1,
            payload: dict | None = None) -> dict:
    return {
        "filing_id": filing_id,
        "title": title,
        "doc_type": "puct-filing",
        "filer": "",
        "filed_at": date(2026, 5, 27),
        "docket_id": docket_id,
        "extraction_payload": payload if payload is not None else {},
    }


# ── #16: _is_hallucinated_summary ────────────────────────────────────────────

class TestIsHallucinatedSummary:
    def test_exact_phrase(self):
        assert _is_hallucinated_summary("Filing summary unavailable; see source.")

    def test_exact_phrase_uppercase(self):
        assert _is_hallucinated_summary("FILING SUMMARY UNAVAILABLE; SEE SOURCE.")

    def test_mixed_case(self):
        assert _is_hallucinated_summary("Filing Summary Unavailable")

    def test_empty_string(self):
        assert not _is_hallucinated_summary("")

    def test_valid_summary_containing_unavailable_elsewhere(self):
        assert not _is_hallucinated_summary(
            "AEP requests rate changes; prior refunds will be unavailable under new tariff."
        )

    def test_starts_with_filing_summary_but_not_unavailable(self):
        assert not _is_hallucinated_summary(
            "Filing summary of the proposed order is attached."
        )

    def test_partial_prefix_only(self):
        assert not _is_hallucinated_summary("Filing summary")


# ── #16: _has_claims ─────────────────────────────────────────────────────────

class TestHasClaims:
    def test_has_summary_field(self):
        assert _has_claims(_filing("a", payload={"summary": "AEP requests changes."}))

    def test_has_relief_requested(self):
        assert _has_claims(_filing("a", payload={"relief_requested": "Approve tariff."}))

    def test_has_outcome(self):
        assert _has_claims(_filing("a", payload={"outcome": "Order granted."}))

    def test_has_key_points(self):
        assert _has_claims(_filing("a", payload={"key_points": ["Rate increase proposed."]}))

    def test_empty_payload_dict(self):
        assert not _has_claims(_filing("a", payload={}))

    def test_all_fields_empty_strings(self):
        assert not _has_claims(_filing("a", payload={
            "summary": "", "relief_requested": "", "outcome": ""
        }))

    def test_empty_key_points_list(self):
        assert not _has_claims(_filing("a", payload={"key_points": []}))

    def test_null_payload(self):
        assert not _has_claims(_filing("a", payload=None))

    def test_missing_payload_key(self):
        f = _filing("a")
        del f["extraction_payload"]
        assert not _has_claims(f)


# ── #16: filter_no_claims ────────────────────────────────────────────────────

class TestFilterNoClaims:
    def test_drops_zero_claim_filings(self):
        filings = [
            _filing("a", payload={}),
            _filing("b", payload={"summary": "Valid summary."}),
            _filing("c", payload={}),
        ]
        result = filter_no_claims(filings)
        assert [f["filing_id"] for f in result] == ["b"]

    def test_keeps_all_when_all_have_claims(self):
        filings = [
            _filing("a", payload={"summary": "A"}),
            _filing("b", payload={"relief_requested": "B"}),
            _filing("c", payload={"key_points": ["C"]}),
        ]
        assert len(filter_no_claims(filings)) == 3

    def test_empty_input_returns_empty(self):
        assert filter_no_claims([]) == []

    def test_no_warning_when_below_50pct(self, caplog):
        filings = [
            _filing("a", payload={}),           # dropped
            _filing("b", payload={"summary": "x"}),  # kept
            _filing("c", payload={"summary": "y"}),  # kept
        ]
        with caplog.at_level(logging.WARNING, logger="nodalpulse.workers.compose_brief"):
            filter_no_claims(filings)
        assert "50%" not in caplog.text

    def test_warning_logged_when_above_50pct(self, caplog):
        filings = [
            _filing("a", payload={}),           # dropped
            _filing("b", payload={}),           # dropped
            _filing("c", payload={"summary": "x"}),  # kept
        ]
        with caplog.at_level(logging.WARNING, logger="nodalpulse.workers.compose_brief"):
            filter_no_claims(filings)
        assert "50%" in caplog.text

    def test_warning_logged_at_100pct(self, caplog):
        filings = [_filing("a", payload={}), _filing("b", payload={})]
        with caplog.at_level(logging.WARNING, logger="nodalpulse.workers.compose_brief"):
            filter_no_claims(filings)
        assert "50%" in caplog.text


# ── #17: dedup_candidates ────────────────────────────────────────────────────

class TestDedupCandidates:
    def test_same_title_same_docket_reduces_to_one(self):
        filings = [
            _filing("a1", title="Bishop Testimony", docket_id=D1),
            _filing("a2", title="Bishop Testimony", docket_id=D1),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_keeps_richer_extraction(self):
        rich = {"summary": "Detailed summary.", "key_points": ["A", "B", "C"]}
        sparse = {"summary": "Short."}
        filings = [
            _filing("a1", title="Bishop Testimony", docket_id=D1, payload=sparse),
            _filing("a2", title="Bishop Testimony", docket_id=D1, payload=rich),
        ]
        result = dedup_candidates(filings)
        assert result[0]["filing_id"] == "a2"

    def test_tiebreak_keeps_first_seen(self):
        # Equal richness — first seen (most recently filed, per DESC order) wins
        same = {"summary": "Same content."}
        filings = [
            _filing("first",  title="Bishop Testimony", docket_id=D1, payload=same),
            _filing("second", title="Bishop Testimony", docket_id=D1, payload=same),
        ]
        result = dedup_candidates(filings)
        assert result[0]["filing_id"] == "first"

    def test_different_titles_same_docket_both_kept(self):
        filings = [
            _filing("a1", title="Bishop Testimony",       docket_id=D1),
            _filing("a2", title="Wesely Protective Order", docket_id=D1),
        ]
        assert len(dedup_candidates(filings)) == 2

    def test_same_title_different_docket_both_kept(self):
        filings = [
            _filing("a1", title="Initial Brief", docket_id=D1),
            _filing("a2", title="Initial Brief", docket_id=D2),
        ]
        assert len(dedup_candidates(filings)) == 2

    def test_empty_title_all_kept(self):
        filings = [
            _filing("a1", title="",           docket_id=D1),
            _filing("a2", title="",           docket_id=D1),
            _filing("a3", title="Real Title", docket_id=D1),
        ]
        result = dedup_candidates(filings)
        # a1 + a2 kept via no-title path; a3 kept via seen dict
        assert len(result) == 3

    def test_three_duplicates_reduces_to_one(self):
        filings = [
            _filing(f"f{i}", title="Statement of Position", docket_id=D1)
            for i in range(3)
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_38x_crawler_fanout_reduces_to_one(self):
        filings = [
            _filing(f"f{i}", title="CY 2026 Registration", docket_id=D1)
            for i in range(38)
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_title_comparison_case_insensitive(self):
        filings = [
            _filing("a1", title="BISHOP TESTIMONY", docket_id=D1),
            _filing("a2", title="Bishop Testimony", docket_id=D1),
            _filing("a3", title="bishop testimony", docket_id=D1),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_title_comparison_strips_whitespace(self):
        filings = [
            _filing("a1", title="  Bishop Testimony  ", docket_id=D1),
            _filing("a2", title="Bishop Testimony",     docket_id=D1),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_ercot_filings_none_docket_dedup_by_title(self):
        filings = [
            _filing("a1", title="NPRR 1234 Revision", docket_id=None),
            _filing("a2", title="NPRR 1234 Revision", docket_id=None),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_empty_input(self):
        assert dedup_candidates([]) == []

    def test_single_filing_unchanged(self):
        filings = [_filing("solo", title="Solo Filing", docket_id=D1)]
        result = dedup_candidates(filings)
        assert len(result) == 1
        assert result[0]["filing_id"] == "solo"

    def test_drop_count_logged(self, caplog):
        filings = [
            _filing(f"f{i}", title="Direct Testimony", docket_id=D1)
            for i in range(24)
        ]
        with caplog.at_level(logging.INFO, logger="nodalpulse.workers.compose_brief"):
            dedup_candidates(filings)
        assert "23" in caplog.text  # dropped 23 out of 24
