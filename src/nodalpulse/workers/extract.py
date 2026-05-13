"""Job handler for extract queue jobs."""

import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO

import pdfplumber
from selectolax.parser import HTMLParser

from nodalpulse.db.extractions import get_filing, insert_extraction
from nodalpulse.llm.client import classify
from nodalpulse.llm.client import extract as llm_extract
from nodalpulse.storage import r2

logger = logging.getLogger(__name__)

SCHEMA_VER = "1.0"
PROMPT_VER = "1.0"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

_TRIAGE_SYSTEM = """\
You are a document relevance classifier for Texas electricity market regulation.

Classify the document as:
- "relevant": directly concerns electricity generation, transmission, distribution,
  rates, wholesale markets, or Texas grid/ERCOT operations
- "irrelevant": unrelated to electricity markets (e.g. telecom, water, gas pipelines only)
- "uncertain": could be relevant but unclear from the text alone

Respond with JSON only: {"verdict": "relevant"|"irrelevant"|"uncertain", "reason": "<one sentence>"}\
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
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}]
}\
"""

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
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}]
}\
"""

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
  "deadlines": [{"description": "...", "date": "<ISO date or null>"}]
}\
"""


def _extract_system_for_doc_type(doc_type: str) -> str:
    if doc_type == "ercot-mn":
        return _EXTRACT_SYSTEM_ERCOT_MN
    if doc_type.startswith("ercot-"):
        return _EXTRACT_SYSTEM_ERCOT_NPRR
    return _EXTRACT_SYSTEM_PUCT


async def handle_extract(payload: dict) -> dict:
    filing_id = payload["filing_id"]
    r2_key = payload["r2_key"]
    doc_type = payload.get("doc_type", "puct-filing")

    filing = await get_filing(filing_id)
    if not filing:
        raise RuntimeError(f"Filing {filing_id} not found")

    content = r2.download(r2_key)
    file_ext = r2_key.rsplit(".", 1)[-1].lower()
    text = _extract_text(content, file_ext)

    if not text.strip():
        logger.warning("No text extracted from %s (%s)", filing_id, r2_key)
        return {"filing_id": filing_id, "skipped": True, "reason": "no_text"}

    # Haiku triage — fast/cheap pass before running Sonnet
    triage_raw = await classify(_TRIAGE_SYSTEM, f"Document type: {doc_type}\n\n{text[:8_000]}", filing_id=filing_id)
    try:
        haiku_verdict = _parse_json(triage_raw).get("verdict", "uncertain")
    except Exception:
        haiku_verdict = "uncertain"

    logger.info("Filing %s verdict=%s", filing_id, haiku_verdict)

    if haiku_verdict == "irrelevant":
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

    # Sonnet extraction for relevant/uncertain
    extract_raw = await llm_extract(
        _extract_system_for_doc_type(doc_type),
        f"Document type: {doc_type}\n\n{text[:40_000]}",
        filing_id=filing_id,
    )
    try:
        extracted = _parse_json(extract_raw)
    except Exception:
        logger.warning("Failed to parse extraction JSON for %s: %.200s", filing_id, extract_raw)
        extracted = {"raw": extract_raw[:2_000]}

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
