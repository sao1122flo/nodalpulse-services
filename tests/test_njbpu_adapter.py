"""NJ BPU adapter parsing — pure, hermetic (no DB / no network).

Guards the WebForms result-grid contract: the gvSearchRs table, the
DocumentHandler / CaseSummary link shapes, the Posted Date column, the lCount
total, and the pager 'Next' target. Fixtures are trimmed verbatim from the live
SearchDocResults.aspx response so a silent site redesign trips a test.
"""

from nodalpulse.crawlers.njbpu import (
    _doc_type,
    _parse_date,
    extract_viewstate,
    is_electric,
    next_page_target,
    parse_results,
    parse_total,
)

# Two rows trimmed from a real SearchDocResults.aspx page: one electric rate case
# (ER…), one gas (GR…) so the electric scoping can be exercised end-to-end.
_RESULTS_HTML = """
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS-ABC123" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="AB827D4F" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="EV-XYZ789" />
<span id="ContentPlaceHolder1_lCount">1 - 30 of 114</span>
<table id="ContentPlaceHolder1_gvSearchRs">
  <tr>
    <th>Docket #</th><th>Document Title</th><th>Folder</th><th>Uploaded By</th>
    <th>Description</th><th>Posted Date</th><th>Fragment</th>
  </tr>
  <tr>
    <td><a href='CaseSummary.aspx?case_id=2112583'>ER23120924-</a></td>
    <td><a href="DocumentHandler.ashx?document_id=1324620">2023-12-29 - PSEG - 2023 Rate Case Filing</a></td>
    <td>PETITIONS</td>
    <td>BPU Staff</td>
    <td>In the Matter of the Petition of Public Service Electric and Gas Company</td>
    <td>12/29/2023</td>
    <td><span>Electric and Gas Company for Approval of an Increase</span></td>
  </tr>
  <tr>
    <td><a href='CaseSummary.aspx?case_id=2200001'>GR24050123-</a></td>
    <td><a href="DocumentHandler.ashx?document_id=1400001">2024-05-10 - NJNG - Gas Rate Filing</a></td>
    <td>ORDERS</td>
    <td>BPU Staff</td>
    <td>Gas base rate proceeding</td>
    <td>05/10/2024</td>
    <td></td>
  </tr>
</table>
<a href="javascript:__doPostBack(&#39;ctl00$ContentPlaceHolder1$gvSearchRs$ctl33$lbtnNext&#39;,&#39;&#39;)">Next</a>
"""

_LAST_PAGE_HTML = """
<span id="ContentPlaceHolder1_lCount">91 - 114 of 114</span>
<table id="ContentPlaceHolder1_gvSearchRs"><tr><th>Docket #</th></tr></table>
"""


def test_extract_viewstate():
    vs = extract_viewstate(_RESULTS_HTML)
    assert vs["__VIEWSTATE"] == "VS-ABC123"
    assert vs["__VIEWSTATEGENERATOR"] == "AB827D4F"
    assert vs["__EVENTVALIDATION"] == "EV-XYZ789"


def test_parse_total():
    assert parse_total(_RESULTS_HTML) == 114
    assert parse_total("<span>no count here</span>") is None


def test_next_page_target_present_and_absent():
    assert next_page_target(_RESULTS_HTML) == "ctl00$ContentPlaceHolder1$gvSearchRs$ctl33$lbtnNext"
    assert next_page_target(_LAST_PAGE_HTML) is None


def test_parse_results_extracts_all_columns():
    rows = parse_results(_RESULTS_HTML)
    assert len(rows) == 2
    r = rows[0]
    assert r["document_id"] == "1324620"
    assert r["docket"] == "ER23120924"  # trailing dash stripped
    assert r["case_id"] == "2112583"
    assert r["folder"] == "PETITIONS"
    assert r["uploaded_by"] == "BPU Staff"
    assert r["title"] == "2023-12-29 - PSEG - 2023 Rate Case Filing"
    assert r["filed_at"].startswith("2023-12-29T05:00:00+00:00")  # EST midnight → UTC


def test_parse_results_missing_table_is_empty():
    assert parse_results("<html><body>no grid</body></html>") == []


def test_is_electric_by_docket_prefix():
    assert is_electric("ER23120924")  # electric rate
    assert is_electric("EO26050220")  # electric other
    assert is_electric("EE26050202L")  # clean energy / energy efficiency
    assert is_electric("QO26060340")  # clean energy (solar/storage)
    assert not is_electric("GR24050123")  # gas
    assert not is_electric("WR24010001")  # water
    assert not is_electric("TO24010001")  # telecom


def test_doc_type_mapping():
    assert _doc_type("ORDERS") == "njbpu-order"
    assert _doc_type("PETITIONS") == "njbpu-petition"
    assert _doc_type("APPLICATIONS") == "njbpu-application"
    assert _doc_type("Tariff Filing") == "njbpu-tariff"
    assert _doc_type("SOMETHING ELSE") == "njbpu-filing"  # default


def test_parse_date_eastern_to_utc():
    # EDT (summer, UTC-4): midnight ET → 04:00 UTC
    assert _parse_date("06/15/2026").startswith("2026-06-15T04:00:00+00:00")
    # EST (winter, UTC-5): midnight ET → 05:00 UTC
    assert _parse_date("12/29/2023").startswith("2023-12-29T05:00:00+00:00")
    assert _parse_date("not a date") is None
