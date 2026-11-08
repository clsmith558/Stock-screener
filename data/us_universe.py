"""
US large-cap ticker universes for SEC buyback / insider scans.
Russell 1000 ∪ Nasdaq-100 ∪ S&P 500 (deduplicated, Yahoo-compatible symbols).
"""

from __future__ import annotations

import io
import logging
import sqlite3
import urllib.request
from datetime import datetime

from config.identity import wiki_user_agent

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_DB = "asx_announcements_cache.db"
BUILTIN_RUSSELL1000_LIST_ID = "__builtin__:russell1000"
BUILTIN_NASDAQ100_LIST_ID = "__builtin__:nasdaq100"


def _wiki_headers() -> dict:
    return {"User-Agent": wiki_user_agent()}


def normalize_us_ticker(raw: str) -> str:
    sym = str(raw or "").strip().upper()
    if not sym or sym == "NAN":
        return ""
    return sym.replace(".", "-")


def _read_wikipedia_tables(url: str) -> list:
    import pandas as pd

    try:
        try:
            return pd.read_html(url, storage_options=_wiki_headers())
        except TypeError:
            req = urllib.request.Request(url, headers=_wiki_headers())
            with urllib.request.urlopen(req, timeout=45) as resp:
                return pd.read_html(io.BytesIO(resp.read()))
    except Exception as e:
        logger.warning("Wikipedia table load failed for %s: %s", url, e)
        return []


def _symbols_from_df(df, symbol_cols: tuple) -> list[str]:
    if df is None or df.empty:
        return []
    cols = {str(c).strip().lower(): c for c in df.columns}
    col = None
    for want in symbol_cols:
        if want.lower() in cols:
            col = cols[want.lower()]
            break
    if col is None:
        for c in df.columns:
            cl = str(c).strip().lower()
            if "symbol" in cl or cl == "ticker":
                col = c
                break
    if col is None:
        return []
    out = []
    for raw in df[col].tolist():
        sym = normalize_us_ticker(raw)
        if sym:
            out.append(sym)
    return out


_us_index_cache: dict[str, tuple[float, list[str]]] = {}
_US_INDEX_CACHE_TTL_SEC = 3600.0


def load_russell1000_tickers(force_live: bool = False) -> list[str]:
    """Russell 1000 constituents (SQLite/memory cache first unless force_live=True, else Wikipedia)."""
    import time

    now = time.time()
    cached = _us_index_cache.get(BUILTIN_RUSSELL1000_LIST_ID)
    if not force_live and cached and (now - cached[0]) < _US_INDEX_CACHE_TTL_SEC:
        return list(cached[1])

    if not force_live:
        db_syms = _load_builtin_fallback(BUILTIN_RUSSELL1000_LIST_ID)
        if len(db_syms) >= 900:
            _us_index_cache[BUILTIN_RUSSELL1000_LIST_ID] = (now, db_syms)
            return list(db_syms)

    tables = _read_wikipedia_tables("https://en.wikipedia.org/wiki/Russell_1000_Index")
    for df in tables:
        if len(df) < 900:
            continue
        syms = _symbols_from_df(df, ("Symbol", "Ticker"))
        if len(syms) >= 900:
            res = sorted(set(syms))
            _us_index_cache[BUILTIN_RUSSELL1000_LIST_ID] = (now, res)
            return res
    fallback = _load_builtin_fallback(BUILTIN_RUSSELL1000_LIST_ID)
    if fallback:
        _us_index_cache[BUILTIN_RUSSELL1000_LIST_ID] = (now, fallback)
    return fallback


