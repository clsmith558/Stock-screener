#!/usr/bin/env python3
"""
Weekly SEC EDGAR buyback + Form 4 insider scan.

Usage:
  python scripts/collect_us_buybacks.py
  python scripts/collect_us_buybacks.py --days 90
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
logger = logging.getLogger("collect_us_buybacks")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan SEC filings for US buybacks/insiders")
    parser.add_argument("--days", type=int, default=120, help="Lookback window (default 120)")
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=0,
        help="Cap universe size (0 = full Russell 1000 ∪ Nasdaq-100 ∪ S&P 500)",
    )
    args = parser.parse_args()

    from data.buybacks import refresh_us_buybacks_data

    max_tickers = int(args.max_tickers) or None
    logger.info("Starting US SEC buyback/insider scan days=%s max_tickers=%s", args.days, max_tickers)
    result = refresh_us_buybacks_data(days=int(args.days), max_tickers=max_tickers)
    status = result.get("status") or result.get("ok")
    logger.info(
        "US SEC scan %s events=%s new_events=%s insider_new=%s stocks=%s",
        status,
        result.get("event_count") or result.get("events"),
        result.get("new_events"),
        (result.get("insider") or {}).get("new_purchases"),
        result.get("stocks"),
    )
    if status in ("ok", True, None) and not result.get("error"):
        return 0
    logger.error("US SEC scan failed: %s", result.get("error") or result)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
