#!/usr/bin/env python3
"""
Official supporting series → SQLite (Phase 1 daily/weekly + Phase 3 monthly/AEMO).

Usage (from repo root):
  python scripts/collect_supporting_series.py
  python scripts/collect_supporting_series.py --status
  python scripts/collect_supporting_series.py --phase 3
  python scripts/collect_supporting_series.py --series henry_hub,newcastle_coal
  python scripts/collect_supporting_series.py --full
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
    parser = argparse.ArgumentParser(
        description="Collect official supporting series (EIA/FRED/Pink Sheet/AEMO)"
    )
    parser.add_argument(
        "--series",
        default="",
        help="Comma list of catalog ids (default: all official for --phase)",
    )
    parser.add_argument(
        "--phase",
        default="all",
        choices=("1", "3", "all"),
        help="Which catalog slice to fetch when --series is omitted (default all)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Re-pull full history (AEMO from 2018; else last overlap window)",
    )
    parser.add_argument("--status", action="store_true", help="Print store status only")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    from data.supporting_series import (
        collect_official,
        collection_status,
        official_series_ids,
        phase1_series_ids,
        phase3_series_ids,
    )

    if args.status:
        st = collection_status()
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        print(
            f"Official series: {st.get('phase1_n', 0)} phase-1 + "
            f"{st.get('phase3_n', 0)} phase-3  db={st['db']}"
        )
        for s in st.get("series") or []:
            err = s.get("last_error")
            tail = f"  ERR {err}" if err else ""
            span = ""
            if s.get("first_date") and s.get("last_date"):
                span = f"  {s['first_date']} -> {s['last_date']}"
            src = s.get("provider") or "-"
            phase = f"p{s.get('phase')}" if s.get("phase") else "  "
            print(f"  {s['id']:22} {phase} n={s['n']:<6}{span}  {src}{tail}")
        return 0

    ids = [x.strip() for x in args.series.split(",") if x.strip()] or None
    if ids is None:
        if args.phase == "1":
            n = len(phase1_series_ids())
        elif args.phase == "3":
            n = len(phase3_series_ids())
        else:
            n = len(official_series_ids())
        print(f"Collecting {n} official series (phase={args.phase})...")
        phases = {1, 3} if args.phase == "all" else {int(args.phase)}
        result = collect_official(full=args.full, phases=phases)
    else:
        print(f"Collecting {len(ids)} series...")
        result = collect_official(series_ids=ids, full=args.full)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(
            f"Stored/updated points: {result.get('stored')}  "
            f"ok={result.get('fetched')}  failed={result.get('failed')}"
        )
        for r in result.get("results") or []:
            sid = r.get("series_id")
            if r.get("ok"):
                print(
                    f"  {sid:22} {r.get('provider')}/{r.get('source_id')}  "
                    f"n={r.get('n')} stored={r.get('stored')}"
                )
            else:
                print(f"  {sid:22} FAIL {r.get('error')}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
