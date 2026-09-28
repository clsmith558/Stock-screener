#!/usr/bin/env python3
"""
Phase 2: extract production / realized price / hedge % / capex from filings.

Usage (from repo root):
  python scripts/collect_operating_extract.py
  python scripts/collect_operating_extract.py --tickers OXY,EQT,NHC.AX
  python scripts/collect_operating_extract.py --status
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
    parser = argparse.ArgumentParser(description="Collect Phase-2 operating extracts from filings")
    parser.add_argument("--tickers", default="", help="Comma list, default = seed in config/operating_extract.yaml")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--us-only", action="store_true")
    parser.add_argument("--asx-only", action="store_true")
    args = parser.parse_args()

    from data.operating_extract import (
        collect_all_operating,
        collect_asx_operating,
        collect_us_operating,
        get_operating_history,
        operating_status,
    )

    if args.status:
        st = operating_status()
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        print(f"Seed: {', '.join(st.get('seed') or [])}")
        print(f"Tickers with extracts: {st['ticker_count']}")
        for t in st.get("tickers") or []:
            fields = ", ".join(
                f"{k} n={v['n']} {v['from']}->{v['to']}"
                for k, v in (t.get("fields") or {}).items()
            )
            print(f"  {t['ticker']:8} {fields}")
            hist = get_operating_history(t["ticker"])
            for h in hist[-8:]:
                print(
                    f"    {h['period_end']}  {h['field']:16} {h['value']} {h['unit']}  "
                    f"{h.get('source')}  {(h.get('snippet') or '')[:70]}"
                )
        return 0

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    print("Collecting operating extracts from filings...")
    if args.us_only:
        result = {"us": collect_us_operating(tickers=tickers), "asx": {}, "stored": 0}
        result["stored"] = result["us"].get("stored") or 0
        result["errors"] = result["us"].get("errors") or []
    elif args.asx_only:
        result = {"asx": collect_asx_operating(tickers=tickers), "us": {}, "stored": 0}
        result["stored"] = result["asx"].get("stored") or 0
        result["errors"] = result["asx"].get("errors") or []
    else:
        result = collect_all_operating(tickers=tickers)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Stored/updated points: {result.get('stored')}")
        errs = result.get("errors") or []
        if errs:
            print("Errors:")
            for e in errs[:8]:
                print(f"  {e}")
        st = operating_status()
        for t in st.get("tickers") or []:
            print(f"  {t['ticker']:8} n={t['n']}  fields={list((t.get('fields') or {}).keys())}")
    return 0 if (result.get("stored") or 0) or not (result.get("errors") or []) else 1


if __name__ == "__main__":
    raise SystemExit(main())
