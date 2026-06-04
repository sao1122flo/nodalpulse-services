"""Job handler for extract queue jobs."""

import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
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
PROMPT_VER = "1.4"  # PJM extraction prompt (rpm_parameters/rtep/sector_vote); no Texas taxonomy for PJM/IMM
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

_CONTENT_TYPES: dict[str, str] = {
    "pdf":  "application/pdf",
    "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt":  "text/plain",
}

_TRIAGE_SYSTEM = """\
You are a document relevance classifier for US electricity market regulation.

Classify the document as:
- "relevant": directly concerns electricity generation, transmission, distribution,
  rates, wholesale markets, or grid operations in any US jurisdiction, including:
    ERCOT / PUCT (Texas), CAISO / CPUC (California),
    PJM and FERC-jurisdictional wholesale electricity proceedings.
- "irrelevant": unrelated to electricity markets — e.g. telecommunications,
  water utilities, or natural gas pipelines with no electricity component.
- "uncertain": electricity relevance is plausible but unclear from the available text.

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

_EXTRACT_SYSTEM_PJM = """\
You are an expert analyst of PJM Interconnection regulatory filings at FERC (Federal Energy
Regulatory Commission), including filings by PJM itself, the PJM Independent Market Monitor
(IMM / Monitoring Analytics), load-serving entities, generators, transmission owners, and
intervenors across PJM's footprint (PA, NJ, MD, DE, OH, MI, IL, IN, VA, WV, NC, KY, DC).

Extract structured information from the document. Respond with JSON only, no markdown fences:
{
  "summary": "<2-3 sentence plain-language summary>",
  "key_points": ["<point>", ...],
  "parties": ["<party name>", ...],
  "docket_number": "<primary FERC docket ID, e.g. ER25-1357, or null>",
  "relief_requested": "<what the filer is requesting from FERC, or null>",
  "outcome": "<if this is a FERC order: the disposition, or null>",
  "effective_date": "<ISO date if mentioned as proposed or ordered effective date, or null>",
  "deadlines": [{"type": "stakeholder_comment|compliance|hearing|other", "description": "...", "date": "<ISO date or null>", "source": "filing", "estimated": false, "verify_url": null}],
  "dollar_impacts": [{"type": "<price_cap|price_floor|clearing_price|cost_allocation|penalty|other>", "unit": "<$/MW-day|$/MWh|$|other>", "value": <number or null>, "description": "<context>"}],
  "rpm_parameters": {
    "price_cap_ucap_mwday": <number or null>,
    "price_floor_ucap_mwday": <number or null>,
    "clearing_price_ucap_mwday": <number or null>,
    "delivery_years": ["<e.g. 2026/2027>", ...],
    "mw_procured": <number or null>,
    "reserve_margin_pct": <number or null>,
    "capacity_basis": "<UCAP|ICAP|null>"
  },
  "rtep_cost_allocation": [{"zone": "<zone name>", "dollars": <number or null>}],
  "sector_vote": {
    "committee": "<MRC|MC|null>",
    "result": "<approved|rejected|deferred|null>",
    "transmission_owners": {"support": <int>, "oppose": <int>, "abstain": <int>},
    "electric_distributors": {"support": <int>, "oppose": <int>, "abstain": <int>},
    "generation_owners": {"support": <int>, "oppose": <int>, "abstain": <int>},
    "other_suppliers": {"support": <int>, "oppose": <int>, "abstain": <int>},
    "end_use_customers": {"support": <int>, "oppose": <int>, "abstain": <int>}
  },
  "role_tags": []
}

rpm_parameters — populate ONLY for RPM / BRA / capacity auction filings. Set null otherwise.
  - Prices are per UCAP MW-day unless the document explicitly says ICAP. Set capacity_basis
    accordingly. CRITICAL: UCAP (Unforced Capacity) and ICAP (Installed Capacity) have
    different numeric values — do not conflate them. When uncertain, set capacity_basis null
    and note the ambiguity in key_points.
  - Price caps, floors, and clearing prices typically appear in TABLES or attachments, not
    in the narrative. Scan all tables before concluding a value is absent.
  - Few-shot: ER25-1357 (RPM cap/floor) → price_cap_ucap_mwday: 329.17,
    price_floor_ucap_mwday: 177.24, delivery_years: ["2026/2027","2027/2028"], basis: "UCAP".
  - EL25-49 (co-located load complaint) → rpm_parameters: null.

