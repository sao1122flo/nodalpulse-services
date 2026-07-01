"""VA SCC adapter parsing + electric case-scoping — pure, hermetic (no DB/network).

Guards the Breeze GetAllDailyFilings JSON contract (CaseNumber/CaseName/DocName/
Year-Month-Day/DocID/FileName), the doc-URL encoding, and the case-scoping rule that
makes coverage complete: an intervenor filing in a Dominion case is kept (CaseName is
the case utility, not the filer) while gas / securities rows are dropped.
"""

from datetime import date

from nodalpulse.crawlers.vascc import (
    VaSccAdapter,
    _doc_type,
    _encode_filename,
    _filed_at,
    _month_windows,
    is_electric_case,
    parse_rows,
)

# Trimmed verbatim from a live GetAllDailyFilings response: a Dominion filing, an
# intervenor (Appalachian Voices) filing *in a Dominion case*, an Appalachian Power
# filing, an electric-coop filing, a gas row, and a securities/underground row.
_JSON = """[
 {"$id":"1","CaseNumber":"PUR-2025-00210","DocName":"Appalachian Voices - Post-Hearing Brief.","DocID":390307,"Month":6,"Day":30,"Year":2026,"FileName":"8d5v01!.PDF","DateFiled":"2026-06-30T00:00:00.000","CaseName":"Virginia Electric and Power Co"},
 {"$id":"2","CaseNumber":"PUR-2026-00011","DocName":"Google, LLC - Brief Regarding Fast-Track Interconnection.","DocID":390331,"Month":6,"Day":30,"Year":2026,"FileName":"8d6j01!.PDF","DateFiled":"2026-06-30T00:00:00.000","CaseName":"Virginia Electric and Power Co"},
 {"$id":"3","CaseNumber":"PUR-2026-00065","DocName":"Appalachian Power Company - Order for Notice and Comment - 06/26/2026.","DocID":390272,"Month":6,"Day":26,"Year":2026,"FileName":"8d4w01!.PDF","DateFiled":"2026-06-26T00:00:00.000","CaseName":"Appalachian Power Company"},
 {"$id":"4","CaseNumber":"PST-2026-00015","DocName":"Central Virginia Electric Cooperative - Supplemental Assessment Order.","DocID":390270,"Month":6,"Day":26,"Year":2026,"FileName":"8d4@01!.PDF","DateFiled":"2026-06-26T00:00:00.000","CaseName":"Central Virginia Electric Coop"},
 {"$id":"5","CaseNumber":"PUR-2026-00093","DocName":"Washington Gas Light Company - Cover Letter enclosing filing fee.","DocID":390249,"Month":6,"Day":26,"Year":2026,"FileName":"8d4901!.PDF","DateFiled":"2026-06-26T00:00:00.000","CaseName":"Washington Gas Light Company"},
 {"$id":"6","CaseNumber":"SEC-2026-00044","DocName":"Acme Capital LLC - Application for registration.","DocID":390100,"Month":6,"Day":20,"Year":2026,"FileName":"8d1001!.PDF","DateFiled":"2026-06-20T00:00:00.000","CaseName":"Acme Capital LLC"}
]"""


def test_parse_rows():
    rows = parse_rows(_JSON)
    assert len(rows) == 6
    r = rows[0]
    assert r["doc_id"] == "390307"
    assert r["case_number"] == "PUR-2025-00210"
    assert r["case_name"] == "Virginia Electric and Power Co"
    assert r["file_name"] == "8d5v01!.PDF"
    assert r["filed_date"] == date(2026, 6, 30)


def test_parse_rows_handles_inlinecount_wrapper():
    wrapped = (
        '{"$id":"1","$type":"Breeze.WebApi2.QueryResult","Results":' + _JSON + ',"InlineCount":6}'
    )
    assert len(parse_rows(wrapped)) == 6


def test_parse_rows_tolerates_bom_and_empty():
    assert parse_rows("﻿" + _JSON)  # leading BOM stripped
    assert parse_rows("") == []
    assert parse_rows("[]") == []


