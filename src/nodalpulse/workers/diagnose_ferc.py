"""Diagnostic: sort order probe + transmittals for two colliding March-9 filings."""
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

_COLLISION_ACCS = ["20260309-5165", "20260309-5267"]


def _search_body(docket=None, accession=None, page=1, results=5, sort_by=""):
    return {
        "searchText": "*",
        "searchFullText": True,
        "searchDescription": True,
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}] if docket else [],
        "dateSearches": [],
        "affiliations": [],
        "categories": [],
        "libraries": [],
        "classTypes": [],
        "accessionNumber": accession,
        "eFiling": False,
        "resultsPerPage": results,
        "curPage": page,
        "groupBy": "NONE",
        "sortBy": sort_by,
        "allDates": True,
    }


async def handle_diagnose_ferc(payload: dict) -> dict:
    out = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:

        # === Sort order probe ===
        # Fetch page 1 and page 2 of ER25-1357, check if page 1 has more recent dates
        body_p1 = _search_body(docket="ER25-1357", page=1, results=3)
        body_p2 = _search_body(docket="ER25-1357", page=2, results=3)

        r1 = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body_p1))
        d1 = r1.json()
        h1 = d1.get("searchHits", [])

        r2 = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body_p2))
        d2 = r2.json()
        h2 = d2.get("searchHits", [])

        out["sort_order"] = {
            "totalHits": d1.get("totalHits"),
            "page1_dates": [h.get("filedDate") for h in h1],
            "page1_accs": [h.get("acesssionNumber") for h in h1],
            "page2_dates": [h.get("filedDate") for h in h2],
            "page2_accs": [h.get("acesssionNumber") for h in h2],
        }
        logger.info("sort_order: p1=%s p2=%s",
                    [h.get("filedDate") for h in h1],
                    [h.get("filedDate") for h in h2])

        # Test sortBy="FILED_DATE" and sortBy="FILED_DATE_DESC"
        for sort_val in ["FILED_DATE", "FILED_DATE_DESC", "filedDate", "filed_date"]:
            body_sorted = _search_body(docket="ER25-1357", page=1, results=3, sort_by=sort_val)
            try:
                r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                      content=json.dumps(body_sorted))
                d = r.json()
                h = d.get("searchHits", [])
                out[f"sort_{sort_val}"] = {
                    "status": r.status_code,
                    "dates": [x.get("filedDate") for x in h],
                    "accs": [x.get("acesssionNumber") for x in h],
                }
                logger.info("sort_%s: dates=%s", sort_val, [x.get("filedDate") for x in h])
            except Exception as exc:
                out[f"sort_{sort_val}"] = {"error": str(exc)[:100]}

        # === Transmittals for both colliding filings ===
        for acc in _COLLISION_ACCS:
            try:
                body = _search_body(accession=acc, results=1)
                r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                      content=json.dumps(body))
                d = r.json()
                hits = d.get("searchHits", [])
                if hits:
                    h = hits[0]
                    transmittals = h.get("transmittals", [])
                    out[f"acc_{acc}"] = {
                        "status": r.status_code,
                        "found_acc": h.get("acesssionNumber"),
                        "filedDate": h.get("filedDate"),
                        "description": h.get("description", "")[:120],
                        "filer": next((a.get("affiliation") for a in h.get("affiliations", [])
                                       if a.get("afType") == "AUTHOR"), None),
                        "docketNumbers": h.get("docketNumbers", []),
                        "transmittals": transmittals,  # full, no truncation
                    }
                else:
                    out[f"acc_{acc}"] = {"status": r.status_code, "found": False}
                logger.info("acc_%s: status=%d hits=%d", acc, r.status_code, len(hits))
            except Exception as exc:
                out[f"acc_{acc}"] = {"error": str(exc)[:200]}

    return out
