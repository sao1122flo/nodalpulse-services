"""Scrape PUCT Interchange for commenter contact info on specified dockets.

Produces a CSV of leads from "Comments" and "Reply Comments" filings.
Party/org is pulled from the HTML filing table; email and phone are extracted
from the first 3 pages of each comment PDF via pdfplumber regex.

Usage:
    uv run python scripts/scrape_puct_commenters.py --dockets 59475,58923,59336
    uv run python scripts/scrape_puct_commenters.py --dockets-file dockets.txt
    uv run python scripts/scrape_puct_commenters.py --dockets 59475 --out leads.csv

IMPORTANT — network requirement:
    interchange.puc.texas.gov is behind Cloudflare and blocks requests from most
    residential/VPN IPs. The script must run from an IP Cloudflare allows — in
    practice, Railway's production infrastructure (the same servers that run the
    nightly PUCT crawler). Run it as a one-off command inside an existing service
    via Railway's dashboard Shell tab, or deploy it as a Railway job.

    Local dev machines will receive HTTP 403 even from a real browser engine.

Dedup: on re-run against the same --out file, existing (org, email) pairs are
loaded first so no row is written twice.

No LLM calls. No anthropic import. Pure httpx + selectolax + pdfplumber.
"""

import argparse
import csv
import io
import re
import sys
import time
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pdfplumber
from selectolax.parser import HTMLParser

# ── config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://interchange.puc.texas.gov"
FILINGS_URL = f"{BASE_URL}/search/filings/"
DOCUMENTS_URL = f"{BASE_URL}/search/documents/"

USER_AGENT = "NodalPulse/1.0 regulatory-leads (contact: contact@nodalpulse.com)"
REQUEST_DELAY = 1.1  # seconds — keeps rate ≤1 req/s with network latency

# ASP.NET site requires a session cookie; establish it by hitting the root first.
WARMUP_URL = f"{BASE_URL}/search/search/"

CSV_COLUMNS = [
    "commenter_name", "organization", "email", "phone",
    "control_number", "filing_date", "filing_url", "role_guess",
]

# ── comment item filter (Fix 3) ───────────────────────────────────────────────

_INCLUDE = re.compile(r"(?i)\b(?:comments?|reply\s+comments?)\b")
_EXCLUDE = re.compile(
    r"(?i)\b(?:notice\s+of\s+appearance|intervention|motion\s+to|"
    r"protective\s+order|certificate\s+of\s+service)\b"
)

# ── contact regex (Fix 1) ─────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b")

# ── role heuristic (lawyer-first ordering) ────────────────────────────────────

_ROLE_RULES: list[tuple[re.Pattern, str]] = [
    # lawyer first — catches "Baker Botts (counsel for NextEra)"
    (re.compile(
        r"(?i)\b(?:law|LLP|L\.L\.P|attorney|counsel|Bracewell|Vinson|"
        r"Baker\s*Botts|McGinnis|Jackson\s*Walker|Lloyd\s*Gosselink|"
        r"Husch|Scott\s*Douglass|Locke\s*Lord|Winstead|Munsch|"
        r"Graves|Dougherty|Glasscock|Lloyd\s*Gosselink|lglawfirm)\b"
    ), "lawyer"),
    (re.compile(
        r"(?i)\b(?:consulting|consultants|advisors|advisory|Navigant|ICF|"
        r"Analysis\s*Group|Potomac\s*Economics|Wood\s*Mac|NERA|Guidehouse|"
        r"Power\s*Advocates|Astrapé|Silverstein)\b"
    ), "consultant"),
    (re.compile(
        r"(?i)\b(?:REP|retail\s+electric|Constellation|Gexa|Chariot|Amigo|"
        r"Cirro|Tomorrow\s+Energy|4Change|Spark\s+Energy|Veteran|"
        r"Spring\s+Power|TXU|Reliant|Green\s+Mountain|APG&E|Pulse\s+Power)\b"
    ), "REP-compliance"),
    (re.compile(
        r"(?i)\b(?:generation|generator|Luminant|Calpine|Vistra|NextEra|"
        r"Enel|EDF|Recurrent|AES|NTE|Panda|Ørsted|Orsted|Invenergy|"
        r"Apex|EDP|RWE|Savion|Capital\s*Power|Exelon|GE\s*Renewable|"
        r"Siemens\s*Gamesa|Vestas|National\s*Grid\s*Renewables|"
        r"Key\s*Capture|wind|solar|storage|BESS|battery|renewables)\b"
    ), "generator"),
    (re.compile(
        r"(?i)\b(?:Oncor|CenterPoint|AEP|TNMP|SWEPCO|NRG|LCRA|Sharyland|"
        r"Enbridge|South\s*Texas\s*Electric|GEUS|transmission|"
        r"distribution|utilities|utility|cooperative|co-op|coop|"
        r"municipal|city\s+of)\b"
    ), "wires-or-fuel"),
    (re.compile(
        r"(?i)\b(?:ERCOT|OPUC|TIEC|TCPA|AARP|Public\s*Citizen|"
        r"Texas\s*Solar\s*Power|Texas\s*Electric\s*Cooperatives|"
        r"Advanced\s*Power\s*Alliance|American\s*Clean\s*Power|"
        r"Conservative\s*Texans|Texas\s*Advanced\s*Energy|"
        r"Steering\s*Committee|Cities\s*Served)\b"
    ), "advocate"),
]


