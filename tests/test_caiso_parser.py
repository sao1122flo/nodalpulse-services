"""Tests for CAISO crawler docket parsing (no network required).

Covers the _parse_dockets regex specifically for multi-docket co-captioned
titles — the docket after "and" is the one a mis-anchored regex silently drops.
"""

import pytest

from nodalpulse.crawlers.caiso import _parse_dockets


# ── _parse_dockets ────────────────────────────────────────────────────────────

def test_single_docket_parenthesized():
    assert _parse_dockets("SPTO Tariff Amendment (ER25-2442)") == ["ER25-2442"]


def test_two_dockets_comma():
    result = _parse_dockets("Joint Motion (ER23-2309, ER24-1394)")
    assert result == ["ER23-2309", "ER24-1394"]


def test_three_dockets_with_and():
    # Exact title structure of the DCR Transmission co-captioned filing.
    # EL26-34 appears after "and" — this is the docket a mis-anchored regex drops.
    title = (
        "Joint Motion for Extension of Procedural Schedule — "
        "DCR Transmission (ER23-2309, ER24-1394, and EL26-34)"
    )
    result = _parse_dockets(title)
    assert result == ["ER23-2309", "ER24-1394", "EL26-34"], (
        f"Expected 3 dockets; got {result}. "
        "EL26-34 (post-'and') is likely being dropped by the regex."
    )


def test_three_dockets_preserves_order():
    result = _parse_dockets("ER23-2309, ER24-1394, and EL26-34")
    assert result[0] == "ER23-2309"
    assert result[1] == "ER24-1394"
    assert result[2] == "EL26-34"


def test_sub_docket_normalized():
    # Dockets like ER23-2309-001 should normalize to ER23-2309.
    assert _parse_dockets("Filing under ER23-2309-001") == ["ER23-2309"]


def test_dedup_same_docket_twice():
    assert _parse_dockets("ER25-2442 and ER25-2442") == ["ER25-2442"]


def test_no_dockets_returns_empty():
    assert _parse_dockets("CPUC Application for Energy Storage Permit") == []


def test_no_dockets_court_string():
    assert _parse_dockets("Ninth Circuit Court of Appeals No. 24-1234") == []


def test_mixed_case_normalized():
    # Input is uppercased internally; result is always uppercase.
    result = _parse_dockets("er25-2442")
    assert result == ["ER25-2442"]
