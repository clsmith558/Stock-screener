#!/usr/bin/env python3
"""
Inactive symbol hygiene — purge confirmed-dead tickers so YF jobs stay lean.

Usage:
  python scripts/hygiene_inactive_symbols.py --status
  python scripts/hygiene_inactive_symbols.py --dry-run
  python scripts/hygiene_inactive_symbols.py
  python scripts/hygiene_inactive_symbols.py --no-revalidate --no-rewrite-csvs
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
logger = logging.getLogger("hygiene_inactive")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inactive symbol hygiene for screener lists")
    parser.add_argument("--status", action="store_true", help="Show inactive counts only")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without mutating")
    parser.add_argument("--no-revalidate", action="store_true", help="Skip live yfinance re-probe")
    parser.add_argument("--no-purge-entries", action="store_true", help="Keep dead symbols on list entries")
    parser.add_argument("--no-rewrite-csvs", action="store_true", help="Do not rewrite Business lists CSVs")
    parser.add_argument("--no-prune-orphans", action="store_true", help="Keep orphan inactive rows")
    parser.add_argument("--revalidate-limit", type=int, default=60, help="Max tentative symbols to re-probe")
    parser.add_argument("--json", action="store_true", help="Print full report as JSON")
    args = parser.parse_args()

    from data.inactive_hygiene import (
        list_inactive_asx,
        list_inactive_business,
        run_inactive_hygiene,
    )

    if args.status:
        biz = list_inactive_business()
        asx = list_inactive_asx()
        print(
            f"Business inactive: {len(biz)} "
            f"(confirmed={sum(1 for x in biz if x['confirmed'])}, "
            f"tentative={sum(1 for x in biz if not x['confirmed'])})"
        )
        print(
            f"ASX inactive:      {len(asx)} "
            f"(confirmed={sum(1 for x in asx if x['confirmed'])}, "
            f"tentative={sum(1 for x in asx if not x['confirmed'])})"
        )
        # sample confirmed
        conf_b = [x for x in biz if x["confirmed"]][:15]
        conf_a = [x for x in asx if x["confirmed"]][:15]
        if conf_b:
            print("Sample business confirmed:", ", ".join(x["symbol"] for x in conf_b))
        if conf_a:
            print("Sample ASX confirmed:", ", ".join(x["symbol"] for x in conf_a))
        return 0

    report = run_inactive_hygiene(
        revalidate=not args.no_revalidate,
        revalidate_limit=args.revalidate_limit,
        purge_entries=not args.no_purge_entries,
        rewrite_csvs=not args.no_rewrite_csvs,
        prune_orphans=not args.no_prune_orphans,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        s = report.get("summary") or {}
        print("Inactive hygiene complete")
        print(f"  dry_run={report.get('dry_run')}")
        print(f"  cache sync ASX={report.get('asx_cache_sync')} business={report.get('business_cache_sync')}")
        if report.get("revalidate_business"):
            print(f"  revalidate business: {report['revalidate_business']}")
        if report.get("revalidate_asx"):
            print(f"  revalidate ASX: {report['revalidate_asx']}")
        if report.get("purge_entries"):
            print(f"  purge entries: {report['purge_entries']}")
        if report.get("rewrite_csvs"):
            rc = report["rewrite_csvs"]
            print(
                f"  rewrite CSVs: files={rc.get('files_rewritten') or rc.get('files_would_rewrite')} "
                f"symbols_removed={rc.get('symbols_removed')}"
            )
        if report.get("prune_orphans"):
            print(f"  prune orphans: {report['prune_orphans']}")
        print(
            f"  summary: biz confirmed={s.get('business_inactive_confirmed')} "
            f"tentative={s.get('business_inactive_tentative')} | "
            f"asx confirmed={s.get('asx_inactive_confirmed')} "
            f"tentative={s.get('asx_inactive_tentative')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
