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
    "resultsPerPage": 5,
    "curPage": 1,
    "groupBy": "NONE",
    "sortBy": "",
    "allDates": True,
}

_SEARCH_BODY_DATED = {
    "searchText": "*",
    "searchFullText": True,
    "searchDescription": True,
    "docketSearches": [{"docketNumber": "ER25-1357", "subDocketNumbers": []}],
    "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
    "affiliations": [],
    "categories": [],
    "libraries": [],
    "classTypes": [],
    "accessionNumber": None,
    "eFiling": False,
    "resultsPerPage": 5,
    "curPage": 1,
    "groupBy": "NONE",
    "sortBy": "",
    "allDates": False,
}

_SEARCH_BODY_AFFIL = {
    "searchText": "*",
    "searchFullText": True,
    "searchDescription": True,
    "docketSearches": [],
    "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
    "affiliations": ["PJM Interconnection, L.L.C."],
    "categories": [],
    "libraries": [],
    "classTypes": [],
    "accessionNumber": None,
    "eFiling": False,
    "resultsPerPage": 5,
    "curPage": 1,
    "groupBy": "NONE",
    "sortBy": "",
    "allDates": False,
}


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Probe FERC eLibrarywebapi API — 4 probes:
    1. Docket search (ER25-1357, allDates=True) — capture field names + accession
    2. Dated search on same docket — confirm dateSearches[] shape
    3. Affiliation search (PJM Interconnection) — confirm discovery pump
    4. PDF fetch for accession from probe 1
    """
    results = {}

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:

        # --- Probe 1: docket search, all dates ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_DOCKET),
            )
            body = resp.text
            try:
                data = resp.json()
                first = data.get("results", [{}])[0] if data.get("results") else {}
                results["probe1_docket"] = {
                    "status": resp.status_code,
                    "totalHits": data.get("totalHits"),
                    "resultsPerPage": data.get("resultsPerPage"),
                    "first_item_keys": list(first.keys()),
                    "first_item": first,
                }
            except Exception:
                results["probe1_docket"] = {
                    "status": resp.status_code,
                    "parse_error": True,
                    "preview": body[:400],
                }
            logger.info("probe1_docket status=%d", resp.status_code)
        except Exception as exc:
            results["probe1_docket"] = {"error": str(exc)[:200]}
            logger.warning("probe1_docket error: %s", exc)

        # --- Probe 2: dated search — confirm dateSearches[] shape ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_DATED),
            )
            try:
                data = resp.json()
                first = data.get("results", [{}])[0] if data.get("results") else {}
                results["probe2_dated"] = {
                    "status": resp.status_code,
                    "totalHits": data.get("totalHits"),
                    "first_item": first,
                }
            except Exception:
                results["probe2_dated"] = {
                    "status": resp.status_code,
                    "preview": resp.text[:400],
                }
            logger.info("probe2_dated status=%d", resp.status_code)
        except Exception as exc:
            results["probe2_dated"] = {"error": str(exc)[:200]}
            logger.warning("probe2_dated error: %s", exc)

        # --- Probe 3: affiliation discovery pump ---
        try:
            resp = await client.post(
                f"{_BASE}/Search/AdvancedSearch",
                content=json.dumps(_SEARCH_BODY_AFFIL),
            )
            try:
                data = resp.json()
                items = data.get("results", [])
                results["probe3_affil"] = {
                    "status": resp.status_code,
                    "totalHits": data.get("totalHits"),
                    "first_3_descriptions": [r.get("description", r.get("filingDescription", r)) for r in items[:3]],
                    "first_3_dockets": [r.get("docketNumber", r.get("docketNumbers", "?")) for r in items[:3]],
                }
            except Exception:
                results["probe3_affil"] = {
                    "status": resp.status_code,
                    "preview": resp.text[:400],
                }
            logger.info("probe3_affil status=%d", resp.status_code)
        except Exception as exc:
            results["probe3_affil"] = {"error": str(exc)[:200]}
            logger.warning("probe3_affil error: %s", exc)

        # --- Probe 4: PDF download for first accession from probe 1 ---
        acc = None
        p1 = results.get("probe1_docket", {})
        first = p1.get("first_item", {})
        # Try common key names
        for key in ("accessionNumber", "accession_number", "accession", "id"):
            if key in first:
                acc = first[key]
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
                logger.info("probe4_pdf accession=%s status=%d len=%d is_pdf=%s",
                            acc, resp.status_code, len(resp.content),
                            resp.content[:4] == b"%PDF")
            except Exception as exc:
                results["probe4_pdf"] = {"accession_used": acc, "error": str(exc)[:200]}
        else:
            results["probe4_pdf"] = {"skipped": "no accession from probe1", "probe1_keys": list(first.keys())}

    return results
