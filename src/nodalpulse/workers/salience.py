"""Market salience ranking + Haiku headlines — #128.

STEP 1  compute_market_salience()
  Pure SQL heuristic. Score: filings_count*3 + distinct_filers*2 + max_doc_weight.
  Gate: only dockets with max_doc_weight >= MIN_DOC_WEIGHT are eligible — removes
  high-volume routine proceedings (Annual Charges, CCN water, etc.) from results.
  Window: full 7-day ISO week (week_start .. week_start+7d). Filings from future
  days don't exist, so scores accumulate naturally as the week progresses.
  UPSERT on (market, week_start, rank) — headline/headline_at preserved.

STEP 2  generate_salience_headlines()
  Haiku generates one sentence per top-3 docket explaining why it's salient.
  Only fires when the top-3 docket_keys change vs what's stored — ~1 set of
  calls per market per week. Logs to llm_calls (pipeline_stage haiku-salience-
  headline).

Corpus:
  FERC           → discovery_feed (broad 30-day FERC metadata, issue #85)
  PUCT/ERCOT     → filings + dockets (daily crawler output)
  CAISO/PJM/CPUC → filings + dockets

doc_type weights (market-intrinsic importance):
  Commission Order / Opinion   80
  Tariff amendment / PFD       60-70
  Protocol revision (ERCOT)    50-60
  Application / Notice         20-40
  Routine filing               10
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from nodalpulse.db.engine import AsyncSessionLocal
from nodalpulse.llm.client import tracked_messages_create
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

HAIKU_MODEL = "claude-haiku-4-5-20251001"

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_DOC_WEIGHT = 20   # gate: docket must have at least one filing of this weight
SURFACE_FLOOR  = 50   # min score to display; below this → "Quiet week" on surfaces
TOP_N = 3
HEADLINE_PROMPT_VER = "1.0"

# ── Retry (mirrors llm/client.py pattern) ────────────────────────────────────

import anthropic as _anthropic


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _anthropic.APIStatusError):
        return exc.status_code == 529 or exc.status_code >= 500
    return isinstance(exc, _anthropic.APIConnectionError)


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=20),
    reraise=True,
)

# ── Market config ─────────────────────────────────────────────────────────────

_MARKET_JURISDICTIONS: dict[str, list[str]] = {
    "PUCT":  ["PUCT"],
    "ERCOT": ["ERCOT"],
    "CAISO": ["CAISO-FERC", "CAISO", "CPUC"],
    "PJM":   ["PJM-FERC", "PJM"],
    "CPUC":  ["CPUC"],
}
_DISCOVERY_MARKETS = {"FERC"}

# ── Doc weight expressions ────────────────────────────────────────────────────

_FERC_DOC_WEIGHT = """
    CASE doc_type
        WHEN 'ferc-order'            THEN 80
        WHEN 'ferc-tariff-amendment' THEN 60
        ELSE 10
    END
"""

_FILING_DOC_WEIGHT = """
    CASE f.doc_type
        WHEN 'ferc-order'            THEN 80
        WHEN 'ferc-tariff-amendment' THEN 60
        WHEN 'puct-order'            THEN 80
        WHEN 'puct-pfd'              THEN 70
        WHEN 'puct-rulemaking'       THEN 65
        WHEN 'puct-open-meeting'     THEN 40
        WHEN 'puct-application'      THEN 25
        WHEN 'puct-compliance'       THEN 15
        WHEN 'puct-response'         THEN 15
        WHEN 'ercot-nprr'            THEN 60
        WHEN 'ercot-pgrr'            THEN 55
        WHEN 'ercot-mprr'            THEN 50
        WHEN 'ercot-mn'              THEN 20
        ELSE 10
    END
"""

# ── Ranking SQL ───────────────────────────────────────────────────────────────

_FERC_SQL = f"""
WITH filing_dockets AS (
    SELECT
        unnest(docket_numbers) AS docket,
        accession,
        doc_type,
        filer_names
    FROM discovery_feed
    WHERE filed_at >= CAST(:week_start AS date)
      AND filed_at <  CAST(:week_end   AS date)
      AND jurisdiction = 'FERC'
      AND cardinality(docket_numbers) > 0
),
with_filers AS (
    SELECT docket, accession, doc_type, unnest(filer_names) AS filer
    FROM filing_dockets WHERE cardinality(filer_names) > 0
    UNION ALL
    SELECT docket, accession, doc_type, NULL::text AS filer
    FROM filing_dockets WHERE cardinality(filer_names) = 0
),
agg AS (
    SELECT
        docket                          AS docket_key,
        NULL::text                      AS docket_title,
        COUNT(DISTINCT accession)       AS filings_count,
        COUNT(DISTINCT filer)           AS distinct_filers,
        MAX({_FERC_DOC_WEIGHT})         AS max_doc_weight
    FROM with_filers
    WHERE docket <> ''
    GROUP BY docket
)
SELECT
    docket_key,
    docket_title,
    filings_count,
    distinct_filers,
    max_doc_weight,
    (filings_count * 3 + distinct_filers * 2 + max_doc_weight)::numeric AS score
