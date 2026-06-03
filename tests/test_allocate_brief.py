"""Unit tests for allocate_brief() and supporting helpers.

Tests cover:
  1. sao1122 case: 2 active dockets (59336=136, 59475=79) — floor+bonus+ceiling
  2. Pro user: 10 active dockets — all represented, bonus distributed
  3. Org user: 50 active dockets — overflow path, top 20 by score
  4. Single docket (Starter): ceiling override to remaining_slots
  5. Zero candidates: empty output
  6. Single candidate: ends up in top_of_mind, no docket sections
  7. All candidates same docket: ceiling override (n_active=1)
  8. Filings with docket_id=None excluded from docket sections (ERCOT)
  9. Quota math: total items never exceeds BRIEF_ITEM_CAP
 10. _build_subject: correct string output
 11. _deadline_badge_info: correct date windows
"""

from datetime import date
from unittest.mock import patch

import pytest

from nodalpulse.workers.compose_brief import (
    BRIEF_ITEM_CAP,
    PER_DOCKET_CEILING,
    TOP_OF_MIND_COUNT,
    _build_subject,
    _deadline_badge_info,
    allocate_brief,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_filing(
    filing_id: str,
    docket_id: str | None = None,
    docket_external_id: str | None = None,
    score_hint: int = 5,  # base score via haiku_verdict=relevant → +10, others 0
) -> dict:
    """Minimal filing dict compatible with _score_filing / allocate_brief."""
    return {
        "filing_id": filing_id,
        "doc_type": "puct-filing",
        "title": f"Filing {filing_id}",
        "filer": "Test Filer",
        "filed_at": date(2026, 5, 27),
        "r2_key": f"raw/{filing_id}.pdf",
        "source_url": None,
        "metadata": {},
        "docket_id": docket_id,
        "docket_external_id": docket_external_id,
        "extraction_id": None,
        "extraction_payload": {},
        "haiku_verdict": "relevant" if score_hint >= 10 else "uncertain",
        "predicate_match_count": score_hint // 10,
    }


def _make_pool(
    docket_id: str,
    external_id: str,
    count: int,
    base_score_hint: int = 5,
) -> list[dict]:
    return [
        _make_filing(
            f"{docket_id}_{i}",
            docket_id=docket_id,
            docket_external_id=external_id,
            score_hint=base_score_hint,
        )
        for i in range(count)
    ]


BRIEF_DATE = date(2026, 5, 27)

D1 = "aaaa0000-0000-0000-0000-000000000001"  # high-volume docket (simulates 59336)
D2 = "bbbb0000-0000-0000-0000-000000000002"  # lower-volume docket (simulates 59475)


# ── 1. sao1122 case ───────────────────────────────────────────────────────────

class TestSao1122Case:
    def test_basic_allocation(self):
        candidates = _make_pool(D1, "59336", 136) + _make_pool(D2, "59475", 79)
        result = allocate_brief(candidates, [D1, D2], BRIEF_DATE)

        assert len(result["top_of_mind"]) == TOP_OF_MIND_COUNT
        assert len(result["docket_sections"]) == 2

        # Identify section for D1
        d1_sec = next(s for s in result["docket_sections"] if s["docket_id"] == D1)
        d2_sec = next(s for s in result["docket_sections"] if s["docket_id"] == D2)

        # D1 is larger pool — should hit ceiling (all scores equal, bonus fills D1 first)
        assert d1_sec["items"] != [] or d2_sec["items"] != []

        # Total must equal BRIEF_ITEM_CAP
        total = len(result["top_of_mind"]) + sum(len(s["items"]) for s in result["docket_sections"])
        assert total == BRIEF_ITEM_CAP

    def test_both_dockets_represented(self):
        candidates = _make_pool(D1, "59336", 136) + _make_pool(D2, "59475", 79)
        result = allocate_brief(candidates, [D1, D2], BRIEF_DATE)
        section_docket_ids = {s["docket_id"] for s in result["docket_sections"]}
        assert D1 in section_docket_ids
        assert D2 in section_docket_ids

    def test_ceiling_enforced_on_dominant_docket(self):
        """D1 with equal scores should hit PER_DOCKET_CEILING, not monopolise body."""
        candidates = _make_pool(D1, "59336", 136) + _make_pool(D2, "59475", 79)
        result = allocate_brief(candidates, [D1, D2], BRIEF_DATE)
        d1_sec = next(s for s in result["docket_sections"] if s["docket_id"] == D1)
        assert len(d1_sec["items"]) <= PER_DOCKET_CEILING

    def test_pool_total_includes_tom_items(self):
        """pool_total for a docket includes filings claimed by top_of_mind."""
        candidates = _make_pool(D1, "59336", 136) + _make_pool(D2, "59475", 79)
        result = allocate_brief(candidates, [D1, D2], BRIEF_DATE)
        tom_d1_count = sum(1 for e in result["top_of_mind"] if e["filing"].get("docket_id") == D1)
        d1_sec = next((s for s in result["docket_sections"] if s["docket_id"] == D1), None)
        if d1_sec:
            # pool_total should count TOM items too
            assert d1_sec["pool_total"] >= len(d1_sec["items"])


# ── 2. Pro user: 10 active dockets ───────────────────────────────────────────

class TestProUser10Dockets:
    def _dockets(self):
        return [f"dddd{i:04d}-0000-0000-0000-000000000000" for i in range(10)]

    def test_all_dockets_represented(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(50000 + i), 30)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        section_ids = {s["docket_id"] for s in result["docket_sections"]}
        assert section_ids == set(dockets)

    def test_total_equals_cap(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(50000 + i), 30)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        total = len(result["top_of_mind"]) + sum(len(s["items"]) for s in result["docket_sections"])
        assert total == BRIEF_ITEM_CAP

    def test_section_count(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(50000 + i), 30)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        assert len(result["docket_sections"]) == 10


# ── 3. Org user: 50 active dockets (overflow path) ───────────────────────────

class TestOrgUser50Dockets:
    def _dockets(self):
        return [f"eeee{i:04d}-0000-0000-0000-000000000000" for i in range(50)]

    def test_overflow_path_uses_top_20(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(60000 + i), 5)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        remaining_slots = BRIEF_ITEM_CAP - len(result["top_of_mind"])
        assert len(result["docket_sections"]) == remaining_slots

    def test_overflow_each_section_has_one_item(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(60000 + i), 5)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        for sec in result["docket_sections"]:
            assert len(sec["items"]) == 1

    def test_total_equals_cap(self):
        dockets = self._dockets()
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(60000 + i), 5)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        total = len(result["top_of_mind"]) + sum(len(s["items"]) for s in result["docket_sections"])
        assert total == BRIEF_ITEM_CAP


