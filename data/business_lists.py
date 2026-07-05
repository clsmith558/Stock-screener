"""
Business Lists Data Layer
Reads ticker CSVs from the 'Business lists' folder, fetches metrics via yfinance
(sequential, rate-limited), with persistent cache + inactive skip list (like ASX).
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import yfinance as yf

from config.identity import wiki_user_agent
from data.asx import (
    DEFAULT_UNIVERSE_DB,
    YF_CACHE_MAX_AGE_DAYS,
    YF_FETCH_SLEEP_SECONDS,
    _build_earnings_history,
    _build_price_trend,
    _business_summary,
    _calc_ma200w_gap,
    _detect_yf_inactive,
    _inactive_from_yf_error,
    _is_yf_fresh,
    _metrics_from_yf_info,
    _safe_float,
    _safe_round,
    asx_official_name,
    infer_currency,
    load_asx_universe_details,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_LISTS_DIR = _PROJECT_ROOT / "Business lists"

_business_cache = {}
BUSINESS_CACHE_TTL = 600

TENTATIVE_INACTIVE_REASONS = frozenset({"cached_zero_price"})

BUILTIN_BUYBACKS_LIST_ID = "__builtin__:buybacks"
BUILTIN_ASX_LIST_ID = "__builtin__:asx"
BUILTIN_SP500_LIST_ID = "__builtin__:sp500"
BUILTIN_RUSSELL1000_LIST_ID = "__builtin__:russell1000"
BUILTIN_NASDAQ100_LIST_ID = "__builtin__:nasdaq100"
BUILTIN_LIST_IDS = (
    BUILTIN_BUYBACKS_LIST_ID,
    BUILTIN_ASX_LIST_ID,
    BUILTIN_SP500_LIST_ID,
    BUILTIN_RUSSELL1000_LIST_ID,
    BUILTIN_NASDAQ100_LIST_ID,
)


def _lists_conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS business_list_registry (
            list_id TEXT PRIMARY KEY,
            filename TEXT,
            display_name TEXT,
            ticker_count INTEGER,
            scanned_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS business_list_entries (
            list_id TEXT,
            yf_symbol TEXT,
            raw_ticker TEXT,
            PRIMARY KEY (list_id, yf_symbol)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS business_list_yf_cache (
            yf_symbol TEXT PRIMARY KEY,
            last_updated TEXT,
            price REAL,
            low_52w REAL,
            high_52w REAL,
            name TEXT,
            sector TEXT,
            metrics_json TEXT,
            p_fcf REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS business_list_inactive (
            yf_symbol TEXT PRIMARY KEY,
            reason TEXT,
            marked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT
        )
    """)
    conn.commit()
    return conn


def _normalize_raw_ticker(raw: str) -> str:
    t = (raw or "").strip().strip('"').strip("'")
    if not t or t.lower() in ("ticker", "symbol", "symbols"):
        return ""
    return t


def _to_yf_symbol(raw: str) -> str:
    """Convert CSV token to Yahoo Finance symbol."""
    t = _normalize_raw_ticker(raw)
    if not t:
        return ""
    if "." in t:
        return t.upper()
    return t.upper()


def _parse_csv_tickers(path: Path) -> list:
    """Parse tickers from varied CSV layouts (header row, .AX, .T, comma blobs)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"Cannot read {path}: {e}")
        return []

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    tickers = []
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Japan-style: one line, comma-separated 1234.T symbols
    if len(lines) <= 2 and ".T" in text and text.count(",") > 10:
        blob = text.replace("\n", "")
        for part in blob.split(","):
            t = _normalize_raw_ticker(part)
            if t:
                tickers.append(t)
        return tickers

    for line in lines:
        if line.lower() in ("ticker", "symbol", "symbols"):
            continue
        if line.lower().startswith("ticker") and len(line) < 12:
            continue
        if line.count(",") > 5:
            for part in line.split(","):
                t = _normalize_raw_ticker(part)
                if t:
                    tickers.append(t)
            continue
        if "," in line:
            for part in line.split(","):
                t = _normalize_raw_ticker(part)
                if t:
                    tickers.append(t)
        else:
            t = _normalize_raw_ticker(line)
            if t:
                tickers.append(t)

    # Dedupe preserving order
    seen = set()
    out = []
    for t in tickers:
        sym = _to_yf_symbol(t)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_sp500_tickers() -> list:
    """Load current S&P 500 constituents (Yahoo-compatible symbols)."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": wiki_user_agent()}
    try:
        import pandas as pd
        try:
            tables = pd.read_html(url, storage_options=headers)
        except TypeError:
            import io
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read()
            tables = pd.read_html(io.BytesIO(html))
        df = tables[0]
        col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        tickers = []
        for raw in df[col].tolist():
            sym = str(raw).strip().upper()
            if not sym or sym == "NAN":
                continue
            sym = sym.replace(".", "-")
            tickers.append(sym)
        if tickers:
            return tickers
    except Exception as e:
        logger.warning(f"S&P 500 Wikipedia load failed: {e}")

    # Fallback: reuse last registered SP500 universe from SQLite
    try:
        conn = _lists_conn()
        rows = conn.execute(
            "SELECT yf_symbol FROM business_list_entries WHERE list_id=? ORDER BY yf_symbol",
            (BUILTIN_SP500_LIST_ID,),
        ).fetchall()
        conn.close()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass
    return []