FROM agg
WHERE max_doc_weight >= :min_doc_weight
ORDER BY score DESC
LIMIT :top_n
"""

_FILINGS_SQL = f"""
WITH agg AS (
    SELECT
        d.external_id                                     AS docket_key,
        d.title                                           AS docket_title,
        COUNT(f.id)                                       AS filings_count,
        COUNT(DISTINCT NULLIF(COALESCE(f.filer,''), '')) AS distinct_filers,
        MAX({_FILING_DOC_WEIGHT})                         AS max_doc_weight
    FROM filings f
    JOIN dockets d ON d.id = f.docket_id
    WHERE f.filed_at >= CAST(:week_start AS timestamptz)
      AND f.filed_at <  CAST(:week_end   AS timestamptz)
      AND d.jurisdiction = ANY(:jurisdictions)
      AND f.docket_id IS NOT NULL
    GROUP BY d.id, d.external_id, d.title
)
SELECT
    docket_key,
    docket_title,
    filings_count,
    distinct_filers,
    max_doc_weight,
    (filings_count * 3 + distinct_filers * 2 + max_doc_weight)::numeric AS score
FROM agg
WHERE max_doc_weight >= :min_doc_weight
ORDER BY score DESC
LIMIT :top_n
"""

_UPSERT_SQL = """
INSERT INTO market_salience
  (market, week_start, rank, docket_key, docket_title,
   score, filings_count, distinct_filers, max_doc_weight, computed_at)
VALUES
  (:market, CAST(:week_start AS date), :rank, :docket_key, :docket_title,
   :score, :filings_count, :distinct_filers, :max_doc_weight, NOW())
ON CONFLICT (market, week_start, rank) DO UPDATE SET
  docket_key      = EXCLUDED.docket_key,
  docket_title    = EXCLUDED.docket_title,
  score           = EXCLUDED.score,
  filings_count   = EXCLUDED.filings_count,
  distinct_filers = EXCLUDED.distinct_filers,
  max_doc_weight  = EXCLUDED.max_doc_weight,
  computed_at     = NOW()
"""

_HEADLINE_UPDATE_SQL = """
UPDATE market_salience
SET headline = :headline, headline_at = NOW()
WHERE market = :market AND week_start = CAST(:week_start AS date) AND rank = :rank
"""

_STORED_TOP3_SQL = """
SELECT rank, docket_key, headline
FROM market_salience
WHERE market = :market AND week_start = CAST(:week_start AS date)
ORDER BY rank
"""

# ── Context queries for Haiku ─────────────────────────────────────────────────

_FERC_CONTEXT_SQL = """
SELECT description, doc_type
FROM discovery_feed
WHERE :docket_key = ANY(docket_numbers)
  AND filed_at >= CAST(:week_start AS date)
  AND filed_at <  CAST(:week_end   AS date)
ORDER BY filed_at DESC
LIMIT 5
"""

_FILINGS_CONTEXT_SQL = """
SELECT f.title, f.doc_type
FROM filings f
JOIN dockets d ON d.id = f.docket_id
WHERE d.external_id = :docket_key
  AND d.jurisdiction = ANY(:jurisdictions)
  AND f.filed_at >= CAST(:week_start AS timestamptz)
  AND f.filed_at <  CAST(:week_end   AS timestamptz)
ORDER BY f.filed_at DESC
LIMIT 5
"""

# ── Haiku prompt ──────────────────────────────────────────────────────────────

_HEADLINE_SYSTEM = """\
You write one-sentence regulatory intelligence summaries for energy-industry professionals.