def load_nasdaq100_tickers(force_live: bool = False) -> list[str]:
    """Nasdaq-100 constituents (SQLite/memory cache first unless force_live=True, else Wikipedia)."""
    import time

    now = time.time()
    cached = _us_index_cache.get(BUILTIN_NASDAQ100_LIST_ID)
    if not force_live and cached and (now - cached[0]) < _US_INDEX_CACHE_TTL_SEC:
        return list(cached[1])

    if not force_live:
        db_syms = _load_builtin_fallback(BUILTIN_NASDAQ100_LIST_ID)
        if 90 <= len(db_syms) <= 110:
            _us_index_cache[BUILTIN_NASDAQ100_LIST_ID] = (now, db_syms)
            return list(db_syms)

    tables = _read_wikipedia_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
    for df in tables:
        syms = _symbols_from_df(df, ("Ticker", "Symbol"))
        if 90 <= len(syms) <= 110:
            res = sorted(set(syms))
            _us_index_cache[BUILTIN_NASDAQ100_LIST_ID] = (now, res)
            return res
    fallback = _load_builtin_fallback(BUILTIN_NASDAQ100_LIST_ID)
    if fallback:
        _us_index_cache[BUILTIN_NASDAQ100_LIST_ID] = (now, fallback)
    return fallback


def _load_builtin_fallback(list_id: str) -> list[str]:
    try:
        conn = sqlite3.connect(DEFAULT_UNIVERSE_DB)
        rows = conn.execute(
            "SELECT yf_symbol FROM business_list_entries WHERE list_id=? ORDER BY yf_symbol",
            (list_id,),
        ).fetchall()
        conn.close()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        pass
    return []


_us_index_sets_cache: tuple[float, dict[str, set[str]]] | None = None


def load_us_index_sets(force_live: bool = False) -> dict[str, set[str]]:
    """Ticker membership for Russell 1000 and Nasdaq-100 (Yahoo symbols)."""
    import time

    global _us_index_sets_cache
    now = time.time()
    if (
        not force_live
        and _us_index_sets_cache is not None
        and (now - _us_index_sets_cache[0]) < _US_INDEX_CACHE_TTL_SEC
    ):
        return _us_index_sets_cache[1]
    sets = {
        "russell1000": set(load_russell1000_tickers(force_live=force_live)),
        "nasdaq100": set(load_nasdaq100_tickers(force_live=force_live)),
    }
    _us_index_sets_cache = (now, sets)
    return sets


def us_indices_for_symbol(sym: str, index_sets: dict[str, set[str]] | None = None) -> list[str]:
    """Return index ids (russell1000, nasdaq100) for a US ticker."""
    s = normalize_us_ticker(sym)
    if not s or s.endswith((".AX", ".HK", ".T")):
        return []
    sets = index_sets or load_us_index_sets()
    out = []
    if s in sets.get("russell1000", ()):
        out.append("russell1000")
    if s in sets.get("nasdaq100", ()):
        out.append("nasdaq100")
    return out


def load_us_scan_universe() -> list[str]:
    """Russell 1000 ∪ Nasdaq-100 ∪ S&P 500 for SEC scans."""
    from data.business_lists import load_sp500_tickers

    seen: set[str] = set()
    out: list[str] = []
    for loader in (load_russell1000_tickers, load_nasdaq100_tickers, load_sp500_tickers):
        try:
            tickers = loader()
        except Exception as e:
            logger.warning("US universe loader failed: %s", e)
            tickers = []
        for raw in tickers:
            sym = normalize_us_ticker(raw)
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return sorted(out)


def register_us_builtin_lists(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """Persist Russell 1000 + Nasdaq-100 into business_list_entries for reuse."""
    from data.business_lists import _lists_conn, _register_builtin_list

    conn = _lists_conn(db_path)
    now = datetime.now().isoformat()
    r1k = load_russell1000_tickers()
    ndx = load_nasdaq100_tickers()
    _register_builtin_list(conn, BUILTIN_RUSSELL1000_LIST_ID, "US Russell 1000", r1k, now)
    _register_builtin_list(conn, BUILTIN_NASDAQ100_LIST_ID, "US Nasdaq-100", ndx, now)
    conn.commit()
    conn.close()
    return {"russell1000": len(r1k), "nasdaq100": len(ndx)}