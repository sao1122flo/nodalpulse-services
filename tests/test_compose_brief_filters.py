"""Unit tests for #16 (hallucination filter) and #17 (dedup) compose_brief helpers.

Tests cover:
  #16 — _is_hallucinated_summary: exact match, case variants, false positives
  #16 — _has_claims: all claim-field combinations
  #16 — filter_no_claims: drops zero-claim candidates, warns at >50%
  #17 — dedup_candidates: item_key dedup, richness tiebreaker,
         filed_at-order tiebreak, no-item-key pass-through (ERCOT),
         38× different-party same-title case all kept
"""

import logging
from datetime import date

import pytest

from nodalpulse.workers.compose_brief import (
    _extract_item_key,
    _has_claims,
    _is_hallucinated_summary,
    dedup_candidates,
    filter_no_claims,
)

D1 = "aaaa0000-0000-0000-0000-000000000001"
D2 = "bbbb0000-0000-0000-0000-000000000002"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _filing(filing_id: str, title: str = "Test Filing", docket_id: str | None = D1,
            payload: dict | None = None, item_key: str | None = None) -> dict:
    metadata: dict = {}
    if item_key:
        metadata["item_key"] = item_key
    return {
        "filing_id": filing_id,
        "title": title,
        "doc_type": "puct-filing",
        "filer": "",
        "filed_at": date(2026, 5, 27),
        "docket_id": docket_id,
        "metadata": metadata,
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

class TestExtractItemKey:
    def test_returns_item_key_from_dict_metadata(self):
        f = _filing("a1", item_key="59070_448")
        assert _extract_item_key(f) == "59070_448"

    def test_returns_none_when_no_metadata(self):
        f = _filing("a1")  # no item_key
        assert _extract_item_key(f) is None

    def test_parses_json_string_metadata(self):
        import json
        f = _filing("a1")
        f["metadata"] = json.dumps({"item_key": "59070_448"})
        assert _extract_item_key(f) == "59070_448"

    def test_returns_none_for_empty_string_item_key(self):
        f = _filing("a1")
        f["metadata"] = {"item_key": ""}
        assert _extract_item_key(f) is None


class TestDedupCandidates:
    def test_same_item_key_reduces_to_one(self):
        # ZIP + PDF of the same submission share item_key → collapsed
        filings = [
            _filing("a1", title="Bishop Testimony", item_key="59475_100"),
            _filing("a2", title="Bishop Testimony", item_key="59475_100"),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_keeps_richer_extraction(self):
        rich = {"summary": "Detailed summary.", "key_points": ["A", "B", "C"]}
        sparse = {"summary": "Short."}
        filings = [
            _filing("a1", title="Bishop Testimony", item_key="59475_100", payload=sparse),
            _filing("a2", title="Bishop Testimony", item_key="59475_100", payload=rich),
        ]
        result = dedup_candidates(filings)
        assert result[0]["filing_id"] == "a2"

    def test_tiebreak_keeps_first_seen(self):
        # Equal richness — first seen (most recently filed, per DESC sort) wins
        same = {"summary": "Same content."}
        filings = [
            _filing("first",  title="Bishop Testimony", item_key="59475_100", payload=same),
            _filing("second", title="Bishop Testimony", item_key="59475_100", payload=same),
        ]
        result = dedup_candidates(filings)
        assert result[0]["filing_id"] == "first"

    def test_different_item_keys_same_title_both_kept(self):
        # THE CORE FIX: different parties filing the same doc type → different item_keys → both kept
        filings = [
            _filing("a1", title="Statement of Position", docket_id=D1, item_key="59475_100"),
            _filing("a2", title="Statement of Position", docket_id=D1, item_key="59475_101"),
        ]
        assert len(dedup_candidates(filings)) == 2

    def test_different_item_keys_different_dockets_both_kept(self):
        filings = [
            _filing("a1", title="Initial Brief", docket_id=D1, item_key="59070_10"),
            _filing("a2", title="Initial Brief", docket_id=D2, item_key="59475_10"),
        ]
        assert len(dedup_candidates(filings)) == 2

    def test_no_item_key_all_kept(self):
        # ERCOT filings have no item_key — all pass through without dedup
        filings = [
            _filing("a1", title="Market Notice"),
            _filing("a2", title="Market Notice"),
            _filing("a3", title="Market Notice"),
        ]
        assert len(dedup_candidates(filings)) == 3

    def test_mixed_item_key_and_no_key(self):
        filings = [
            _filing("a1", title="PUCT Filing", item_key="59070_1"),
            _filing("a2", title="PUCT Filing", item_key="59070_1"),  # dup of a1
            _filing("a3", title="ERCOT Notice"),  # no key, pass-through
        ]
        # a1+a2 collapse to 1, a3 passes through
        assert len(dedup_candidates(filings)) == 2

    def test_three_files_same_item_key_reduces_to_one(self):
        # Submission with 3 attachments (PDF, ZIP, DOCX) → keep richest
        filings = [
            _filing(f"f{i}", title="Direct Testimony", item_key="59475_200")
            for i in range(3)
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_38x_different_parties_all_kept(self):
        # THE REGRESSION TEST: 38 different companies filing same type → 38 different item_keys
        # Old behavior incorrectly collapsed these to 1. New behavior keeps all 38.
        filings = [
            _filing(f"f{i}", title="CY 2026 Registration", docket_id=D1,
                    item_key=f"59070_{448 + i}")
            for i in range(38)
        ]
        assert len(dedup_candidates(filings)) == 38

    def test_zip_pdf_pair_reduces_to_one(self):
        # Real-world: same submission, one ZIP + one PDF → both share item_key → collapse to 1
        filings = [
            _filing("zip", title="Joint Application", item_key="59336_2367"),
            _filing("pdf", title="Joint Application", item_key="59336_2367"),
        ]
        assert len(dedup_candidates(filings)) == 1

    def test_ercot_filings_no_item_key_all_pass_through(self):
        # ERCOT filings don't carry item_key — all are kept regardless of title
        filings = [
            _filing("a1", title="NPRR 1234 Revision", docket_id=None),
            _filing("a2", title="NPRR 1234 Revision", docket_id=None),
        ]
        assert len(dedup_candidates(filings)) == 2

    def test_empty_input(self):
        assert dedup_candidates([]) == []

    def test_single_filing_unchanged(self):
        filings = [_filing("solo", title="Solo Filing", docket_id=D1)]
        result = dedup_candidates(filings)
        assert len(result) == 1
        assert result[0]["filing_id"] == "solo"

    def test_drop_count_logged(self, caplog):
        # 24 filings sharing one item_key → 23 dropped, 1 kept
        filings = [
            _filing(f"f{i}", title="Direct Testimony", docket_id=D1, item_key="59475_999")
            for i in range(24)
        ]
        with caplog.at_level(logging.INFO, logger="nodalpulse.workers.compose_brief"):
            dedup_candidates(filings)
        assert "23" in caplog.text  # dropped 23 out of 24