def _guess_role(org: str) -> str:
    for pattern, role in _ROLE_RULES:
        if pattern.search(org):
            return role
    return "other"


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _cell_text(cell) -> str:
    raw = cell.text(strip=True).replace("\xa0", " ")
    return re.sub(r"\s+", " ", raw).strip()


def _parse_filings_page(html: str, control_number: str) -> list[dict]:
    """Parse /search/filings/ → comment items matching INCLUDE/EXCLUDE filters."""
    tree = HTMLParser(html)
    table = tree.css_first("table")
    if not table:
        print(f"  [{control_number}] filings table not found in HTML", file=sys.stderr)
        return []
    results = []
    for tr in table.css("tr")[1:]:
        cells = tr.css("td")
        if len(cells) < 4:
            continue
        item_number  = _cell_text(cells[0])
        filing_date  = _cell_text(cells[1])
        party_org    = _cell_text(cells[2])
        item_type    = _cell_text(cells[3])
        description  = _cell_text(cells[4]) if len(cells) > 4 else ""

        combined = f"{item_type} {description}"
        if not _INCLUDE.search(combined):
            continue
        if _EXCLUDE.search(combined):
            continue
        if not item_number:
            continue

        results.append({
            "control_number": control_number,
            "item_number":    item_number,
            "filing_date":    filing_date,
            "organization":   party_org,
        })
    return results


def _parse_document_urls(html: str) -> list[str]:
    """Parse /search/documents/ → absolute PDF URLs."""
    tree = HTMLParser(html)
    table = tree.css_first("table")
    if not table:
        return []
    urls = []
    for a in table.css("a[href]"):
        href = a.attrs.get("href", "")
        if not href:
            continue
        if href.upper().endswith(".PDF") or "/Documents/" in href:
            url = href if href.startswith("http") else urljoin(BASE_URL, href)
            urls.append(url)
    return urls


# ── PDF contact extraction (Fix 1 + Fix 2) ───────────────────────────────────

def _extract_contact(pdf_bytes: bytes) -> tuple[str, str]:
    """Return (email, phone) from the first 3 pages of a PDF.

    Text is whitespace-collapsed before regex so line breaks don't split tokens.
    Returns empty string for each field if not found.
    """
    email = phone = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:3]:  # cap at page 0–2 (Fix 2)
                raw = page.extract_text() or ""
                flat = re.sub(r"\s+", " ", raw)  # collapse whitespace (Fix 1)
                if not email:
                    m = _EMAIL_RE.search(flat)
                    if m:
                        email = m.group(0)
                if not phone:
                    m = _PHONE_RE.search(flat)
                    if m:
                        phone = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
                if email and phone:
                    break
    except Exception as exc:
        print(f"    PDF parse error: {exc}", file=sys.stderr)
    return email, phone


# ── ZIP-aware PDF fetcher ─────────────────────────────────────────────────────

def _get_pdf_bytes(client: httpx.Client, url: str) -> bytes:
    """Download URL; if it's a ZIP, extract the first PDF inside and return its bytes."""
    resp = client.get(url)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)

    if url.upper().endswith(".ZIP"):
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            pdf_names = [n for n in zf.namelist() if n.upper().endswith(".PDF")]
            if not pdf_names:
                raise ValueError(f"no PDF inside ZIP: {url}")
            return zf.read(pdf_names[0])
    return resp.content


# ── per-item processing (Fix 4: isolated try/except) ─────────────────────────