Rules:
- Output exactly one sentence, maximum 15 words
- Name the specific regulatory action (order, tariff, rulemaking, revision) and what it affects
- Avoid generic phrases: "significant activity", "high engagement", "regulatory developments"
- Bad: "Multiple parties engaged in FERC docket this week"
- Good: "FERC issues compliance order on Southwest Power Pool reactive power compensation tariff"
- Good: "Commission clears wind developer tariff over protest in Texas interconnection proceeding"
- Output ONLY the sentence — no quotes, no leading label, no trailing period\
"""


def _weight_label(max_doc_weight: int) -> str:
    if max_doc_weight >= 80:
        return "Commission Order / Opinion"
    if max_doc_weight >= 70:
        return "Proposal for Decision"
    if max_doc_weight >= 60:
        return "Tariff Amendment / Protocol Revision"
    if max_doc_weight >= 40:
        return "Significant Filing / Open Meeting"
    return "Active Proceeding"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SalienceEntry:
    rank: int
    docket_key: str
    docket_title: str | None
    score: float
    filings_count: int
    distinct_filers: int
    max_doc_weight: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso_week_start(d: date) -> date:
    """Return the Monday (ISO week start) for a given date."""
    return d - timedelta(days=d.weekday())


async def _stored_top3(market: str, week_start: date) -> dict[int, dict]:
    """Return {rank: {docket_key, headline}} for existing rows. Empty if none."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(_STORED_TOP3_SQL),
            {"market": market, "week_start": week_start},
        )
        return {row[0]: {"docket_key": row[1], "headline": row[2]} for row in result}


async def _ferc_context(docket_key: str, week_start: date, week_end: date) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(_FERC_CONTEXT_SQL),
            {"docket_key": docket_key, "week_start": week_start, "week_end": week_end},
        )
        return [f"[{row[1]}] {row[0][:200]}" for row in result]


async def _filings_context(
    docket_key: str,
    jurisdictions: list[str],
    week_start: date,
    week_end: date,
) -> list[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(_FILINGS_CONTEXT_SQL),
            {
                "docket_key":   docket_key,
                "jurisdictions": jurisdictions,
                "week_start":   week_start,
                "week_end":     week_end,
            },
        )
        return [f"[{row[1]}] {row[0][:200]}" for row in result]


