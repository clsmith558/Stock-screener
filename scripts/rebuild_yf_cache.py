#!/usr/bin/env python3
"""
Refresh yfinance price/metrics caches used by the screener.

Unlike the dashboard "Rebuild YF" buttons (need Flask running), this is a CLI
job for Task Scheduler / run_scheduled_updates.py.

Scopes:
  watchlist     — tickers on the personal watchlist (fast)
  business      — CSV business lists + S&P 500 (filtered by --max-list-size)
  buybacks      — buybacks registry universe
  sp500         — US S&P 500 builtin only
  lists         — business CSVs (<=max-list-size) + buybacks + sp500
  large-lists   — business CSVs / builtins larger than --max-list-size
  asx           — full ASX listed universe
  large-and-asx — rotating oldest-first chunk across >max-list-size lists + full ASX
  all-light     — watchlist + lists (not full ASX)

By default only symbols with cache older than --max-cache-days are refetched.
Use --force to refresh every symbol in the scope, and --max-symbols N to cap
how many oldest/missing symbols are fetched per run (rotating over time).

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


def _load_yf_timestamps(table: str, key_col: str) -> dict[str, str]:
    """Return {SYMBOL_UPPER: last_updated_iso} for all rows in a YF cache table."""
    from data.asx import _get_universe_conn
    from data.business_lists import DEFAULT_UNIVERSE_DB, _lists_conn

    conn = (
        _get_universe_conn()
        if table == "asx_yf_cache"
        else _lists_conn(DEFAULT_UNIVERSE_DB)
    )
    try:
        rows = conn.execute(f"SELECT {key_col}, last_updated FROM {table}").fetchall()
    finally:
        conn.close()
    out: dict[str, str] = {}
    for sym, upd in rows:
        if sym:
            out[str(sym).upper()] = str(upd or "")
    return out


def _order_and_cap_stale(
    symbols: list[str],
    ts_map: dict[str, str],
    max_symbols: int = 0,
    *,
    label: str = "symbols",
) -> list[str]:
    """Sort stale symbols oldest/missing-first and optionally cap to `max_symbols`."""
    uniq = list(dict.fromkeys(symbols))
    ordered = sorted(uniq, key=lambda s: (ts_map.get(s.upper(), ""), s.upper()))
    if max_symbols and max_symbols > 0 and len(ordered) > max_symbols:
        logger.info(
            "%s: capping %d stale symbols to %d (oldest/missing first)",
            label,
            len(ordered),
            max_symbols,
        )
        return ordered[:max_symbols]
    return ordered


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


def _select_business_lists(
    *,
    min_size: int | None = None,
    max_size: int | None = None,
) -> tuple[list[str], list[str]]:
    """Return (selected_list_ids, skipped_labels) sorted smallest-first."""
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
        L for L in lists if L.get("list_id") and L.get("list_id") not in skip
    ]
    candidates.sort(key=lambda L: int(L.get("ticker_count") or 10**9))
    selected: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for L in candidates:
        lid = L.get("list_id")
        n = int(L.get("ticker_count") or 0)
        if not lid or lid in seen:
            continue
        seen.add(lid)
        if max_size and n > max_size:
            skipped.append(f"{lid}:{n}")
            continue
        if min_size and n <= min_size:
            continue
        selected.append(lid)
    return selected, skipped


def _gather_unique_business_symbols(list_ids: list[str]) -> list[str]:
    """Deduplicate active symbols across a collection of business list IDs."""
    from data.business_lists import filter_active_symbols, load_list_symbols

    agg: set[str] = set()
    for lid in list_ids:
        for sym in load_list_symbols(lid) or []:
            if sym:
                agg.add(sym)
    return filter_active_symbols(sorted(agg), confirmed_only=True)


def scope_business(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    max_list_size: int | None = None,
    max_symbols: int = 0,
    dry_run: bool = False,
) -> int:
    """All non-ASX-full business CSV lists + S&P 500 (not full ASX builtin)."""
    ordered, skipped_large = _select_business_lists(max_size=max_list_size)
    if skipped_large:
        logger.info(
            "Skipping %d large lists (>%s tickers): %s",
            len(skipped_large),
            max_list_size,
            ", ".join(skipped_large[:12]),
        )
    logger.info("Business lists to refresh: %d", len(ordered))
    if max_symbols or dry_run:
        from data.business_lists import _fetch_yf_sequential

        symbols = _gather_unique_business_symbols(ordered)
        need = _stale_business_symbols(symbols, max_cache_days, force)
        ts_map = _load_yf_timestamps("business_list_yf_cache", "yf_symbol")
        need = _order_and_cap_stale(need, ts_map, max_symbols, label="business yf")
        logger.info("Business (<%s) YF: %d unique active, %d to fetch", max_list_size, len(symbols), len(need))
        if dry_run or not need:
            return len(need) if dry_run else 0
        _fetch_yf_sequential(
            need,
            sleep_seconds=sleep,
            progress_every=10,
            retry_tentative_inactive=True,
            progress_label="business yf",
        )
        return len(need)
    return scope_list_ids(ordered, max_cache_days, force, sleep)


def scope_large_lists(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    min_list_size: int = 800,
    max_symbols: int = 0,
    dry_run: bool = False,
) -> int:
    """Refresh >min_list_size business lists oldest-first with optional symbol budget."""
    from data.business_lists import _fetch_yf_sequential

    large_ids, _ = _select_business_lists(min_size=min_list_size)
    logger.info("Large business lists (>%s tickers): %d", min_list_size, len(large_ids))
    if not large_ids:
        return 0
    symbols = _gather_unique_business_symbols(large_ids)
    need = _stale_business_symbols(symbols, max_cache_days, force)
    ts_map = _load_yf_timestamps("business_list_yf_cache", "yf_symbol")
    chosen = _order_and_cap_stale(need, ts_map, max_symbols, label="large lists yf")
    logger.info("Large lists YF: %d unique active, %d stale, %d selected", len(symbols), len(need), len(chosen))
    if dry_run or not chosen:
        return len(chosen) if dry_run else 0
    _fetch_yf_sequential(
        chosen,
        sleep_seconds=sleep,
        progress_every=10,
        retry_tentative_inactive=True,
        progress_label="large lists yf",
    )
    return len(chosen)


def _stale_asx_with_timestamps(
    max_cache_days: int, force: bool
) -> tuple[list[str], list[str], dict[str, str]]:
    from data.asx import (
        ASX_MONITORED_TICKERS,
        load_asx_ticker_universe,
        sync_inactive_from_cache,
    )

    sync_inactive_from_cache()
    base = list(load_asx_ticker_universe() or ASX_MONITORED_TICKERS)
    need = _stale_asx_tickers(base, max_cache_days, force)
    ts_map = _load_yf_timestamps("asx_yf_cache", "ticker")
    return base, need, ts_map


def scope_large_and_asx(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    min_list_size: int = 800,
    max_symbols: int = 900,
    dry_run: bool = False,
) -> int:
    """Rotating oldest-first chunk across >min_list_size business lists + full ASX."""
    from data.asx import _fetch_yf_data_sequentially
    from data.business_lists import _fetch_yf_sequential

    large_ids, _ = _select_business_lists(min_size=min_list_size)
    biz_symbols = _gather_unique_business_symbols(large_ids) if large_ids else []
    biz_stale = _stale_business_symbols(biz_symbols, max_cache_days, force)
    biz_ts = _load_yf_timestamps("business_list_yf_cache", "yf_symbol")

    asx_base, asx_stale, asx_ts = _stale_asx_with_timestamps(max_cache_days, force)

    combined: list[tuple[str, str, str]] = []
    for s in dict.fromkeys(biz_stale):
        combined.append((biz_ts.get(s.upper(), ""), "biz", s))
    for t in dict.fromkeys(asx_stale):
        combined.append((asx_ts.get(t.upper(), ""), "asx", t))
    combined.sort(key=lambda item: (item[0], item[1], item[2].upper()))

    total_stale = len(combined)
    if max_symbols and max_symbols > 0 and len(combined) > max_symbols:
        chosen = combined[:max_symbols]
    else:
        chosen = combined

    asx_chunk = [sym for _, pool, sym in chosen if pool == "asx"]
    biz_chunk = [sym for _, pool, sym in chosen if pool == "biz"]
    oldest_ts = chosen[0][0][:10] if chosen and chosen[0][0] else "missing"
    newest_ts = chosen[-1][0][:10] if chosen and chosen[-1][0] else "missing"
    logger.info(
        "Large-and-ASX YF: lists=%d (biz_active=%d, biz_stale=%d), asx_universe=%d (asx_stale=%d); "
        "selected %d/%d stale (asx=%d, biz=%d, ts_range=%s..%s)",
        len(large_ids),
        len(biz_symbols),
        len(biz_stale),
        len(asx_base),
        len(asx_stale),
        len(chosen),
        total_stale,
        len(asx_chunk),
        len(biz_chunk),
        oldest_ts,
        newest_ts,
    )
    if dry_run or not chosen:
        return len(chosen) if dry_run else 0

    if asx_chunk:
        _fetch_yf_data_sequentially(
            asx_chunk,
            sleep_seconds=sleep,
            retry_tentative_inactive=True,
        )
    if biz_chunk:
        _fetch_yf_sequential(
            biz_chunk,
            sleep_seconds=sleep,
            progress_every=10,
            retry_tentative_inactive=True,
            progress_label="large lists yf",
        )
    return len(chosen)


def scope_buybacks(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    max_symbols: int = 0,
    dry_run: bool = False,
) -> int:
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
    ts_map = _load_yf_timestamps("business_list_yf_cache", "yf_symbol")
    chosen = _order_and_cap_stale(need, ts_map, max_symbols, label="buybacks yf")
    logger.info("Buybacks YF: %d symbols, %d stale, %d to fetch", len(symbols), len(need), len(chosen))
    if dry_run or not chosen:
        return len(chosen) if dry_run else 0
    _fetch_yf_sequential(
        chosen,
        sleep_seconds=sleep,
        progress_every=10,
        retry_tentative_inactive=True,
        progress_label="buybacks yf",
    )
    return len(chosen)


def scope_sp500(max_cache_days: int, force: bool, sleep: float | None) -> int:
    from data.business_lists import BUILTIN_SP500_LIST_ID, sync_builtin_lists

    try:
        sync_builtin_lists()
    except Exception as e:
        logger.warning("sync_builtin_lists: %s", e)
    return scope_list_ids([BUILTIN_SP500_LIST_ID], max_cache_days, force, sleep)


def scope_asx(
    max_cache_days: int,
    force: bool,
    sleep: float | None,
    *,
    max_symbols: int = 0,
    dry_run: bool = False,
) -> int:
    from data.asx import _fetch_yf_data_sequentially

    base, need, ts_map = _stale_asx_with_timestamps(max_cache_days, force)
    chosen = _order_and_cap_stale(need, ts_map, max_symbols, label="ASX yf")
    logger.info(
        "ASX YF: universe %d, stale %d, to fetch %d",
        len(base),
        len(need),
        len(chosen),
    )
    if dry_run or not chosen:
        return len(chosen) if dry_run else 0
    _fetch_yf_data_sequentially(
        chosen,
        sleep_seconds=sleep,
        retry_tentative_inactive=True,
    )
    return len(chosen)


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
            "large-lists",
            "asx",
            "large-and-asx",
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
            "List size threshold (default 800). Skips lists >N in business/lists "
            "scopes, or selects lists >N in large-lists / large-and-asx scopes. 0 = no cap."
        ),
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help=(
            "Cap total stale symbols fetched per run (0 = unlimited). Selects "
            "oldest/missing caches first so runs rotate across the universe."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log counts/chunks that would be fetched without hitting yfinance",
    )
    args = parser.parse_args()

    force = bool(args.force)
    days = int(args.max_cache_days)
    sleep = args.sleep
    max_list_size = int(args.max_list_size) or None
    max_symbols = max(0, int(args.max_symbols or 0))
    dry_run = bool(args.dry_run)
    fetched = 0

    logger.info(
        "=== rebuild_yf_cache scope=%s max_cache_days=%s force=%s max_list_size=%s max_symbols=%s dry_run=%s ===",
        args.scope,
        days,
        force,
        max_list_size,
        max_symbols,
        dry_run,
    )

    if args.lists.strip():
        lids = [x.strip() for x in args.lists.split(",") if x.strip()]
        fetched += scope_list_ids(lids, days, force, sleep) if not dry_run else 0
    elif args.scope == "watchlist":
        fetched += scope_watchlist(days, force, sleep) if not dry_run else 0
    elif args.scope == "business":
        fetched += scope_business(
            days, force, sleep, max_list_size=max_list_size, max_symbols=max_symbols, dry_run=dry_run
        )
    elif args.scope == "buybacks":
        fetched += scope_buybacks(
            days, force, sleep, max_symbols=max_symbols, dry_run=dry_run
        )
    elif args.scope == "sp500":
        fetched += scope_sp500(days, force, sleep) if not dry_run else 0
    elif args.scope == "large-lists":
        fetched += scope_large_lists(
            days,
            force,
            sleep,
            min_list_size=max_list_size or 800,
            max_symbols=max_symbols,
            dry_run=dry_run,
        )
    elif args.scope == "asx":
        fetched += scope_asx(
            days, force, sleep, max_symbols=max_symbols, dry_run=dry_run
        )
    elif args.scope == "large-and-asx":
        fetched += scope_large_and_asx(
            days,
            force,
            sleep,
            min_list_size=max_list_size or 800,
            max_symbols=max_symbols or 900,
            dry_run=dry_run,
        )
    elif args.scope == "lists":
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=max_list_size)
        fetched += scope_buybacks(days, force, sleep, max_symbols=max_symbols)
    elif args.scope == "all-light":
        fetched += scope_watchlist(days, force, sleep)
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=max_list_size)
        fetched += scope_buybacks(days, force, sleep, max_symbols=max_symbols)
    elif args.scope == "all":
        fetched += scope_watchlist(days, force, sleep)
        fetched += scope_sp500(days, force, sleep)
        fetched += scope_business(days, force, sleep, max_list_size=None)
        fetched += scope_buybacks(days, force, sleep, max_symbols=max_symbols)
        fetched += scope_asx(days, force, sleep, max_symbols=max_symbols)

    logger.info("=== rebuild_yf_cache done; symbols fetched this run: %d ===", fetched)
    print(f"YF refresh complete. Symbols fetched: {fetched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
