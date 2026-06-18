"""STEP 1+2 acceptance check for #128 salience.

Runs compute_market_salience + generate_salience_headlines for a market,
prints the top-3 with scores and Haiku headlines.

Usage:
    python check_salience.py [MARKET]   (default: FERC)

Verify: are the top-3 FERC dockets Commission Orders / real developments,
not routine administrative proceedings? Cross-check at:
https://elibrary.ferc.gov -> Recent Filings -> sort by docket activity.
"""

import asyncio
import sys
from datetime import date

from nodalpulse.workers.salience import (
    _iso_week_start,
    _stored_top3,
    compute_market_salience,
    generate_salience_headlines,
)


async def main() -> None:
    market = sys.argv[1] if len(sys.argv) > 1 else "FERC"
    today = date.today()
    week_start = _iso_week_start(today)
    from datetime import timedelta
    week_end = week_start + timedelta(days=7)

    print(f"\n=== Market Salience: {market} ===")
    print(f"    Window  : {week_start} to {week_end} (7-day ISO week)")
    print(f"    Score   : filings_count*3 + distinct_filers*2 + max_doc_weight")
    print(f"    Gate    : max_doc_weight >= 20 (min one important filing)")
    print()

    entries = await compute_market_salience(market, week_start)

    if not entries:
        print("  (no eligible dockets — all filtered by doc_weight gate)")
        return

    for e in entries:
        print(f"  #{e.rank}  {e.docket_key}")
        if e.docket_title:
            print(f"       {e.docket_title[:80]}")
        print(
            f"       score={e.score:.0f}  "
            f"filings={e.filings_count}  "
            f"filers={e.distinct_filers}  "
            f"doc_weight={e.max_doc_weight}"
        )

    print()
    print("Generating Haiku headlines...")
    await generate_salience_headlines(market, week_start, week_end, entries)

    print()
    print("=== Headlines ===")
    stored = await _stored_top3(market, week_start)
    for rank, row in sorted(stored.items()):
        print(f"  #{rank}  {row['docket_key']}")
        print(f"       {row['headline'] or '(no headline)'}")
        print()

    print("Upserted into market_salience table.")
    print("Verify: are these real Orders/developments, not routine admin filings?")


asyncio.run(main())