rtep_cost_allocation — populate ONLY for RTEP transmission planning or Schedule 12 cost
allocation filings. Set null otherwise.
  - Zone-by-zone dollar responsibility (e.g. PSEG, PPL, AEP, Dominion). Values are often
    in millions of dollars in tables. Return empty array [] if the filing discusses RTEP
    but provides no zone-level dollar splits.

sector_vote — populate ONLY when a PJM stakeholder committee vote is described.
  - PJM's five sectors: Transmission Owners, Electric Distributors, Generation Owners,
    Other Suppliers, End-Use Customers. Approval requires 2/3 supermajority at MRC and MC.
  - Set null if no vote is described.

role_tags: subset of market roles most likely to care about this filing.
Use only values from: "Regulatory Analyst", "Compliance Officer", "Energy Lawyer",
"BESS Regulatory Lead", "Trader / Risk Manager", "Consultant / Advisory",
"Utility / Co-op Staff", "Developer / IPP".
Empty array means relevant to all roles.

=== PJM ELECTRICITY REGULATORY REFERENCE ===

PJM MARKET STRUCTURE

PJM Interconnection, L.L.C. is the FERC-regulated Regional Transmission Organization (RTO)
operating the wholesale electricity market and transmission system serving 13 states and DC:
Pennsylvania, New Jersey, Maryland, Delaware, Ohio, Michigan, Illinois, Indiana, Virginia,
West Virginia, North Carolina, Kentucky, and the District of Columbia (~65 million people).
Unlike ERCOT (Texas), PJM is FERC-jurisdictional: all tariff changes, capacity market rules,
and transmission cost allocations are filed at and approved by FERC.

CAPACITY MARKET — RPM (Reliability Pricing Model)

The Base Residual Auction (BRA) clears capacity 3 years ahead of the delivery year. The
Independent Market Monitor (IMM) and FERC scrutinize the Variable Resource Requirement
(VRR) demand curve, price caps (Capacity Performance CP Net CONE) and floors. Prices are
expressed in $/MW-day on a UCAP (Unforced Capacity) basis. ICAP (Installed Capacity) is
sometimes referenced in older documents; they differ by the Equivalent Forced Outage Rate
(EFORd) deration factor. The 2024/25 BRA cleared at ~$329/MW-day CP, the highest in PJM
history, primarily driven by data-center load growth in the zone. RPM auction rules are set
by PJM's Reliability Assurance Agreement (RAA) and Open Access Transmission Tariff (OATT).

TRANSMISSION PLANNING — RTEP (Regional Transmission Expansion Plan)

RTEP is PJM's annual transmission planning process. Cost allocation for transmission projects
follows Schedule 12 of the OATT. Zone-by-zone cost responsibility is a frequent contested
issue; the key dockets are ER24-2236 and ER24-2238 (RTEP protocol revisions, 2024).

STAKEHOLDER PROCESS — Manual 34

PJM's stakeholder process is the most formal of any US RTO. Proposals advance through
subcommittees to the Markets & Reliability Committee (MRC) and Members Committee (MC),
each requiring a 2/3 supermajority across five weighted voting sectors. Votes are public
and filed at FERC. Sector positions are evidence of market consensus or controversy.

INDEPENDENT MARKET MONITOR (IMM)

The IMM (Monitoring Analytics, LLC) is the independent market monitor for PJM. It files
complaints, answers, and annual State of the Market reports at FERC. IMM complaints (§206
complaints under the Federal Power Act) are high-signal filings that often drive major
market rule changes. The data-center / co-located load complaint (docket EL26-XX, later
renumbered) is the marquee active matter as of 2026.

KEY DOCKET TYPES

ER (Rates) dockets: Tariff amendments, compliance filings, RPM parameter changes.
EL (Electric) dockets: Complaints under FPA §206; capacity market disputes.
RM (Rulemaking) dockets: FERC-initiated rulemakings affecting PJM markets.
Protest/comment windows: Set by the FERC Notice (separate document), not the filing itself.

