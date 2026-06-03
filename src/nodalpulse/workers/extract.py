"""Job handler for extract queue jobs."""

import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

import httpx
import pdfplumber
from selectolax.parser import HTMLParser

from nodalpulse.db.extractions import get_filing, insert_extraction, update_filing_r2_key
from nodalpulse.db.filings import find_or_create_docket, upsert_filing_dockets
from nodalpulse.llm.client import classify
from nodalpulse.llm.client import extract as llm_extract
from nodalpulse.llm.taxonomy import TEXAS_ELECTRICITY_TAXONOMY
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

SCHEMA_VER = "1.0"
PROMPT_VER = "1.2"  # CAISO initiative_name + cpuc_proceeding_refs; deferred-R2 post-triage
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

_CONTENT_TYPES: dict[str, str] = {
    "pdf":  "application/pdf",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt":  "text/plain",
}

_TRIAGE_SYSTEM = """\
You are a document relevance classifier for Texas electricity market regulation.

Classify the document as:
- "relevant": directly concerns electricity generation, transmission, distribution,
  rates, wholesale markets, or Texas grid/ERCOT operations
- "irrelevant": unrelated to electricity markets (e.g. telecom, water, gas pipelines only)
- "uncertain": could be relevant but unclear from the text alone

Respond with JSON only: {"verdict": "relevant"|"irrelevant"|"uncertain", "reason": "<one sentence>"}\
"""

_ROLE_TAGS_FIELD = """\
  "role_tags": ["<role>", ...]
Role tags: subset of market roles most likely to care about this filing.
Use only values from: "Regulatory Analyst", "Compliance Officer", "Energy Lawyer",
"BESS Regulatory Lead", "Trader / Risk Manager", "Consultant / Advisory",
"Utility / Co-op Staff", "Developer / IPP".
Empty array means relevant to all roles.\
"""

_EXTRACT_SYSTEM_CAISO = """\
You are an expert analyst of CAISO (California ISO) regulatory filings submitted to FERC
(Federal Energy Regulatory Commission). CAISO files tariff amendments, compliance filings,
informational filings, and motions in FERC proceedings that relate to California grid operations.

Extract structured information from the document. Respond with JSON only, no markdown fences:
{
  "summary": "<2-3 sentence plain-language summary>",
  "key_points": ["<point>", ...],
  "parties": ["<party name>", ...],
  "docket_number": "<primary FERC docket ID, e.g. ER25-2442, or null>",
  "relief_requested": "<what CAISO or the filer is requesting from FERC, or null>",
  "outcome": "<if this is a FERC order: the disposition, or null>",
  "effective_date": "<ISO date if mentioned as a proposed or ordered effective date, or null>",
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}],
  "initiative_name": "<CAISO internal initiative or tariff topic name, e.g. 'Storage as a Transmission-Only Asset (SPTO)', 'Resource Adequacy (RA)', or null if not identifiable>",
  "cpuc_proceeding_refs": ["<CPUC proceeding number e.g. A.22-11-017 or R.21-06-017, if the document cross-references a CPUC proceeding>"],
  "role_tags": []
}

initiative_name guidance: CAISO filings usually reference an internal initiative by name in
the transmittal letter or title. Common examples: "Storage as a Transmission-Only Asset",
"Resource Adequacy", "Distributed Energy Resources Provider", "Energy Storage", "BESS".
Extract the full initiative name as written. If no initiative name is identifiable, return null.

cpuc_proceeding_refs guidance: CPUC proceeding numbers follow the format Letter.YY-MM-NNN
(e.g. A.22-11-017, C.22-08-027, I.20-06-020, R.21-06-017, D.23-02-041).
Only include refs explicitly cited in the document — do not infer them.
Return an empty array [] if none are cited.

Few-shot examples of initiative_name extraction:
- Title "Errata to Informational Filing of 2-Year Suspension — SPTO Tariff Amendment (ER25-2442)"
  → initiative_name: "Storage as a Transmission-Only Asset (SPTO)"
- Title "Joint Motion for Extension… DCR Transmission (ER23-2309, ER24-1394, EL26-34)"
  → initiative_name: null  (DCR Transmission is a project name, not an initiative)
- Filing body mentions "CAISO's Resource Adequacy (RA) initiative" repeatedly
  → initiative_name: "Resource Adequacy (RA)"\
"""

