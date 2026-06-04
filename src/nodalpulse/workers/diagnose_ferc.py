"""Probe File/DownloadP8File — the real FERC PDF endpoint (FileNet P8 CMS)."""
import json
import logging
import httpx

logger = logging.getLogger(__name__)

_BASE = "https://elibrary.ferc.gov/eLibrarywebapi/api"

# Browser-like headers (Referer required to bypass WAF)
_HEADERS_BROWSER = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Origin": "https://elibrary.ferc.gov",
    "Referer": "https://elibrary.ferc.gov/eLibrary/",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Known fileIds from prior probes
_FILINGS = [
    # acc, fileId, description
    ("20260309-5165", "DB029976-B851-C94C-843A-9CD363100000", "EL25-49 PJM Informational Report"),
    ("20260309-5267", "50147018-B7A1-CFF8-B59E-9CD43D100000", "EL25-49 PJM Answer"),
    ("20260331-5252", "368A0776-4E35-CECD-B91D-9D44E9100000", "ER25-1357 ACP motion"),
]

# Known accession numbers for DownloadPDF test
_ACCESSIONS = ["20260309-5165", "20260309-5267"]


async def handle_diagnose_ferc(payload: dict) -> dict:
    """Probe File/DownloadP8File with individual fileIds and DownloadPDF by accession."""
    out = {}

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=_HEADERS_BROWSER,
    ) as client:

        # First: GET the SPA to pick up any session cookies
        try:
            r = await client.get("https://elibrary.ferc.gov/eLibrary/",
                                 headers={**_HEADERS_BROWSER, "Accept": "text/html,*/*"})
            out["session_get"] = {
                "status": r.status_code,
                "cookies": dict(r.cookies),
                "ct": r.headers.get("content-type", "?")[:50],
            }
            logger.info("session_get: status=%d cookies=%s", r.status_code, list(r.cookies.keys()))
        except Exception as exc:
            out["session_get"] = {"error": str(exc)[:100]}

        # Probe DownloadP8File for each known fileId
        for acc, file_id, desc in _FILINGS:
            key = f"p8file_{acc}"
            try:
                body = {"fileidLst": [file_id]}
                r = await client.post(
                    f"{_BASE}/File/DownloadP8File",
                    content=json.dumps(body),
                )
                content = r.content
                out[key] = {
                    "acc": acc,
                    "fileId": file_id,
                    "desc": desc,
                    "status": r.status_code,
                    "ct": r.headers.get("content-type", "?")[:80],
                    "len": len(content),
                    "is_pdf": content[:4] == b"%PDF",
                    "preview_hex": content[:20].hex() if content else "",
                    "preview_text": content[:100].decode("utf-8", errors="replace") if content else "",
                }
                logger.info("p8file_%s: status=%d len=%d is_pdf=%s", acc, r.status_code,
                            len(content), content[:4] == b"%PDF")
            except Exception as exc:
                out[key] = {"acc": acc, "fileId": file_id, "error": str(exc)[:200]}
                logger.warning("p8file_%s: %s", acc, exc)

        # Probe DownloadPDF by accession (POST with body {"serverLocation": ""})
        for acc in _ACCESSIONS:
            key = f"pdf_acc_{acc}"
            try:
                r = await client.post(
                    f"{_BASE}/File/DownloadPDF",
                    params={"accessionNumber": acc},
                    content=json.dumps({"serverLocation": ""}),
                )
                content = r.content
                out[key] = {
                    "acc": acc,
                    "status": r.status_code,
                    "ct": r.headers.get("content-type", "?")[:80],
                    "len": len(content),
                    "is_pdf": content[:4] == b"%PDF",
                    "preview_text": content[:150].decode("utf-8", errors="replace") if content else "",
                }
                logger.info("pdf_acc_%s: status=%d len=%d is_pdf=%s", acc, r.status_code,
                            len(content), content[:4] == b"%PDF")
            except Exception as exc:
                out[key] = {"acc": acc, "error": str(exc)[:200]}

        # Probe filelist endpoint (HTML but might reveal correct download URL)
        try:
            r = await client.get(
                "https://elibrary.ferc.gov/eLibrary/filelist",
                params={"accession_number": "20260309-5165"},
                headers={**_HEADERS_BROWSER, "Accept": "text/html,*/*"},
            )
            out["filelist"] = {
                "status": r.status_code,
                "ct": r.headers.get("content-type", "?")[:80],
                "len": len(r.content),
                "preview": r.text[:400],
            }
            logger.info("filelist: status=%d len=%d", r.status_code, len(r.content))
        except Exception as exc:
            out["filelist"] = {"error": str(exc)[:100]}

    return out
