"""Temporary diagnostic: probe FERC eLibrarywebapi API — raw JSON capture."""
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
    "resultsPerPage": 3, "curPage": 1, "groupBy": "NONE", "sortBy": "", "allDates": True,
}

_SEARCH_AFFIL_ALL_DATES = {
    "searchText": "*", "searchFullText": True, "searchDescription": True,
    "docketSearches": [],
    "dateSearches": [], "affiliations": ["PJM Interconnection, L.L.C."],
    "categories": [], "libraries": [], "classTypes": [],
    "accessionNumber": None, "eFiling": False,
    "resultsPerPage": 3, "curPage": 1, "groupBy": "NONE", "sortBy": "", "allDates": True,
}

_SEARCH_DOCKET_DATED_MDY = {
    **_SEARCH_DOCKET_ALL_DATES,
    "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
    "allDates": False,
}


async def handle_diagnose_ferc(payload: dict) -> dict:
    """4 probes: docket-all-dates, docket-dated, PJM-affil, PDF fetch."""
    out = {}

    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS) as client:

        # Probe 1 — docket ER25-1357, all dates, grab raw JSON
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_SEARCH_DOCKET_ALL_DATES))
            raw = r.text
            try:
                data = r.json()
            except Exception:
                data = {}
            top_keys = list(data.keys()) if isinstance(data, dict) else []
            # Find the list-valued key
            items_list = []
            items_key = None
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list):
                        items_list = v
                        items_key = k
                        break
            first_item = items_list[0] if items_list else {}
            out["p1_docket"] = {
                "status": r.status_code,
                "top_keys": top_keys,
                "items_key": items_key,
                "items_count": len(items_list),
                "first_item_keys": list(first_item.keys()),
                "first_item": first_item,
                "raw_preview": raw[:1000],  # dump raw to see structure
            }
            logger.info("p1_docket status=%d top_keys=%s items_key=%s items=%d",
                        r.status_code, top_keys, items_key, len(items_list))
        except Exception as exc:
            out["p1_docket"] = {"error": str(exc)[:300]}

        # Probe 2 — same docket with date filter (MDY)
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_SEARCH_DOCKET_DATED_MDY))
            data = r.json() if r.status_code == 200 else {}
            top_keys = list(data.keys()) if isinstance(data, dict) else []
            items_list = next((v for v in data.values() if isinstance(v, list)), []) if data else []
            out["p2_dated_mdy"] = {
                "status": r.status_code,
                "top_keys": top_keys,
                "items_count": len(items_list),
            }
            logger.info("p2_dated_mdy status=%d top_keys=%s items=%d",
                        r.status_code, top_keys, len(items_list))
        except Exception as exc:
            out["p2_dated_mdy"] = {"error": str(exc)[:200]}

        # Probe 3 — PJM affiliation, all dates
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(_SEARCH_AFFIL_ALL_DATES))
            raw = r.text
            data = r.json() if r.status_code == 200 else {}
            top_keys = list(data.keys()) if isinstance(data, dict) else []
            items_list = next((v for v in data.values() if isinstance(v, list)), []) if data else []
            first_item = items_list[0] if items_list else {}
            out["p3_affil"] = {
                "status": r.status_code,
                "top_keys": top_keys,
                "items_count": len(items_list),
                "first_item": first_item,
                "raw_preview": raw[:500],
            }
            logger.info("p3_affil status=%d top_keys=%s items=%d",
                        r.status_code, top_keys, len(items_list))
        except Exception as exc:
            out["p3_affil"] = {"error": str(exc)[:200]}

        # Probe 4 — PDF download for first accession
        first_item = out.get("p1_docket", {}).get("first_item", {})
        acc = None
        for k in ("accessionNumber", "accession_number", "accession", "id",
                  "AccessionNumber", "Accession", "AccessionNo"):
            if k in first_item:
                acc = str(first_item[k])
                break

        if acc:
            try:
                r = await client.get(f"{_BASE}/File/DownloadPDF",
                                     params={"accessionNumber": acc})
                out["p4_pdf"] = {
                    "acc": acc,
                    "status": r.status_code,
                    "ct": r.headers.get("content-type", "?")[:80],
                    "len": len(r.content),
                    "is_pdf": r.content[:4] == b"%PDF",
                }
                logger.info("p4_pdf acc=%s status=%d is_pdf=%s", acc, r.status_code, r.content[:4] == b"%PDF")
            except Exception as exc:
                out["p4_pdf"] = {"acc": acc, "error": str(exc)[:200]}
        else:
            out["p4_pdf"] = {"skipped": True, "p1_keys": list(first_item.keys())}

    return out