=== END PJM ELECTRICITY REGULATORY REFERENCE ===\
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
  "deadlines": [{"type": "stakeholder_comment|hearing|other", "description": "...", "date": "<ISO date or null>", "source": "filing", "estimated": false, "verify_url": null}],
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
    # PJM/IMM: standalone prompt with embedded PJM reference — no Texas taxonomy.
    # The prompt exceeds 1024 tokens so cache_control: ephemeral engages normally.
    if source_slug in {"pjm", "imm"}:
        return _EXTRACT_SYSTEM_PJM
    if source_slug == "caiso":
        base = _EXTRACT_SYSTEM_CAISO
    elif doc_type == "ercot-mn":
        base = _EXTRACT_SYSTEM_ERCOT_MN
    elif doc_type.startswith("ercot-"):
        base = _EXTRACT_SYSTEM_ERCOT_NPRR
    else:
        base = _EXTRACT_SYSTEM_PUCT
    return base + "\n\n" + TEXAS_ELECTRICITY_TAXONOMY


_FERC_ELIBRARY_SEARCH = "https://elibrary.ferc.gov/eLibrary/search?q={docket}"

# Sources that file exclusively with FERC — protest/comment window applies.
_FERC_FAMILY_SOURCES = {"caiso", "pjm", "ferc", "imm"}


def _enrich_deadlines(
    extracted: dict,
    doc_type: str,
    filed_at: str,
    source_slug: str,
) -> dict:
    """Post-process extraction payload to add computed deadlines (scope B).

    Adds deterministic deadline entries AFTER LLM extraction:
      - rehearing: 30d from order date (FPA §313) when doc_type='ferc-order'
      - effective_date: wraps the top-level field as a structured deadline entry
      - protest_notice: non-date entry with eLibrary verify_url for FERC-family filings

    Existing LLM-extracted deadlines are preserved. All entries get type/source/
    estimated/verify_url fields if missing (normalises old {description, date} shape).

    This function is idempotent: checks for existing type before inserting.
    """
    deadlines: list[dict] = []

    # Normalise LLM-extracted deadline entries (may be old {description, date} shape).
    # Force estimated=True regardless of what the LLM claims: a date mentioned in
    # filing prose can be (a) this filing's deadline, (b) another proceeding's
    # deadline, or (c) a historical reference. The LLM cannot reliably distinguish
    # them. Marking estimated=True prevents these from driving the brief's +60
    # urgency score (scope B: only surface deadlines we are certain of).
    # Phase 2 will replace these with authoritative dates from the CAISO initiative page.
    for dl in (extracted.get("deadlines") or []):
        if not isinstance(dl, dict):
            continue
        deadlines.append({
            "type":        dl.get("type", "other"),
            "description": dl.get("description", ""),
            "date":        dl.get("date"),
            "source":      dl.get("source", "filing"),
            "estimated":   True,   # always — LLM date attribution is not certifiable
            "verify_url":  dl.get("verify_url"),
        })

    existing_types = {d["type"] for d in deadlines}

    # Wrap effective_date into the deadlines array so scoring/rendering is uniform
    eff = extracted.get("effective_date")
    if eff and "effective_date" not in existing_types:
        deadlines.append({
            "type":        "effective_date",
            "description": "Proposed effective date",
            "date":        eff,
            "source":      "filing",
            "estimated":   False,
            "verify_url":  None,
        })

    # Rehearing — 30 days from FERC order date (FPA §313).
    # Anchors to the order, not a party filing. Not every FERC order starts this
    # clock (procedural orders are not final dispositions), but we surface it and
    # let beta users flag false positives during the reliability window.
    if doc_type == "ferc-order" and filed_at and "rehearing" not in existing_types:
        try:
            order_date = date.fromisoformat(filed_at[:10])
            rehearing_date = order_date + timedelta(days=30)
            deadlines.append({
                "type":        "rehearing",
                "description": "Rehearing request deadline (FPA §313 — 30 days from order)",
                "date":        rehearing_date.isoformat(),
                "source":      "order",
                "estimated":   False,
                "verify_url":  None,
            })
        except ValueError:
            logger.warning("_enrich_deadlines: unparseable filed_at %r for rehearing", filed_at)

    # Protest notice — never compute the window; surface the FERC Notice link.
    # expedited proceedings have shorter windows than any default, so a guessed
    # estimate fails exactly in the urgent cases (scope B hard rule).
    if source_slug in _FERC_FAMILY_SOURCES and "protest_notice" not in existing_types:
        docket = extracted.get("docket_number") or ""
        verify_url = _FERC_ELIBRARY_SEARCH.format(docket=docket) if docket else None
        deadlines.append({
            "type":        "protest_notice",
            "description": "Protest/comment deadline — window varies by proceeding type; see FERC Notice",
            "date":        None,
            "source":      "order",
            "estimated":   False,
            "verify_url":  verify_url,
        })

    extracted["deadlines"] = deadlines
    return extracted


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

    import json as _json
    _meta: dict = _json.loads(filing.get("metadata_json") or "{}")
    ferc_file_id: str = _meta.get("ferc_file_id", "")

    # Fetch bytes — priority: R2 (already uploaded) → source_url → FERC DownloadP8File.
    # Bandwidth-only until triage; R2 Class A write is deferred until after triage passes.
    if r2_key:
        content = r2.download(r2_key)
    elif source_url:
        try:
            content = await _fetch_source_url(source_url)
        except Exception as exc:
            if ferc_file_id:
                logger.warning("Filing %s: source_url fetch failed (%s), falling back to DownloadP8File", filing_id, exc)
                content = await _fetch_ferc_p8file(ferc_file_id)
            else:
                raise
    elif ferc_file_id:
        logger.info("Filing %s: fetching PDF via FERC DownloadP8File fileId=%s", filing_id, ferc_file_id)
        content = await _fetch_ferc_p8file(ferc_file_id)
        if not content:
            logger.warning("Filing %s: DownloadP8File returned empty (auth error or non-PDF) — skipping", filing_id)
            return {"filing_id": filing_id, "skipped": True, "reason": "ferc_p8file_unavailable"}
    else:
        logger.warning("Filing %s has no r2_key, no source_url, no ferc_file_id — skipping", filing_id)
        return {"filing_id": filing_id, "skipped": True, "reason": "no_content_source"}

    text = _extract_text(content, file_ext)
    if not text.strip():
        logger.warning("No text extracted from %s", filing_id)
        return {"filing_id": filing_id, "skipped": True, "reason": "no_text"}

    # Haiku triage — cheap pass before any R2 write or Sonnet call.
    # CAISO and IMM skip triage: both are curated/high-signal corpora where
    # every filing is electricity-relevant by definition.
    # - CAISO: operator-curated HTML index; Texas-focused triage prompt produces false negatives.
    # - IMM: <20 filings/year, 100% FERC electricity (complaints, briefs, SoM reports).
    # PJM uses a firehose-discovered set — a broader surface that Haiku filters first.
    _TRIAGE_SKIP_SOURCES = {"caiso", "imm"}
    if source_slug in _TRIAGE_SKIP_SOURCES:
        haiku_verdict = "relevant"
        logger.info("Filing %s triage skipped (source=%s — curated/high-signal)", filing_id, source_slug)
    else:
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

    # Deadline engine — compute/inject structured deadline entries (scope B).
    extracted = _enrich_deadlines(extracted, doc_type, filing.get("filed_at") or "", source_slug)

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


