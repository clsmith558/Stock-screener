#!/usr/bin/env python3
"""
ASX mining pilot (phases 1–3) for YAL, WHC, NHC + NEM dual-list.

Usage (from Stock screener folder):
  python scripts/collect_asx_mining.py
  python scripts/collect_asx_mining.py --force
  python scripts/collect_asx_mining.py --tickers YAL,NHC
  python scripts/collect_asx_mining.py --status
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
    parser = argparse.ArgumentParser(description="Scan ASX mining pilot (reorg/reserves/costs)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from data.asx_mining import asx_mining_status_public, scan_asx_mining_universe

    if args.status:
        st = asx_mining_status_public()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(
                f"Universe: {st['universe_size']} · cached: {st['summaries_cached']} · "
                f"reorg: {st['with_reorg']} · life: {st['with_reserve_life']} · cost: {st['with_cost']}"
            )
            print(st.get("disclaimer") or "")
            for c in st.get("companies") or []:
                print(
                    f"  {c['ticker']:5} life={c.get('reserve_life_years')} "
                    f"R={c.get('reserve_display') or '—'} P={c.get('production_display') or '—'} "
                    f"cost={c.get('cost_display') or '—'} "
                    f"rank={c.get('cost_rank')}/{c.get('peer_count')} "
                    f"reorg={c.get('reorg_survivor')} us={c.get('us_crossfill') or '—'}"
                )
                if c.get("parse_notes"):
                    print(f"         notes={c['parse_notes'][:3]}")
        return 0

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    print(
        f"Scanning ASX mining pilot"
        f"{' (' + ','.join(tickers) + ')' if tickers else ''}{' force' if args.force else ''}…"
    )
    result = scan_asx_mining_universe(force=args.force, tickers=tickers)
    if args.json:
        print(
            json.dumps(
                {
                    "scanned": result["scanned"],
                    "skipped": result["skipped"],
                    "errors": result["errors"],
                    "ranks": result.get("ranks"),
                    "summaries": result.get("summaries"),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(
            f"Done: scanned={result['scanned']} skipped={result['skipped']} "
            f"errors={result['errors']}"
        )
        for s in result.get("summaries") or []:
            print(
                f"  {s.get('ticker'):5} life={s.get('reserve_life_years')} "
                f"R={s.get('reserve_display') or '—'} P={s.get('production_display') or '—'} "
                f"cost={s.get('cost_display') or '—'} "
                f"rank={s.get('cost_rank')}/{s.get('peer_count')} "
                f"reorg={s.get('reorg_survivor_candidate')} "
                f"err={s.get('scan_error') or '—'}"
            )
            if s.get("parse_notes"):
                print(f"         notes={s['parse_notes'][:4]}")
            if s.get("sources"):
                print(f"         sources={s['sources'][:6]}")
        st = asx_mining_status_public()
        print(
            f"Cache: reorg={st['with_reorg']} life={st['with_reserve_life']} "
            f"cost={st['with_cost']}"
        )
    return 0 if result.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
