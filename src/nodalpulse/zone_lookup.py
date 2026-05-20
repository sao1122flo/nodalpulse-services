"""Conservative filer-name → ERCOT load-zone mapping.

Maps lowercase keyword substrings found in filing.filer to one of the four
ERCOT load zones that onboarding exposes: north, houston, west, south.

Only well-established TDU/TDSP filer names are included. Unknown filers fall
through unchanged — no zone match, no filtering error. "all" in tracked_tags
is treated as "no zone filter" (too broad to be useful; collapses to global
fallback path in compose_brief).

This is intentionally conservative. A false-negative (miss a zone-relevant
filing from an unlisted filer) is preferable to false-positives (surface
irrelevant filings). The tagger upgrade (Prompt 3.5) will supersede this.
"""

from __future__ import annotations

# Keyword substring (lowercase) → ERCOT load zone
# Only the four investor-owned TDUs with unambiguous zone assignments are listed.
# Cooperatives included only where zone is clear from service territory.
_FILER_ZONE_MAP: dict[str, str] = {
    # North zone — Oncor serves Dallas/Fort Worth metro
    "oncor": "north",
    # Houston zone — CenterPoint serves greater Houston
    "centerpoint energy": "houston",
    "centerpoint electric": "houston",
    # South zone — AEP Texas Central serves Corpus Christi / Rio Grande Valley
    "aep texas central": "south",
    # West zone — TNMP serves Midland/Odessa / West Texas
    "texas-new mexico power": "west",
    "tnmp": "west",
    # Additional co-ops with clear zone assignments
    "bluebonnet electric": "north",
    "pedernales electric": "north",
    "golden spread electric": "north",
    "navarro county electric": "north",
    "magic valley electric": "south",
    "rio grande electric": "south",
    "bandera electric": "south",
    "grayson-collin electric": "north",
    "south plains electric": "west",
    "cap rock energy": "west",
    "lighthouse electric": "west",
}


def zones_for_filer(filer: str) -> frozenset[str]:
    """Return the ERCOT zones associated with a filer name string.

    Returns an empty frozenset for unknown filers.
    """
    if not filer:
        return frozenset()
    filer_lower = filer.lower()
    return frozenset(
        zone for keyword, zone in _FILER_ZONE_MAP.items()
        if keyword in filer_lower
    )


def ilike_patterns_for_zones(zones: list[str] | None) -> list[str]:
    """Return SQL ILIKE patterns for filer names that map to the given zones.

    "all" is treated as no filter — caller uses the global filing path.
    Empty or None zones list returns an empty list.
    """
    if not zones:
        return []
    target = frozenset(z for z in zones if z and z != "all")
    if not target:
        return []
    return [
        f"%{keyword}%"
        for keyword, zone in _FILER_ZONE_MAP.items()
        if zone in target
    ]
