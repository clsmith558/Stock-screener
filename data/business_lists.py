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
from data.yf_helpers import (
    calc_p_fcf,
    pct_from_52w_low,
    quote_price,
    reconcile_52w_range,
    sanitize_stock_figures,
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
BUILTIN_PARIAH_LIST_ID = "__builtin__:pariah"
BUILTIN_LIST_IDS = (
    BUILTIN_BUYBACKS_LIST_ID,
    BUILTIN_ASX_LIST_ID,
    BUILTIN_SP500_LIST_ID,
    BUILTIN_RUSSELL1000_LIST_ID,
    BUILTIN_NASDAQ100_LIST_ID,
    BUILTIN_PARIAH_LIST_ID,
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
    # Strip UTF-8 BOM that can stick to the first header cell
    t = t.lstrip("\ufeff").strip()
    if not t or t.lower() in ("ticker", "symbol", "symbols", "name", "region", "notes", "layer"):
        return ""
    if t.startswith("#"):
        return ""
    return t


def _looks_like_yf_symbol(sym: str) -> bool:
    """Reject prose / company names that leaked from multi-column CSVs."""
    if not sym:
        return False
    s = sym.strip().upper()
    # Company names, comments, regions — never valid Yahoo symbols
    if " " in s or "\t" in s:
        return False
    if s.startswith("#"):
        return False
    if len(s) > 16:
        return False
    # Allow AAL, BRK-B, 7203.T, 005930.KS, GOLL4.SA, VOLARA.MX, FIA1S.HE
    import re
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9._\-]*", s))


def _to_yf_symbol(raw: str) -> str:
    """Convert CSV token to Yahoo Finance symbol."""
    t = _normalize_raw_ticker(raw)
    if not t:
        return ""
    if "." in t:
        sym = t.upper()
    else:
        sym = t.upper()
    return sym if _looks_like_yf_symbol(sym) else ""


def _parse_csv_tickers(path: Path) -> list:
    """Parse tickers from varied CSV layouts (header row, .AX, .T, comma blobs).

    Multi-column CSVs with a ticker/symbol header use **only that column**.
    Comment lines starting with # are ignored. Tokens with spaces are rejected.
    """
    import csv
    import io

    try:
        # utf-8-sig strips BOM so header "ticker" matches cleanly
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except Exception as e:
        logger.warning(f"Cannot read {path}: {e}")
        return []

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    tickers = []
    # Drop comment lines (# …) before layout detection
    lines = [
        ln.strip()
        for ln in text.split("\n")
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return []

    # Multi-column CSV with ticker/symbol header → that column only
    # (avoids treating "American Airlines" name cells as tickers)
    header_cells = [c.strip().lower().strip('"').lstrip("\ufeff") for c in lines[0].split(",")]
    ticker_col_keys = ("ticker", "symbol", "yf_symbol", "symbols")
    if any(h in ticker_col_keys for h in header_cells) and len(header_cells) >= 2:
        try:
            reader = csv.DictReader(io.StringIO("\n".join(lines)))
            field = next(
                (
                    f
                    for f in (reader.fieldnames or [])
                    if (f or "").strip().lower().lstrip("\ufeff") in ticker_col_keys
                ),
                None,
            )
            if field:
                for row in reader:
                    t = _normalize_raw_ticker((row or {}).get(field) or "")
                    if t:
                        tickers.append(t)
                seen_h: set[str] = set()
                out_h: list[str] = []
                for t in tickers:
                    sym = _to_yf_symbol(t)
                    if sym and sym not in seen_h:
                        seen_h.add(sym)
                        out_h.append(sym)
                return out_h
        except Exception as e:
            logger.warning("CSV DictReader parse failed for %s: %s — falling back", path.name, e)

    # Asia-style blob: one/two lines, comma-separated 7203.T / 005930.KS / 035420.KQ
    if len(lines) <= 2 and "\n".join(lines).count(",") > 10 and any(
        sfx in "\n".join(lines) for sfx in (".T", ".KS", ".KQ", ".AX", ".HK")
    ):
        blob = "\n".join(lines).replace("\n", "")
        for part in blob.split(","):
            t = _normalize_raw_ticker(part)
            if t:
                tickers.append(t)
        seen_b: set[str] = set()
        out_b: list[str] = []
        for t in tickers:
            sym = _to_yf_symbol(t)
            if sym and sym not in seen_b:
                seen_b.add(sym)
                out_b.append(sym)
        return out_b

    for line in lines:
        low = line.lower()
        if low in ("ticker", "symbol", "symbols"):
            continue
        # Header row: ticker,name,... — skip entire line
        if low.startswith("ticker") and ("," in line or len(line) < 12):
            continue
        # Multi-column data row: **first cell only** (never name/region/notes)
        if "," in line:
            first = line.split(",")[0]
            t = _normalize_raw_ticker(first)
            if t:
                tickers.append(t)
            continue
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


def refresh_us_index_builtin_lists(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """
    Re-fetch S&P 500, Russell 1000, and Nasdaq-100 constituents and re-register
    the builtin business lists. Intended for ~6-month cadence (Wikipedia sources).
    """
    from data.us_universe import load_nasdaq100_tickers, load_russell1000_tickers

    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    registered = []

    def _clean(symbols: list) -> list:
        # Drop confirmed-inactive so delisted leavers do not re-enter
        return filter_active_symbols(list(symbols or []), confirmed_only=True)

    sp500 = _clean(load_sp500_tickers())
    _register_builtin_list(conn, BUILTIN_SP500_LIST_ID, "US S&P 500", sp500, now)
    registered.append({
        "list_id": BUILTIN_SP500_LIST_ID,
        "display_name": "US S&P 500",
        "ticker_count": len(sp500),
        "scanned_at": now,
        "source": "wikipedia",
    })

    r1k = _clean(load_russell1000_tickers())
    _register_builtin_list(conn, BUILTIN_RUSSELL1000_LIST_ID, "US Russell 1000", r1k, now)
    registered.append({
        "list_id": BUILTIN_RUSSELL1000_LIST_ID,
        "display_name": "US Russell 1000",
        "ticker_count": len(r1k),
        "scanned_at": now,
        "source": "wikipedia",
    })

    ndx = _clean(load_nasdaq100_tickers())
    _register_builtin_list(conn, BUILTIN_NASDAQ100_LIST_ID, "US Nasdaq-100", ndx, now)
    registered.append({
        "list_id": BUILTIN_NASDAQ100_LIST_ID,
        "display_name": "US Nasdaq-100",
        "ticker_count": len(ndx),
        "scanned_at": now,
        "source": "wikipedia",
    })

    conn.commit()
    conn.close()
    logger.info(
        "US index builtins refreshed: SP500=%d Russell1000=%d Nasdaq100=%d",
        len(sp500),
        len(r1k),
        len(ndx),
    )
    return registered


def sync_builtin_lists(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Register built-in ASX + S&P 500 lists (refreshed from official sources)."""
    from data.asx import load_asx_ticker_universe, ASX_MONITORED_TICKERS

    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    registered = []

    asx_base = load_asx_ticker_universe() or ASX_MONITORED_TICKERS
    asx_symbols = [f"{t}.AX" for t in asx_base]
    # Hygiene: exclude confirmed inactive from ASX builtin entries too
    asx_symbols = filter_active_symbols(asx_symbols, confirmed_only=True)
    _register_builtin_list(conn, BUILTIN_ASX_LIST_ID, "ASX Listed Companies", asx_symbols, now)
    registered.append({
        "list_id": BUILTIN_ASX_LIST_ID,
        "filename": BUILTIN_ASX_LIST_ID,
        "display_name": "ASX Listed Companies",
        "ticker_count": len(asx_symbols),
        "scanned_at": now,
        "builtin": True,
    })

    conn.commit()
    conn.close()

    # S&P 500 / Russell / Nasdaq — shared helper (also used by 6-month job)
    try:
        registered.extend(refresh_us_index_builtin_lists(db_path=db_path))
    except Exception as e:
        logger.warning("US index builtin lists skipped: %s", e)

    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    try:
        from data.buybacks import sync_builtin_buybacks_list
        bb = sync_builtin_buybacks_list(db_path=db_path, conn=conn, now=now)
        if bb:
            registered.append(bb)
    except Exception as e:
        logger.warning("Buybacks builtin list sync skipped: %s", e)

    conn.commit()
    conn.close()
    try:
        registered.append(sync_builtin_pariah_list(db_path=db_path))
    except Exception as e:
        logger.warning("Pariah builtin list sync skipped: %s", e)
    return registered


def sync_builtin_pariah_list(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """Register the capital-cycle pariah universe as a Lists-tab builtin."""
    from data.pariah_filter import pariah_universe_tickers

    symbols = sorted(pariah_universe_tickers())
    symbols = filter_active_symbols(symbols, confirmed_only=True)
    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    _register_builtin_list(
        conn, BUILTIN_PARIAH_LIST_ID, "Capital-cycle pariah", symbols, now
    )
    conn.commit()
    conn.close()
    return {
        "list_id": BUILTIN_PARIAH_LIST_ID,
        "filename": BUILTIN_PARIAH_LIST_ID,
        "display_name": "Capital-cycle pariah",
        "ticker_count": len(symbols),
        "scanned_at": now,
        "builtin": True,
    }


def _is_junk_list_file(name: str) -> bool:
    """LibreOffice/Excel lock files and other non-lists (e.g. .~lock.airlines.csv#)."""
    n = (name or "").strip().lower()
    if not n:
        return True
    if n.endswith(".meta.csv") or n.endswith(".meta.csv.csv") or ".meta." in n:
        return True
    if n.startswith(".~lock") or n.startswith("~lock") or ".~lock." in n:
        return True
    if n.startswith(".") and "lock" in n:
        return True
    if n.endswith("#"):
        return True
    return False


def scan_business_lists(force: bool = False, db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Scan Business lists folder and register CSV files + tickers in SQLite."""
    sync_builtin_lists(db_path=db_path)

    if not BUSINESS_LISTS_DIR.is_dir():
        logger.warning(f"Business lists folder not found: {BUSINESS_LISTS_DIR}")
        return get_available_lists_from_db(db_path)

    conn = _lists_conn(db_path)
    registered = []
    now = datetime.now().isoformat()

    keep_ids: set[str] = set()
    for path in sorted(BUSINESS_LISTS_DIR.glob("*.csv*")):
        list_id = path.name
        if _is_junk_list_file(list_id):
            continue
        display_name = path.stem.replace(".csv", "").replace("_", " ").strip()
        if display_name.lower().startswith(".~lock") or display_name.lower().startswith("~lock"):
            continue
        keep_ids.add(list_id)

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
        # Hygiene: never re-admit confirmed-dead symbols into list entries
        before_n = len(symbols)
        symbols = filter_active_symbols(symbols, confirmed_only=True)
        skipped_dead = before_n - len(symbols)
        if skipped_dead:
            logger.info(
                "List %s: excluded %d confirmed inactive symbols at scan",
                display_name,
                skipped_dead,
            )
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

    stale = conn.execute("SELECT list_id FROM business_list_registry").fetchall()
    for (lid,) in stale:
        if not lid or lid in BUILTIN_LIST_IDS or str(lid).startswith("__builtin__:"):
            continue
        if lid not in keep_ids or _is_junk_list_file(lid):
            conn.execute("DELETE FROM business_list_entries WHERE list_id=?", (lid,))
            conn.execute("DELETE FROM business_list_registry WHERE list_id=?", (lid,))
            logger.info("Dropped stale/junk business list %s", lid)

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

    # Default every 10 symbols so Task Scheduler console shows motion on long runs
    if progress_every is None or progress_every < 1:
        progress_every = 10
    progress_every = min(progress_every, 25)

    est_min = (total * float(sleep_seconds)) / 60.0
    logger.info(
        "%s: starting %d symbols (sleep=%.1fs, ~%.0f min if all succeed)",
        progress_label,
        total,
        sleep_seconds,
        est_min,
    )
    t0 = time.monotonic()
    done_ok = 0
    for i, sym in enumerate(symbols):
        n = i + 1
        if sym in inactive_skip:
            if n == 1 or n % progress_every == 0 or n == total:
                logger.info(
                    "  %s progress: %d/%d (ok=%d) skip-inactive %s",
                    progress_label,
                    n,
                    total,
                    done_ok,
                    sym,
                )
            continue

        # Progress before work so long .info() calls still show last ticker
        if n == 1 or n % progress_every == 0 or n == total:
            elapsed = time.monotonic() - t0
            rate = done_ok / elapsed if elapsed > 1 else 0
            remain = total - n
            eta_min = (remain / rate / 60.0) if rate > 0 else (remain * sleep_seconds / 60.0)
            logger.info(
                "  %s progress: %d/%d (%.0f%%) ok=%d elapsed=%.0fs eta~%.0fmin now=%s",
                progress_label,
                n,
                total,
                100.0 * n / total,
                done_ok,
                elapsed,
                eta_min,
                sym,
            )
            if on_progress:
                try:
                    on_progress(n, total)
                except Exception as ex:
                    logger.warning("on_progress callback failed: %s", ex)

        try:
            yt = yf.Ticker(sym)
            info = yt.info or {}
            price = quote_price(info)

            if price <= 0:
                reason = _detect_yf_inactive(info, price) or "quote_not_found"
                mark_symbol_inactive(sym, reason, source="yf_fetch")
                inactive_skip.add(sym)
                logger.info("  %s: inactive (%s) - skip", sym, reason)
                time.sleep(sleep_seconds)
                continue

            low = _safe_float(info.get("fiftyTwoWeekLow"))
            high = _safe_float(info.get("fiftyTwoWeekHigh"))
            p_fcf = calc_p_fcf(info, price, ticker=sym)

            ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
            from data.yf_helpers import calc_last_52w_low_metrics, calc_own_history_cheapness, calc_rsi_metrics
            low52 = calc_last_52w_low_metrics(yt)
            metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=sym)
            metrics["last_52w_low_date"] = low52.get("last_52w_low_date")
            metrics["days_since_52w_low"] = low52.get("days_since_52w_low")
            p_fcf = metrics.get("p_fcf") or p_fcf
            own = calc_own_history_cheapness(
                yt,
                price=price,
                pb=metrics.get("pb") or 0,
                p_fcf=p_fcf or 0,
                fcf_yield=metrics.get("fcf_yield") or 0,
            )
            metrics.update(own)
            metrics.update(calc_rsi_metrics(yt, with_series=False))
            data = {
                "price": _safe_round(price, 2),
                "low_52w": _safe_round(low, 2),
                "high_52w": _safe_round(high, 2),
                "name": info.get("shortName") or info.get("longName") or sym,
                "sector": info.get("sector") or "Unknown",
                "industry": info.get("industry") or info.get("industryDisp") or metrics.get("industry") or "",
                "metrics": metrics,
                "p_fcf": p_fcf,
            }
            out[sym] = data
            save_yf_cached(sym, data)
            remove_inactive_symbol(sym)
            inactive_skip.discard(sym)
            done_ok += 1
            time.sleep(sleep_seconds)
        except Exception as e:
            msg = str(e)
            logger.warning("  business yf error for %s: %s", sym, msg[:120])
            inactive_reason = _inactive_from_yf_error(msg)
            if inactive_reason:
                mark_symbol_inactive(sym, inactive_reason, source="yf_error")
                inactive_skip.add(sym)
                logger.info("  %s: inactive (%s)", sym, inactive_reason)
            else:
                logger.info("  %s: transient error, will retry later", sym)
            sleep = 30 if "Rate" in msg or "429" in msg else max(1.0, sleep_seconds / 2)
            time.sleep(sleep)

    logger.info(
        "%s complete: saved %d / %d in %.0fs",
        progress_label,
        len(out),
        total,
        time.monotonic() - t0,
    )
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
        "rsi": None,
        "metrics": {
            "pe": 0, "forward_pe": 0, "pb": 0, "p_fcf": 0, "pcf": 0,
            "ev_ebitda": 0, "enterprise_value": 0, "ev_m": 0,
            "debt_to_equity": 0, "debt_to_mcap": 0,
            "cash_on_hand_m": 0, "net_cash_m": 0, "net_debt_m": 0,
            "net_cash_pct_mcap": 0, "net_assets_m": 0, "net_assets_vs_mcap_m": 0,
            "net_assets_vs_mcap_pct": 0, "currency": infer_currency(ticker=sym),
            "roe": 0, "insider_ownership_pct": 0, "roic": 0,
            "fcf_m": 0, "ocf_m": 0, "capex_m": 0, "fcf_yield": 0, "ev_fcf": 0,
            "fcf_quality": "unknown", "fcf_ocf_ratio": None, "capex_ocf_ratio": None,
            "div_yield": 0, "market_cap": 0,
            "ma_200w": 0, "pct_from_200w_ma": 0,
        },
        "signals": [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "Awaiting yfinance data — use Rebuild YF"}],
        "_market": list_meta.get("_market") or "business",
        "_list_id": list_meta.get("list_id"),
        "_pending_yf": True,
    }


def _stock_row(sym: str, ydat: dict, list_meta: dict) -> dict:
    price = ydat.get("price", 0)
    low, high = reconcile_52w_range(price, ydat.get("low_52w", 0), ydat.get("high_52w", 0))
    m = ydat.get("metrics", {}) or {}
    p_fcf = ydat.get("p_fcf", m.get("p_fcf", 0) or 0)
    pct_from_low = pct_from_52w_low(price, low)

    row = {
        "ticker": sym,
        "name": ydat.get("name") or sym,
        "sector": ydat.get("sector") or "Unknown",
        "industry": (ydat.get("metrics") or {}).get("industry") or ydat.get("industry") or "",
        "list_name": list_meta.get("display_name") or list_meta.get("list_id", ""),
        "insider_buys_2026": 0,
        "buyback_announced": False,
        "buyback_date": None,
        "pct_from_52w_low": pct_from_low,
        "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
        "rsi": m.get("rsi"),
        "last_52w_low_date": m.get("last_52w_low_date"),
        "days_since_52w_low": m.get("days_since_52w_low"),
        "last_activity": datetime.now().strftime("%Y-%m-%d"),
        "current_price": _safe_round(price, 2) if price else 0,
        "low_52w": _safe_round(low, 2) if low else 0,
        "high_52w": _safe_round(high, 2) if high else 0,
        "market_cap": m.get("market_cap", 0),
        "currency": m.get("currency") or infer_currency(ticker=sym),
        "financial_currency": m.get("financial_currency") or m.get("currency") or infer_currency(ticker=sym),
        "currency_converted": bool(m.get("currency_converted")),
        "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
        "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
        "pe": m.get("pe", 0),
        "pb": m.get("pb", 0),
        "peg": m.get("peg", 0),
        "p_fcf": p_fcf,
        "insider_ownership_pct": m.get("insider_ownership_pct", 0),
        "metrics": {
            "pe": m.get("pe", 0),
            "forward_pe": m.get("forward_pe", 0),
            "pb": m.get("pb", 0),
            "p_fcf": p_fcf,
            "pcf": m.get("pcf", 0),
            "ev_ebitda": m.get("ev_ebitda", 0),
            "enterprise_value": m.get("enterprise_value", 0),
            "ev_m": m.get("ev_m", 0),
            "debt_to_equity": m.get("debt_to_equity", 0),
            "debt_to_mcap": m.get("debt_to_mcap", 0),
            "net_debt_to_equity": m.get("net_debt_to_equity", 0),
            "ebitda_est_m": m.get("ebitda_est_m", 0),
            "debt_to_ebitda": m.get("debt_to_ebitda", 0),
            "cash_on_hand_m": m.get("cash_on_hand_m", 0),
            "net_cash_m": m.get("net_cash_m", 0),
            "net_debt_m": m.get("net_debt_m", 0),
            "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
            "net_assets_m": m.get("net_assets_m", 0),
            "net_assets_vs_mcap_m": m.get("net_assets_vs_mcap_m", 0),
            "net_assets_vs_mcap_pct": m.get("net_assets_vs_mcap_pct", 0),
            "currency": m.get("currency") or infer_currency(ticker=sym),
            "financial_currency": m.get("financial_currency") or m.get("currency") or infer_currency(ticker=sym),
            "fx_to_quote": m.get("fx_to_quote", 1.0),
            "currency_converted": bool(m.get("currency_converted")),
            "industry": m.get("industry") or ydat.get("industry") or "",
            "roe": m.get("roe", 0),
            "insider_ownership_pct": m.get("insider_ownership_pct", 0),
            "roic": m.get("roic", 0),
            "fcf_m": m.get("fcf_m", 0),
            "ocf_m": m.get("ocf_m", 0),
            "capex_m": m.get("capex_m", 0),
            "fcf_yield": m.get("fcf_yield", 0),
            "ev_fcf": m.get("ev_fcf", 0),
            "fcf_quality": m.get("fcf_quality") or "unknown",
            "fcf_ocf_ratio": m.get("fcf_ocf_ratio"),
            "capex_ocf_ratio": m.get("capex_ocf_ratio"),
            "earnings_growth_pct": m.get("earnings_growth_pct", 0),
            "peg": m.get("peg", 0) or 0,
            "div_yield": m.get("div_yield", 0),
            "market_cap": m.get("market_cap", 0),
            "ma_200w": m.get("ma_200w", 0),
            "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
            "last_52w_low_date": m.get("last_52w_low_date"),
            "days_since_52w_low": m.get("days_since_52w_low"),
            "own_hist_weeks": m.get("own_hist_weeks") or 0,
            "own_hist_pb_pctile": m.get("own_hist_pb_pctile"),
            "own_hist_pfcf_pctile": m.get("own_hist_pfcf_pctile"),
            "own_hist_fcf_yield_pctile": m.get("own_hist_fcf_yield_pctile"),
            "own_hist_cheap": bool(m.get("own_hist_cheap")),
            "own_hist_label": m.get("own_hist_label") or "unknown",
        },
        "own_hist_cheap": bool(m.get("own_hist_cheap")),
        "own_hist_label": m.get("own_hist_label") or "unknown",
        "signals": [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "Business list — no signal scan"}],
        "_market": "business",
        "_list_id": list_meta.get("list_id"),
        "_live": True,
    }
    return sanitize_stock_figures(row)


def fetch_business_list_stocks(
    list_id: str,
    force_yf: bool = False,
    signals_only: bool = False,
    fast: bool = False,
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
    if list_id == BUILTIN_PARIAH_LIST_ID:
        list_meta["display_name"] = "Capital-cycle pariah"

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

    if not fast:
        try:
            from data.edgar_metrics import enrich_stocks_edgar_cached
            results = enrich_stocks_edgar_cached(results)
        except Exception as e:
            logger.warning("EDGAR cache enrich skipped for business list '%s': %s", list_id, e)

        try:
            from data.bankruptcy_reorg import enrich_stocks_reorg_cached
            results = enrich_stocks_reorg_cached(results)
        except Exception as e:
            logger.warning("Reorg cache enrich skipped for business list '%s': %s", list_id, e)

        try:
            from data.mining_reserves import enrich_stocks_reserves_cached
            results = enrich_stocks_reserves_cached(results)
        except Exception as e:
            logger.warning("Reserves cache enrich skipped for business list '%s': %s", list_id, e)

        try:
            from data.mining_costs import enrich_stocks_costs_cached
            results = enrich_stocks_costs_cached(results)
        except Exception as e:
            logger.warning("Unit-cost cache enrich skipped for business list '%s': %s", list_id, e)

        try:
            from data.asx_mining import enrich_stocks_asx_mining_cached
            results = enrich_stocks_asx_mining_cached(results)
        except Exception as e:
            logger.warning("ASX mining cache enrich skipped for business list '%s': %s", list_id, e)

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
        price = quote_price(info)
        low, high = reconcile_52w_range(
            price, info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
        )
        pct = pct_from_52w_low(price, low)

        p_fcf = calc_p_fcf(info, price, ticker=sym)

        ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
        from data.yf_helpers import calc_last_52w_low_metrics, calc_own_history_cheapness, calc_rsi_metrics
        low52 = calc_last_52w_low_metrics(yt)
        metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=sym)
        metrics["last_52w_low_date"] = low52.get("last_52w_low_date")
        metrics["days_since_52w_low"] = low52.get("days_since_52w_low")
        p_fcf = metrics.get("p_fcf") or p_fcf
        metrics.update(
            calc_own_history_cheapness(
                yt,
                price=price,
                pb=metrics.get("pb") or 0,
                p_fcf=p_fcf or 0,
                fcf_yield=metrics.get("fcf_yield") or 0,
            )
        )
        rsi_pack = calc_rsi_metrics(yt, with_series=True)
        metrics["rsi"] = rsi_pack.get("rsi")
        metrics["rsi_period"] = rsi_pack.get("rsi_period")
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
            "rsi_trend": rsi_pack.get("rsi_trend") or [],
            "rsi": metrics.get("rsi"),
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
            from data.bankruptcy_reorg import attach_reorg_to_stock
            attach_reorg_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Reorg attach skipped for business %s: %s", sym, ex)
        try:
            from data.mining_reserves import attach_reserves_to_stock
            attach_reserves_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Reserves attach skipped for business %s: %s", sym, ex)
        try:
            from data.mining_costs import attach_costs_to_stock
            attach_costs_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Unit-cost attach skipped for business %s: %s", sym, ex)
        try:
            from data.asx_mining import attach_asx_mining_to_stock, get_asx_mining_summary
            if get_asx_mining_summary(sym, max_age_days=0):
                attach_asx_mining_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("ASX mining attach skipped for business %s: %s", sym, ex)
        try:
            from data.earnings_multiples import attach_earnings_multiples
            # Prefer yfinance annual EPS when present; EDGAR path already ran above for US
            attach_earnings_multiples(stock)
        except Exception as ex:
            logger.debug("earnings multiples skipped for %s: %s", sym, ex)
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