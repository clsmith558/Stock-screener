#!/usr/bin/env python3
"""
Refresh builtin US index universes: S&P 500, Russell 1000, Nasdaq-100.

Sources: Wikipedia constituent tables (via load_sp500_tickers / us_universe loaders).
Cadence: intended ~every 6 months (wired in config/update_schedule.yaml).

Usage:
  python scripts/refresh_us_index_lists.py
  python scripts/refresh_us_index_lists.py --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("refresh_us_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh S&P 500 / Russell 1000 / Nasdaq-100 lists")
    parser.add_argument("--json", action="store_true", help="Print result JSON")
    args = parser.parse_args()

    from data.business_lists import refresh_us_index_builtin_lists

    try:
        registered = refresh_us_index_builtin_lists()
    except Exception as e:
        logger.exception("US index list refresh failed: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"lists": registered}, indent=2, default=str))
    else:
        print("US index builtin lists refreshed:")
        for row in registered:
            print(
                f"  {row.get('display_name') or row.get('list_id')}: "
                f"{row.get('ticker_count')} tickers"
            )
        # Smoke checks
        from data.business_lists import (
            BUILTIN_NASDAQ100_LIST_ID,
            BUILTIN_RUSSELL1000_LIST_ID,
            BUILTIN_SP500_LIST_ID,
            load_list_symbols,
        )

        samples = {
            BUILTIN_SP500_LIST_ID: ("AAPL", "MSFT", "JPM"),
            BUILTIN_RUSSELL1000_LIST_ID: ("AAPL", "NVDA"),
            BUILTIN_NASDAQ100_LIST_ID: ("AAPL", "TSLA", "GOOGL"),
        }
        for lid, tickers in samples.items():
            syms = set(load_list_symbols(lid))
            missing = [t for t in tickers if t not in syms]
            print(f"  check {lid}: n={len(syms)} missing_samples={missing or 'none'}")

    # Fail soft if counts look absurdly low (wiki scrape failure → fallback may be ok)
    counts = {r.get("list_id"): r.get("ticker_count") or 0 for r in registered}
    if counts.get("__builtin__:sp500", 0) < 400:
        logger.warning("S&P 500 count low (%s) — check Wikipedia scrape", counts.get("__builtin__:sp500"))
        return 2
    if counts.get("__builtin__:nasdaq100", 0) < 80:
        logger.warning("Nasdaq-100 count low (%s)", counts.get("__builtin__:nasdaq100"))
        return 2
    # Russell wiki table is flaky; warn only
    if counts.get("__builtin__:russell1000", 0) < 500:
        logger.warning(
            "Russell 1000 count low (%s) — may be Wikipedia fallback",
            counts.get("__builtin__:russell1000"),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