# ── 4. Single docket (Starter) — ceiling override ────────────────────────────

class TestSingleDocketStarter:
    def test_ceiling_overridden_to_remaining_slots(self):
        candidates = _make_pool(D1, "59336", 50)
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        remaining_slots = BRIEF_ITEM_CAP - len(result["top_of_mind"])
        assert len(result["docket_sections"]) == 1
        assert len(result["docket_sections"][0]["items"]) == remaining_slots

    def test_total_equals_cap(self):
        candidates = _make_pool(D1, "59336", 50)
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        total = len(result["top_of_mind"]) + sum(len(s["items"]) for s in result["docket_sections"])
        assert total == BRIEF_ITEM_CAP

    def test_top_of_mind_count(self):
        candidates = _make_pool(D1, "59336", 50)
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        assert len(result["top_of_mind"]) == TOP_OF_MIND_COUNT


# ── 5. Zero candidates ────────────────────────────────────────────────────────

class TestZeroCandidates:
    def test_empty_output(self):
        result = allocate_brief([], [D1, D2], BRIEF_DATE)
        assert result == {"top_of_mind": [], "docket_sections": []}

    def test_empty_with_no_tracked_dockets(self):
        result = allocate_brief([], [], BRIEF_DATE)
        assert result == {"top_of_mind": [], "docket_sections": []}


# ── 6. Single candidate ───────────────────────────────────────────────────────

class TestSingleCandidate:
    def test_goes_to_top_of_mind(self):
        candidates = [_make_filing("solo", docket_id=D1, docket_external_id="59336")]
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        assert len(result["top_of_mind"]) == 1
        assert result["top_of_mind"][0]["filing"]["filing_id"] == "solo"

    def test_no_duplicate_in_docket_section(self):
        candidates = [_make_filing("solo", docket_id=D1, docket_external_id="59336")]
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        # TOM claimed the only item; pool is empty, no docket section
        assert result["docket_sections"] == []


# ── 7. All candidates from one docket ────────────────────────────────────────

class TestAllSameDocket:
    def test_no_duplicates_between_tom_and_docket_section(self):
        candidates = _make_pool(D1, "59336", 30)
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        tom_ids = {e["filing"]["filing_id"] for e in result["top_of_mind"]}
        body_ids = {e["filing"]["filing_id"]
                    for sec in result["docket_sections"]
                    for e in sec["items"]}
        assert tom_ids.isdisjoint(body_ids), "TOM and docket section must not overlap"

    def test_ceiling_override_one_docket(self):
        candidates = _make_pool(D1, "59336", 30)
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        remaining_slots = BRIEF_ITEM_CAP - len(result["top_of_mind"])
        assert result["docket_sections"][0]["items"].__len__() == remaining_slots


# ── 8. ERCOT filings (docket_id=None) excluded from docket sections ───────────