@_retry
async def _generate_headline(
    market: str,
    entry: SalienceEntry,
    context_lines: list[str],
) -> str:
    doc_label = _weight_label(entry.max_doc_weight)
    title_part = f": {entry.docket_title[:80]}" if entry.docket_title else ""
    context_block = (
        "\n".join(f"- {line}" for line in context_lines[:3])
        if context_lines else "(no filing descriptions available)"
    )
    user_msg = (
        f"Market: {market} | Docket: {entry.docket_key}{title_part}\n"
        f"Week activity: {entry.filings_count} filing(s), "
        f"{entry.distinct_filers} distinct party/parties, "
        f"highest filing type: {doc_label}\n"
        f"Filing context:\n{context_block}\n\n"
        "Write one sentence (max 15 words) explaining what's driving this docket this week."
    )
    msg = await tracked_messages_create(
        pipeline_stage="haiku-salience-headline",
        prompt_version=HEADLINE_PROMPT_VER,
        model=HAIKU_MODEL,
        max_tokens=80,
        system=[{"type": "text", "text": _HEADLINE_SYSTEM}],
        messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text.strip().rstrip(".")


# ── STEP 2: headline generation ───────────────────────────────────────────────

async def generate_salience_headlines(
    market: str,
    week_start: date,
    week_end: date,
    entries: list[SalienceEntry],
) -> None:
    """Generate (or skip) Haiku headlines for the top-3 salient dockets.

    Only fires when the ranked docket_keys have changed vs what's stored.
    One Haiku call per winner — 2-3 calls per market per week at most.
    """
    if not entries:
        return

    stored = await _stored_top3(market, week_start)
    new_keys = [e.docket_key for e in entries]
    old_keys = [stored.get(i + 1, {}).get("docket_key") for i in range(len(entries))]
    top3_changed = new_keys != old_keys

    # Also regenerate if any winner has no headline yet
    any_missing = any(
        stored.get(e.rank, {}).get("headline") is None for e in entries
    )

    if not top3_changed and not any_missing:
        logger.info(
            "salience headlines: top-3 unchanged for market=%s week_start=%s — skipping Haiku",
            market, week_start,
        )
        return

    logger.info(
        "salience headlines: generating for market=%s week_start=%s (changed=%s missing=%s)",
        market, week_start, top3_changed, any_missing,
    )

    jurisdictions = _MARKET_JURISDICTIONS.get(market, [])

    async with AsyncSessionLocal() as session:
        for entry in entries:
            try:
                if market in _DISCOVERY_MARKETS:
                    context = await _ferc_context(entry.docket_key, week_start, week_end)
                else:
                    context = await _filings_context(entry.docket_key, jurisdictions, week_start, week_end)

                headline = await _generate_headline(market, entry, context)

                await session.execute(
                    text(_HEADLINE_UPDATE_SQL),
                    {
                        "market":     market,
                        "week_start": week_start,
                        "rank":       entry.rank,
                        "headline":   headline,
                    },
                )
                logger.info(
                    "salience headline: market=%s rank=%d docket=%s | %r",
                    market, entry.rank, entry.docket_key, headline,
                )
            except Exception:
                logger.exception(
                    "salience headline failed for market=%s rank=%d docket=%s — skipping",
                    market, entry.rank, entry.docket_key,
                )
        await session.commit()


# ── STEP 1: ranking ───────────────────────────────────────────────────────────

async def compute_market_salience(
    market: str,
    week_start: date,
    top_n: int = TOP_N,
    min_doc_weight: int = MIN_DOC_WEIGHT,
) -> list[SalienceEntry]:
    """Rank the corpus for *market* over the 7-day ISO week and upsert top_n.

    Returns the ranked entries (rank 1 = highest score).
    Gate: only dockets where max_doc_weight >= min_doc_weight are eligible.
    Pure SQL — zero LLM calls.
    """
    week_end = week_start + timedelta(days=7)

    async with AsyncSessionLocal() as session:
        if market in _DISCOVERY_MARKETS:
            rows = await session.execute(
                text(_FERC_SQL),
                {
                    "week_start":     week_start,
                    "week_end":       week_end,
                    "min_doc_weight": min_doc_weight,
                    "top_n":          top_n,
                },
            )
        else:
            jurisdictions = _MARKET_JURISDICTIONS.get(market)
            if not jurisdictions:
                raise ValueError(f"Unknown market for filings ranking: {market!r}")
            rows = await session.execute(
                text(_FILINGS_SQL),
                {
                    "week_start":     week_start,
                    "week_end":       week_end,
                    "jurisdictions":  jurisdictions,
                    "min_doc_weight": min_doc_weight,
                    "top_n":          top_n,
                },
            )

        results = rows.mappings().fetchall()

    if not results:
        logger.info(
            "salience: no eligible data for market=%s week_start=%s (min_doc_weight=%d)",
            market, week_start, min_doc_weight,
        )
        return []

    entries: list[SalienceEntry] = []
    async with AsyncSessionLocal() as session:
        for rank_idx, row in enumerate(results, start=1):
            entry = SalienceEntry(
                rank=rank_idx,
                docket_key=row["docket_key"],
                docket_title=row["docket_title"],
                score=float(row["score"]),
                filings_count=int(row["filings_count"]),
                distinct_filers=int(row["distinct_filers"]),
                max_doc_weight=int(row["max_doc_weight"]),
            )
            entries.append(entry)
            await session.execute(
                text(_UPSERT_SQL),
                {
                    "market":          market,
                    "week_start":      week_start,
                    "rank":            rank_idx,
                    "docket_key":      entry.docket_key,
                    "docket_title":    entry.docket_title,
                    "score":           entry.score,
                    "filings_count":   entry.filings_count,
                    "distinct_filers": entry.distinct_filers,
                    "max_doc_weight":  entry.max_doc_weight,
                },
            )
        await session.commit()

    logger.info(
        "salience: market=%s week_start=%s — top %d: %s",
        market, week_start, len(entries),
        [(e.rank, e.docket_key, e.score) for e in entries],
    )
    return entries


# ── STEP 3: read cached salience for surfaces ─────────────────────────────────

_FETCH_SALIENCE_SQL = """
SELECT market, rank, docket_key, docket_title, score, headline
FROM market_salience
WHERE market = ANY(:markets)
  AND week_start = CAST(:week_start AS date)
  AND score >= :floor
ORDER BY market, rank
"""


async def get_market_salience(
    markets: list[str],
    week_start: date,
    floor: int = SURFACE_FLOOR,
) -> list[dict]:
    """Return cached salience rows above *floor* for the current week.

    Only rows with score >= floor are returned (below floor = quiet week).
    Rows are sorted by market, rank. Returns [] if market_salience table is
    empty for this week (e.g. first days of a new week before jobs ran).
    """
    if not markets:
        return []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(_FETCH_SALIENCE_SQL),
            {"markets": markets, "week_start": week_start, "floor": floor},
        )
        return [dict(row) for row in result.mappings()]


# ── Job handler ───────────────────────────────────────────────────────────────

async def handle_compute_salience(payload: dict[str, Any]) -> None:
    """Worker handler for 'compute-market-salience' jobs.

    Payload: {"market": "FERC", "week_start": "2026-06-16"}
    Runs ranking (STEP 1) then headline generation (STEP 2) in sequence.
    """
    market = payload["market"]
    week_start = date.fromisoformat(payload["week_start"])
    week_end = week_start + timedelta(days=7)

    entries = await compute_market_salience(market, week_start)
    await generate_salience_headlines(market, week_start, week_end, entries)
