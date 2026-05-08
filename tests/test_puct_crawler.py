"""Tests for PUCT crawler HTML parsers (no network required).

Fixtures are synthetic HTML modelled on the exact structure returned by
httpx from interchange.puc.texas.gov (server-rendered ASP.NET MVC,
no JavaScript execution needed).
"""

from nodalpulse.crawlers.puct import (
    _doc_id_from_url,
    _parse_date,
    _parse_docket_results,
    _parse_document_results,
    _parse_filing_results,
)

# ── fixtures ──────────────────────────────────────────────────────────────────

DOCKETS_HTML = """
<html><body>
<table class="table table-responsive table-hover table-striped table-bordered table-condensed">
  <tr><th>Control Number</th><th>Items</th><th>Party</th><th>Description</th></tr>
  <tr>
    <td><strong><a href="/search/filings/?ControlNumber=56896">56896</a></strong></td>
    <td>3</td><td>PUC EXECUTIVE DIRECTOR</td><td>Some Project</td>
  </tr>
  <tr>
    <td><strong><a href="/search/filings/?ControlNumber=12345">12345</a></strong></td>
    <td>1</td><td>Acme Energy LLC</td><td>Another Docket</td>
  </tr>
</table>
</body></html>
"""

FILINGS_HTML = """
<html><body>
<table class="table table-responsive table-hover table-striped table-bordered table-condensed">
  <tr><th>Item</th><th>File Stamp</th><th>Party</th><th>Item Type</th><th>Filing Description</th></tr>
  <tr>
    <td><strong><a href="/search/documents/?controlNumber=56896&itemNumber=1">1</a></strong></td>
    <td>5/6/2026</td><td>PUC EXECUTIVE DIRECTOR</td><td>ORD</td><td>ORDER GRANTING MOTION</td>
  </tr>
  <tr>
    <td><strong><a href="/search/documents/?controlNumber=56896&itemNumber=2">2</a></strong></td>
    <td>5/7/2026</td><td>Acme Energy LLC</td><td>APP</td><td>APPLICATION FOR CERTIFICATE</td>
  </tr>
</table>
</body></html>
"""

DOCUMENTS_HTML = """
<html><body>
<table>
  <tr><th>Document</th><th>Pages</th><th>Type</th></tr>
  <tr>
    <td><strong><a href="https://interchange.puc.texas.gov/Documents/56896_1_1415464.PDF">56896_1_1415464</a></strong></td>
    <td>Pages 1 to 2</td><td>PDF</td>
  </tr>
  <tr>
    <td><strong><a href="https://interchange.puc.texas.gov/Documents/56896_1_1415465.PDF">56896_1_1415465</a></strong></td>
    <td>Pages 3 to 10</td><td>PDF</td>
  </tr>
</table>
</body></html>
"""

NBSP_FILINGS_HTML = """
<html><body>
<table class="table table-responsive">
  <tr><th>Item</th><th>File Stamp</th><th>Party</th><th>Item Type</th><th>Filing Description</th></tr>
  <tr>
    <td><strong><a href="/search/documents/?controlNumber=99&itemNumber=1">1</a></strong></td>
    <td>5/6/2026</td><td>Some\xa0Company\xa0LLC</td><td>MOT</td><td>MOTION TO DISMISS</td>
  </tr>
</table>
</body></html>
"""


# ── docket results ────────────────────────────────────────────────────────────

def test_parse_docket_results_count():
    assert len(_parse_docket_results(DOCKETS_HTML)) == 2


def test_parse_docket_results_fields():
    rows = _parse_docket_results(DOCKETS_HTML)
    assert rows[0]["control_number"] == "56896"
    assert rows[0]["party"] == "PUC EXECUTIVE DIRECTOR"
    assert rows[1]["control_number"] == "12345"


def test_parse_docket_results_empty():
    assert _parse_docket_results("<html><body>No results</body></html>") == []


# ── filing results ────────────────────────────────────────────────────────────

def test_parse_filing_results_count():
    assert len(_parse_filing_results(FILINGS_HTML, "56896")) == 2


def test_parse_filing_results_fields():
    rows = _parse_filing_results(FILINGS_HTML, "56896")
    r = rows[0]
    assert r["control_number"] == "56896"
    assert r["item_number"] == "1"
    assert r["item_key"] == "56896_1"
    assert r["party"] == "PUC EXECUTIVE DIRECTOR"
    assert r["item_type"] == "ORD"
    assert r["description_raw"] == "ORDER GRANTING MOTION"
    assert "ORDER GRANTING MOTION" in r["title"]
    assert "56896" in r["title"]


def test_parse_filing_results_doc_type_order():
    rows = _parse_filing_results(FILINGS_HTML, "56896")
    assert rows[0]["doc_type"] == "puct-order"


def test_parse_filing_results_doc_type_application():
    rows = _parse_filing_results(FILINGS_HTML, "56896")
    assert rows[1]["doc_type"] == "puct-filing"


def test_parse_filing_results_nbsp_normalization():
    rows = _parse_filing_results(NBSP_FILINGS_HTML, "99")
    assert rows[0]["party"] == "Some Company LLC"


def test_parse_filing_results_item_key_format():
    rows = _parse_filing_results(FILINGS_HTML, "56896")
    assert rows[1]["item_key"] == "56896_2"


# ── document results ──────────────────────────────────────────────────────────

def test_parse_document_results_count():
    assert len(_parse_document_results(DOCUMENTS_HTML)) == 2


def test_parse_document_results_urls():
    urls = _parse_document_results(DOCUMENTS_HTML)
    assert "56896_1_1415464.PDF" in urls[0]
    assert "56896_1_1415465.PDF" in urls[1]
    assert all(u.startswith("https://") for u in urls)


def test_parse_document_results_empty():
    assert _parse_document_results("<html><body></body></html>") == []


# ── helpers ───────────────────────────────────────────────────────────────────

def test_doc_id_from_url_uppercase_ext():
    assert _doc_id_from_url("https://interchange.puc.texas.gov/Documents/56896_1_1415464.PDF") == "56896_1_1415464"


def test_doc_id_from_url_lowercase_ext():
    assert _doc_id_from_url("https://interchange.puc.texas.gov/Documents/56896_2_9999.pdf") == "56896_2_9999"


def test_parse_date_slash_no_padding():
    # Live site uses M/D/YYYY without zero-padding; May is CDT (UTC-5)
    assert _parse_date("5/6/2026") == "2026-05-06T05:00:00+00:00"


def test_parse_date_slash_padded():
    assert _parse_date("05/06/2026") == "2026-05-06T05:00:00+00:00"


def test_parse_date_iso():
    assert _parse_date("2026-05-06") == "2026-05-06T05:00:00+00:00"


def test_parse_date_invalid():
    assert _parse_date("garbage") is None
