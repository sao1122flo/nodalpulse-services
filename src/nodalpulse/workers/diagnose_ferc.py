"""Probe: get ALL ER24-843 filings (50, DESC) to find PJM RTEP tariff filing."""
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
    """Fetch all 50 ER24-843 filings; return PJM and tariff-type subsets with full transmittals."""
    out = {}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_HEADERS) as client:
        body = {
            "searchText": "*", "searchFullText": True, "searchDescription": True,
            "docketSearches": [{"docketNumber": "ER24-843", "subDocketNumbers": []}],
            "dateSearches": [], "affiliations": [], "categories": [],
            "libraries": ["Electric"], "classTypes": [], "accessionNumber": None,
            "eFiling": False, "resultsPerPage": 50, "curPage": 1,
            "groupBy": "NONE", "sortBy": "", "allDates": True,
        }
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch", content=json.dumps(body))
            d = r.json()
            hits = d.get("searchHits") or []

            all_filings = []
            for h in hits:
                filer = next(
                    (a.get("affiliation") for a in h.get("affiliations", [])
                     if a.get("afType", "").upper() == "AUTHOR"),
                    None,
                )
                transmittals = [
                    {
                        "fileId": t.get("fileId"),
                        "fileDesc": t.get("fileDesc"),
                        "fileName": t.get("fileName"),
                    }
                    for t in (h.get("transmittals") or [])
                ]
                all_filings.append({
                    "acc": h.get("acesssionNumber"),
                    "filed": h.get("filedDate"),
                    "filer": filer,
                    "doc_type": [ct.get("documentType") for ct in h.get("classTypes", [])],
                    "desc": h.get("description", "")[:120],
                    "transmittals": transmittals,
                    "dockets": h.get("docketNumbers", [])[:3],
                })

            pjm_filings = [f for f in all_filings if "PJM" in (f.get("filer") or "")]
            tariff_filings = [
                f for f in all_filings
                if any("Tariff" in dt for dt in f.get("doc_type", []))
            ]

            out["er24_843_all"] = {
                "totalHits": d.get("totalHits"),
                "all_count": len(all_filings),
                "pjm_count": len(pjm_filings),
                "tariff_count": len(tariff_filings),
                "pjm_filings": pjm_filings,
                "tariff_filings": tariff_filings,
                "all_last_10": all_filings[-10:],
            }
            logger.info(
                "er24_843_all: totalHits=%s all=%d pjm=%d tariff=%d",
                d.get("totalHits"), len(all_filings), len(pjm_filings), len(tariff_filings),
            )
        except Exception as exc:
            out["er24_843_all"] = {"error": str(exc)[:300]}

    return out