def _process_item(
    client: httpx.Client,
    item: dict,
    seen: set,
) -> list[dict]:
    """Fetch documents page + PDF for one comment item. Returns 0 or 1 lead rows."""
    cn   = item["control_number"]
    inum = item["item_number"]

    print(f"  [{cn}/{inum}] documents …")
    resp = client.get(DOCUMENTS_URL, params={"controlNumber": cn, "itemNumber": inum})
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)

    pdf_urls = _parse_document_urls(resp.text)
    if not pdf_urls:
        print(f"  [{cn}/{inum}] no PDF — skipping", file=sys.stderr)
        return []

    filing_url = pdf_urls[0]
    print(f"  [{cn}/{inum}] {filing_url.split('/')[-1]} …")
    pdf_bytes = _get_pdf_bytes(client, filing_url)
    email, phone = _extract_contact(pdf_bytes)

    org = item["organization"]

    # Dedup on (org, email) — name blank in v1
    # TODO Phase 22: extract commenter_name from "By: NAME, TITLE" PDF pattern;
    # v2 can add Apollo enrichment off the email.
    dedup_key = (org.lower(), email.lower())
    if dedup_key in seen:
        print(f"  [{cn}/{inum}] duplicate ({org}) — skipping")
        return []
    seen.add(dedup_key)

    return [{
        "commenter_name": "",
        "organization":   org,
        "email":          email,
        "phone":          phone,
        "control_number": cn,
        "filing_date":    item["filing_date"],
        "filing_url":     filing_url,
        "role_guess":     _guess_role(org),
    }]


def _scrape_docket(client: httpx.Client, control_number: str, seen: set) -> list[dict]:
    """Scrape comment filings for one docket. Returns new lead rows."""
    print(f"\n[{control_number}] fetching filings …")
    resp = client.get(FILINGS_URL, params={"ControlNumber": control_number})
    if resp.status_code == 403:
        print(
            "ERROR: HTTP 403 from interchange.puc.texas.gov.\n"
            "The site is behind Cloudflare and blocks this IP.\n"
            "Run the script from Railway's production infrastructure — see docstring.",
            file=sys.stderr,
        )
        sys.exit(1)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)

    comment_items = _parse_filings_page(resp.text, control_number)
    if not comment_items:
        print(f"[{control_number}] no comment items found")
        return []
    print(f"[{control_number}] {len(comment_items)} comment item(s) to process")

    leads: list[dict] = []
    for item in comment_items:
        try:
            leads.extend(_process_item(client, item, seen))
        except Exception as exc:
            print(
                f"  skip {item['control_number']}/{item['item_number']}: {exc}",
                file=sys.stderr,
            )
            continue

    print(f"[{control_number}] {len(leads)} new lead(s)")
    return leads


# ── CSV I/O ───────────────────────────────────────────────────────────────────

def _load_existing(path: Path) -> tuple[list[dict], set]:
    """Load prior run's CSV; return (rows, dedup_keys)."""
    if not path.exists():
        return [], set()
    rows: list[dict] = []
    seen: set = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
            seen.add((row.get("organization", "").lower(), row.get("email", "").lower()))
    print(f"Loaded {len(rows)} existing rows from {path}")
    return rows, seen


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dockets",
        metavar="CN[,CN...]",
        help="Comma-separated PUCT control numbers",
    )
    group.add_argument(
        "--dockets-file",
        metavar="FILE",
        help="Text file with one control number per line (# = comment)",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="Output CSV path (default: out/leads/YYYY-MM-DD.csv)",
    )
    args = parser.parse_args()

    if args.dockets:
        control_numbers = [cn.strip() for cn in args.dockets.split(",") if cn.strip()]
    else:
        lines = Path(args.dockets_file).read_text(encoding="utf-8").splitlines()
        control_numbers = [
            ln.strip() for ln in lines
            if ln.strip() and not ln.startswith("#")
        ]

    if not control_numbers:
        print("No control numbers provided.", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else Path(f"out/leads/{date.today().isoformat()}.csv")
    existing_rows, seen = _load_existing(out_path)

    with httpx.Client(
        follow_redirects=True,
        timeout=30,
        verify=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    ) as client:
        # Warm up the ASP.NET session — /search/filings/ returns 403 without a session cookie
        print("Establishing session …")
        client.get(WARMUP_URL)
        time.sleep(REQUEST_DELAY)

        new_rows: list[dict] = []
        for cn in control_numbers:
            new_rows.extend(_scrape_docket(client, cn, seen))

    all_rows = existing_rows + new_rows
    _write_csv(out_path, all_rows)
    print(f"\n{'='*60}")
    print(f"New leads:   {len(new_rows)}")
    print(f"Total leads: {len(all_rows)}")
    print(f"Output:      {out_path}")


if __name__ == "__main__":
    main()
