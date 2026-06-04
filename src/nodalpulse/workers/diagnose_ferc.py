"""Temporary diagnostic: find PDF download URL + author-scoped PJM search."""
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

# From prior probes — real values
_ACC = "20260331-5252"
_FILE_ID = "368A0776-4E35-CECD-B91D-9D44E9100000"
_DOC_ID  = "33154EF8-C41D-C3A0-843E-9D44E9000000"


async def handle_diagnose_ferc(payload: dict) -> dict:
    out = {}

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True,
        headers=_HEADERS,
    ) as client:

        # PDF attempt D: DownloadPDF?documentId=<doc-UUID>
        try:
            r = await client.get(f"{_BASE}/File/DownloadPDF",
                                 params={"documentId": _DOC_ID})
            out["pdf_by_docid"] = {
                "status": r.status_code, "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "ct": r.headers.get("content-type", "?")[:80],
                "preview": r.text[:200] if not r.content[:4] == b"%PDF" else None,
            }
            logger.info("pdf_by_docid status=%d is_pdf=%s", r.status_code, r.content[:4] == b"%PDF")
        except Exception as exc:
            out["pdf_by_docid"] = {"error": str(exc)[:200]}

        # PDF attempt E: POST DownloadPDF with body
        try:
            r = await client.post(f"{_BASE}/File/DownloadPDF",
                                  content=json.dumps({"accessionNumber": _ACC}))
            out["pdf_post_accession"] = {
                "status": r.status_code, "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "ct": r.headers.get("content-type", "?")[:80],
                "preview": r.text[:200] if not r.content[:4] == b"%PDF" else None,
            }
        except Exception as exc:
            out["pdf_post_accession"] = {"error": str(exc)[:200]}

        # PDF attempt F: DownloadFile via POST
        try:
            r = await client.post(f"{_BASE}/File/DownloadFile",
                                  content=json.dumps({"fileId": _FILE_ID}))
            out["pdf_post_fileid"] = {
                "status": r.status_code, "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "ct": r.headers.get("content-type", "?")[:80],
                "preview": r.text[:200] if not r.content[:4] == b"%PDF" else None,
            }
        except Exception as exc:
            out["pdf_post_fileid"] = {"error": str(exc)[:200]}

        # PDF attempt G: GetDocument?fileId
        try:
            r = await client.get(f"{_BASE}/File/GetDocument",
                                 params={"fileId": _FILE_ID})
            out["pdf_getdoc_fileid"] = {
                "status": r.status_code, "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "ct": r.headers.get("content-type", "?")[:80],
                "preview": r.text[:200] if not r.content[:4] == b"%PDF" else None,
            }
        except Exception as exc:
            out["pdf_getdoc_fileid"] = {"error": str(exc)[:200]}

        # PJM author-scoped: searchText="PJM Interconnection" + classTypes tariff + recent
        pjm_tariff_body = {
            "searchText": "PJM Interconnection",
            "searchFullText": False,
            "searchDescription": True,
            "docketSearches": [],
            "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
            "affiliations": [],
            "categories": [],
            "libraries": ["Electric"],
            "classTypes": [{"documentType": "Tariff Filing", "documentClass": "Application/Petition/Request"}],
            "accessionNumber": None,
            "eFiling": False,
            "resultsPerPage": 5, "curPage": 1,
            "groupBy": "NONE", "sortBy": "", "allDates": False,
        }
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(pjm_tariff_body))
            data = r.json() if r.status_code == 200 else {}
            hits = data.get("searchHits", [])
            out["pjm_tariff_text"] = {
                "status": r.status_code,
                "totalHits": data.get("totalHits"),
                "items_count": len(hits),
                "first_3_affils": [
                    [a.get("affiliation") for a in h.get("affiliations", [])]
                    for h in hits[:3]
                ],
                "first_3_dockets": [h.get("docketNumbers", [])[:3] for h in hits[:3]],
                "first_3_desc": [h.get("description", "")[:100] for h in hits[:3]],
            }
            logger.info("pjm_tariff_text totalHits=%s items=%d", data.get("totalHits"), len(hits))
        except Exception as exc:
            out["pjm_tariff_text"] = {"error": str(exc)[:200]}

        # PJM via docket prefix search — ER2[0-9]-XXX filed by anyone, narrow by text "PJM"
        # Try "searchText: PJM" + Electric library + Filed Date recent
        pjm_text_body = {
            "searchText": "\"PJM Interconnection, L.L.C.\" tariff",
            "searchFullText": False,
            "searchDescription": True,
            "docketSearches": [],
            "dateSearches": [{"startDate": "05-01-2026", "endDate": "06-03-2026", "dateType": "Filed Date"}],
            "affiliations": [],
            "categories": [],
            "libraries": ["Electric"],
            "classTypes": [],
            "accessionNumber": None,
            "eFiling": False,
            "resultsPerPage": 5, "curPage": 1,
            "groupBy": "NONE", "sortBy": "", "allDates": False,
        }
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(pjm_text_body))
            data = r.json() if r.status_code == 200 else {}
            hits = data.get("searchHits", [])
            out["pjm_text_desc"] = {
                "status": r.status_code,
                "totalHits": data.get("totalHits"),
                "items_count": len(hits),
                "first_3_affils": [
                    [a.get("affiliation") for a in h.get("affiliations", [])]
                    for h in hits[:3]
                ],
                "first_3_dockets": [h.get("docketNumbers", [])[:3] for h in hits[:3]],
                "first_3_desc": [h.get("description", "")[:100] for h in hits[:3]],
            }
            logger.info("pjm_text_desc totalHits=%s items=%d", data.get("totalHits"), len(hits))
        except Exception as exc:
            out["pjm_text_desc"] = {"error": str(exc)[:200]}

    return out
