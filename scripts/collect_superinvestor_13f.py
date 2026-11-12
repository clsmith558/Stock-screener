#!/usr/bin/env python3
"""
Collect SEC Form 13F-HR holdings for the 16 curated Superinvestors + scan ASX
Substantial Holder notices (Form 603 / 604) into `asx_announcements_cache.db`.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")


def main():
    parser = argparse.ArgumentParser(description="Collect Superinvestor 13F-HR & ASX substantial holder signals")
    parser.add_argument("--no-sec", action="store_true", help="Rebuild summary from cached 13F + ASX notices without SEC HTTP calls")
    args = parser.parse_args()

    from data.superinvestor_13f import collect_and_rebuild_superinvestor_13f, load_superinvestor_summary_map

    res = collect_and_rebuild_superinvestor_13f(fetch_sec=not args.no_sec)
    print(
        f"[13F CLONER] Managers fetched: {res['managers_fetched']} | "
        f"Total 13F positions: {res['total_13f_positions']} | "
        f"Stocks with cloner/subst signal: {res['stocks_with_cloner_signal']}"
    )
    smap = load_superinvestor_summary_map()
    top = sorted(
        [(k, v) for k, v in smap.items() if not k.endswith(".AX") or "." in k],
        key=lambda x: (x[1]["cloner_holder_count"], x[1]["cloner_max_weight_pct"]),
        reverse=True,
    )[:12]
    for sym, info in top:
        print(
            f"  {sym:<10} holders={info['cloner_holder_count']} "
            f"new/add={info['cloner_new_position_flag']} "
            f"max_wt={info['cloner_max_weight_pct']:.1f}% -> {info['cloner_holders_summary']}"
        )


if __name__ == "__main__":
    main()
