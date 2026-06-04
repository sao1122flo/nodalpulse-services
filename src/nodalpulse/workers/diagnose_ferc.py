"""Temporary diagnostic: probe FERC eLibrarywebapi API endpoints from Railway server."""
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

_SEARCH_BODY_DOCKET = {
    "searchText": "*",
    "searchFullText": True,
    "searchDescription": True,
    "docketSearches": [{"docketNumber": "ER25-1357", "subDocketNumbers": []}],
    "dateSearches": [],
    "affiliations": [],
    "categories": [],
    "libraries": [],
    "classTypes": [],
    "accessionNumber": None,
    "eFiling": False,
    "resultsPerPage": 3,
    "curPage": 1,
    "groupBy": "NONE",
    "sortBy": "",
    "allDates": True,
}

# Probe 2a: date range with MM-DD-YYYY (as in the wrapper)
_SEARCH_BODY_DATED_MDY = {
    **_SEARCH_BODY_DOCKET,
    "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
    "allDates": False,
}

# Probe 2b: date range with YYYY-MM-DD (ISO)
_SEARCH_BODY_DATED_ISO = {
    **_SEARCH_BODY_DOCKET,
    "dateSearches": [{"startDate": "2026-05-01", "endDate": "2026-06-03", "dateType": "Filed Date"}],
    "allDates": False,
}

# Probe 3: PJM affiliation, all dates
_SEARCH_BODY_AFFIL = {
    "searchText": "*",
    "searchFullText": True,
    "searchDescription": True,
    "docketSearches": [],
    "dateSearches": [],
    "affiliations": ["PJM Interconnection, L.L.C."],
    "categories": [],
    "libraries": [],
    "classTypes": [],
    "accessionNumber": None,
    "eFiling": False,
    "resultsPerPage": 3,
    "curPage": 1,
    "groupBy": "NONE",
    "sortBy": "",
    "allDates": True,
}


def _capture_response(resp: httpx.Response) -> dict:
    """Parse JSON and capture structure regardless of key names."""
    try:
        data = resp.json()
    except Exception as exc:
        return {"status": resp.status_code, "parse_error": str(exc), "preview": resp.text[:300]}

    top_keys = list(data.keys()) if isinstance(data, dict) else ["<list>"]

    # Find the items list — try common key names
    items = []
    items_key = None
    for k in ("results", "items", "filings", "documents", "data", "hits"):
        if isinstance(data.get(k), list):
            items = data[k]
            items_key = k
            break
    # Fallback: any list value
    if not items_key and isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and len(v) > 0:
                items = v
                items_key = k
                break

    first = items[0] if items else {}

    return {
        "status": resp.status_code,
        "top_keys": top_keys,
        "total_hits_raw": data.get("totalHits", data.get("total", data.get("count", "?"))),
        "items_key_found": items_key,
        "items_count": len(items),
        "first_item_keys": list(first.keys()) if first else [],
        "first_item": first,
        # Dump full JSON if small enough
        "full_json_if_small": data if len(str(data)) < 2000 else "<too large>",
    }


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Probe FERC eLibrarywebapi API — structured shape discovery."""
    results = {}

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:

        # --- Probe 1: docket search, all dates — capture full shape ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_DOCKET),
            )
            results["probe1_docket"] = _capture_response(resp)
            logger.info("probe1_docket status=%d", resp.status_code)
        except Exception as exc:
            results["probe1_docket"] = {"error": str(exc)[:200]}
            logger.warning("probe1_docket error: %s", exc)

        # --- Probe 2a: dated search MM-DD-YYYY ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_DATED_MDY),
            )
            results["probe2a_dated_mdy"] = _capture_response(resp)
            logger.info("probe2a_dated_mdy status=%d totalHits=%s",
                        resp.status_code, results["probe2a_dated_mdy"].get("total_hits_raw"))
        except Exception as exc:
            results["probe2a_dated_mdy"] = {"error": str(exc)[:200]}

        # --- Probe 2b: dated search ISO ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_DATED_ISO),
            )
            results["probe2b_dated_iso"] = _capture_response(resp)
            logger.info("probe2b_dated_iso status=%d totalHits=%s",
                        resp.status_code, results["probe2b_dated_iso"].get("total_hits_raw"))
        except Exception as exc:
            results["probe2b_dated_iso"] = {"error": str(exc)[:200]}

        # --- Probe 3: PJM affiliation, all dates ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_AFFIL),
            )
            results["probe3_affil"] = _capture_response(resp)
            logger.info("probe3_affil status=%d totalHits=%s",
                        resp.status_code, results["probe3_affil"].get("total_hits_raw"))
        except Exception as exc:
            results["probe3_affil"] = {"error": str(exc)[:200]}
            logger.warning("probe3_affil error: %s", exc)

        # --- Probe 4: PDF download for first accession from probe 1 ---
        first = results.get("probe1_docket", {}).get("first_item", {})
        acc = None
        for key in ("accessionNumber", "accession_number", "accession", "id", "AccessionNumber", "Accession"):
            if key in first:
                acc = str(first[key])
                break

        if acc:
            try:
                resp = await client.get(
                    f"{_BASE}/File/DownloadPDF",
                    params={"accessionNumber": acc},
                )
                results["probe4_pdf"] = {
                    "accession_used": acc,
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", "?")[:80],
                    "len": len(resp.content),
                    "is_pdf": resp.content[:4] == b"%PDF",
                }
                logger.info("probe4_pdf acc=%s status=%d len=%d is_pdf=%s",
                            acc, resp.status_code, len(resp.content), resp.content[:4] == b"%PDF")
            except Exception as exc:
                results["probe4_pdf"] = {"accession_used": acc, "error": str(exc)[:200]}
        else:
            results["probe4_pdf"] = {
                "skipped": "no accession from probe1",
                "probe1_item_keys": list(first.keys()),
            }

    return results