_FERC_ELIBRARY_BASE = "https://elibrary.ferc.gov"
_FERC_P8FILE_URL = f"{_FERC_ELIBRARY_BASE}/eLibrarywebapi/api/File/DownloadP8File"
_FERC_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Origin": _FERC_ELIBRARY_BASE,
    "Referer": f"{_FERC_ELIBRARY_BASE}/eLibrary/",
}

import json as _json_mod


async def _fetch_ferc_p8file(file_id: str) -> bytes:
    """Download a FERC PDF via File/DownloadP8File (FileNet P8 CMS).

    Requires a two-step flow: GET /eLibrary/ to get session cookies (F5 load-balancer
    + TS security token), then POST DownloadP8File with {"fileidLst": [file_id]}.
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60,
        headers=_FERC_BROWSER_HEADERS,
    ) as client:
        # Step 1: get session cookies
        await client.get(f"{_FERC_ELIBRARY_BASE}/eLibrary/")
        # Step 2: download PDF bytes
        resp = await client.post(
            _FERC_P8FILE_URL,
            content=_json_mod.dumps({"fileidLst": [file_id]}),
        )
        if resp.status_code in (401, 403):
            # Some FERC filings have access restrictions; skip gracefully
            logger.warning("DownloadP8File auth error %d for fileId=%s — skipping", resp.status_code, file_id)
            return b""
        resp.raise_for_status()
        if not resp.content or resp.content[:4] != b"%PDF":
            # ZIP, HTML, or other non-PDF — log and return empty
            logger.warning(
                "DownloadP8File non-PDF: status=%d len=%d head=%r fileId=%s",
                resp.status_code, len(resp.content), resp.content[:8], file_id,
            )
            return b""
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