_EXTRACT_SYSTEM_PUCT = """\
You are an expert analyst of Texas electricity regulatory filings at the Public Utility
Commission of Texas (PUCT).

Extract structured information from the document. Respond with JSON only, no markdown fences:
{
  "summary": "<2-3 sentence plain-language summary>",
  "key_points": ["<point>", ...],
  "parties": ["<party name>", ...],
  "docket_number": "<PUCT control number or null>",
  "relief_requested": "<what the filer is asking for, or null>",
  "outcome": "<if this is an order: the ruling, or null>",
  "effective_date": "<ISO date if mentioned, or null>",
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}],
  "role_tags": []
}

""" + _ROLE_TAGS_FIELD

_EXTRACT_SYSTEM_ERCOT_NPRR = """\
You are an expert analyst of ERCOT (Electric Reliability Council of Texas) protocol
revision documents, including NPRRs, PGRRs, MPRRs, NOGRRs, SCRs, SMOGRRs, and RMGRRs.

Extract structured information from the document. Respond with JSON only, no markdown fences:
{
  "summary": "<2-3 sentence plain-language summary>",
  "key_points": ["<point>", ...],
  "parties": ["<party name or submitting entity>", ...],
  "docket_number": "<NPRR/PGRR/MPRR number, e.g. NPRR1287, or null>",
  "relief_requested": "<what protocol change is being proposed, or null>",
  "outcome": "<if this is a final disposition: the ruling or withdrawal status, or null>",
  "effective_date": "<ISO date if mentioned, or null>",
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}],
  "role_tags": []
}

""" + _ROLE_TAGS_FIELD

_EXTRACT_SYSTEM_ERCOT_MN = """\
You are an expert analyst of ERCOT (Electric Reliability Council of Texas) Market Notices,
which are operational communications to ERCOT market participants.

Extract structured information from the document. Respond with JSON only, no markdown fences:
{
  "summary": "<2-3 sentence plain-language summary>",
  "key_points": ["<point>", ...],
  "parties": ["<affected market segment or entity>", ...],
  "docket_number": "<Market Notice ID or null>",
  "relief_requested": null,
  "outcome": null,
  "effective_date": "<ISO date if mentioned, or null>",
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}],
  "role_tags": []
}

""" + _ROLE_TAGS_FIELD


def _extract_system_for_doc_type(doc_type: str, source_slug: str = "") -> str:
    if source_slug == "caiso":
        base = _EXTRACT_SYSTEM_CAISO
    elif doc_type == "ercot-mn":
        base = _EXTRACT_SYSTEM_ERCOT_MN
    elif doc_type.startswith("ercot-"):
        base = _EXTRACT_SYSTEM_ERCOT_NPRR
    else:
        base = _EXTRACT_SYSTEM_PUCT
    return base + "\n\n" + TEXAS_ELECTRICITY_TAXONOMY


