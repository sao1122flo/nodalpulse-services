"""MD PSC adapter parsing + electric case-scoping — pure, hermetic (no DB/network).

Guards the maillog contract (ML# button + 'filed, on <date>' + Case No.) and the
case-scoping rule that makes coverage complete: a Commission ORDER in an electric
case is kept even though its filer isn't a utility.
"""

from datetime import date

from nodalpulse.crawlers.mdpsc import (
    MdPscAdapter,
    _date_windows,
    _doc_type,
    _parse_md_date,
    extract_viewstate,
    is_electric_filer,
    parse_rows,
)

# Trimmed verbatim from a live SearchDocResults maillog: a utility filing (Delmarva,
# Case 9681), a Commission ORDER in that same case, and an off-topic telecom row.
_HTML = """
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS-MD-123" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="FB63F147" />
<table id="maillogdata"><tbody>
<tr><td><button type="button" class='btnOpenPdf' data-pdf='/DMS/maillogpdfview/MailLog/0/0/331616/0'> ML# 331616 </button></td>
<td>Delmarva Power &amp; Light Company filed, on June 30, 2026, its Rate of Return Report Case No. 9681</td></tr>
<tr><td><button type="button" class='btnOpenPdf' data-pdf='/DMS/maillogpdfview/MailLog/0/0/331599/0'> ML# 331599 </button></td>
<td>Commission filed, on June 29, 2026, Order No. 92489 on Appeal. Case No. 9681</td></tr>
<tr><td><button type="button" class='btnOpenPdf' data-pdf='/DMS/maillogpdfview/MailLog/0/0/331500/0'> ML# 331500 </button></td>
<td>American Broadband and Telecommunications Company filed, on June 30, 2026, Compliance filing FCC Form 481 Case No. 9999</td></tr>
</tbody></table>
"""


def test_extract_viewstate():
    vs = extract_viewstate(_HTML)
    assert vs["__VIEWSTATE"] == "VS-MD-123"
    assert vs["__VIEWSTATEGENERATOR"] == "FB63F147"


def test_parse_rows():
    rows = parse_rows(_HTML)
    assert len(rows) == 3
    r = rows[0]
    assert r["maillog"] == "331616"
    assert r["pdf_path"] == "/DMS/maillogpdfview/MailLog/0/0/331616/0"
    assert r["filer"] == "Delmarva Power & Light Company"
    assert r["case"] == "9681"
    assert r["filed_at"].startswith("2026-06-30T04:00:00+00:00")  # EDT midnight → UTC


def test_parse_rows_missing_table_is_empty():
    assert parse_rows("<html>no maillog here</html>") == []


def test_is_electric_filer():
    assert is_electric_filer("Delmarva Power & Light Company")
    assert is_electric_filer("Potomac Electric Power Company")
    assert is_electric_filer("Baltimore Gas and Electric Company")
    assert is_electric_filer("Southern Maryland Electric Coop., Inc.")
    assert not is_electric_filer("Commission")
    assert not is_electric_filer("American Broadband and Telecommunications Company")
    assert not is_electric_filer("Washington Gas Light Company")


def test_doc_type():
    assert _doc_type("Commission filed Order No. 92489") == "mdpsc-order"
    assert _doc_type("its Rate of Return Report") == "mdpsc-report"
    assert _doc_type("Revised Tariff Pages") == "mdpsc-tariff"
    assert _doc_type("Application for authority") == "mdpsc-application"
    assert _doc_type("Compliance filing FCC Form 481") == "mdpsc-filing"  # default


def test_parse_md_date():
    assert _parse_md_date("June 30, 2026").startswith("2026-06-30T04:00:00+00:00")  # EDT
    assert _parse_md_date("January 5, 2026").startswith("2026-01-05T05:00:00+00:00")  # EST
    assert _parse_md_date("not a date") is None


def test_date_windows():
    w = _date_windows(date(2026, 1, 1), date(2026, 3, 15), days=31)
    assert w[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert w[-1][1] == date(2026, 3, 15)
    # windows are contiguous and non-overlapping
    for (_, a_to), (b_from, _) in zip(w, w[1:]):
        assert (b_from - a_to).days == 1


def test_case_scoping_keeps_orders_in_electric_cases():
    """The completeness guarantee: a Commission order in an electric case survives,
    while an off-topic telecom row is dropped — even though neither is a utility filing."""
    adapter = MdPscAdapter(watch_cases=set())  # empty watch set; discover from window
    rows = parse_rows(_HTML)
    filings = adapter._finalize(rows, date(2026, 6, 1), date(2026, 6, 30))
    mls = {f.external_id for f in filings}
    assert "331616" in mls  # Delmarva utility filing (seeds electric case 9681)
    assert "331599" in mls  # Commission ORDER in case 9681 — kept by case-scoping
    assert "331500" not in mls  # telecom case 9999 — not electric, dropped
    # the order is a non-utility filer that a company-name filter would have missed
    order = next(f for f in filings if f.external_id == "331599")
    assert order.doc_type == "mdpsc-order"
    assert not is_electric_filer(order.metadata["filer"])


def test_case_scoping_keeps_utility_filing_without_case():
    """A utility filing with no Case No. is still kept (electric by filer)."""
    html = _HTML.replace(" Case No. 9681", "")  # strip the case from the Delmarva row
    adapter = MdPscAdapter(watch_cases=set())
    filings = adapter._finalize(parse_rows(html), date(2026, 6, 1), date(2026, 6, 30))
    assert "331616" in {f.external_id for f in filings}
