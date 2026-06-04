"""Temporary diagnostic: probe FERC eLibrarywebapi — PDF download confirmation."""
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

# Known good values from prior probe
_KNOWN_ACCESSION = "20260331-5252"  # from p1_docket first result
_KNOWN_FILE_ID = "368A0776-4E35-CECD-B91D-9D44E9100000"   # transmittals[0].fileId from p1
_KNOWN_DOC_ID   = "33154EF8-C41D-C3A0-843E-9D44E9000000"  # documentId from p1


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Probe 3 PDF download patterns + confirm PJM-specific affiliation query."""
    out = {}

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True,
        headers={**_HEADERS, "Accept": "*/*"},
    ) as client:

        # PDF pattern A: DownloadPDF?accessionNumber=YYYYMMDD-NNNN
        try:
            r = await client.get(f"{_BASE}/File/DownloadPDF",
                                 params={"accessionNumber": _KNOWN_ACCESSION})
            out["pdf_by_accession"] = {
                "url": str(r.url),
                "status": r.status_code,
                "ct": r.headers.get("content-type", "?")[:80],
                "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "preview": r.text[:200] if r.status_code != 200 else None,
            }
            logger.info("pdf_by_accession status=%d is_pdf=%s", r.status_code, r.content[:4] == b"%PDF")
        except Exception as exc:
            out["pdf_by_accession"] = {"error": str(exc)[:200]}

        # PDF pattern B: DownloadFile?fileId=<UUID>
        try:
            r = await client.get(f"{_BASE}/File/DownloadFile",
                                 params={"fileId": _KNOWN_FILE_ID})
            out["pdf_by_fileid"] = {
                "url": str(r.url),
                "status": r.status_code,
                "ct": r.headers.get("content-type", "?")[:80],
                "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
                "preview": r.text[:200] if r.status_code != 200 else None,
            }
            logger.info("pdf_by_fileid status=%d is_pdf=%s", r.status_code, r.content[:4] == b"%PDF")
        except Exception as exc:
            out["pdf_by_fileid"] = {"error": str(exc)[:200]}

        # PDF pattern C: older elibrary endpoint
        try:
            r = await client.get(
                "https://elibrary.ferc.gov/eLibrary/idmws/common/openNative.asp",
                params={"fileId": _KNOWN_FILE_ID})
            out["pdf_legacy"] = {
                "status": r.status_code,
                "ct": r.headers.get("content-type", "?")[:80],
                "len": len(r.content),
                "is_pdf": r.content[:4] == b"%PDF",
            }
        except Exception as exc:
            out["pdf_legacy"] = {"error": str(exc)[:200]}

        # Confirm PJM-specific affiliation: use "PJM Interconnection" (without LLC)
        pjm_body = {
            "searchText": "*", "searchFullText": True, "searchDescription": True,
            "docketSearches": [], "dateSearches": [],
            "affiliations": ["PJM Interconnection"],
            "categories": [], "libraries": [], "classTypes": [],
            "accessionNumber": None, "eFiling": False,
            "resultsPerPage": 3, "curPage": 1, "groupBy": "NONE", "sortBy": "", "allDates": True,
        }
        try:
            r = await client.post(f"{_BASE}/Search/AdvancedSearch",
                                  content=json.dumps(pjm_body))
            data = r.json() if r.status_code == 200 else {}
            hits = data.get("searchHits", [])
            out["pjm_affil_short"] = {
                "status": r.status_code,
                "totalHits": data.get("totalHits"),
                "items_count": len(hits),
                "first_3_affils": [
                    [a.get("affiliation") for a in h.get("affiliations", [])]
                    for h in hits[:3]
                ],
                "first_3_dockets": [h.get("docketNumbers", [])[:2] for h in hits[:3]],
                "first_3_descriptions": [h.get("description", "")[:100] for h in hits[:3]],
            }
            logger.info("pjm_affil_short status=%d totalHits=%s items=%d",
                        r.status_code, data.get("totalHits"), len(hits))
        except Exception as exc:
            out["pjm_affil_short"] = {"error": str(exc)[:200]}

    return out