async def handle_extract(payload: dict) -> dict:
    filing_id = payload["filing_id"]
    doc_type = payload.get("doc_type", "puct-filing")

    filing = await get_filing(filing_id)
    if not filing:
        raise RuntimeError(f"Filing {filing_id} not found")

    r2_key: str | None = filing.get("r2_key")
    source_url: str | None = filing.get("source_url")
    file_ext: str = (filing.get("file_ext") or "pdf").lower()
    source_slug: str = filing.get("source_slug") or ""
    source_id: str | None = filing.get("source_id")

    # Fetch bytes — from R2 if already materialized, else from source_url (deferred adapters).
    # Bandwidth-only until triage; R2 Class A write is deferred until after triage passes.
    if r2_key:
        content = r2.download(r2_key)
    elif source_url:
        content = await _fetch_source_url(source_url)
    else:
        logger.warning("Filing %s has no r2_key and no source_url — skipping", filing_id)
        return {"filing_id": filing_id, "skipped": True, "reason": "no_content_source"}

    text = _extract_text(content, file_ext)
    if not text.strip():
        logger.warning("No text extracted from %s", filing_id)
        return {"filing_id": filing_id, "skipped": True, "reason": "no_text"}

    # Haiku triage — cheap pass before any R2 write or Sonnet call.
    triage_raw = await classify(_TRIAGE_SYSTEM, f"Document type: {doc_type}\n\n{text[:8_000]}", filing_id=filing_id)
    try:
        haiku_verdict = _parse_json(triage_raw).get("verdict", "uncertain")
    except Exception:
        haiku_verdict = "uncertain"

    logger.info("Filing %s verdict=%s source=%s", filing_id, haiku_verdict, source_slug)

    if haiku_verdict == "irrelevant":
        # Don't materialize R2 — only ~27% of filings pass triage; this is the deferred-R2 saving.
        extraction_id = await insert_extraction(
            filing_id=filing_id,
            schema_ver=SCHEMA_VER,
            model=SONNET_MODEL,
            prompt_ver=PROMPT_VER,
            payload={},
            haiku_verdict=haiku_verdict,
            haiku_model=HAIKU_MODEL,
        )
        return {"filing_id": filing_id, "extraction_id": extraction_id, "verdict": "irrelevant"}

    # Filing passed triage — now materialize R2 if content was fetched from source_url.
    # Idempotent: if r2_key already set (re-extract), skip the upload.
    if not r2_key:
        filed_at = filing.get("filed_at") or ""
        date_parts = filed_at[:10].split("-") if filed_at else ["0000", "00", "00"]
        external_id = filing.get("external_id") or filing_id
        r2_key = (
            f"raw/{source_slug}/{date_parts[0]}/{date_parts[1]}/"
            f"{date_parts[2]}/{external_id}.{file_ext}"
        )
        r2.upload(r2_key, content, _CONTENT_TYPES.get(file_ext, "application/octet-stream"))
        await update_filing_r2_key(filing_id, r2_key)
        logger.info("Materialized R2 for %s → %s", filing_id, r2_key)

    # Sonnet extraction — system block is cached; user message (filing text) is not.
    extract_raw = await llm_extract(
        _extract_system_for_doc_type(doc_type, source_slug),
        f"Document type: {doc_type}\n\n{text[:40_000]}",
        filing_id=filing_id,
    )
    try:
        extracted = _parse_json(extract_raw)
    except Exception:
        logger.warning("Failed to parse extraction JSON for %s: %.200s", filing_id, extract_raw)
        extracted = {"raw": extract_raw[:2_000]}

    # CAISO post-extraction: write CPUC proceeding cross-refs into filing_dockets.
    if source_slug == "caiso" and source_id:
        await _write_cpuc_cross_refs(filing_id, source_id, extracted)

    extraction_id = await insert_extraction(
        filing_id=filing_id,
        schema_ver=SCHEMA_VER,
        model=SONNET_MODEL,
        prompt_ver=PROMPT_VER,
        payload=extracted,
        haiku_verdict=haiku_verdict,
        haiku_model=HAIKU_MODEL,
    )

    return {"filing_id": filing_id, "extraction_id": extraction_id, "verdict": haiku_verdict}


async def _fetch_source_url(url: str) -> bytes:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={"User-Agent": "NodalPulse/1.0 regulatory-monitor"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _write_cpuc_cross_refs(filing_id: str, source_id: str, extracted: dict) -> None:
    refs = extracted.get("cpuc_proceeding_refs") or []
    if not refs:
        return
    docket_ids: list[str] = []
    for ref in refs:
        try:
            docket_id = await find_or_create_docket(source_id, str(ref), jurisdiction="CPUC")
            docket_ids.append(docket_id)
        except Exception as exc:
            logger.warning("Failed to create CPUC docket ref %s: %s", ref, exc)
    if docket_ids:
        # first_is_primary=False — primary docket was set at crawl time from the FERC caption
        await upsert_filing_dockets(filing_id, docket_ids, first_is_primary=False)
        logger.info("Wrote %d CPUC cross-ref(s) for filing %s", len(docket_ids), filing_id)


# ── text extraction helpers ───────────────────────────────────────────────────

def _extract_text(content: bytes, file_ext: str) -> str:
    if file_ext == "pdf":
        return _pdf_text(content)
    if file_ext in ("html", "htm"):
        return HTMLParser(content.decode("utf-8", errors="replace")).text()[:60_000]
    if file_ext == "docx":
        return _docx_text(content)
    return content.decode("utf-8", errors="replace")[:50_000]


def _pdf_text(content: bytes) -> str:
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            parts = []
            for page in pdf.pages[:40]:
                t = page.extract_text() or ""
                parts.append(t)
            return "\n\n".join(parts)[:60_000]
    except Exception:
        logger.warning("pdfplumber failed, returning empty string")
        return ""


def _docx_text(content: bytes) -> str:
    try:
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with zipfile.ZipFile(BytesIO(content)) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
        texts = [node.text for node in tree.findall(".//w:t", ns) if node.text]
        return " ".join(texts)[:60_000]
    except Exception:
        return ""


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        text = m.group(1).strip()
    return json.loads(text)