def _register_builtin_list(conn, list_id: str, display_name: str, symbols: list, scanned_at: str):
    conn.execute("DELETE FROM business_list_entries WHERE list_id=?", (list_id,))
    if symbols:
        conn.executemany(
            "INSERT OR REPLACE INTO business_list_entries (list_id, yf_symbol, raw_ticker) VALUES (?,?,?)",
            [(list_id, sym, sym) for sym in symbols],
        )
    conn.execute(
        """INSERT OR REPLACE INTO business_list_registry
           (list_id, filename, display_name, ticker_count, scanned_at) VALUES (?,?,?,?,?)""",
        (list_id, list_id, display_name, len(symbols), scanned_at),
    )


def sync_builtin_lists(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Register built-in ASX + S&P 500 lists (refreshed from official sources)."""
    from data.asx import load_asx_ticker_universe, ASX_MONITORED_TICKERS

    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    registered = []

    asx_base = load_asx_ticker_universe() or ASX_MONITORED_TICKERS
    asx_symbols = [f"{t}.AX" for t in asx_base]
    _register_builtin_list(conn, BUILTIN_ASX_LIST_ID, "ASX Listed Companies", asx_symbols, now)
    registered.append({
        "list_id": BUILTIN_ASX_LIST_ID,
        "filename": BUILTIN_ASX_LIST_ID,
        "display_name": "ASX Listed Companies",
        "ticker_count": len(asx_symbols),
        "scanned_at": now,
        "builtin": True,
    })

    sp500 = load_sp500_tickers()
    _register_builtin_list(conn, BUILTIN_SP500_LIST_ID, "US S&P 500", sp500, now)
    registered.append({
        "list_id": BUILTIN_SP500_LIST_ID,
        "filename": BUILTIN_SP500_LIST_ID,
        "display_name": "US S&P 500",
        "ticker_count": len(sp500),
        "scanned_at": now,
        "builtin": True,
    })

    try:
        from data.us_universe import load_russell1000_tickers, load_nasdaq100_tickers
        r1k = load_russell1000_tickers()
        ndx = load_nasdaq100_tickers()
        _register_builtin_list(conn, BUILTIN_RUSSELL1000_LIST_ID, "US Russell 1000", r1k, now)
        _register_builtin_list(conn, BUILTIN_NASDAQ100_LIST_ID, "US Nasdaq-100", ndx, now)
        registered.append({
            "list_id": BUILTIN_RUSSELL1000_LIST_ID,
            "filename": BUILTIN_RUSSELL1000_LIST_ID,
            "display_name": "US Russell 1000",
            "ticker_count": len(r1k),
            "scanned_at": now,
            "builtin": True,
        })
        registered.append({
            "list_id": BUILTIN_NASDAQ100_LIST_ID,
            "filename": BUILTIN_NASDAQ100_LIST_ID,
            "display_name": "US Nasdaq-100",
            "ticker_count": len(ndx),
            "scanned_at": now,
            "builtin": True,
        })
    except Exception as e:
        logger.warning("US Russell/Nasdaq builtin lists skipped: %s", e)

    try:
        from data.buybacks import sync_builtin_buybacks_list
        bb = sync_builtin_buybacks_list(db_path=db_path, conn=conn, now=now)
        if bb:
            registered.append(bb)
    except Exception as e:
        logger.warning("Buybacks builtin list sync skipped: %s", e)

    conn.commit()
    conn.close()
    return registered


def scan_business_lists(force: bool = False, db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Scan Business lists folder and register CSV files + tickers in SQLite."""
    sync_builtin_lists(db_path=db_path)

    if not BUSINESS_LISTS_DIR.is_dir():
        logger.warning(f"Business lists folder not found: {BUSINESS_LISTS_DIR}")
        return get_available_lists_from_db(db_path)

    conn = _lists_conn(db_path)
    registered = []
    now = datetime.now().isoformat()

    for path in sorted(BUSINESS_LISTS_DIR.glob("*.csv*")):
        list_id = path.name
        display_name = path.stem.replace(".csv", "").replace("_", " ").strip()

        if not force:
            row = conn.execute(
                "SELECT scanned_at, ticker_count FROM business_list_registry WHERE list_id=?",
                (list_id,),
            ).fetchone()
            if row:
                registered.append({
                    "list_id": list_id,
                    "filename": list_id,
                    "display_name": display_name,
                    "ticker_count": row[1],
                    "scanned_at": row[0],
                    "builtin": list_id in BUILTIN_LIST_IDS,
                })
                continue

        symbols = _parse_csv_tickers(path)
        conn.execute("DELETE FROM business_list_entries WHERE list_id=?", (list_id,))
        if symbols:
            conn.executemany(
                "INSERT OR REPLACE INTO business_list_entries (list_id, yf_symbol, raw_ticker) VALUES (?,?,?)",
                [(list_id, sym, sym) for sym in symbols],
            )
        conn.execute(
            """INSERT OR REPLACE INTO business_list_registry
               (list_id, filename, display_name, ticker_count, scanned_at) VALUES (?,?,?,?,?)""",
            (list_id, list_id, display_name, len(symbols), now),
        )
        registered.append({
            "list_id": list_id,
            "filename": list_id,
            "display_name": display_name,
            "ticker_count": len(symbols),
            "scanned_at": now,
            "builtin": False,
        })
        logger.info(f"Registered business list '{display_name}': {len(symbols)} tickers")

    conn.commit()
    conn.close()
    return get_available_lists_from_db(db_path)


def get_available_lists_from_db(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    conn = _lists_conn(db_path)
    rows = conn.execute(
        "SELECT list_id, display_name, ticker_count, scanned_at FROM business_list_registry ORDER BY display_name"
    ).fetchall()
    conn.close()
    lists = [{
        "list_id": r[0],
        "filename": r[0],
        "display_name": r[1],
        "ticker_count": r[2],
        "scanned_at": r[3],
        "builtin": r[0] in BUILTIN_LIST_IDS,
    } for r in rows]
    return _sort_available_lists(lists)


def _sort_available_lists(lists: list) -> list:
    order = {bid: i for i, bid in enumerate(BUILTIN_LIST_IDS)}
    builtins = sorted(
        [l for l in lists if l.get("builtin") or l.get("list_id") in BUILTIN_LIST_IDS],
        key=lambda x: order.get(x.get("list_id"), 99),
    )
    csvs = sorted(
        [l for l in lists if l.get("list_id") not in BUILTIN_LIST_IDS and not l.get("builtin")],
        key=lambda x: (x.get("display_name") or x.get("list_id") or "").lower(),
    )
    return builtins + csvs


def get_available_lists() -> list:
    lists = scan_business_lists(force=False)
    if not lists:
        scan_business_lists(force=True)
        lists = get_available_lists_from_db()
    return lists


def load_list_symbols(list_id: str, db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    conn = _lists_conn(db_path)
    cur = conn.execute(
        "SELECT yf_symbol FROM business_list_entries WHERE list_id=? ORDER BY yf_symbol",
        (list_id,),
    )
    symbols = [row[0] for row in cur.fetchall()]
    conn.close()
    return symbols


def load_inactive_symbols(confirmed_only: bool = False, db_path: str = DEFAULT_UNIVERSE_DB) -> set:
    try:
        conn = _lists_conn(db_path)
        if confirmed_only:
            ph = ",".join("?" for _ in TENTATIVE_INACTIVE_REASONS)
            cur = conn.execute(
                f"SELECT yf_symbol FROM business_list_inactive WHERE reason NOT IN ({ph})",
                tuple(TENTATIVE_INACTIVE_REASONS),
            )
        else:
            cur = conn.execute("SELECT yf_symbol FROM business_list_inactive")
        out = {row[0] for row in cur.fetchall()}
        conn.close()
        return out
    except Exception as e:
        logger.warning(f"load_inactive_symbols failed: {e}")
        return set()


def mark_symbol_inactive(yf_symbol: str, reason: str, source: str = "yf_fetch", db_path: str = DEFAULT_UNIVERSE_DB):
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return
    conn = _lists_conn(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO business_list_inactive (yf_symbol, reason, marked_at, source) VALUES (?,?,?,?)",
        (sym, reason or "unknown", datetime.now().isoformat(), source),
    )
    conn.commit()
    conn.close()


def remove_inactive_symbol(yf_symbol: str, db_path: str = DEFAULT_UNIVERSE_DB):
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return
    conn = _lists_conn(db_path)
    conn.execute("DELETE FROM business_list_inactive WHERE yf_symbol=?", (sym,))
    conn.commit()
    conn.close()


def filter_active_symbols(symbols: list, confirmed_only: bool = False) -> list:
    inactive = load_inactive_symbols(confirmed_only=confirmed_only)
    if not inactive:
        return list(symbols)
    active = [s for s in symbols if s not in inactive]
    skipped = len(symbols) - len(active)
    if skipped:
        label = "confirmed inactive" if confirmed_only else "inactive"
        logger.info(f"Skipping {skipped} {label} business-list symbols")
    return active


def sync_inactive_from_cache(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = _lists_conn(db_path)
        cur = conn.execute("""
            SELECT c.yf_symbol FROM business_list_yf_cache c
            LEFT JOIN business_list_inactive i ON c.yf_symbol = i.yf_symbol
            WHERE (c.price IS NULL OR c.price <= 0) AND i.yf_symbol IS NULL
        """)
        to_mark = [row[0] for row in cur.fetchall()]
        conn.close()
        for sym in to_mark:
            mark_symbol_inactive(sym, "cached_zero_price", source="cache_scan", db_path=db_path)
        if to_mark:
            logger.info(f"Added {len(to_mark)} zero-price business symbols to inactive skip list")
        return len(to_mark)
    except Exception as e:
        logger.warning(f"business sync_inactive_from_cache failed: {e}")
        return 0


def _load_asx_yf_bridge(symbols: list, max_days: int = None, db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """Reuse existing ASX yf cache for .AX symbols (instant load for ASX builtin list)."""
    ax_syms = [s for s in symbols if str(s).upper().endswith(".AX")]
    if not ax_syms:
        return {}
    if max_days is None:
        max_days = YF_CACHE_MAX_AGE_DAYS
    base_tickers = [s[:-3] for s in ax_syms]
    try:
        from data.asx import load_yf_cached_data
        asx_cached = load_yf_cached_data(base_tickers, max_days=max_days)
    except Exception as e:
        logger.warning(f"ASX yf bridge failed: {e}")
        return {}

    out = {}
    for sym in ax_syms:
        base = sym[:-3]
        ydat = asx_cached.get(base)
        if ydat:
            out[sym] = dict(ydat)
    return out


def load_yf_cached(symbols: list, max_days: int = None, db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    if not symbols:
        return {}
    if max_days is None:
        max_days = YF_CACHE_MAX_AGE_DAYS
    conn = _lists_conn(db_path)
    ph = ",".join("?" for _ in symbols)
    cur = conn.execute(
        f"""SELECT yf_symbol, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf
            FROM business_list_yf_cache WHERE yf_symbol IN ({ph})""",
        symbols,
    )
    out = {}
    for row in cur.fetchall():
        sym, last_up, price, low, high, name, sector, mjson, pf = row
        if _is_yf_fresh(last_up, max_days):
            try:
                metrics = json.loads(mjson) if mjson else {}
            except Exception:
                metrics = {}
            out[sym] = {
                "price": price or 0,
                "low_52w": low or 0,
                "high_52w": high or 0,
                "name": name or sym,
                "sector": sector or "Unknown",
                "metrics": metrics,
                "p_fcf": pf or 0,
            }
    conn.close()

    missing_ax = [s for s in symbols if s not in out and str(s).upper().endswith(".AX")]
    if missing_ax:
        bridged = _load_asx_yf_bridge(missing_ax, max_days=max_days, db_path=db_path)
        out.update(bridged)
    return out


def save_yf_cached(yf_symbol: str, data: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    conn = _lists_conn(db_path)
    metrics_json = json.dumps(data.get("metrics") or {})
    conn.execute("""
        INSERT OR REPLACE INTO business_list_yf_cache
        (yf_symbol, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        yf_symbol,
        datetime.now().isoformat(),
        data.get("price") or 0,
        data.get("low_52w") or 0,
        data.get("high_52w") or 0,
        data.get("name") or yf_symbol,
        data.get("sector") or "Unknown",
        metrics_json,
        data.get("p_fcf") or 0,
    ))
    conn.commit()
    conn.close()


def _fetch_yf_sequential(
    symbols: list,
    sleep_seconds: float = None,
    progress_every: int = 50,
    retry_tentative_inactive: bool = False,
    progress_label: str = "business yf",
    on_progress=None,
) -> dict:
    if sleep_seconds is None:
        sleep_seconds = YF_FETCH_SLEEP_SECONDS
    sync_inactive_from_cache()
    symbols = filter_active_symbols(symbols, confirmed_only=retry_tentative_inactive)
    inactive_skip = load_inactive_symbols(confirmed_only=retry_tentative_inactive)
    out = {}
    total = len(symbols)
    if not total:
        logger.info("Business list yf fetch: no active symbols.")
        return out

    logger.info(f"Business list yf fetch starting for {total} symbols (sleep={sleep_seconds}s)...")
    for i, sym in enumerate(symbols):
        if sym in inactive_skip:
            continue
        if i and i % progress_every == 0:
            logger.info(f"  {progress_label} progress: {i}/{total}")
            if on_progress:
                try:
                    on_progress(i, total)
                except Exception as ex:
                    logger.warning("on_progress callback failed: %s", ex)

        try:
            yt = yf.Ticker(sym)
            info = yt.info or {}
            price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice"))

            if price <= 0:
                reason = _detect_yf_inactive(info, price) or "quote_not_found"
                mark_symbol_inactive(sym, reason, source="yf_fetch")
                inactive_skip.add(sym)
                logger.info(f"  {sym}: inactive ({reason}) — skip list")
                time.sleep(sleep_seconds)
                continue

            low = _safe_float(info.get("fiftyTwoWeekLow"))
            high = _safe_float(info.get("fiftyTwoWeekHigh"))
            p_fcf = 0
            try:
                fcf = _safe_float(info.get("freeCashflow"))
                shares = _safe_float(info.get("sharesOutstanding")) or _safe_float(info.get("impliedSharesOutstanding"))
                if price and fcf and shares > 0:
                    fps = fcf / shares
                    if fps > 0:
                        p_fcf = _safe_round(price / fps, 1)
            except Exception:
                pass

            ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
            metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=sym)
            data = {
                "price": _safe_round(price, 2),
                "low_52w": _safe_round(low, 2),
                "high_52w": _safe_round(high, 2),
                "name": info.get("shortName") or info.get("longName") or sym,
                "sector": info.get("sector") or info.get("industry") or "Unknown",
                "metrics": metrics,
                "p_fcf": p_fcf,
            }
            out[sym] = data
            save_yf_cached(sym, data)
            remove_inactive_symbol(sym)
            inactive_skip.discard(sym)
            time.sleep(sleep_seconds)
        except Exception as e:
            msg = str(e)
            logger.warning(f"  business yf error for {sym}: {msg[:120]}")
            inactive_reason = _inactive_from_yf_error(msg)
            if inactive_reason:
                mark_symbol_inactive(sym, inactive_reason, source="yf_error")
                inactive_skip.add(sym)
                logger.info(f"  {sym}: inactive ({inactive_reason})")
            else:
                logger.info(f"  {sym}: transient error, will retry later")
            sleep = 30 if "Rate" in msg or "429" in msg else max(1.0, sleep_seconds / 2)
            time.sleep(sleep)

    logger.info(f"Business list yf fetch complete: {len(out)} / {total}")
    if on_progress:
        try:
            on_progress(total, total)
        except Exception as ex:
            logger.warning("on_progress callback failed: %s", ex)
    return out


def _placeholder_row(sym: str, list_meta: dict) -> dict:
    """Row for symbols not yet in yf cache — shown instantly until Rebuild YF runs."""
    return {
        "ticker": sym,
        "name": sym,
        "sector": "Unknown",
        "list_name": list_meta.get("display_name") or list_meta.get("list_id", ""),
        "insider_buys_2026": 0,
        "buyback_announced": False,
        "buyback_date": None,
        "pct_from_52w_low": 0,
        "pct_from_200w_ma": 0,
        "last_activity": datetime.now().strftime("%Y-%m-%d"),
        "current_price": 0,
        "low_52w": 0,
        "high_52w": 0,
        "market_cap": 0,
        "currency": infer_currency(ticker=sym),
        "net_cash_pct_mcap": 0,
        "net_assets_vs_mcap_pct": 0,
        "pe": 0,
        "pb": 0,
        "p_fcf": 0,
        "metrics": {
            "pe": 0, "forward_pe": 0, "pb": 0, "p_fcf": 0, "pcf": 0,
            "ev_ebitda": 0, "debt_to_equity": 0, "cash_on_hand_m": 0, "net_cash_m": 0,
            "net_cash_pct_mcap": 0, "net_assets_m": 0, "net_assets_vs_mcap_m": 0,
            "net_assets_vs_mcap_pct": 0, "currency": infer_currency(ticker=sym),
            "roe": 0, "insider_ownership_pct": 0, "roic": 0, "fcf_yield": 0, "div_yield": 0, "market_cap": 0,
            "ma_200w": 0, "pct_from_200w_ma": 0,
        },
        "signals": [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "Awaiting yfinance data — use Rebuild YF"}],
        "_market": list_meta.get("_market") or "business",
        "_list_id": list_meta.get("list_id"),
        "_pending_yf": True,
    }


def _stock_row(sym: str, ydat: dict, list_meta: dict) -> dict:
    price = ydat.get("price", 0)
    low = ydat.get("low_52w", 0)
    m = ydat.get("metrics", {}) or {}
    p_fcf = ydat.get("p_fcf", m.get("p_fcf", 0) or 0)
    pct_from_low = 0.0
    if price and low and low > 0:
        pct_from_low = _safe_round(((price - low) / low) * 100, 1)

    return {
        "ticker": sym,
        "name": ydat.get("name") or sym,
        "sector": ydat.get("sector") or "Unknown",
        "list_name": list_meta.get("display_name") or list_meta.get("list_id", ""),
        "insider_buys_2026": 0,
        "buyback_announced": False,
        "buyback_date": None,
        "pct_from_52w_low": pct_from_low,
        "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
        "last_activity": datetime.now().strftime("%Y-%m-%d"),
        "current_price": _safe_round(price, 2) if price else 0,
        "low_52w": _safe_round(low, 2) if low else 0,
        "high_52w": _safe_round(ydat.get("high_52w", 0), 2),
        "market_cap": m.get("market_cap", 0),
        "currency": m.get("currency") or infer_currency(ticker=sym),
        "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
        "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
        "pe": m.get("pe", 0),
        "pb": m.get("pb", 0),
        "p_fcf": p_fcf,
        "insider_ownership_pct": m.get("insider_ownership_pct", 0),
        "metrics": {
            "pe": m.get("pe", 0),
            "forward_pe": m.get("forward_pe", 0),
            "pb": m.get("pb", 0),
            "p_fcf": p_fcf,
            "pcf": m.get("pcf", 0),
            "ev_ebitda": m.get("ev_ebitda", 0),
            "debt_to_equity": m.get("debt_to_equity", 0),
            "cash_on_hand_m": m.get("cash_on_hand_m", 0),
            "net_cash_m": m.get("net_cash_m", 0),
            "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
            "net_assets_m": m.get("net_assets_m", 0),
            "net_assets_vs_mcap_m": m.get("net_assets_vs_mcap_m", 0),
            "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
            "currency": m.get("currency") or infer_currency(ticker=sym),
            "roe": m.get("roe", 0),
            "insider_ownership_pct": m.get("insider_ownership_pct", 0),
            "roic": m.get("roic", 0),
            "fcf_yield": m.get("fcf_yield", 0),
            "div_yield": m.get("div_yield", 0),
            "market_cap": m.get("market_cap", 0),
            "ma_200w": m.get("ma_200w", 0),
            "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
        },
        "signals": [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "Business list — no signal scan"}],
        "_market": "business",
        "_list_id": list_meta.get("list_id"),
        "_live": True,
    }


def fetch_business_list_stocks(
    list_id: str,
    force_yf: bool = False,
    signals_only: bool = False,
) -> list:
    """Load stocks for one business list CSV (yf cache + slow sequential refresh)."""
    if not list_id:
        return []

    if list_id == BUILTIN_BUYBACKS_LIST_ID:
        from data.buybacks import fetch_buyback_stocks_for_business_list
        cache_key = list_id
        now = time.time()
        if (
            cache_key in _business_cache
            and now - _business_cache[cache_key].get("ts", 0) < BUSINESS_CACHE_TTL
            and not force_yf
        ):
            return list(_business_cache[cache_key]["data"])
        results = fetch_buyback_stocks_for_business_list(force_yf=force_yf)
        _business_cache[cache_key] = {
            "data": results,
            "ts": now,
            "last_scraped": datetime.now().isoformat(),
        }
        return results

    scan_business_lists(force=False)
    symbols = load_list_symbols(list_id)
    if not symbols:
        scan_business_lists(force=True)
        symbols = load_list_symbols(list_id)

    cache_key = f"{list_id}:signals" if signals_only else list_id
    now = time.time()
    if cache_key in _business_cache and now - _business_cache[cache_key].get("ts", 0) < BUSINESS_CACHE_TTL and not force_yf:
        return list(_business_cache[cache_key]["data"])

    list_meta = {"list_id": list_id, "display_name": list_id.replace(".csv", "").replace("_", " ")}
    conn = _lists_conn()
    row = conn.execute(
        "SELECT display_name, ticker_count FROM business_list_registry WHERE list_id=?",
        (list_id,),
    ).fetchone()
    conn.close()
    if row:
        list_meta["display_name"] = row[0]
        list_meta["universe_count"] = row[1]

    sync_inactive_from_cache()
    symbols = filter_active_symbols(symbols)

    max_yf_age = 0 if force_yf else YF_CACHE_MAX_AGE_DAYS
    cached = load_yf_cached(symbols, max_days=max_yf_age)
    stale = [s for s in symbols if s not in cached]

    if stale and force_yf:
        logger.info(f"[FORCED] Business list '{list_id}': fetching {len(stale)} symbols sequentially...")
        newly = _fetch_yf_sequential(
            stale,
            sleep_seconds=YF_FETCH_SLEEP_SECONDS,
            progress_every=25,
            retry_tentative_inactive=True,
        )
        cached.update(newly)
    elif stale:
        logger.info(
            f"Business list '{list_id}': {len(cached)} cached, {len(stale)} pending — instant load (use Rebuild YF to fetch)"
        )
    else:
        logger.info(f"Business list '{list_id}': all {len(symbols)} symbols from yf cache.")

    list_meta["_market"] = "business"
    asx_univ = load_asx_universe_details()

    results = []
    for sym in symbols:
        ydat = cached.get(sym, {})
        if ydat and (ydat.get("price") or 0):
            row = _stock_row(sym, ydat, list_meta)
            row["_market"] = "business"
            results.append(row)
        else:
            row = _placeholder_row(sym, list_meta)
            row["_market"] = "business"
            results.append(row)
        if sym.upper().endswith(".AX"):
            row["name"] = asx_official_name(sym, asx_univ, fallback=row.get("name") or sym)

    try:
        from data.edgar_metrics import enrich_stocks_edgar_cached
        results = enrich_stocks_edgar_cached(results)
    except Exception as e:
        logger.warning("EDGAR cache enrich skipped for business list '%s': %s", list_id, e)

    if signals_only:
        try:
            from data.buybacks import enrich_stocks_with_buyback_signals
            results = enrich_stocks_with_buyback_signals(results, filter_only=True)
        except Exception as e:
            logger.warning("Buyback signal filter skipped for '%s': %s", list_id, e)
            results = []

    results.sort(key=lambda s: (
        1 if s.get("_pending_yf") else 0,
        s.get("pct_from_52w_low") or 9999,
    ))
    _business_cache[cache_key] = {"data": results, "ts": now, "last_scraped": datetime.now().isoformat()}
    return results


def get_business_meta(list_id: str) -> dict:
    if list_id == BUILTIN_BUYBACKS_LIST_ID:
        try:
            from data.buybacks import get_buybacks_meta
            meta = get_buybacks_meta()
            meta["market"] = "business"
            meta["list_id"] = list_id
            meta["list_name"] = "Buybacks & insider activity"
            meta["data_source"] = meta.get("data_source") or "Buybacks registry • yfinance"
            return meta
        except Exception:
            pass

    scan_business_lists(force=False)
    conn = _lists_conn()
    row = conn.execute(
        "SELECT display_name, ticker_count, scanned_at FROM business_list_registry WHERE list_id=?",
        (list_id,),
    ).fetchone()
    inactive = conn.execute("SELECT COUNT(*) FROM business_list_inactive").fetchone()[0] or 0
    yf_count = conn.execute("SELECT COUNT(*) FROM business_list_yf_cache").fetchone()[0] or 0
    conn.close()

    cache = _business_cache.get(list_id, {})
    return {
        "data_source": f"yfinance • Business list: {(row[0] if row else list_id)}",
        "last_scraped": cache.get("last_scraped") or datetime.now().isoformat(),
        "market": "business",
        "list_id": list_id,
        "list_name": row[0] if row else list_id,
        "universe_count": row[1] if row else 0,
        "scanned_at": row[2] if row else None,
        "yf_cached_count": yf_count,
        "inactive_count": inactive,
    }


def _fetch_single_business(yf_symbol: str):
    """Rich detail fetch for business-list modal."""
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return None
    try:
        yt = yf.Ticker(sym)
        info = yt.info or {}
        price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice"))
        low = _safe_float(info.get("fiftyTwoWeekLow"))
        high = _safe_float(info.get("fiftyTwoWeekHigh"))
        pct = 0.0
        if price and low > 0:
            pct = _safe_round(((price - low) / low) * 100, 1)

        p_fcf = 0
        try:
            fcf = _safe_float(info.get("freeCashflow"))
            shares = _safe_float(info.get("sharesOutstanding")) or _safe_float(info.get("impliedSharesOutstanding"))
            if price and fcf and shares > 0:
                fps = fcf / shares
                if fps > 0:
                    p_fcf = _safe_round(price / fps, 1)
        except Exception:
            pass

        ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
        metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=sym)
        summary = _business_summary(info)
        price_trend = _build_price_trend(yt, period="2y", interval="1wk")
        earnings_history = _build_earnings_history(yt)

        stock = _stock_row(sym, {
            "price": price,
            "low_52w": low,
            "high_52w": high,
            "name": info.get("shortName") or info.get("longName") or sym,
            "sector": info.get("sector") or info.get("industry") or "Unknown",
            "metrics": metrics,
            "p_fcf": p_fcf,
        }, {"list_id": "", "display_name": "Business list"})
        stock.update({
            "summary": summary,
            "price_trend": price_trend,
            "earnings_history": earnings_history,
            "_live": True,
        })
        stock["pct_from_52w_low"] = pct
        stock["_market"] = "business"
        if sym.endswith(".AX"):
            stock["name"] = asx_official_name(sym, fallback=stock.get("name") or sym)
        try:
            from data.edgar_metrics import is_us_edgar_eligible, fetch_edgar_metrics, attach_edgar_to_stock
            if is_us_edgar_eligible(sym):
                stock["_edgar_eligible"] = True
                edgar = fetch_edgar_metrics(sym)
                attach_edgar_to_stock(stock, edgar=edgar, ticker=sym)
            else:
                stock["_edgar_eligible"] = False
        except Exception as ex:
            logger.warning("EDGAR detail fetch failed for business %s: %s", sym, ex)
        try:
            from data.buybacks import build_quarterly_buyback_breakdown

            shares_out = _safe_float(info.get("sharesOutstanding")) or _safe_float(
                info.get("impliedSharesOutstanding")
            )
            quarterly = build_quarterly_buyback_breakdown(
                sym,
                shares_outstanding=shares_out if shares_out > 0 else None,
            )
            if quarterly:
                stock["buyback_quarterly"] = quarterly
        except Exception as ex:
            logger.debug("Quarterly buyback breakdown skipped for %s: %s", sym, ex)
        try:
            from data.buybacks import merge_buyback_registry_into_stock
            merge_buyback_registry_into_stock(stock, sym=sym)
        except Exception as ex:
            logger.debug("Buyback registry overlay skipped for %s: %s", sym, ex)
        return stock
    except Exception as e:
        logger.warning(f"_fetch_single_business failed for {sym}: {e}")
        return None


def rebuild_business_yf_cache(list_id: str, sleep_seconds: float = None):
    """CLI / manual rebuild for one business list."""
    symbols = filter_active_symbols(load_list_symbols(list_id), confirmed_only=True)
    logger.info(f"=== REBUILD business list YF: {list_id} ({len(symbols)} symbols) ===")
    _fetch_yf_sequential(symbols, sleep_seconds=sleep_seconds, retry_tentative_inactive=True)
    _business_cache.pop(list_id, None)
    logger.info("=== Business list YF rebuild finished ===")