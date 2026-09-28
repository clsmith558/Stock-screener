#!/usr/bin/env python3
"""
Collect multi-period unit costs from ASX quarterly PDFs + US 10-Q/8-K/10-K.

Usage:
  python scripts/collect_unit_cost_history.py
  python scripts/collect_unit_cost_history.py --tickers NHC.AX,WHC.AX,NEM
  python scripts/collect_unit_cost_history.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default="", help="Comma list e.g. NHC.AX,NEM")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--asx-only", action="store_true")
    parser.add_argument("--us-only", action="store_true")
    args = parser.parse_args()

    from data.unit_cost_history import (
        collect_all_cost_history,
        collect_asx_cost_history,
        collect_us_cost_history,
        cost_history_status,
        get_cost_history,
    )

    if args.status:
        st = cost_history_status()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"Tickers with history: {st['ticker_count']}")
            for t in st.get("tickers") or []:
                # ASCII arrow: Windows Task Scheduler / cp1252 cannot encode Unicode arrows
                print(f"  {t['ticker']:8} n={t['n_points']}  {t['from']} -> {t['to']}")
                hist = get_cost_history(t["ticker"])
                for h in hist[-6:]:
                    print(
                        f"    {h['period_end']}  ${h['cost_value_usd']:.2f}  "
                        f"{h.get('cost_metric')}  {h.get('source')}"
                    )
        return 0

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    print("Collecting multi-period unit costs...")
    if args.asx_only:
        result = {"asx": collect_asx_cost_history(tickers=tickers), "us": {}, "stored": 0}
        result["stored"] = result["asx"].get("stored") or 0
    elif args.us_only:
        result = {"us": collect_us_cost_history(tickers=tickers), "asx": {}, "stored": 0}
        result["stored"] = result["us"].get("stored") or 0
    else:
        result = collect_all_cost_history(tickers=tickers)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Stored/updated points: {result.get('stored')}")
        if result.get("asx", {}).get("errors"):
            print("ASX errors:", result["asx"]["errors"][:5])
        if result.get("us", {}).get("errors"):
            print("US errors:", result["us"]["errors"][:5])
        st = cost_history_status()
        for t in st.get("tickers") or []:
            print(f"  {t['ticker']:8} n={t['n_points']}  {t['from']} -> {t['to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
