"""Tests for PUCT crawler HTML parser (no network required)."""

from nodalpulse.crawlers.puct import _parse_date, _parse_results

SAMPLE_HTML = """
<html><body>
<table id="ctl00_ContentPlaceHolder1_grdFilings">
  <tr>
    <th>Project</th><th>Filed</th><th>Type</th><th>Filer</th><th>Document</th>
  </tr>
  <tr>
    <td>55555</td>
    <td>05/06/2026</td>
    <td>Application</td>
    <td>Acme Energy LLC</td>
    <td><a href="/Apps/Filings/GetDocument.aspx?document_id=123456">PDF</a></td>
  </tr>
  <tr>
    <td>55556</td>
    <td>05/06/2026</td>
    <td>Order</td>
    <td>PUCT Staff</td>
    <td><a href="/Apps/Filings/GetDocument.aspx?document_id=123457">PDF</a></td>
  </tr>
</table>
</body></html>
"""


def test_parse_results_returns_rows():
    rows = _parse_results(SAMPLE_HTML)
    assert len(rows) == 2


def test_parse_results_fields():
    rows = _parse_results(SAMPLE_HTML)
    r = rows[0]
    assert r["external_id"] == "123456"
    assert r["docket"] == "55555"
    assert r["doc_type"] == "puct-filing"
    assert r["filer"] == "Acme Energy LLC"
    assert "interchange.puc.texas.gov" in r["doc_url"]


def test_parse_results_order_type():
    rows = _parse_results(SAMPLE_HTML)
    assert rows[1]["doc_type"] == "puct-order"


def test_parse_results_empty_table():
    assert _parse_results("<html><body>No results</body></html>") == []


def test_parse_date_formats():
    # PUCT dates are midnight Central time; May is CDT (UTC-5)
    assert _parse_date("05/06/2026") == "2026-05-06T05:00:00+00:00"
    assert _parse_date("2026-05-06") == "2026-05-06T05:00:00+00:00"
    assert _parse_date("garbage") is None
