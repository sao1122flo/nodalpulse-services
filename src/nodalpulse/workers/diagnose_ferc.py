"""Temporary diagnostic: confirm date-filtered AdvancedSearch returns results."""
import json
import logging
import httpx

logger = logging.getLogger(__name__)

_BASE = "https://elibrary.ferc.gov/eLibrarywebapi/api"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 NodalPulse/1.0 regulatory-monitor",
    "Origin": "https://elibrary.ferc.gov",
    "Referer": "https://elibrary.ferc.gov/",
}

# Known accession: 20260331-5252, filedDate: 03/31/2026, in ER25-1357 docket
_DOCKET = "ER25-1357"
_DATE_RANGE_WIDE = ("02-01-2026", "06-04-2026")  # should catch Mar 31 filing
_DATE_RANGE_NARROW = ("03-01-2026", "04-01-2026")  # March only — must catch Mar 31
_DATE_RANGE_MISS = ("05-01-2026", "06-03-2026")    # should return 0 (proven correct)


def _make_body(docket, start, end, all_dates=False, search_text="*"):
    return {
        "searchText": search_text,
        "searchFullText": True,
        "searchDescription": True,
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
        "dateSearches": [] if all_dates else [{"startDate": start, "endDate": end, "dateType": "Filed Date"}],
        "affiliations": [], "categories": [], "libraries": [], "classTypes": [],
        "accessionNumber": None, "eFiling": False,
        "resultsPerPage": 10, "curPage": 1, "groupBy": "NONE", "sortBy": "",
        "allDates": all_dates,
    }


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Targeted date-filter debug: ER25-1357 with multiple date ranges."""
    out = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:

        # Control: allDates=True, should return ~116 items
        body = _make_body(_DOCKET, None, None, all_dates=True)
        r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
        data = r.json()
        hits = data.get("searchHits", [])
        out["control_all_dates"] = {
            "status": r.status_code,
            "totalHits": data.get("totalHits"),
            "numHits": data.get("numHits"),
            "items_in_batch": len(hits),
            "first_acc": hits[0].get("acesssionNumber") if hits else None,
            "first_filed": hits[0].get("filedDate") if hits else None,
        }
        logger.info("control_all_dates: totalHits=%s items=%d", data.get("totalHits"), len(hits))

        # Test A: Feb-Jun 2026, should return Mar 31 filing
        body = _make_body(_DOCKET, *_DATE_RANGE_WIDE, all_dates=False)
        r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
        data = r.json()
        hits = data.get("searchHits", [])
        out["test_feb_jun_2026"] = {
            "status": r.status_code,
            "totalHits": data.get("totalHits"),
            "items_in_batch": len(hits),
            "first_acc": hits[0].get("acesssionNumber") if hits else None,
            "first_filed": hits[0].get("filedDate") if hits else None,
        }
        logger.info("test_feb_jun_2026: totalHits=%s items=%d", data.get("totalHits"), len(hits))

        # Test B: March 2026 only, should return Mar 31 filing
        body = _make_body(_DOCKET, *_DATE_RANGE_NARROW, all_dates=False)
        r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
        data = r.json()
        hits = data.get("searchHits", [])
        out["test_march_2026"] = {
            "status": r.status_code,
            "totalHits": data.get("totalHits"),
            "items_in_batch": len(hits),
            "first_acc": hits[0].get("acesssionNumber") if hits else None,
            "first_filed": hits[0].get("filedDate") if hits else None,
        }
        logger.info("test_march_2026: totalHits=%s items=%d", data.get("totalHits"), len(hits))

        # Test C: May-Jun 2026 (known-zero), sanity check
        body = _make_body(_DOCKET, *_DATE_RANGE_MISS, all_dates=False)
        r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
        data = r.json()
        out["test_may_jun_2026_expect0"] = {
            "status": r.status_code,
            "totalHits": data.get("totalHits"),
            "items_in_batch": len(data.get("searchHits", [])),
        }
        logger.info("test_may_jun_expect0: totalHits=%s", data.get("totalHits"))

        # Test D: searchText="" instead of "*"
        body = _make_body(_DOCKET, *_DATE_RANGE_WIDE, all_dates=False, search_text="")
        r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
        data = r.json()
        hits = data.get("searchHits", [])
        out["test_empty_text_feb_jun"] = {
            "status": r.status_code,
            "totalHits": data.get("totalHits"),
            "items_in_batch": len(hits),
            "first_acc": hits[0].get("acesssionNumber") if hits else None,
        }
        logger.info("test_empty_text_feb_jun: totalHits=%s items=%d", data.get("totalHits"), len(hits))

    return out
