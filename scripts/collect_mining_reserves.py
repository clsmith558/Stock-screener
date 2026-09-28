#!/usr/bin/env python3
"""
Collect S-K 1300 / 10-K proven+probable reserves and reserve life for US mining pilot.

Usage (from Stock screener folder):
  python scripts/collect_mining_reserves.py
  python scripts/collect_mining_reserves.py --force
  python scripts/collect_mining_reserves.py --tickers BTU,HCC,FCX,NEM,AMR
  python scripts/collect_mining_reserves.py --status
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
    parser = argparse.ArgumentParser(description="Scan mining universe for 10-K reserves / life")
    parser.add_argument("--force", action="store_true", help="Rescan even if cache is fresh")
    parser.add_argument("--tickers", type=str, default="", help="Comma-separated subset")
    parser.add_argument("--status", action="store_true", help="Print cached status only")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON")
    args = parser.parse_args()

    from data.mining_reserves import (
        load_mining_universe,
        reserves_status_public,
        scan_mining_reserves,
    )

    if args.status:
        st = reserves_status_public()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(
                f"Universe: {st['universe_size']} · cached: {st['summaries_cached']} · "
                f"reserves: {st['with_reserves']} · life: {st['with_reserve_life']} · "
                f"Ex96: {st['with_exhibit_96']}"
            )
            for s in st.get("lives") or []:
                print(
                    f"  {s['ticker']:6} life={s.get('reserve_life_years')}y "
                    f"({s.get('life_confidence')}) "
                    f"R={s.get('reserve_display')} P={s.get('production_display')} "
                    f"ex96={s.get('has_exhibit_96')} filed={s.get('filing_date')}"
                )
        return 0

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    n = len(tickers) if tickers else len((load_mining_universe().get("companies") or []))
    print(
        f"Scanning mining reserves universe"
        f"{' (subset ' + ','.join(tickers) + ')' if tickers else f' ({n} names)'}"
        f"{' force' if args.force else ''}…"
    )
    result = scan_mining_reserves(force=args.force, tickers=tickers)
    if args.json:
        slim = {
            "scanned": result["scanned"],
            "skipped": result["skipped"],
            "errors": result["errors"],
            "finished_at": result["finished_at"],
            "summaries": [
                {
                    "ticker": s.get("ticker"),
                    "reserve_life_years": s.get("reserve_life_years"),
                    "life_confidence": s.get("life_confidence"),
                    "reserve_display": s.get("reserve_display"),
                    "production_display": s.get("production_display"),
                    "has_exhibit_96": s.get("has_exhibit_96"),
                    "scan_error": s.get("scan_error"),
                    "parse_notes": s.get("parse_notes"),
                }
                for s in result.get("summaries") or []
            ],
        }
        print(json.dumps(slim, indent=2))
    else:
        print(
            f"Done: scanned={result['scanned']} skipped={result['skipped']} "
            f"errors={result['errors']}"
        )
        for s in result.get("summaries") or []:
            life = s.get("reserve_life_years")
            if life is not None or s.get("reserve_value") or s.get("scan_error") or s.get("has_exhibit_96"):
                err = f" ERR={s.get('scan_error')}" if s.get("scan_error") else ""
                notes = s.get("parse_notes") or []
                note_s = f" notes={notes[:2]}" if notes else ""
                print(
                    f"  {s.get('ticker'):6} life={life}y "
                    f"R={s.get('reserve_display') or '—'} "
                    f"P={s.get('production_display') or '—'} "
                    f"ex96={bool(s.get('has_exhibit_96'))}{err}{note_s}"
                )
        st = reserves_status_public()
        print(
            f"Cache: {st['with_reserves']} with reserves · "
            f"{st['with_reserve_life']} with life · "
            f"{st['with_exhibit_96']} Ex96"
        )
    return 0 if result.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
