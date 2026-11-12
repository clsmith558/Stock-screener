#!/usr/bin/env python3
"""
Refresh `index_series_cache` (global equity index prices, drawdown stats,
ETF trailing PE proxies, and S&P 500 Multpl PE history).

Scheduled alternative to relying on `/api/indices` requests while Flask runs.
"""

from __future__ import annotations

import argparse
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
logger = logging.getLogger("refresh_index_cache")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh global index series SQLite cache")
    parser.add_argument(
        "--if-stale",
        action="store_true",
        help="Only refetch entries older than 24h (default forces a full refresh)",
    )
    args = parser.parse_args()

    from data.indices import get_indices_overview

    force = not bool(args.if_stale)
    logger.info("=== refresh_index_cache start force=%s ===", force)
    payload = get_indices_overview(force=force)
    indices = payload.get("indices") or []
    errors = [row for row in indices if row.get("error")]
    ok_count = len(indices) - len(errors)
    for row in errors:
        logger.warning("Index %s failed: %s", row.get("id"), row.get("error"))
    logger.info(
        "=== refresh_index_cache done: ok=%d errors=%d total=%d ===",
        ok_count,
        len(errors),
        len(indices),
    )
    print(f"Index cache refresh complete: {ok_count}/{len(indices)} OK (errors={len(errors)})")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
