"""Probe: find PJM Schedule 12-Appendix A RTEP cost allocation docket (2026)."""
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


def _search_body(text, docket=None, affil=None, start=None, end=None, page=1):
    return {
        "searchText": text,
        "searchFullText": False,
        "searchDescription": True,
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}] if docket else [],
        "dateSearches": [{"startDate": start, "endDate": end, "dateType": "Filed Date"}] if start else [],
        "affiliations": [affil] if affil else [],
        "categories": [],
        "libraries": ["Electric"],
        "classTypes": [],
        "accessionNumber": None,
        "eFiling": False,
        "resultsPerPage": 10, "curPage": page,
        "groupBy": "NONE", "sortBy": "", "allDates": not bool(start),
    }


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Search for PJM Schedule 12-Appendix A RTEP cost allocation filings."""
    out = {}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_HEADERS) as client:

        # Probe 1: ER24-843 (known RTEP PJM docket — backup)
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_search_body("*", docket="ER24-843")))
            d = r.json()
            hits = d.get("searchHits") or []
            out["er24_843"] = {
                "totalHits": d.get("totalHits"),
                "filings": [
                    {
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "filer": next((a.get("affiliation") for a in h.get("affiliations", [])
                                       if a.get("afType", "").upper() == "AUTHOR"), None),
                        "doc_type": [ct.get("documentType") for ct in h.get("classTypes", [])],
                        "desc": h.get("description", "")[:120],
                        "file_id": (h.get("transmittals") or [{}])[0].get("fileId"),
                        "dockets": h.get("docketNumbers", [])[:4],
                    }
                    for h in hits
                ],
            }
            logger.info("er24_843: totalHits=%s hits=%d", d.get("totalHits"), len(hits))
        except Exception as exc:
            out["er24_843"] = {"error": str(exc)[:200]}

        # Probe 2: "Schedule 12" text + PJM filer + 2026
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_search_body(
                                      "Schedule 12",
                                      affil="PJM Interconnection, L.L.C.",
                                      start="01-01-2026", end="06-04-2026")))
            d = r.json()
            hits = d.get("searchHits") or []
            out["schedule12_pjm_2026"] = {
                "totalHits": d.get("totalHits"),
                "filings": [
                    {
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "filer": next((a.get("affiliation") for a in h.get("affiliations", [])
                                       if a.get("afType", "").upper() == "AUTHOR"), None),
                        "doc_type": [ct.get("documentType") for ct in h.get("classTypes", [])],
                        "desc": h.get("description", "")[:120],
                        "file_id": (h.get("transmittals") or [{}])[0].get("fileId"),
                        "dockets": h.get("docketNumbers", [])[:4],
                    }
                    for h in hits
                ],
            }
            logger.info("schedule12_pjm_2026: totalHits=%s hits=%d", d.get("totalHits"), len(hits))
        except Exception as exc:
            out["schedule12_pjm_2026"] = {"error": str(exc)[:200]}

        # Probe 3: FERC order May 15 2026 (narrow window) containing "Schedule 12"
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_search_body(
                                      "Schedule 12 Appendix",
                                      start="05-01-2026", end="06-04-2026")))
            d = r.json()
            hits = d.get("searchHits") or []
            out["schedule12_appendix_may2026"] = {
                "totalHits": d.get("totalHits"),
                "filings": [
                    {
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "filer": next((a.get("affiliation") for a in h.get("affiliations", [])
                                       if a.get("afType", "").upper() == "AUTHOR"), None),
                        "doc_type": [ct.get("documentType") for ct in h.get("classTypes", [])],
                        "desc": h.get("description", "")[:120],
                        "file_id": (h.get("transmittals") or [{}])[0].get("fileId"),
                        "dockets": h.get("docketNumbers", [])[:4],
                    }
                    for h in hits
                ],
            }
            logger.info("schedule12_appendix_may2026: totalHits=%s hits=%d", d.get("totalHits"), len(hits))
        except Exception as exc:
            out["schedule12_appendix_may2026"] = {"error": str(exc)[:200]}

        # Probe 4: "RTEP" + PJM + 2026 (broader)
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_search_body(
                                      "RTEP cost allocation",
                                      affil="PJM Interconnection, L.L.C.",
                                      start="01-01-2026", end="06-04-2026")))
            d = r.json()
            hits = d.get("searchHits") or []
            out["rtep_cost_pjm_2026"] = {
                "totalHits": d.get("totalHits"),
                "filings": [
                    {
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "filer": next((a.get("affiliation") for a in h.get("affiliations", [])
                                       if a.get("afType", "").upper() == "AUTHOR"), None),
                        "desc": h.get("description", "")[:120],
                        "file_id": (h.get("transmittals") or [{}])[0].get("fileId"),
                        "dockets": h.get("docketNumbers", [])[:4],
                    }
                    for h in hits
                ],
            }
            logger.info("rtep_cost_pjm_2026: totalHits=%s hits=%d", d.get("totalHits"), len(hits))
        except Exception as exc:
            out["rtep_cost_pjm_2026"] = {"error": str(exc)[:200]}

    return out
