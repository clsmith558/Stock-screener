#!/usr/bin/env python3
"""
Refresh yfinance price/metrics caches used by the screener.

Unlike the dashboard "Rebuild YF" buttons (need Flask running), this is a CLI
job for Task Scheduler / run_scheduled_updates.py.

Scopes:
  watchlist   — tickers on the personal watchlist (fast)
  business    — all CSV business lists + S&P 500 builtin (if registered)
  buybacks    — buybacks registry universe
  sp500       — US S&P 500 builtin only
  asx         — full ASX listed universe (slow: hours)
  lists       — business CSVs + buybacks + sp500 (not watchlist, not full ASX)
  all-light   — watchlist + lists (not full ASX)

By default only symbols with cache older than --max-cache-days are refetched.
Use --force to refresh every symbol in the scope.

Examples:
  python scripts/rebuild_yf_cache.py --scope watchlist
  python scripts/rebuild_yf_cache.py --scope all-light --max-cache-days 7
  python scripts/rebuild_yf_cache.py --scope asx --force
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
logger = logging.getLogger("rebuild_yf")


def _stale_business_symbols(symbols: list[str], max_cache_days: int, force: bool) -> list[str]:
    from data.business_lists import filter_active_symbols, load_yf_cached

    symbols = filter_active_symbols(symbols, confirmed_only=True)
    if force or max_cache_days <= 0:
        return symbols
    fresh = load_yf_cached(symbols, max_days=max_cache_days) or {}
    return [s for s in symbols if s not in fresh]


def _stale_asx_tickers(base_tickers: list[str], max_cache_days: int, force: bool) -> list[str]:
    from data.asx import filter_active_tickers, load_yf_cached_data

    base_tickers = filter_active_tickers(base_tickers, confirmed_only=True)
    if force or max_cache_days <= 0:
        return base_tickers
    fresh = load_yf_cached_data(base_tickers, max_days=max_cache_days) or {}
    return [t for t in base_tickers if t not in fresh]


def scope_watchlist(max_cache_days: int, force: bool, sleep: float | None) -> int:
    from data.business_lists import _fetch_yf_sequential
    from data.watchlist import list_watchlist

    symbols = [e["ticker"] for e in list_watchlist() if e.get("ticker")]
    if not symbols:
        logger.info("Watchlist empty — nothing to refresh")
        return 0
    need = _stale_business_symbols(symbols, max_cache_days, force)
    logger.info(
        "Watchlist YF: %d symbols, %d stale/missing (max_cache_days=%s force=%s)",
        len(symbols),
        len(need),
        max_cache_days,
        force,
    )
    if not need:
        return 0
    _fetch_yf_sequential(
        need,
        sleep_seconds=sleep,
        progress_every=5,
        retry_tentative_inactive=True,
        progress_label="watchlist yf",
    )
    return len(need)


def scope_list_ids(
    list_ids: list[str],
    max_cache_days: int,
    force: bool,
    sleep: float | None,
) -> int:
    from data.business_lists import (
        _fetch_yf_sequential,
        load_list_symbols,
        sync_builtin_lists,
    )

    try:
        sync_builtin_lists()
    except Exception as e:
        logger.debug("sync_builtin_lists: %s", e)

    total_fetched = 0
    for lid in list_ids:
        if not lid:
            continue
        symbols = load_list_symbols(lid)
        if not symbols:
            logger.info("List %s: no symbols (register lists / scan CSVs first)", lid)
            continue
        need = _stale_business_symbols(symbols, max_cache_days, force)
        logger.info(
            "List %s: %d symbols, %d to fetch",
            lid,
            len(symbols),
            len(need),
        )
        if not need:
            continue
        _fetch_yf_sequential(
            need,
            sleep_seconds=sleep,
            progress_every=10,
            retry_tentative_inactive=True,
            progress_label=f"yf {lid[:40]}",
        )
        total_fetched += len(need)
    return total_fetched


def scope_business(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    max_list_size: int | None = None,
) -> int:
    """All non-ASX-full business CSV lists + S&P 500 (not full ASX builtin)."""
    from data.business_lists import (
        BUILTIN_ASX_LIST_ID,
        BUILTIN_BUYBACKS_LIST_ID,
        get_available_lists,
        sync_builtin_lists,
    )

    try:
        sync_builtin_lists()
    except Exception as e:
        logger.warning("sync_builtin_lists failed: %s", e)

    lists = get_available_lists() or []
    skip = {BUILTIN_ASX_LIST_ID, BUILTIN_BUYBACKS_LIST_ID}
    candidates = [
        L
        for L in lists
        if L.get("list_id") and L.get("list_id") not in skip
    ]
    # Smallest first so a timeout still leaves S&P / moat / Hang Seng fresh
    # instead of dying inside Japan/Korea/small-cap dumps.
    candidates.sort(key=lambda L: int(L.get("ticker_count") or 10**9))
    ordered = []
    seen = set()
    skipped_large = []
    for L in candidates:
        lid = L.get("list_id")
        n = int(L.get("ticker_count") or 0)
        if lid in seen:
            continue
        seen.add(lid)
        if max_list_size and n > max_list_size:
            skipped_large.append(f"{lid}:{n}")
            continue
        ordered.append(lid)
    if skipped_large:
        logger.info(
            "Skipping %d large lists (>%s tickers): %s",
            len(skipped_large),
            max_list_size,
            ", ".join(skipped_large[:12]),
        )
    logger.info("Business lists to refresh: %d", len(ordered))
    return scope_list_ids(ordered, max_cache_days, force, sleep)


def scope_buybacks(max_cache_days: int, force: bool, sleep: float | None) -> int:
    from data.business_lists import _fetch_yf_sequential, filter_active_symbols
    from data.buybacks import (
        _code_to_yf_symbol,
        _hk_code_to_yf_symbol,
        load_asx_buyback_events,
        load_asx_insider_events,
        load_hk_events,
        load_hk_insider_events,
        load_tse_events,
        load_us_buyback_events,
        load_us_insider_events,
    )

    agg = set()
    try:
        for c in load_tse_events()["Code"].dropna().unique():
            agg.add(_code_to_yf_symbol(c))
    except Exception as e:
        logger.debug("TSE codes: %s", e)
    try:
        for c in load_hk_events()["Stock code"].dropna().unique():
            agg.add(_hk_code_to_yf_symbol(c))
        for c in load_hk_insider_events()["Stock code"].dropna().unique():
            agg.add(_hk_code_to_yf_symbol(c))
    except Exception as e:
        logger.debug("HK codes: %s", e)
    try:
        for e in load_asx_buyback_events():
            if e.get("yf_symbol"):
                agg.add(e["yf_symbol"])
        for e in load_asx_insider_events():
            if e.get("yf_symbol"):
                agg.add(e["yf_symbol"])
    except Exception as e:
        logger.debug("ASX buyback codes: %s", e)
    try:
        for e in load_us_buyback_events():
            if e.get("yf_symbol"):
                agg.add(e["yf_symbol"])
        for e in load_us_insider_events():
            if e.get("yf_symbol"):
                agg.add(e["yf_symbol"])
    except Exception as e:
        logger.debug("US buyback codes: %s", e)

    symbols = filter_active_symbols(sorted(agg), confirmed_only=True)
    need = _stale_business_symbols(symbols, max_cache_days, force)
    logger.info("Buybacks YF: %d symbols, %d to fetch", len(symbols), len(need))
    if not need:
        return 0
    _fetch_yf_sequential(
        need,
        sleep_seconds=sleep,
        progress_every=10,
        retry_tentative_inactive=True,
        progress_label="buybacks yf",
    )
    return len(need)


def scope_sp500(max_cache_days: int, force: bool, sleep: float | None) -> int:
    from data.business_lists import BUILTIN_SP500_LIST_ID, sync_builtin_lists

    try:
        sync_builtin_lists()
    except Exception as e:
        logger.warning("sync_builtin_lists: %s", e)
    return scope_list_ids([BUILTIN_SP500_LIST_ID], max_cache_days, force, sleep)


def scope_asx(max_cache_days: int, force: bool, sleep: float | None) -> int:
    from data.asx import (
        ASX_MONITORED_TICKERS,
        _fetch_yf_data_sequentially,
        load_asx_ticker_universe,
        sync_inactive_from_cache,
    )

    sync_inactive_from_cache()
    base = load_asx_ticker_universe() or ASX_MONITORED_TICKERS
    need = _stale_asx_tickers(list(base), max_cache_days, force)
    logger.info(
        "ASX YF: universe %d, to fetch %d (this can take hours if large)",
        len(base),
        len(need),
    )
    if not need:
        return 0
    _fetch_yf_data_sequentially(
        need,
        sleep_seconds=sleep,
        retry_tentative_inactive=True,
    )
    return len(need)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild yfinance caches (CLI / scheduler)")
    parser.add_argument(
        "--scope",
        default="all-light",
        choices=(
            "watchlist",
            "business",
            "buybacks",
            "sp500",
            "lists",
            "asx",
            "all-light",
            "all",
        ),
        help="Which universe to refresh (default: all-light)",
    )
    parser.add_argument(
        "--max-cache-days",
        type=int,
        default=7,
        help="Skip symbols with cache newer than this many days (0 = always fetch)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh all symbols in scope (ignore cache age)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="Seconds between yfinance calls (default from app constants)",
    )
    parser.add_argument(
        "--lists",
        default="",
        help="Comma-separated business list_ids (only with --scope business override)",
    )
    parser.add_argument(
        "--max-list-size",
        type=int,
        default=800,
        help=(
            "Skip business CSVs larger than this many tickers (default 800). "
            "Prevents the scheduled lists job from dying inside Japan/Korea/"
            "small-cap dumps. 0 = no cap."
        ),
    )
    args = parser.parse_args()

    force = bool(args.force)
    days = int(args.max_cache_days)
    sleep = args.sleep
    max_list_size = int(args.max_list_size) or None
    fetched = 0

    logger.info(
        "=== rebuild_yf_cache scope=%s max_cache_days=%s force=%s max_list_size=%s ===",
        args.scope,
        days,
        force,
        max_list_size,
    )

    if args.lists.strip():
        lids = [x.strip() for x in args.lists.split(",") if x.strip()]
        fetched += scope_list_ids(lids, days, force, sleep)
    elif args.scope == "watchlist":
        fetched += scope_watchlist(days, force, sleep)
    elif args.scope == "business":
        fetched += scope_business(days, force, sleep, max_list_size=max_list_size)
    elif args.scope == "buybacks":
        fetched += scope_buybacks(days, force, sleep)
    elif args.scope == "sp500":
        fetched += scope_sp500(days, force, sleep)
    elif args.scope == "asx":
        fetched += scope_asx(days, force, sleep)
    elif args.scope == "lists":
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=max_list_size)
        fetched += scope_buybacks(days, force, sleep)
    elif args.scope == "all-light":
        fetched += scope_watchlist(days, force, sleep)
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=max_list_size)
        fetched += scope_buybacks(days, force, sleep)
    elif args.scope == "all":
        fetched += scope_watchlist(days, force, sleep)
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=None)
        fetched += scope_buybacks(days, force, sleep)
        fetched += scope_asx(days, force, sleep)

    logger.info("=== rebuild_yf_cache done; symbols fetched this run: %d ===", fetched)
    print(f"YF refresh complete. Symbols fetched: {fetched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
