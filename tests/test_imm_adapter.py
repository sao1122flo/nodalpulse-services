"""IMM adapter filename parsing — pure, hermetic (no DB/network).

Guards the 2026-06-29 re-point: filings index moved to /filings/{year}.shtml and
State-of-the-Market reports are ingested from /reports/PJM_State_of_the_Market/.
Sample filenames are verbatim from the live site.
"""

from datetime import date

from nodalpulse.crawlers.imm import _parse_filename, _parse_som_filename


def test_filing_comment_with_docket_and_date():
    doc_type, dockets, filed = _parse_filename(
        "IMM_Comments_Docket_No_ER26-2738_et_al_20260626.pdf"
    )
    assert doc_type == "imm-comment"
    assert dockets == ["ER26-2738"]
    assert filed == date(2026, 6, 26)


def test_filing_complaint_real_docket():
    doc_type, dockets, filed = _parse_filename(
        "IMM_Complaint_re_Data_Center_Loads_Docket_No_EL26-119_20251125.pdf"
    )
    assert doc_type == "imm-complaint"
    assert dockets == ["EL26-119"]
    assert filed == date(2025, 11, 25)


def test_som_quarterly_full_report():
    period_end, label = _parse_som_filename("2026q1-som-pjm.pdf")
    assert period_end == date(2026, 3, 31)
    assert "Q1 State of the Market" in label


def test_som_annual_volume():
    period_end, label = _parse_som_filename("2025-som-pjm-vol1.pdf")
    assert period_end == date(2025, 12, 31)
    assert "Annual State of the Market" in label
    assert "Vol 1" in label


def test_som_sections_and_aux_are_skipped():
    for fname in (
        "2026q1-som-pjm-sec5.pdf",
        "2026q1-som-pjm-toc.pdf",
        "2026q1-som-pjm-preface.pdf",
        "2026q1-som-pjm-appendix.pdf",
    ):
        period_end, _ = _parse_som_filename(fname)
        assert period_end is None, f"{fname} should be skipped"