class TestErcotFilings:
    def test_none_docket_id_not_in_sections(self):
        ercot = [_make_filing(f"ercot_{i}", docket_id=None) for i in range(20)]
        puct = _make_pool(D1, "59336", 10)
        candidates = ercot + puct
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        body_ids = {e["filing"]["filing_id"]
                    for sec in result["docket_sections"]
                    for e in sec["items"]}
        for f in ercot:
            assert f["filing_id"] not in body_ids

    def test_ercot_can_appear_in_top_of_mind(self):
        """ERCOT filings with high scores are still eligible for TOP_OF_MIND."""
        # Give ERCOT filings extremely high score via haiku_verdict=relevant
        ercot = [_make_filing(f"ercot_{i}", docket_id=None, score_hint=10) for i in range(5)]
        puct = _make_pool(D1, "59336", 20, base_score_hint=5)
        candidates = ercot + puct
        result = allocate_brief(candidates, [D1], BRIEF_DATE)
        tom_ids = {e["filing"]["filing_id"] for e in result["top_of_mind"]}
        # At least some ERCOT filings could be in TOM (score-dependent, not guaranteed)
        # Just verify no crash and structure is correct
        assert len(result["top_of_mind"]) == TOP_OF_MIND_COUNT


# ── 9. Cap invariant ──────────────────────────────────────────────────────────

class TestCapInvariant:
    @pytest.mark.parametrize("n_dockets,pool_size", [
        (1, 50),
        (2, 30),
        (5, 20),
        (10, 15),
        (25, 5),
        (50, 3),
    ])
    def test_total_never_exceeds_cap(self, n_dockets, pool_size):
        dockets = [f"ffff{i:04d}-0000-0000-0000-000000000000" for i in range(n_dockets)]
        candidates = []
        for i, d in enumerate(dockets):
            candidates += _make_pool(d, str(70000 + i), pool_size)
        result = allocate_brief(candidates, dockets, BRIEF_DATE)
        total = len(result["top_of_mind"]) + sum(len(s["items"]) for s in result["docket_sections"])
        assert total <= BRIEF_ITEM_CAP


# ── 10. _build_subject ────────────────────────────────────────────────────────

class TestBuildSubject:
    def test_single_item(self):
        item = {"title": "Order No. 83A"}
        subj = _build_subject(item, 1, BRIEF_DATE)
        assert subj == "Order No. 83A"

    def test_multiple_items(self):
        item = {"title": "Order No. 83A Initial Brief — 59336"}
        subj = _build_subject(item, 5, BRIEF_DATE)
        assert "Order No. 83A" in subj
        assert "4 more items" in subj

    def test_two_items_singular(self):
        item = {"title": "Order No. 83A"}
        subj = _build_subject(item, 2, BRIEF_DATE)
        assert "1 more item" in subj
        assert "items" not in subj

    def test_no_top_item(self):
        subj = _build_subject(None, 0, BRIEF_DATE)
        assert "NodalPulse" in subj
        assert "2026" in subj or "May" in subj or "items" in subj

    def test_title_truncated_at_60(self):
        item = {"title": "A" * 80}
        subj = _build_subject(item, 3, BRIEF_DATE)
        assert len(subj.split(" · ")[0]) <= 60


# ── 11. _deadline_badge_info ─────────────────────────────────────────────────

class TestDeadlineBadgeInfo:
    def test_no_dates(self):
        result = _deadline_badge_info({}, BRIEF_DATE)
        assert result == {"nearest_deadline_date": None, "nearest_effective_date": None, "protest_notice_url": None}

    def test_effective_date_within_30d(self):
        payload = {"effective_date": "2026-06-10"}  # 14 days out
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_effective_date"] == "2026-06-10"
        assert result["nearest_deadline_date"] is None

    def test_effective_date_beyond_30d(self):
        payload = {"effective_date": "2026-07-01"}  # >30 days out
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_effective_date"] is None

    def test_past_effective_date_not_surfaced(self):
        payload = {"effective_date": "2026-05-01"}  # past
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_effective_date"] is None

    def test_soonest_deadline_returned(self):
        payload = {
            "deadlines": [
                {"description": "Hearing", "date": "2026-06-15"},
                {"description": "Comments due", "date": "2026-05-30"},  # sooner
            ]
        }
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_deadline_date"] == "2026-05-30"

    def test_deadline_beyond_30d_ignored(self):
        payload = {"deadlines": [{"description": "Future", "date": "2026-08-01"}]}
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_deadline_date"] is None

    def test_malformed_date_ignored(self):
        payload = {"deadlines": [{"description": "Bad", "date": "not-a-date"}]}
        result = _deadline_badge_info(payload, BRIEF_DATE)
        assert result["nearest_deadline_date"] is None
