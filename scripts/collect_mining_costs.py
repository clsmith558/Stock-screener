#!/usr/bin/env python3
"""
Collect reported unit costs from mining 10-Ks and rank within pilot peer cohorts.

Usage (from Stock screener folder):
  python scripts/collect_mining_costs.py
  python scripts/collect_mining_costs.py --force
  python scripts/collect_mining_costs.py --tickers BTU,HCC,AMR,FCX,NEM
  python scripts/collect_mining_costs.py --status
  python scripts/collect_mining_costs.py --rank-only
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
    parser = argparse.ArgumentParser(description="Scan mining universe for unit costs + peer rank")
    parser.add_argument("--force", action="store_true", help="Rescan even if cache is fresh")
    parser.add_argument("--tickers", type=str, default="", help="Comma-separated subset")
    parser.add_argument("--status", action="store_true", help="Print cached status only")
    parser.add_argument("--rank-only", action="store_true", help="Recompute peer ranks only")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON")
    args = parser.parse_args()

    from data.mining_costs import (
        costs_status_public,
        load_mining_universe,
        recompute_peer_ranks,
        scan_mining_costs,
    )

    if args.status:
        st = costs_status_public()
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(
                f"Universe: {st['universe_size']} · cached: {st['summaries_cached']} · "
                f"with cost: {st['with_cost']} · low-cost Q: {st['low_cost_quartile']}"
            )
            print(st.get("disclaimer") or "")
            for c in st.get("cohorts") or []:
                print(f"\n[{c['metric']}] n={c['count']}")
                for p in c.get("peers") or []:
                    q = " Q1" if p.get("low_cost_quartile") else ""
                    print(
                        f"  #{p.get('cost_rank')} {p['ticker']:6} "
                        f"{p.get('cost_display')}{q} filed={p.get('filing_date')}"
                    )
        return 0

    if args.rank_only:
        ranks = recompute_peer_ranks()
        if args.json:
            print(json.dumps(ranks, indent=2))
        else:
            print(f"Recomputed ranks for {ranks.get('updated')} rows")
            for m, info in (ranks.get("cohorts") or {}).items():
                print(f"  {m}: {info.get('count')} peers {info.get('tickers')}")
        return 0

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    n = len(tickers) if tickers else len((load_mining_universe().get("companies") or []))
    print(
        f"Scanning mining unit costs"
        f"{' (subset ' + ','.join(tickers) + ')' if tickers else f' ({n} names)'}"
        f"{' force' if args.force else ''}…"
    )
    result = scan_mining_costs(force=args.force, tickers=tickers)
    if args.json:
        slim = {
            "scanned": result["scanned"],
            "skipped": result["skipped"],
            "errors": result["errors"],
            "finished_at": result["finished_at"],
            "ranks": result.get("ranks"),
            "summaries": [
                {
                    "ticker": s.get("ticker"),
                    "cost_metric": s.get("cost_metric"),
                    "cost_value": s.get("cost_value"),
                    "cost_display": s.get("cost_display"),
                    "cost_rank": s.get("cost_rank"),
                    "peer_count": s.get("peer_count"),
                    "low_cost_quartile": s.get("low_cost_quartile"),
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
            if s.get("cost_value") is not None or s.get("scan_error"):
                err = f" ERR={s.get('scan_error')}" if s.get("scan_error") else ""
                q = " Q1" if s.get("low_cost_quartile") else ""
                print(
                    f"  {s.get('ticker'):6} {s.get('cost_metric') or '—':28} "
                    f"{s.get('cost_display') or '—':14} "
                    f"rank={s.get('cost_rank')}/{s.get('peer_count')}{q}{err}"
                )
        st = costs_status_public()
        print(
            f"Cache: {st['with_cost']} with cost · "
            f"{st['low_cost_quartile']} low-cost quartile"
        )
    return 0 if result.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
