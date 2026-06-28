"""Predicate translator for SavedSearchQuery → PredicateBundle.

Single source of truth for how a user's saved searches and tracked dockets
get translated into SQL-ready filtering parameters. Imported by:
  - nodalpulse.workers.compose_brief  (Prompt 3 — daily brief personalization)
  - nodalpulse.api.app  /saved-searches/fire endpoint  (Prompt 4 — not yet built)

Implementable predicates (Prompt 3):
  - query.markets    → source slug = ANY(...)  via sources.slug
  - query.text       → ILIKE on filings.title + filings.filer  (no tsvector yet)
  - query.docket_ids → f.docket_id FK match for tracked docket UUIDs
  - tracked_tags     → ERCOT zone filer patterns via zone_lookup  (passed in pre-built)

DEFERRED — visible noops logged to stdout for metrics visibility:
  - query.tags       → no filing tag column exists; counted in noop_tag_count
                       Prompt 3.5 (tagger upgrade) will populate filing tags.
  - query.tdu_zones  → handled separately via zone_lookup at call site, not here
  - role filtering   → market_roles passed in for noop_role_count logging only

These deferrals are intentional and tracked. Do not silently remove the counters.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PredicateBundle:
    """Compiled, SQL-ready predicates for one user's brief personalization."""

    # Implementable predicates
    market_slugs: list[str] = field(default_factory=list)
    text_ilike_patterns: list[str] = field(default_factory=list)
    tracked_docket_uuids: list[str] = field(default_factory=list)
    zone_filer_patterns: list[str] = field(default_factory=list)

    # Noop counters — logged for metric visibility, excluded from SQL
    noop_tag_count: int = 0
    noop_role_count: int = 0

    @property
    def has_implementable_predicates(self) -> bool:
        """True if at least one SQL-expressible predicate is set."""
        return bool(
            self.market_slugs
            or self.text_ilike_patterns
            or self.tracked_docket_uuids
            or self.zone_filer_patterns
        )

    def log_noops(self) -> None:
        """Emit info-level counters for deferred predicates. Call once per brief."""
        if self.noop_role_count:
            logger.info(
                "predicate_noop roles=%d (role-based filtering deferred to Prompt 3.5 tagger)",
                self.noop_role_count,
            )
        if self.noop_tag_count:
            logger.info(
                "predicate_noop tags=%d (tag-based filtering deferred to Prompt 3.5 tagger)",
                self.noop_tag_count,
            )

    def build_where_clause(self) -> tuple[str, dict]:
        """Return (SQL WHERE fragment, bind params) for use in sqlalchemy text().

        The fragment is suitable for embedding inside AND (...) in a larger query.
        Returns ('FALSE', {}) when no implementable predicates are set — callers
        must check has_implementable_predicates before calling this.
        """
        if not self.has_implementable_predicates:
            return ("FALSE", {})

        conditions: list[str] = []
        params: dict = {}

        if self.tracked_docket_uuids:
            conditions.append("f.docket_id::text = ANY(:docket_ids)")
            params["docket_ids"] = self.tracked_docket_uuids

        if self.market_slugs:
            conditions.append("s.slug = ANY(:market_slugs)")
            params["market_slugs"] = self.market_slugs

        if self.text_ilike_patterns:
            conditions.append(
                "(f.title ILIKE ANY(:text_patterns) OR f.filer ILIKE ANY(:text_patterns))"
            )
            params["text_patterns"] = self.text_ilike_patterns

        if self.zone_filer_patterns:
            conditions.append("f.filer ILIKE ANY(:zone_patterns)")
            params["zone_patterns"] = self.zone_filer_patterns

        clause = " OR ".join(f"({c})" for c in conditions)
        return (clause, params)

    def build_match_count_expr(self) -> str:
        """Return a SQL expression computing per-filing predicate match count.

        Result is an integer ≥ 0. Each matched predicate contributes +1.
        Used for scoring boost (+10 per matched predicate in _score_filing).
        """
        cases: list[str] = []

        if self.tracked_docket_uuids:
            cases.append("CASE WHEN f.docket_id::text = ANY(:docket_ids) THEN 1 ELSE 0 END")
        if self.market_slugs:
            cases.append("CASE WHEN s.slug = ANY(:market_slugs) THEN 1 ELSE 0 END")
        if self.text_ilike_patterns:
            cases.append(
                "CASE WHEN f.title ILIKE ANY(:text_patterns) "
                "OR f.filer ILIKE ANY(:text_patterns) THEN 1 ELSE 0 END"
            )
        if self.zone_filer_patterns:
            cases.append("CASE WHEN f.filer ILIKE ANY(:zone_patterns) THEN 1 ELSE 0 END")

        return " + ".join(cases) if cases else "0"


def _parse_query(raw_query) -> dict:
    """Normalize a saved_search.query value (may arrive as str or dict)."""
    if isinstance(raw_query, dict):
        return raw_query
    if isinstance(raw_query, str):
        try:
            return json.loads(raw_query)
        except Exception:
            return {}
    return {}


def build_predicate_bundle(
    *,
    saved_searches: list[dict],
    tracked_docket_uuids: list[str],
    zone_filer_patterns: list[str],
    market_roles: list[str],
) -> PredicateBundle:
    """Aggregate all predicates for one user into a single PredicateBundle.

    Args:
        saved_searches: Rows from saved_searches WHERE notify=true. Each dict
            must have at least {"id": str, "query": dict|str}.
        tracked_docket_uuids: Merged UUIDs from user_profiles.tracked_docket_ids
            and user_dockets junction table.
        zone_filer_patterns: Pre-built ILIKE patterns from zone_lookup (already
            filtered for user's tracked_tags / ERCOT zones).
        market_roles: user_profiles.market_roles — captured for noop logging only.
    """
    market_slugs: set[str] = set()
    text_patterns: list[str] = []
    noop_tag_count = 0

    for ss in saved_searches:
        query = _parse_query(ss.get("query"))

        for slug in query.get("markets") or []:
            if slug:
                market_slugs.add(slug)

        text = query.get("text")
        if text and text.strip():
            text_patterns.append(f"%{text.strip()}%")

        tags = query.get("tags") or []
        if tags:
            noop_tag_count += len(tags)
            logger.info(
                "predicate_noop saved_search=%s tags=%r (tag filtering deferred to Prompt 3.5)",
                ss.get("id", "?"),
                tags,
            )

        # query.tdu_zones handled via zone_filer_patterns at call site, not here

    return PredicateBundle(
        market_slugs=sorted(market_slugs),
        text_ilike_patterns=text_patterns,
        tracked_docket_uuids=list(dict.fromkeys(tracked_docket_uuids)),  # dedupe, order-stable
        zone_filer_patterns=zone_filer_patterns,
        noop_tag_count=noop_tag_count,
        noop_role_count=len(market_roles),
    )
