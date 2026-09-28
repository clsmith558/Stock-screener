#!/usr/bin/env python3
"""
Collect SEC Form 8-K Item 1.03 (Bankruptcy or Receivership) for the US mining pilot.

Usage (from Stock screener folder):
  python scripts/collect_item_103.py
  python scripts/collect_item_103.py --force
  python scripts/collect_item_103.py --tickers BTU,ARCH,HCC,AMR
  python scripts/collect_item_103.py --status
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
    parser = argparse.ArgumentParser(description="Scan mining universe for Item 1.03 8-Ks")
    parser.add_argument("--force", action="store_true", help="Rescan even if cache is fresh")
    parser.add_argument(
        "--tickers",
        type=str,
        default="",
        help="Comma-separated ticker subset (default: full universe)",
    )
    parser.add_argument("--status", action="store_true", help="Print cached status only")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    args = parser.parse_args()

    from data.bankruptcy_reorg import (
        list_reorg_summaries,
        reorg_status_public,
        scan_mining_universe,
        universe_tickers,
    )

    if args.status:
        st = reorg_status_public()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"Universe: {st['universe_size']} · cached: {st['summaries_cached']}")
            print(f"Item 1.03 hits: {st['with_item_103']} · survivor candidates: {st['survivor_candidates']}")
            for s in st.get("item_103_hits") or []:
                print(
                    f"  {s['ticker']:6} events={s['event_count']} "
                    f"filed={s.get('first_filing_date')} conf={s.get('confirmation_date')} "
                    f"classes={s.get('classes')}"
                )
            for s in st.get("survivors") or []:
                if s.get("manual_reorg") and not s.get("has_item_103"):
                    print(f"  {s['ticker']:6} manual_reorg only · emerge={s.get('emerge_date')}")
        return 0

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    print(
        f"Scanning mining Item 1.03 universe"
        f"{' (subset ' + ','.join(tickers) + ')' if tickers else f' ({len(universe_tickers())} names)'}"
        f"{' force' if args.force else ''}…"
    )
    result = scan_mining_universe(force=args.force, tickers=tickers)
    if args.json:
        # summaries can be large — keep compact for CLI
        slim = {
            "scanned": result["scanned"],
            "skipped": result["skipped"],
            "errors": result["errors"],
            "finished_at": result["finished_at"],
            "summaries": [
                {
                    "ticker": s.get("ticker"),
                    "has_item_103": s.get("has_item_103"),
                    "survivor_candidate": s.get("survivor_candidate"),
                    "confirmation_date": s.get("confirmation_date"),
                    "emerge_date": s.get("emerge_date"),
                    "event_count": s.get("event_count"),
                    "scan_error": s.get("scan_error"),
                    "manual_reorg": s.get("manual_reorg"),
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
            if s.get("has_item_103") or s.get("survivor_candidate") or s.get("scan_error"):
                flag = "SURV" if s.get("survivor_candidate") else ("103" if s.get("has_item_103") else "—")
                err = f" ERR={s.get('scan_error')}" if s.get("scan_error") else ""
                print(
                    f"  {s.get('ticker'):6} [{flag}] events={s.get('event_count', 0)} "
                    f"conf={s.get('confirmation_date')} emerge={s.get('emerge_date')}{err}"
                )

        st = reorg_status_public()
        print(
            f"Cache: {st['with_item_103']} Item 1.03 · "
            f"{st['survivor_candidates']} survivor candidates"
        )
    return 0 if result.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
