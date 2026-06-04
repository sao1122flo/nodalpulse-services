"""Temporary diagnostic: capture full transmittals + test fileName as URL."""
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

_SEARCH_DOCKET_ALL_DATES = {
    "searchText": "*", "searchFullText": True, "searchDescription": True,
    "docketSearches": [{"docketNumber": "ER25-1357", "subDocketNumbers": []}],
    "dateSearches": [], "affiliations": [], "categories": [], "libraries": [],
    "classTypes": [], "accessionNumber": None, "eFiling": False,
    "resultsPerPage": 2, "curPage": 1, "groupBy": "NONE", "sortBy": "", "allDates": True,
}

# PJM filing discovery — text search in description, no quotes, no classType filter
_SEARCH_PJM_TEXT = {
    "searchText": "PJM Interconnection",
    "searchFullText": False,
    "searchDescription": True,
    "docketSearches": [],
    "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
    "affiliations": [], "categories": [], "libraries": ["Electric"],
    "classTypes": [], "accessionNumber": None, "eFiling": False,
    "resultsPerPage": 3, "curPage": 1, "groupBy": "NONE", "sortBy": "", "allDates": False,
}


async def handle_diagnose_ferc(payload: dict) -> dict:
    out = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:

        # 1. Fetch item and capture FULL transmittals (no truncation)
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_SEARCH_DOCKET_ALL_DATES))
            data = r.json() if r.status_code == 200 else {}
            hits = data.get("searchHits", [])
            first = hits[0] if hits else {}
            transmittals = first.get("transmittals", [])
            out["full_transmittals"] = {
                "status": r.status_code,
                "acesssionNumber": first.get("acesssionNumber"),
                "transmittals_count": len(transmittals),
                "transmittals": transmittals,  # all fields, no truncation
            }
            logger.info("full_transmittals acc=%s transmittals=%d",
                        first.get("acesssionNumber"), len(transmittals))
        except Exception as exc:
            out["full_transmittals"] = {"error": str(exc)[:200]}

        # 2. Try fetching the first transmittal's fileName as a URL
        transmittals = out.get("full_transmittals", {}).get("transmittals", [])
        if transmittals:
            t0 = transmittals[0]
            file_name = t0.get("fileName", "")
            file_id = t0.get("fileId", "")
            # Check if fileName looks like a URL
            if file_name.startswith("http"):
                try:
                    r = await client.get(file_name, headers={"Accept": "*/*"})
                    out["transmittal_filename_url"] = {
                        "url": file_name,
                        "status": r.status_code,
                        "ct": r.headers.get("content-type", "?")[:80],
                        "len": len(r.content),
                        "is_pdf": r.content[:4] == b"%PDF",
                    }
                except Exception as exc:
                    out["transmittal_filename_url"] = {"url": file_name, "error": str(exc)[:200]}
            else:
                out["transmittal_filename_url"] = {"fileName": file_name, "is_url": False}

            # Try DownloadFile with the fileId from transmittals
            if file_id:
                try:
                    r = await client.get(
                        f"{_BASE}/File/DownloadFile",
                        params={"fileId": file_id},
                        headers={**_HEADERS, "Accept": "application/pdf, */*"},
                    )
                    out["transmittal_fileid_pdf_accept"] = {
                        "fileId": file_id,
                        "status": r.status_code,
                        "ct": r.headers.get("content-type", "?")[:80],
                        "len": len(r.content),
                        "is_pdf": r.content[:4] == b"%PDF",
                        "preview": r.text[:100] if not r.content[:4] == b"%PDF" else None,
                    }
                    logger.info("transmittal_fileid_pdf_accept status=%d is_pdf=%s len=%d",
                                r.status_code, r.content[:4] == b"%PDF", len(r.content))
                except Exception as exc:
                    out["transmittal_fileid_pdf_accept"] = {"error": str(exc)[:200]}
        else:
            out["transmittal_filename_url"] = {"skipped": "no transmittals"}

        # 3. PJM text search in description (no quotes, no classType filter)
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_SEARCH_PJM_TEXT))
            data = r.json() if r.status_code == 200 else {}
            hits = data.get("searchHits", [])
            out["pjm_desc_search"] = {
                "status": r.status_code,
                "totalHits": data.get("totalHits"),
                "numHits": data.get("numHits"),
                "items_count": len(hits),
                "first_3": [
                    {
                        "acc": h.get("acesssionNumber"),
                        "filed": h.get("filedDate"),
                        "dockets": h.get("docketNumbers", [])[:3],
                        "affils": [a.get("affiliation") for a in h.get("affiliations", []) if a.get("afType") == "AUTHOR"],
                        "desc": h.get("description", "")[:100],
                    }
                    for h in hits[:3]
                ],
            }
            logger.info("pjm_desc_search totalHits=%s numHits=%s items=%d",
                        data.get("totalHits"), data.get("numHits"), len(hits))
        except Exception as exc:
            out["pjm_desc_search"] = {"error": str(exc)[:200]}

    return out