def test_is_electric_case():
    assert is_electric_case("Virginia Electric and Power Co")  # Dominion
    assert is_electric_case("Virginia Electric & Power Co.")
    assert is_electric_case("Appalachian Power Company")  # no "electric" — explicit
    assert is_electric_case("Central Virginia Electric Coop")
    assert is_electric_case("Northern Virginia Electric Cooperative")  # NOVEC
    assert is_electric_case("Kentucky Utilities Company")  # Old Dominion Power
    assert not is_electric_case("Washington Gas Light Company")
    assert not is_electric_case("Roanoke Gas Company")
    assert not is_electric_case("Acme Capital LLC")
    assert not is_electric_case("")


def test_doc_type():
    assert _doc_type("Appalachian Power Company - Final Order - 06/26/2026.") == "vascc-order"
    assert _doc_type("Appalachian Voices - Post-Hearing Brief.") == "vascc-brief"
    assert _doc_type("Direct Testimony of John Doe") == "vascc-testimony"
    assert _doc_type("Application for a Certificate") == "vascc-application"
    assert _doc_type("Transcript and Word Index of Hearing") == "vascc-hearing"
    assert _doc_type("Something unmapped entirely") == "vascc-filing"  # default


def test_encode_filename():
    # encodeURIComponent leaves "!" unescaped, escapes "@" -> %40 (both verified 200 PDF)
    assert _encode_filename("8d5v01!.PDF") == "8d5v01!.PDF"
    assert _encode_filename("8d4@01!.PDF") == "8d4%4001!.PDF"
    assert _encode_filename("8d4#01!.PDF") == "8d4%2301!.PDF"


def test_filed_at_is_et_midnight_utc():
    assert _filed_at(2026, 6, 30).startswith("2026-06-30T04:00:00+00:00")  # EDT
    assert _filed_at(2026, 1, 5).startswith("2026-01-05T05:00:00+00:00")  # EST


def test_month_windows():
    w = _month_windows(date(2025, 11, 15), date(2026, 2, 3))
    assert w == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    assert _month_windows(date(2026, 6, 1), date(2026, 6, 30)) == [(2026, 6)]


def test_case_scoping_keeps_intervenor_filings_drops_gas_and_securities():
    """The completeness guarantee: an intervenor (Google/Appalachian Voices) filing in
    a Dominion case is kept because CaseName is the case utility — while gas and
    securities rows are dropped."""
    adapter = VaSccAdapter(watch_cases=set())
    filings = adapter._finalize(parse_rows(_JSON), date(2026, 6, 1), date(2026, 6, 30))
    ids = {f.external_id for f in filings}
    assert "390307" in ids  # Appalachian Voices brief in a Dominion case — kept
    assert "390331" in ids  # Google interconnection brief in a Dominion case — kept
    assert "390272" in ids  # Appalachian Power order
    assert "390270" in ids  # Central Virginia Electric Coop
    assert "390249" not in ids  # Washington Gas — dropped
    assert "390100" not in ids  # securities — dropped
    # the Google brief's filer is not a utility, but the case is electric
    google = next(f for f in filings if f.external_id == "390331")
    assert google.metadata["case_name"] == "Virginia Electric and Power Co"
    assert google.metadata["docket_numbers"] == ["PUR-2026-00011"]
    assert google.source_url == ("https://www.scc.virginia.gov/docketsearch/DOCS/8d6j01!.PDF")


def test_watch_set_keeps_non_utility_casename():
    """A case whose CaseName is not an electric utility is still captured when its
    CaseNumber is in the persistent watch set (e.g. a hand-added developer/transmission
    docket)."""
    adapter = VaSccAdapter(watch_cases={"SEC-2026-00044"})  # not really electric, but watched
    filings = adapter._finalize(parse_rows(_JSON), date(2026, 6, 1), date(2026, 6, 30))
    assert "390100" in {f.external_id for f in filings}


def test_finalize_dedupes_by_doc_id():
    rows = parse_rows(_JSON) + parse_rows(_JSON)  # same docs twice
    adapter = VaSccAdapter(watch_cases=set())
    filings = adapter._finalize(rows, date(2026, 6, 1), date(2026, 6, 30))
    assert len(filings) == len({f.external_id for f in filings})


def test_finalize_orders_newest_first():
    """run_adapter's max_filings cap keeps filings[:N], so newest must come first."""
    adapter = VaSccAdapter(watch_cases=set())
    filings = adapter._finalize(parse_rows(_JSON), date(2026, 6, 1), date(2026, 6, 30))
    filed = [f.filed_at for f in filings]
    assert filed == sorted(filed, reverse=True)
    assert filings[0].external_id == "390331"  # 2026-06-30, highest DocID
