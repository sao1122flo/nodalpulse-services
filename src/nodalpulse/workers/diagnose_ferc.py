"""Diagnostic: fetch all ER24-2236 filings (all dates) to find RTEP tariff filing."""
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


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Fetch all ER24-2236 and ER24-2238 filings (all dates) for RTEP verify."""
    out = {}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_HEADERS) as client:
        for docket in ["ER24-2236", "ER24-2238"]:
            try:
                body = {
                    "searchText": "*", "searchFullText": True, "searchDescription": True,
                    "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
                    "dateSearches": [], "affiliations": [], "categories": [],
                    "libraries": [], "classTypes": [], "accessionNumber": None,
                    "eFiling": False, "resultsPerPage": 50, "curPage": 1,
                    "groupBy": "NONE", "sortBy": "", "allDates": True,
                }
                r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                      content=json.dumps(body))
                data = r.json()
                hits = data.get("searchHits") or []

                filings = []
                for h in hits:
                    transmittals = h.get("transmittals") or []
                    filer = next(
                        (a.get("affiliation") for a in h.get("affiliations", [])
                         if a.get("afType", "").upper() == "AUTHOR"),
                        None,
                    )
                    filings.append({
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "filer": filer,
                        "doc_type_raw": [ct.get("documentType") for ct in h.get("classTypes", [])],
                        "desc": h.get("description", "")[:100],
                        "file_id": transmittals[0].get("fileId") if transmittals else None,
                        "file_name": transmittals[0].get("fileName") if transmittals else None,
                    })

                out[docket] = {
                    "totalHits": data.get("totalHits"),
                    "filings": filings,
                }
                logger.info("%s: totalHits=%d", docket, data.get("totalHits", 0))
            except Exception as exc:
                out[docket] = {"error": str(exc)[:200]}
                logger.warning("%s: %s", docket, exc)

    return out
