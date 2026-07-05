"""
Buybacks registry — aggregates repurchase activity across markets (TSE, ASX, …).
List loads are cache-first: persistent SQLite registry + optional yfinance enrichment.
"""

import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from data.asx import (
    DEFAULT_UNIVERSE_DB,
    YF_CACHE_MAX_AGE_DAYS,
    YF_FETCH_SLEEP_SECONDS,
    _net_assets_vs_mcap_metrics,
    _safe_float,
    _safe_round,
    asx_official_name,
    infer_currency,
    load_asx_universe_details,
)
from data.business_lists import (
    _fetch_yf_sequential,
    _placeholder_row,
    _stock_row,
    filter_active_symbols,
    load_yf_cached,
)
from data.hkex_insider_dealings import DEFAULT_CSV_PATH as HK_INSIDER_CSV_PATH, refresh_hk_insider_csv
from data.hkex_repurchases import DEFAULT_CSV_PATH as HK_CSV_PATH, refresh_hkex_csv
from data.jpx_repurchases import DEFAULT_CSV_PATH as TSE_CSV_PATH, refresh_jpx_csv

DEFAULT_CSV_PATH = TSE_CSV_PATH

logger = logging.getLogger(__name__)

_buybacks_cache = {}
BUYBACKS_CACHE_TTL = 3600  # 1 hour in-memory; persistent SQLite survives restarts
_yf_rebuild_lock = threading.Lock()
_us_sec_refresh_lock = threading.Lock()
_us_sec_refresh_state = {
    "running": False,
    "phase": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "message": "",
    "tickers_total": 0,
    "tickers_done": 0,
    "current_ticker": "",
    "filings_scanned": 0,
    "new_events": 0,
    "new_purchases": 0,
    "errors": 0,
}

_yf_rebuild_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "fetched": 0,
    "registry_refreshes": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "message": "",
}


def _sanitize_for_json(val):
    """Remove NaN/Inf so Flask/JS JSON.parse accept the payload."""
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(val, dict):
        return {k: _sanitize_for_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_sanitize_for_json(v) for v in val]
    return val


def _buybacks_conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buyback_registry (
            yf_symbol TEXT PRIMARY KEY,
            row_json TEXT NOT NULL,
            built_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buyback_registry_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _registry_get_meta(key: str, db_path: str = DEFAULT_UNIVERSE_DB) -> str:
    conn = _buybacks_conn(db_path)
    row = conn.execute(
        "SELECT value FROM buyback_registry_meta WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _registry_set_meta(pairs: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    conn = _buybacks_conn(db_path)
    for k, v in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO buyback_registry_meta (key, value) VALUES (?,?)",
            (k, str(v)),
        )
    conn.commit()
    conn.close()


def _csv_mtime(csv_path: Path = None) -> str:
    path = Path(csv_path or TSE_CSV_PATH)
    if not path.exists():
        return ""
    return str(path.stat().st_mtime)


def _registry_needs_rebuild(db_path: str = DEFAULT_UNIVERSE_DB) -> bool:
    conn = _buybacks_conn(db_path)
    count = conn.execute("SELECT COUNT(*) FROM buyback_registry").fetchone()[0]
    conn.close()
    if not count:
        return True
    stored_tse = _registry_get_meta("tse_csv_mtime", db_path)
    stored_hk = _registry_get_meta("hk_csv_mtime", db_path)
    stored_hk_di = _registry_get_meta("hk_insider_csv_mtime", db_path)
    stored_asx_insider = _registry_get_meta("asx_insider_rows", db_path)
    stored_asx_buyback = _registry_get_meta("asx_buyback_rows", db_path)
    stored_us_buyback = _registry_get_meta("us_buyback_rows", db_path)
    stored_us_insider = _registry_get_meta("us_insider_rows", db_path)
    current_asx_insider = _count_asx_insider_purchases()
    current_asx_buyback = _count_asx_buyback_events()
    current_us_buyback = _count_us_buyback_events()
    current_us_insider = _count_us_insider_purchases()
    return (
        stored_tse != _csv_mtime(TSE_CSV_PATH)
        or stored_hk != _csv_mtime(HK_CSV_PATH)
        or stored_hk_di != _csv_mtime(HK_INSIDER_CSV_PATH)
        or stored_asx_insider != str(current_asx_insider)
        or stored_asx_buyback != str(current_asx_buyback)
        or stored_us_buyback != str(current_us_buyback)
        or stored_us_insider != str(current_us_insider)
    )


def _save_registry(rows: list, meta: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    """Persist signal-only registry rows (fundamentals come from business YF cache at load)."""
    conn = _buybacks_conn(db_path)
    now = datetime.now().isoformat()
    payloads = [_sanitize_for_json(_signal_row_for_storage(r)) for r in rows]
    conn.execute("DELETE FROM buyback_registry")
    conn.executemany(
        "INSERT INTO buyback_registry (yf_symbol, row_json, built_at) VALUES (?,?,?)",
        [
            (p["ticker"], json.dumps(p, allow_nan=False), now)
            for p in payloads
            if p.get("ticker")
        ],
    )
    conn.commit()
    conn.close()
    try:
        sync_builtin_buybacks_list(db_path=db_path)
        from data.business_lists import BUILTIN_BUYBACKS_LIST_ID, _business_cache
        _business_cache.pop(BUILTIN_BUYBACKS_LIST_ID, None)
    except Exception as e:
        logger.warning("Buybacks builtin list sync after registry save failed: %s", e)
    _registry_set_meta({
        "built_at": now,
        "tse_csv_mtime": _csv_mtime(TSE_CSV_PATH),
        "hk_csv_mtime": _csv_mtime(HK_CSV_PATH),
        "hk_insider_csv_mtime": _csv_mtime(HK_INSIDER_CSV_PATH),
        "row_count": len(rows),
        "buyback_year": meta.get("buyback_year", datetime.now().year),
        "tse_rows": meta.get("tse_rows", 0),
        "hk_rows": meta.get("hk_rows", 0),
        "hk_insider_rows": meta.get("hk_insider_rows", 0),
        "asx_events": meta.get("asx_events", 0),
        "asx_insider_rows": meta.get("asx_insider_rows", 0),
        "asx_buyback_rows": meta.get("asx_buyback_rows", 0),
        "us_buyback_rows": meta.get("us_buyback_rows", 0),
        "us_insider_rows": meta.get("us_insider_rows", 0),
    }, db_path)


def _load_registry_signals_from_db(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Load persisted signal rows (normalizes legacy full-row registry JSON)."""
    conn = _buybacks_conn(db_path)
    rows = conn.execute(
        "SELECT row_json FROM buyback_registry ORDER BY yf_symbol"
    ).fetchall()
    conn.close()
    out = []
    for (raw,) in rows:
        try:
            out.append(_normalize_stored_registry_row(_sanitize_for_json(json.loads(raw))))
        except Exception:
            continue
    return out


def _load_registry_from_db(lite: bool = True, db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Hydrate display rows: signal registry + business_list_yf_cache fundamentals."""
    composed = _hydrate_buyback_rows(_load_registry_signals_from_db(db_path=db_path))
    if lite:
        composed = [_slim_list_row(r) for r in composed]
    return composed


def _load_registry_row(sym: str, db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    conn = _buybacks_conn(db_path)
    row = conn.execute(
        "SELECT row_json FROM buyback_registry WHERE yf_symbol=?", (sym,)
    ).fetchone()
    conn.close()
    if not row:
        return {}
    try:
        return _sanitize_for_json(json.loads(row[0]))
    except Exception:
        return {}


def _code_to_yf_symbol(code: str) -> str:
    return f"{str(code).strip().upper()}.T"


def _hk_code_to_yf_symbol(code: str) -> str:
    raw = str(code).strip().upper().replace(".HK", "")
    if raw.isdigit():
        raw = raw.zfill(4)
    return f"{raw}.HK" if raw else ""


def _shares_outstanding_from_yf(ydat: dict) -> float:
    """Derive shares outstanding from cached yfinance price + market cap."""
    if not ydat:
        return 0.0
    metrics = ydat.get("metrics") or {}
    mcap = _safe_float(metrics.get("market_cap"))
    price = _safe_float(ydat.get("price"))
    if mcap > 0 and price > 0:
        return mcap / price
    return 0.0


def _asx_base_to_yf(ticker: str) -> str:
    t = (ticker or "").strip().upper().replace(".AX", "")
    return f"{t}.AX" if t else ""


def _parse_csv_price(val) -> float:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    s = str(val).strip().replace(",", "")
    return _safe_float(s, 0.0)


def _csv_snapshot_from_row(row) -> dict:
    """Fundamentals embedded in TSE CSV from last scrape."""
    price = _parse_csv_price(row.get("Price"))
    market_cap = _safe_float(row.get("marketCap"))
    total_cash = _safe_float(row.get("totalCash"))
    total_debt = _safe_float(row.get("totalDebt"))
    net_cash = total_cash - total_debt if total_cash or total_debt else 0
    net_cash_pct = (net_cash / market_cap * 100) if market_cap > 0 else 0
    pb = _safe_round(_safe_float(row.get("priceToBook")), 2)
    mcap_i = int(market_cap) if market_cap > 0 else 0
    net_assets = (market_cap / pb) if mcap_i > 0 and pb > 0 else 0
    na_metrics = _net_assets_vs_mcap_metrics(net_assets, mcap_i)
    currency = str(row.get("financialCurrency") or "").strip().upper() or "JPY"
    return {
        "price": _safe_round(price, 2) if price else 0,
        "market_cap": market_cap,
        "pe": _safe_round(_safe_float(row.get("trailingPE")), 1),
        "forward_pe": _safe_round(_safe_float(row.get("forwardPE")), 1),
        "pb": pb,
        "debt_to_equity": _safe_round(_safe_float(row.get("debtToEquity")), 2),
        "shares_outstanding": _safe_float(row.get("sharesOutstanding")),
        "total_cash": total_cash,
        "net_cash_m": _safe_round(net_cash / 1e6, 1) if net_cash else 0,
        "net_cash_pct_mcap": _safe_round(net_cash_pct, 1),
        "net_assets_m": na_metrics["net_assets_m"],
        "net_assets_vs_mcap_m": na_metrics["net_assets_vs_mcap_m"],
        "net_assets_vs_mcap_pct": na_metrics["net_assets_vs_mcap_pct"],
        "currency": currency,
    }


def _csv_snapshot_from_hk_row(row) -> dict:
    """Fundamentals from HK master CSV row (price from repurchase high)."""
    price = _safe_float(row.get("Highest Repurchase Price"))
    market_cap = _safe_float(row.get("marketCap"))
    total_cash = _safe_float(row.get("totalCash"))
    total_debt = _safe_float(row.get("totalDebt"))
    net_cash = total_cash - total_debt if total_cash or total_debt else 0
    net_cash_pct = (net_cash / market_cap * 100) if market_cap > 0 else 0
    pb = _safe_round(_safe_float(row.get("priceToBook")), 2)
    mcap_i = int(market_cap) if market_cap > 0 else 0
    net_assets = (market_cap / pb) if mcap_i > 0 and pb > 0 else 0
    na_metrics = _net_assets_vs_mcap_metrics(net_assets, mcap_i)
    currency = str(row.get("financialCurrency") or "").strip().upper() or "HKD"
    return {
        "price": _safe_round(price, 2) if price else 0,
        "market_cap": market_cap,
        "pe": _safe_round(_safe_float(row.get("trailingPE")), 1),
        "forward_pe": _safe_round(_safe_float(row.get("forwardPE")), 1),
        "pb": pb,
        "debt_to_equity": _safe_round(_safe_float(row.get("debtToEquity")), 2),
        "shares_outstanding": _safe_float(row.get("sharesOutstanding")),
        "total_cash": total_cash,
        "net_cash_m": _safe_round(net_cash / 1e6, 1) if net_cash else 0,
        "net_cash_pct_mcap": _safe_round(net_cash_pct, 1),
        "net_assets_m": na_metrics["net_assets_m"],
        "net_assets_vs_mcap_m": na_metrics["net_assets_vs_mcap_m"],
        "net_assets_vs_mcap_pct": na_metrics["net_assets_vs_mcap_pct"],
        "currency": currency,
    }


def _ydat_from_csv_snapshot(agg: dict, sym: str) -> dict:
    snap = agg.get("csv_snapshot") or {}
    if not snap.get("price") and not snap.get("market_cap"):
        return {}
    currency = snap.get("currency") or infer_currency(ticker=sym)
    metrics = {
        "pe": snap.get("pe", 0),
        "forward_pe": snap.get("forward_pe", 0),
        "pb": snap.get("pb", 0),
        "p_fcf": 0,
        "debt_to_equity": snap.get("debt_to_equity", 0),
        "cash_on_hand_m": _safe_round(snap.get("total_cash", 0) / 1e6, 1),
        "net_cash_m": snap.get("net_cash_m", 0),
        "net_cash_pct_mcap": snap.get("net_cash_pct_mcap", 0),
        "net_assets_m": snap.get("net_assets_m", 0),
        "net_assets_vs_mcap_m": snap.get("net_assets_vs_mcap_m", 0),
        "net_assets_vs_mcap_pct": snap.get("net_assets_vs_mcap_pct", 0),
        "currency": currency,
        "market_cap": snap.get("market_cap", 0),
        "pct_from_200w_ma": 0,
        "ma_200w": 0,
    }
    return {
        "price": snap.get("price", 0),
        "low_52w": 0,
        "high_52w": 0,
        "name": agg.get("name") or sym,
        "sector": "Unknown",
        "metrics": metrics,
        "p_fcf": 0,
    }


def load_tse_events(csv_path: Path = None) -> pd.DataFrame:
    path = Path(csv_path or DEFAULT_CSV_PATH)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read TSE buyback CSV %s: %s", path, e)
        return pd.DataFrame()
    if df.empty:
        return df

    df["Code"] = df["Code"].astype(str)
    df["Implementation Date"] = pd.to_datetime(df["Implementation Date"], errors="coerce")
    if "No. of Shares to be Purchased" in df.columns:
        df["No. of Shares to be Purchased"] = pd.to_numeric(
            df["No. of Shares to be Purchased"].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    if "sharesOutstanding" in df.columns:
        df["sharesOutstanding"] = pd.to_numeric(df["sharesOutstanding"], errors="coerce")
    df["yf_symbol"] = df["Code"].map(_code_to_yf_symbol)
    df["source"] = "TSE"
    return df


def load_hk_insider_events(csv_path: Path = None) -> pd.DataFrame:
    path = Path(csv_path or HK_INSIDER_CSV_PATH)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read HK insider CSV %s: %s", path, e)
        return pd.DataFrame()
    if df.empty:
        return df

    df["Stock code"] = df["Stock code"].astype(str).str.strip()
    df["Event Date"] = pd.to_datetime(df["Event Date"], errors="coerce")
    df["Shares"] = pd.to_numeric(df["Shares"], errors="coerce")
    df["Average price"] = pd.to_numeric(df["Average price"], errors="coerce")
    df["yf_symbol"] = df["Stock code"].map(_hk_code_to_yf_symbol)
    df["source"] = "HKEX-DI"
    return df


def load_hk_events(csv_path: Path = None) -> pd.DataFrame:
    path = Path(csv_path or HK_CSV_PATH)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, encoding="utf-8")
    except Exception as e:
        logger.warning("Cannot read HK buyback CSV %s: %s", path, e)
        return pd.DataFrame()
    if df.empty:
        return df

    df["Stock code"] = df["Stock code"].astype(str).str.strip()
    df["Trading Date"] = pd.to_datetime(df["Trading Date"], errors="coerce")
    if "No. of Shares to be Purchased" in df.columns:
        df["No. of Shares to be Purchased"] = pd.to_numeric(
            df["No. of Shares to be Purchased"].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
    if "sharesOutstanding" in df.columns:
        df["sharesOutstanding"] = pd.to_numeric(df["sharesOutstanding"], errors="coerce")
    df["yf_symbol"] = df["Stock code"].map(_hk_code_to_yf_symbol)
    df["source"] = "HKEX"
    return df


def _count_asx_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE classification = 'buyback'"
        ).fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def _count_asx_insider_purchases(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM announcements WHERE classification = 'insider_purchase'"
        ).fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def _director_from_asx_headline(headline: str) -> str:
    h = (headline or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in h:
            tail = h.split(sep, 1)[-1].strip()
            if tail and not tail.lower().startswith("x "):
                return tail
    return ""


def load_asx_insider_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT ticker, date, headline, url
            FROM announcements
            WHERE classification = 'insider_purchase'
            ORDER BY date DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("ASX insider load failed: %s", e)
        return []

    events = []
    for ticker, date, headline, url in rows:
        sym = _asx_base_to_yf(ticker)
        if not sym:
            continue
        events.append({
            "yf_symbol": sym,
            "ticker_base": ticker,
            "date": date,
            "headline": headline or "",
            "url": url or "",
            "director": _director_from_asx_headline(headline),
            "source": "ASX",
            "shares": None,
            "price": None,
        })
    return events


def load_asx_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT ticker, date, headline, url
            FROM announcements
            WHERE classification = 'buyback'
            ORDER BY date DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("ASX buyback load failed: %s", e)
        return []

    events = []
    for ticker, date, headline, url in rows:
        sym = _asx_base_to_yf(ticker)
        if not sym:
            continue
        events.append({
            "yf_symbol": sym,
            "ticker_base": ticker,
            "date": date,
            "headline": headline or "",
            "url": url or "",
            "source": "ASX",
            "shares_purchased": None,
        })
    return events


def _us_buybacks_last_fetched() -> str:
    try:
        from data.us_buybacks import get_us_buybacks_meta
        return get_us_buybacks_meta().get("last_fetched") or ""
    except Exception:
        return ""


def _us_insider_last_fetched() -> str:
    try:
        from data.us_insider import get_us_insider_meta
        return get_us_insider_meta().get("last_fetched") or ""
    except Exception:
        return ""


def _us_scan_universe_count() -> int:
    try:
        from data.us_universe import load_us_scan_universe
        return len(load_us_scan_universe())
    except Exception:
        return 0


def _count_us_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        from data.us_buybacks import count_us_buyback_events
        return count_us_buyback_events(db_path)
    except Exception:
        return 0


def _count_us_insider_purchases(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        from data.us_insider import count_us_insider_purchases
        return count_us_insider_purchases(db_path)
    except Exception:
        return 0


def load_us_insider_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    try:
        from data.us_insider import load_us_insider_events as _load
        return _load(db_path)
    except Exception as e:
        logger.warning("US insider load failed: %s", e)
        return []


def _aggregate_us_insider(events: list, year: int) -> dict:
    if not events:
        return {}

    out = {}
    by_sym = {}
    for ev in events:
        by_sym.setdefault(ev["yf_symbol"], []).append(ev)

    for sym, evs in by_sym.items():
        evs.sort(key=lambda e: e.get("date") or "", reverse=True)
        year_evs = [e for e in evs if (e.get("date") or "")[:4] == str(year)]

        history = []
        for e in evs[:16]:
            who = e.get("director") or e.get("insider") or "Insider"
            shares = e.get("shares")
            price = e.get("price")
            history.append({
                "date": e.get("date"),
                "type": "insider",
                "desc": e.get("headline") or (
                    f"SEC Form 4: {who} bought {int(shares):,} @ {price}"
                    if shares and price
                    else f"SEC Form 4: {who} purchase"
                ),
                "headline": e.get("headline") or "",
                "url": e.get("url"),
                "director": who,
                "source": "SEC-4",
            })

        latest = evs[0]
        out[sym] = {
            "yf_symbol": sym,
            "name": sym,
            "source": "SEC-4",
            "insider_buys_2026": len(year_evs),
            "insider_events_total": len(evs),
            "last_insider_date": latest.get("date"),
            "insider_history": history,
            "last_activity": latest.get("date"),
            "buyback_announced": False,
            "buyback_date": None,
            "annual_buyback_pct": 0,
            "csv_snapshot": {},
        }
    return out


def load_us_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    try:
        from data.us_buybacks import load_us_buyback_events as _load
        return _load(db_path)
    except Exception as e:
        logger.warning("US buyback load failed: %s", e)
        return []


def _quarter_label_from_date(date_str: str) -> str:
    if not date_str or len(date_str) < 7:
        return ""
    try:
        y = int(date_str[:4])
        m = int(date_str[5:7])
        return f"{y} Q{(m - 1) // 3 + 1}"
    except ValueError:
        return ""


def build_quarterly_buyback_breakdown(
    yf_symbol: str,
    years_back: int = 3,
    shares_outstanding: float | None = None,
) -> list[dict]:
    """Calendar-quarter repurchase summary (US 10-Q monthly periods rolled up)."""
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return []

    if not shares_outstanding or shares_outstanding <= 0:
        shares_outstanding = _shares_outstanding_from_yf(
            load_yf_cached([sym]).get(sym, {})
        )

    cutoff_year = datetime.now().year - max(1, years_back) + 1
    periods: list[dict] = []

    if sym.endswith((".T", ".HK", ".AX")):
        for ev in _load_repurchase_history_for_symbol(sym, years_back=years_back):
            periods.append({
                "period_end": ev.get("date"),
                "period_label": ev.get("date"),
                "shares": ev.get("shares"),
                "price": ev.get("price"),
                "source": ev.get("source"),
            })
    else:
        try:
            from data.us_buybacks import load_us_buyback_repurchases
            reps = [r for r in load_us_buyback_repurchases() if r.get("yf_symbol") == sym]
        except Exception:
            reps = []
        periods = list(_dedupe_us_repurchase_rows(reps, datetime.now().year).values())

    quarters: dict[str, dict] = {}
    for p in periods:
        pe = (p.get("period_end") or p.get("date") or "").strip()
        if len(pe) < 4:
            continue
        try:
            if int(pe[:4]) < cutoff_year:
                continue
        except ValueError:
            continue
        ql = _quarter_label_from_date(pe)
        if not ql:
            continue
        sh = p.get("shares") or p.get("shares_purchased")
        if not sh or int(sh) <= 0:
            continue
        sh = int(sh)
        pr = _safe_float(p.get("price"))
        bucket = quarters.setdefault(ql, {
            "quarter": ql,
            "shares": 0,
            "dollars": 0.0,
            "periods": [],
        })
        bucket["shares"] += sh
        if pr:
            bucket["dollars"] += sh * pr
        bucket["periods"].append({
            "period_end": pe,
            "period_label": p.get("period_label") or pe,
            "shares": sh,
            "price": pr,
            "source": p.get("source") or "",
        })

    out = []
    for ql in sorted(quarters.keys(), reverse=True):
        b = quarters[ql]
        shares = int(b["shares"])
        pct = None
        if shares_outstanding and shares_outstanding > 0:
            pct = min(_safe_round(shares / shares_outstanding * 100, 2), 100.0)
        avg_price = None
        if shares > 0 and b["dollars"] > 0:
            avg_price = _safe_round(b["dollars"] / shares, 2)
        out.append({
            "quarter": ql,
            "shares": shares,
            "pct_of_outstanding": pct,
            "avg_price": avg_price,
            "periods": sorted(
                b["periods"],
                key=lambda x: x.get("period_end") or "",
                reverse=True,
            ),
        })
    return out


def _dedupe_us_repurchase_rows(rows: list, year: int) -> dict:
    """Dedupe 10-Q monthly repurchase rows by period label (latest filing wins)."""
    min_year = 2010
    max_year = year + 1
    deduped = {}
    for r in rows:
        pe = (r.get("period_end") or r.get("date") or "").strip()
        if len(pe) < 4:
            continue
        try:
            period_year = int(pe[:4])
        except ValueError:
            continue
        if period_year < min_year or period_year > max_year:
            continue
        label = (r.get("period_label") or pe).strip()
        fd = r.get("filing_date") or ""
        existing = deduped.get(label)
        if not existing or fd > (existing.get("filing_date") or ""):
            deduped[label] = r
    return deduped


def _aggregate_us(events: list, year: int) -> dict:
    """
    US buybacks: annual % from 10-Q Item 2 repurchase tables (monthly/quarterly
    periods), not 8-K announcements. SEC issuers report repurchases each quarter.
    """
    try:
        from data.us_buybacks import load_us_buyback_repurchases
        rep_rows = load_us_buyback_repurchases()
    except Exception:
        rep_rows = []

    rep_by_sym: dict[str, list] = {}
    for row in rep_rows:
        rep_by_sym.setdefault(row["yf_symbol"], []).append(row)

    by_sym: dict[str, list] = {}
    for ev in events:
        by_sym.setdefault(ev["yf_symbol"], []).append(ev)

    all_syms = sorted(set(by_sym) | set(rep_by_sym))
    yf_cached = load_yf_cached(all_syms) if all_syms else {}

    out = {}
    year_prefix = str(year)
    for sym in all_syms:
        evs = by_sym.get(sym, [])
        evs.sort(key=lambda e: e.get("date") or "", reverse=True)
        latest_filing = evs[0] if evs else None

        deduped = _dedupe_us_repurchase_rows(rep_by_sym.get(sym, []), year)
        year_shares = 0
        rep_year_count = 0
        for r in deduped.values():
            pe = (r.get("period_end") or "").strip()
            if not pe.startswith(year_prefix):
                continue
            sh = r.get("shares") or r.get("shares_purchased")
            if sh and int(sh) > 0:
                year_shares += int(sh)
                rep_year_count += 1

        shares_out = _shares_outstanding_from_yf(yf_cached.get(sym, {}))
        annual_pct = 0.0
        if year_shares > 0 and shares_out > 0:
            annual_pct = _safe_round(year_shares / shares_out * 100, 2)
            if annual_pct > 100:
                logger.warning(
                    "US buyback %% capped for %s (computed %.2f%%, shares=%s, out=%s)",
                    sym,
                    annual_pct,
                    year_shares,
                    int(shares_out),
                )
                annual_pct = min(annual_pct, 100.0)

        rep_sorted = sorted(
            deduped.values(),
            key=lambda r: r.get("period_end") or "",
            reverse=True,
        )
        history = []
        for r in rep_sorted[:12]:
            sh = r.get("shares") or r.get("shares_purchased")
            history.append({
                "date": r.get("period_end") or r.get("date"),
                "type": "buyback",
                "desc": r.get("headline") or f"SEC 10-Q: {r.get('period_label') or 'repurchase'}",
                "headline": r.get("headline"),
                "url": r.get("url"),
                "shares": int(sh) if sh else None,
                "price": _safe_float(r.get("price")),
                "source": "SEC",
            })
        seen_dates = {h.get("date") for h in history}
        for e in evs:
            if len(history) >= 12:
                break
            d = e.get("date")
            if d in seen_dates or (e.get("shares") or e.get("shares_purchased")):
                continue
            history.append({
                "date": d,
                "type": "buyback",
                "desc": e.get("headline") or f"SEC {e.get('form') or 'filing'} buyback disclosure",
                "headline": e.get("headline"),
                "url": e.get("url"),
                "source": "SEC",
            })
            seen_dates.add(d)

        latest_rep = rep_sorted[0] if rep_sorted else None
        buyback_date = (
            (latest_rep.get("period_end") or latest_rep.get("date"))
            if latest_rep
            else (latest_filing.get("date") if latest_filing else None)
        )
        last_activity = buyback_date or (latest_filing.get("date") if latest_filing else None)
        year_evs = [e for e in evs if (e.get("date") or "")[:4] == year_prefix]

        out[sym] = {
            "yf_symbol": sym,
            "name": sym,
            "source": "SEC",
            "buyback_date": buyback_date,
            "annual_buyback_pct": annual_pct,
            "annual_shares_purchased": year_shares,
            "shares_outstanding": shares_out if shares_out > 0 else None,
            "buyback_events_year": rep_year_count or len(year_evs),
            "buyback_events_total": len(deduped) or len(evs),
            "buyback_announced": bool(deduped or evs),
            "buyback_history": history,
            "last_activity": last_activity,
            "csv_snapshot": {},
        }
    return out


def _aggregate_tse(df: pd.DataFrame, year: int) -> dict:
    if df.empty:
        return {}

    out = {}
    for code, grp in df.groupby("Code"):
        sym = _code_to_yf_symbol(code)
        grp = grp.sort_values("Implementation Date", ascending=False)
        latest_row = grp.iloc[0]
        year_mask = grp["Implementation Date"].dt.year == year
        year_grp = grp[year_mask]
        annual_shares = year_grp["No. of Shares to be Purchased"].sum(skipna=True)

        shares_out = None
        for so in grp["sharesOutstanding"]:
            if pd.notna(so) and so > 0:
                shares_out = float(so)
                break

        annual_pct = 0
        if pd.notna(annual_shares) and annual_shares > 0 and shares_out and shares_out > 0:
            annual_pct = _safe_round(annual_shares / shares_out * 100, 2)

        history = []
        for _, row in grp.head(12).iterrows():
            impl = row["Implementation Date"]
            history.append({
                "date": impl.strftime("%Y-%m-%d") if pd.notna(impl) else "",
                "type": "buyback",
                "desc": (
                    f"TSE: {int(row['No. of Shares to be Purchased']):,} shares"
                    if pd.notna(row.get("No. of Shares to be Purchased"))
                    else "TSE repurchase"
                ),
                "headline": row.get("Issue Name") or sym,
                "shares": row.get("No. of Shares to be Purchased"),
                "price": _parse_csv_price(row.get("Price")),
                "pct_of_shares": row.get("Share purchase pc share count"),
                "source": "TSE",
            })

        latest_dt = latest_row["Implementation Date"]
        out[sym] = {
            "yf_symbol": sym,
            "name": latest_row.get("Issue Name") or sym,
            "source": "TSE",
            "buyback_date": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "annual_buyback_pct": annual_pct,
            "annual_shares_purchased": int(annual_shares) if pd.notna(annual_shares) else 0,
            "shares_outstanding": shares_out,
            "buyback_events_year": int(year_mask.sum()),
            "buyback_events_total": len(grp),
            "buyback_announced": True,
            "buyback_history": history,
            "last_activity": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "csv_snapshot": _csv_snapshot_from_row(latest_row),
        }
    return out


def _aggregate_hk(df: pd.DataFrame, year: int) -> dict:
    if df.empty:
        return {}

    out = {}
    hk_symbols = sorted({
        _hk_code_to_yf_symbol(c)
        for c in df["Stock code"].dropna().unique()
        if _hk_code_to_yf_symbol(c)
    })
    yf_cached = load_yf_cached(hk_symbols)

    for code, grp in df.groupby("Stock code"):
        sym = _hk_code_to_yf_symbol(code)
        if not sym:
            continue
        grp = grp.sort_values("Trading Date", ascending=False)
        latest_row = grp.iloc[0]
        year_mask = grp["Trading Date"].dt.year == year
        year_grp = grp[year_mask]
        annual_shares = year_grp["No. of Shares to be Purchased"].sum(skipna=True)

        shares_out = None
        for so in grp["sharesOutstanding"]:
            if pd.notna(so) and so > 0:
                shares_out = float(so)
                break
        if not shares_out:
            derived = _shares_outstanding_from_yf(yf_cached.get(sym, {}))
            if derived > 0:
                shares_out = derived

        annual_pct = 0
        if pd.notna(annual_shares) and annual_shares > 0 and shares_out and shares_out > 0:
            annual_pct = _safe_round(annual_shares / shares_out * 100, 2)

        history = []
        for _, row in grp.head(12).iterrows():
            impl = row["Trading Date"]
            shares = row.get("No. of Shares to be Purchased")
            row_pct = row.get("Share purchase pc share count")
            if (pd.isna(row_pct) or row_pct is None) and pd.notna(shares) and shares_out:
                row_pct = _safe_round(float(shares) / shares_out * 100, 4)
            history.append({
                "date": impl.strftime("%Y-%m-%d") if pd.notna(impl) else "",
                "type": "buyback",
                "desc": (
                    f"HKEX: {int(shares):,} shares"
                    if pd.notna(shares)
                    else "HKEX repurchase"
                ),
                "headline": row.get("Company") or sym,
                "shares": shares,
                "price": _safe_float(row.get("Highest Repurchase Price")),
                "pct_of_shares": row_pct,
                "source": "HKEX",
            })

        latest_dt = latest_row["Trading Date"]
        out[sym] = {
            "yf_symbol": sym,
            "name": latest_row.get("Company") or sym,
            "source": "HKEX",
            "buyback_date": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "annual_buyback_pct": annual_pct,
            "annual_shares_purchased": int(annual_shares) if pd.notna(annual_shares) else 0,
            "shares_outstanding": shares_out,
            "buyback_events_year": int(year_mask.sum()),
            "buyback_events_total": len(grp),
            "buyback_announced": True,
            "buyback_history": history,
            "last_activity": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "csv_snapshot": _csv_snapshot_from_hk_row(latest_row),
        }
    return out


def _aggregate_asx(events: list, year: int) -> dict:
    out = {}
    by_sym = {}
    for ev in events:
        by_sym.setdefault(ev["yf_symbol"], []).append(ev)

    for sym, evs in by_sym.items():
        evs.sort(key=lambda e: e.get("date") or "", reverse=True)
        latest = evs[0]
        year_evs = [e for e in evs if (e.get("date") or "")[:4] == str(year)]
        history = [{
            "date": e.get("date"),
            "type": "buyback",
            "desc": e.get("headline") or "ASX buy-back announcement",
            "headline": e.get("headline"),
            "url": e.get("url"),
            "source": "ASX",
        } for e in evs[:12]]

        out[sym] = {
            "yf_symbol": sym,
            "name": sym.replace(".AX", ""),
            "source": "ASX",
            "buyback_date": latest.get("date"),
            "annual_buyback_pct": 0,
            "annual_shares_purchased": 0,
            "shares_outstanding": None,
            "buyback_events_year": len(year_evs),
            "buyback_events_total": len(evs),
            "buyback_announced": True,
            "buyback_history": history,
            "last_activity": latest.get("date"),
            "csv_snapshot": {},
        }
    return out


def _aggregate_asx_insider(events: list, year: int) -> dict:
    if not events:
        return {}

    out = {}
    by_sym = {}
    for ev in events:
        by_sym.setdefault(ev["yf_symbol"], []).append(ev)

    for sym, evs in by_sym.items():
        evs.sort(key=lambda e: e.get("date") or "", reverse=True)
        year_evs = [e for e in evs if (e.get("date") or "")[:4] == str(year)]

        history = []
        for e in evs[:16]:
            director = e.get("director") or "Director"
            history.append({
                "date": e.get("date"),
                "type": "insider",
                "desc": e.get("headline") or f"ASX: {director} purchase",
                "headline": e.get("headline"),
                "url": e.get("url"),
                "director": director,
                "source": "ASX",
            })

        latest = evs[0]
        out[sym] = {
            "yf_symbol": sym,
            "name": sym.replace(".AX", ""),
            "source": "ASX-DI",
            "insider_buys_2026": len(year_evs),
            "insider_events_total": len(evs),
            "last_insider_date": latest.get("date"),
            "insider_history": history,
            "last_activity": latest.get("date"),
            "buyback_announced": False,
            "buyback_date": None,
            "annual_buyback_pct": 0,
            "csv_snapshot": {},
        }
    return out


def _aggregate_hk_insider(df: pd.DataFrame, year: int) -> dict:
    if df.empty:
        return {}

    out = {}
    for code, grp in df.groupby("Stock code"):
        sym = _hk_code_to_yf_symbol(code)
        if not sym:
            continue
        grp = grp.sort_values("Event Date", ascending=False)
        year_mask = grp["Event Date"].dt.year == year
        year_grp = grp[year_mask]

        history = []
        for _, row in grp.head(16).iterrows():
            ev = row["Event Date"]
            shares = row.get("Shares")
            price = row.get("Average price")
            history.append({
                "date": ev.strftime("%Y-%m-%d") if pd.notna(ev) else "",
                "type": "insider",
                "desc": (
                    f"HKEX DI: {row.get('Director') or 'Director'} bought "
                    f"{int(shares):,} @ {price}"
                    if pd.notna(shares) and pd.notna(price)
                    else f"HKEX DI: {row.get('Director') or 'Director'} purchase"
                ),
                "headline": row.get("Director") or "",
                "shares": int(shares) if pd.notna(shares) else None,
                "price": _safe_float(price),
                "currency": row.get("Currency") or "HKD",
                "source": "HKEX-DI",
            })

        latest_dt = grp.iloc[0]["Event Date"]
        out[sym] = {
            "yf_symbol": sym,
            "name": grp.iloc[0].get("Company") or sym,
            "source": "HKEX-DI",
            "insider_buys_2026": int(year_mask.sum()),
            "insider_events_total": len(grp),
            "last_insider_date": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "insider_history": history,
            "last_activity": latest_dt.strftime("%Y-%m-%d") if pd.notna(latest_dt) else None,
            "buyback_announced": False,
            "buyback_date": None,
            "annual_buyback_pct": 0,
            "csv_snapshot": {},
        }
    return out


def _merge_date_str(val) -> str:
    return val if isinstance(val, str) else ""


def _merge_aggregates(*agg_dicts) -> dict:
    merged = {}
    for agg in agg_dicts:
        for sym, row in agg.items():
            if sym not in merged:
                merged[sym] = dict(row)
                continue
            existing = merged[sym]
            if (row.get("annual_buyback_pct") or 0) > (existing.get("annual_buyback_pct") or 0):
                existing.update(row)
            elif _merge_date_str(row.get("buyback_date")) > _merge_date_str(existing.get("buyback_date")):
                for k, v in row.items():
                    if k not in existing or not existing.get(k):
                        existing[k] = v
            if existing.get("source") != row.get("source"):
                existing["source"] = f"{existing.get('source')}+{row.get('source')}"
            existing["insider_buys_2026"] = max(
                existing.get("insider_buys_2026") or 0,
                row.get("insider_buys_2026") or 0,
            )
            hist = (existing.get("buyback_history") or []) + (row.get("buyback_history") or [])
            hist.sort(key=lambda h: h.get("date") or "", reverse=True)
            existing["buyback_history"] = hist[:16]
            ihist = (existing.get("insider_history") or []) + (row.get("insider_history") or [])
            ihist.sort(key=lambda h: h.get("date") or "", reverse=True)
            existing["insider_history"] = ihist[:16]
            if (row.get("last_insider_date") or "") > (existing.get("last_insider_date") or ""):
                existing["last_insider_date"] = row.get("last_insider_date")
    return merged


def _enrich_buyback_rows_edgar(rows: list) -> list:
    """Attach cached SEC EDGAR metrics for US tickers (table + filters)."""
    try:
        from data.edgar_metrics import enrich_stocks_edgar_cached
        return enrich_stocks_edgar_cached(rows)
    except Exception as e:
        logger.warning("EDGAR cache enrich skipped for buybacks: %s", e)
        return rows


def _finalize_buyback_list_rows(rows: list) -> list:
    """Apply runtime patches (names, US indices, EDGAR) before API response."""
    rows = _apply_asx_official_names(_apply_us_indices(rows))
    return _enrich_buyback_rows_edgar(rows)


def _apply_us_indices(rows: list, index_sets: dict = None) -> list:
    """Tag US rows with Russell 1000 / Nasdaq-100 membership (works without registry rebuild)."""
    try:
        from data.us_universe import load_us_index_sets, us_indices_for_symbol
        sets = index_sets or load_us_index_sets()
    except Exception:
        return rows
    for row in rows:
        sym = (row.get("ticker") or "").strip().upper()
        indices = us_indices_for_symbol(sym, sets)
        if indices:
            row["us_indices"] = indices
    return rows


_CSV_SNAPSHOT_STORE_KEYS = (
    "price",
    "market_cap",
    "pe",
    "forward_pe",
    "pb",
    "debt_to_equity",
    "shares_outstanding",
    "total_cash",
    "net_cash_m",
    "net_cash_pct_mcap",
    "net_assets_m",
    "net_assets_vs_mcap_m",
    "net_assets_vs_mcap_pct",
    "currency",
)

_SIGNAL_ROW_STORE_FIELDS = (
    "ticker",
    "name",
    "buyback_announced",
    "buyback_date",
    "annual_buyback_pct",
    "annual_shares_purchased",
    "buyback_source",
    "buyback_events_year",
    "buyback_events_total",
    "insider_buys_2026",
    "last_insider_date",
    "last_activity",
    "signals",
    "us_indices",
    "shares_outstanding",
    "csv_snapshot",
)


def _is_legacy_full_registry_row(row: dict) -> bool:
    return bool(row.get("metrics")) or "current_price" in row


def _normalize_stored_registry_row(row: dict) -> dict:
    """Coerce DB JSON to signal-only shape (strips duplicated fundamentals from old saves)."""
    if not row:
        return {}
    if not _is_legacy_full_registry_row(row):
        return dict(row)
    out = {"ticker": (row.get("ticker") or "").strip().upper()}
    for key in _SIGNAL_ROW_STORE_FIELDS:
        if key == "ticker":
            continue
        val = row.get(key)
        if val is not None and val != "" and val != []:
            out[key] = val
    return out


def _signal_row_for_storage(row: dict) -> dict:
    """Keep only fields persisted in buyback_registry."""
    normalized = _normalize_stored_registry_row(row)
    sym = (normalized.get("ticker") or row.get("ticker") or "").strip().upper()
    if not sym:
        return {}
    out = {"ticker": sym}
    for key in _SIGNAL_ROW_STORE_FIELDS:
        if key == "ticker":
            continue
        val = normalized.get(key)
        if val is None or val == "" or val == []:
            continue
        if key == "csv_snapshot" and isinstance(val, dict):
            slim_snap = {k: val[k] for k in _CSV_SNAPSHOT_STORE_KEYS if k in val}
            if slim_snap:
                out["csv_snapshot"] = slim_snap
            continue
        out[key] = val
    return out


def _signal_registry_row(sym: str, agg: dict, us_index_sets: dict = None) -> dict:
    """Build signal-only registry row from aggregated event data."""
    has_buyback = bool(agg.get("buyback_date") or agg.get("buyback_events_total"))
    signals = (agg.get("buyback_history") or []) + (agg.get("insider_history") or [])
    signals.sort(key=lambda h: h.get("date") or "", reverse=True)

    row = {
        "ticker": sym,
        "buyback_announced": has_buyback or bool(agg.get("insider_buys_2026")),
        "buyback_date": agg.get("buyback_date"),
        "annual_buyback_pct": agg.get("annual_buyback_pct") or 0,
        "annual_shares_purchased": agg.get("annual_shares_purchased") or 0,
        "buyback_source": agg.get("source") or "",
        "buyback_events_year": agg.get("buyback_events_year") or 0,
        "buyback_events_total": agg.get("buyback_events_total") or 0,
        "insider_buys_2026": agg.get("insider_buys_2026") or 0,
        "last_insider_date": agg.get("last_insider_date"),
        "last_activity": agg.get("last_activity"),
        "signals": signals[:20],
    }
    if agg.get("name"):
        row["name"] = agg["name"]
    if agg.get("shares_outstanding"):
        row["shares_outstanding"] = agg["shares_outstanding"]
    snap = agg.get("csv_snapshot") or {}
    if snap and (snap.get("price") or snap.get("market_cap")):
        row["csv_snapshot"] = {k: snap[k] for k in _CSV_SNAPSHOT_STORE_KEYS if k in snap}
    indices = []
    if us_index_sets is not None:
        try:
            from data.us_universe import us_indices_for_symbol
            indices = us_indices_for_symbol(sym, us_index_sets)
        except Exception:
            pass
    if indices:
        row["us_indices"] = indices
    return row


def _compose_buyback_display_row(
    sym: str,
    signal_row: dict,
    ydat: dict,
    list_meta: dict,
    asx_univ: dict = None,
    us_index_sets: dict = None,
) -> dict:
    """Merge signal registry row with yfinance (or CSV fallback) fundamentals."""
    csv_ydat = _ydat_from_csv_snapshot(
        {"csv_snapshot": signal_row.get("csv_snapshot") or {}},
        sym,
    )
    use_ydat = ydat if (ydat and ydat.get("price")) else csv_ydat

    if use_ydat and (use_ydat.get("price") or (use_ydat.get("metrics") or {}).get("market_cap")):
        row = _stock_row(sym, use_ydat, list_meta)
        row["_pending_yf"] = not bool(ydat and ydat.get("price"))
        row["_from_csv"] = bool(csv_ydat and not (ydat and ydat.get("price")))
        row["_live"] = bool(ydat and ydat.get("price"))
    else:
        row = _placeholder_row(sym, list_meta)
        row["_pending_yf"] = True
        row["_from_csv"] = False
        if signal_row.get("name"):
            row["name"] = signal_row["name"]

    row.update(_overlay_from_registry_row(signal_row))
    registry_signals = signal_row.get("signals") or []
    if registry_signals:
        row["signals"] = registry_signals

    shares_out = signal_row.get("shares_outstanding")
    if shares_out:
        row.setdefault("metrics", {})["shares_outstanding"] = shares_out

    if sym.endswith(".AX"):
        row["name"] = asx_official_name(sym, asx_univ, fallback=row.get("name") or sym)

    indices = signal_row.get("us_indices") or []
    if not indices and us_index_sets is not None:
        try:
            from data.us_universe import us_indices_for_symbol
            indices = us_indices_for_symbol(sym, us_index_sets)
        except Exception:
            indices = []
    if indices:
        row["us_indices"] = indices

    row["_market"] = "buybacks"
    row["_has_buyback_signals"] = True
    return _sanitize_for_json(row)


def _hydrate_buyback_rows(
    signal_rows: list,
    cached: dict = None,
    asx_univ: dict = None,
    us_index_sets: dict = None,
    list_meta: dict = None,
) -> list:
    """Attach fundamentals from business_list_yf_cache to signal registry rows."""
    list_meta = list_meta or {"list_id": "buybacks", "display_name": "Buybacks registry"}
    symbols = [(r.get("ticker") or "").strip().upper() for r in signal_rows if r.get("ticker")]
    if cached is None:
        cached = load_yf_cached(symbols) if symbols else {}
    if asx_univ is None:
        asx_univ = load_asx_universe_details()
    if us_index_sets is None:
        try:
            from data.us_universe import load_us_index_sets
            us_index_sets = load_us_index_sets()
        except Exception:
            us_index_sets = {}

    out = []
    for signal_row in signal_rows:
        sym = (signal_row.get("ticker") or "").strip().upper()
        if not sym:
            continue
        out.append(
            _compose_buyback_display_row(
                sym,
                signal_row,
                cached.get(sym, {}),
                list_meta,
                asx_univ=asx_univ,
                us_index_sets=us_index_sets,
            )
        )
    return out


def _apply_asx_official_names(rows: list, asx_univ: dict = None) -> list:
    """Patch .AX rows with ASX CSV names (works on cached registry without rebuild)."""
    univ = asx_univ if asx_univ is not None else load_asx_universe_details()
    if not univ:
        return rows
    for row in rows:
        sym = (row.get("ticker") or "").strip().upper()
        if sym.endswith(".AX"):
            row["name"] = asx_official_name(sym, univ, fallback=row.get("name") or sym)
    return rows


def _slim_list_row(row: dict) -> dict:
    slim = dict(row)
    slim.pop("signals", None)
    if slim.get("buyback_events_total"):
        slim["_has_history"] = True
    return slim


def _sort_buyback_rows(rows: list) -> list:
    rows.sort(key=lambda s: (
        -(s.get("annual_buyback_pct") or 0),
        s.get("buyback_date") or "",
    ))
    return rows


def _rebuild_registry(force_yf: bool = False) -> tuple:
    """Rebuild full registry from sources; returns (full_rows, meta, timings_ms)."""
    timings = {}
    t0 = time.time()
    year = datetime.now().year

    tse_df = load_tse_events()
    hk_df = load_hk_events()
    hk_insider_df = load_hk_insider_events()
    timings["csv_read_ms"] = round((time.time() - t0) * 1000, 1)

    t1 = time.time()
    asx_events = load_asx_buyback_events()
    asx_insider_events = load_asx_insider_events()
    us_events = load_us_buyback_events()
    us_insider_events = load_us_insider_events()
    agg = _merge_aggregates(
        _aggregate_tse(tse_df, year),
        _aggregate_hk(hk_df, year),
        _aggregate_hk_insider(hk_insider_df, year),
        _aggregate_asx(asx_events, year),
        _aggregate_asx_insider(asx_insider_events, year),
        _aggregate_us(us_events, year),
        _aggregate_us_insider(us_insider_events, year),
    )
    timings["aggregate_ms"] = round((time.time() - t1) * 1000, 1)

    symbols = sorted(agg.keys())
    symbols = filter_active_symbols(symbols)

    t2 = time.time()
    max_yf_age = 0 if force_yf else YF_CACHE_MAX_AGE_DAYS
    cached = load_yf_cached(symbols, max_days=max_yf_age)
    stale = [s for s in symbols if s not in cached]
    timings["yf_cache_ms"] = round((time.time() - t2) * 1000, 1)

    if stale and force_yf:
        logger.info("[FORCED] Buybacks: fetching %d symbols sequentially...", len(stale))
        t3 = time.time()
        newly = _fetch_yf_sequential(
            stale,
            sleep_seconds=YF_FETCH_SLEEP_SECONDS,
            progress_every=25,
            retry_tentative_inactive=True,
        )
        cached.update(newly)
        timings["yf_fetch_ms"] = round((time.time() - t3) * 1000, 1)
    elif stale:
        logger.info("Buybacks: %d yf cached, %d using CSV/placeholder", len(cached), len(stale))

    asx_univ = load_asx_universe_details()
    us_index_sets = None
    try:
        from data.us_universe import load_us_index_sets
        us_index_sets = load_us_index_sets()
    except Exception:
        pass

    t4 = time.time()
    signal_rows = [
        _signal_registry_row(sym, agg[sym], us_index_sets=us_index_sets)
        for sym in symbols
    ]
    results = _hydrate_buyback_rows(
        signal_rows,
        cached=cached,
        asx_univ=asx_univ,
        us_index_sets=us_index_sets,
    )
    _sort_buyback_rows(results)
    timings["build_rows_ms"] = round((time.time() - t4) * 1000, 1)
    timings["total_ms"] = round((time.time() - t0) * 1000, 1)

    meta = {
        "buyback_year": year,
        "tse_rows": len(tse_df),
        "hk_rows": len(hk_df),
        "hk_insider_rows": len(hk_insider_df),
        "asx_events": len(asx_events),
        "asx_insider_rows": len(asx_insider_events),
        "asx_buyback_rows": len(asx_events),
        "us_events": len(us_events),
        "us_buyback_rows": len(us_events),
        "us_insider_rows": len(us_insider_events),
        "timings_ms": timings,
        "source": "rebuild",
    }
    logger.info("Buybacks registry rebuilt in %s ms: %s", timings["total_ms"], timings)
    return signal_rows, results, meta, timings


def _apply_registry_to_cache(full_rows: list, meta: dict, timings: dict, lite: bool = True) -> list:
    now = time.time()
    list_rows = [_slim_list_row(r) for r in full_rows] if lite else full_rows
    list_rows = [_sanitize_for_json(r) for r in list_rows]
    _buybacks_cache.update({
        "data": list_rows,
        "full_data": full_rows,
        "ts": now,
        "last_scraped": datetime.now().isoformat(),
        "tse_rows": meta["tse_rows"],
        "hk_rows": meta.get("hk_rows", 0),
        "hk_insider_rows": meta.get("hk_insider_rows", 0),
        "asx_events": meta["asx_events"],
        "asx_insider_rows": meta.get("asx_insider_rows", 0),
        "asx_buyback_rows": meta.get("asx_buyback_rows", meta["asx_events"]),
        "us_events": meta.get("us_events", 0),
        "us_buyback_rows": meta.get("us_buyback_rows", meta.get("us_events", 0)),
        "us_insider_rows": meta.get("us_insider_rows", 0),
        "buyback_year": meta["buyback_year"],
        "timings_ms": timings,
        "registry_source": meta.get("source", "rebuild"),
    })
    return list_rows


def _refresh_registry_from_cache(lite: bool = True) -> list:
    """Rebuild signal registry and hydrate display rows from business YF cache."""
    signal_rows, composed, meta, timings = _rebuild_registry(force_yf=False)
    _save_registry(signal_rows, meta)
    return _apply_registry_to_cache(composed, meta, timings, lite=lite)


def get_yf_rebuild_status() -> dict:
    with _yf_rebuild_lock:
        state = dict(_yf_rebuild_state)
    meta = get_buybacks_meta()
    return {
        **state,
        "yf_cached_count": meta.get("yf_cached_count", 0),
        "csv_snapshot_count": meta.get("csv_snapshot_count", 0),
        "universe_count": meta.get("universe_count", 0),
    }


def fetch_buyback_stocks(
    force_yf: bool = False,
    lite: bool = True,
    force_registry: bool = False,
) -> list:
    """Load buyback universe: signal registry hydrated with business_list_yf_cache fundamentals."""
    now = time.time()
    if (
        _buybacks_cache.get("data") is not None
        and now - _buybacks_cache.get("ts", 0) < BUYBACKS_CACHE_TTL
        and not force_yf
        and not force_registry
    ):
        rows = [_sanitize_for_json(r) for r in _buybacks_cache["data"]]
        return _finalize_buyback_list_rows(rows)

    if force_yf or force_registry or _registry_needs_rebuild():
        signal_rows, composed, meta, timings = _rebuild_registry(force_yf=force_yf)
        _save_registry(signal_rows, meta)
        list_rows = _apply_registry_to_cache(composed, meta, timings, lite=lite)
        return _finalize_buyback_list_rows(list_rows)

    t0 = time.time()
    list_rows = _load_registry_from_db(lite=lite)
    load_ms = round((time.time() - t0) * 1000, 1)
    logger.info("Buybacks registry loaded from SQLite: %d rows in %s ms", len(list_rows), load_ms)

    list_rows = [_sanitize_for_json(r) for r in list_rows]
    _buybacks_cache.update({
        "data": list_rows,
        "ts": now,
        "last_scraped": _registry_get_meta("built_at") or datetime.now().isoformat(),
        "tse_rows": int(_registry_get_meta("tse_rows") or 0),
        "hk_rows": int(_registry_get_meta("hk_rows") or 0),
        "hk_insider_rows": int(_registry_get_meta("hk_insider_rows") or 0),
        "asx_events": int(_registry_get_meta("asx_events") or 0),
        "asx_insider_rows": int(_registry_get_meta("asx_insider_rows") or 0),
        "asx_buyback_rows": int(_registry_get_meta("asx_buyback_rows") or 0),
        "us_events": int(_registry_get_meta("us_events") or 0),
        "us_buyback_rows": int(_registry_get_meta("us_buyback_rows") or 0) or _count_us_buyback_events(),
        "us_insider_rows": int(_registry_get_meta("us_insider_rows") or 0) or _count_us_insider_purchases(),
        "buyback_year": int(_registry_get_meta("buyback_year") or datetime.now().year),
        "timings_ms": {"registry_load_ms": load_ms, "total_ms": load_ms},
        "registry_source": "sqlite",
    })
    return _finalize_buyback_list_rows(list_rows)


def get_buybacks_meta() -> dict:
    tse_path = TSE_CSV_PATH
    hk_path = HK_CSV_PATH
    tse_mtime = None
    tse_rows = int(_registry_get_meta("tse_rows") or 0)
    if tse_path.exists():
        tse_mtime = datetime.fromtimestamp(tse_path.stat().st_mtime).isoformat()
        if not tse_rows:
            try:
                tse_rows = len(pd.read_csv(tse_path))
            except Exception:
                pass

    hk_rows = int(_registry_get_meta("hk_rows") or 0)
    hk_mtime = None
    if hk_path.exists():
        hk_mtime = datetime.fromtimestamp(hk_path.stat().st_mtime).isoformat()
        if not hk_rows:
            try:
                hk_rows = len(pd.read_csv(hk_path))
            except Exception:
                pass

    hk_insider_path = HK_INSIDER_CSV_PATH
    hk_insider_rows = int(_registry_get_meta("hk_insider_rows") or 0)
    hk_insider_mtime = None
    if hk_insider_path.exists():
        hk_insider_mtime = datetime.fromtimestamp(hk_insider_path.stat().st_mtime).isoformat()
        if not hk_insider_rows:
            try:
                hk_insider_rows = len(pd.read_csv(hk_insider_path))
            except Exception:
                pass

    data = _buybacks_cache.get("data") or []
    asx_last_fetched = ""
    try:
        from asx_announcements import get_scrape_meta
        asx_last_fetched = get_scrape_meta("last_market_fetch_at", DEFAULT_UNIVERSE_DB)
    except Exception:
        pass

    return {
        "data_source": "JPX TSE + HKEX + ASX + SEC EDGAR (US Russell 1000 / Nasdaq-100 buybacks + Form 4 insider) • yfinance (optional)",
        "last_scraped": _buybacks_cache.get("last_scraped") or _registry_get_meta("built_at") or datetime.now().isoformat(),
        "market": "buybacks",
        "universe_count": len(data),
        "tse_csv_path": str(tse_path),
        "tse_csv_rows": tse_rows,
        "tse_csv_updated": tse_mtime,
        "tse_loaded": _buybacks_cache.get("tse_rows", tse_rows),
        "hk_csv_path": str(hk_path),
        "hk_csv_rows": hk_rows,
        "hk_csv_updated": hk_mtime,
        "hk_loaded": _buybacks_cache.get("hk_rows", hk_rows),
        "hk_insider_csv_path": str(hk_insider_path),
        "hk_insider_csv_rows": hk_insider_rows,
        "hk_insider_csv_updated": hk_insider_mtime,
        "hk_insider_loaded": _buybacks_cache.get("hk_insider_rows", hk_insider_rows),
        "insider_buy_count": sum(s.get("insider_buys_2026") or 0 for s in data),
        "asx_buyback_events": _buybacks_cache.get("asx_events", int(_registry_get_meta("asx_events") or 0)),
        "asx_insider_rows": int(_registry_get_meta("asx_insider_rows") or 0) or _count_asx_insider_purchases(),
        "asx_insider_loaded": _buybacks_cache.get("asx_insider_rows", int(_registry_get_meta("asx_insider_rows") or 0)),
        "asx_buyback_rows": int(_registry_get_meta("asx_buyback_rows") or 0) or _count_asx_buyback_events(),
        "asx_last_fetched": asx_last_fetched,
        "us_buyback_rows": int(_registry_get_meta("us_buyback_rows") or 0) or _count_us_buyback_events(),
        "us_last_fetched": _us_buybacks_last_fetched(),
        "us_insider_rows": int(_registry_get_meta("us_insider_rows") or 0) or _count_us_insider_purchases(),
        "us_insider_last_fetched": _us_insider_last_fetched(),
        "us_universe_count": _us_scan_universe_count(),
        "buyback_year": _buybacks_cache.get("buyback_year", int(_registry_get_meta("buyback_year") or datetime.now().year)),
        "yf_cached_count": sum(1 for s in data if not s.get("_pending_yf")),
        "csv_snapshot_count": sum(1 for s in data if s.get("_from_csv")),
        "timings_ms": _buybacks_cache.get("timings_ms", {}),
        "registry_source": _buybacks_cache.get("registry_source", "unknown"),
    }


def refresh_jpx_buybacks(fetch_yfinance: bool = False) -> dict:
    result = refresh_jpx_csv(fetch_yfinance=fetch_yfinance)
    _buybacks_cache.clear()
    _registry_set_meta({"tse_csv_mtime": ""})
    stocks = fetch_buyback_stocks(force_yf=False)
    result["stocks"] = len(stocks)
    result["meta"] = get_buybacks_meta()
    return result


def refresh_hkex_buybacks(download_gaps: bool = True) -> dict:
    result = refresh_hkex_csv(download_gaps=download_gaps)
    _buybacks_cache.clear()
    _registry_set_meta({"hk_csv_mtime": ""})
    stocks = fetch_buyback_stocks(force_yf=False)
    result["stocks"] = len(stocks)
    result["meta"] = get_buybacks_meta()
    return result


def refresh_hk_insider_buybacks(download_gaps: bool = True) -> dict:
    result = refresh_hk_insider_csv(download_gaps=download_gaps)
    _buybacks_cache.clear()
    _registry_set_meta({"hk_insider_csv_mtime": ""})
    stocks = fetch_buyback_stocks(force_yf=False)
    result["stocks"] = len(stocks)
    result["meta"] = get_buybacks_meta()
    return result


def refresh_asx_buybacks_data(
    days: int = 365,
    use_pdf: bool = True,
    incremental: bool = False,
    backfill: bool = False,
) -> dict:
    from asx_announcements import refresh_asx_announcements

    result = refresh_asx_announcements(
        days=days,
        use_pdf=use_pdf,
        incremental=incremental,
        backfill=backfill,
    )
    _buybacks_cache.clear()
    _registry_set_meta({"asx_insider_rows": "", "asx_buyback_rows": ""})
    stocks = fetch_buyback_stocks(force_yf=False)
    result["stocks"] = len(stocks)
    result["meta"] = get_buybacks_meta()
    return result


def refresh_asx_insider_buybacks(days: int = 365, use_pdf: bool = True) -> dict:
    """Backward-compatible alias."""
    return refresh_asx_buybacks_data(days=days, use_pdf=use_pdf, backfill=True)


def _us_sec_refresh_running() -> bool:
    with _us_sec_refresh_lock:
        return bool(_us_sec_refresh_state.get("running"))


def _us_sec_progress(**kwargs):
    with _us_sec_refresh_lock:
        _us_sec_refresh_state.update(kwargs)
        phase = _us_sec_refresh_state.get("phase") or ""
        done = int(_us_sec_refresh_state.get("tickers_done") or 0)
        total = int(_us_sec_refresh_state.get("tickers_total") or 0)
        ticker = _us_sec_refresh_state.get("current_ticker") or ""
        filings = int(_us_sec_refresh_state.get("filings_scanned") or 0)
        if phase == "buybacks":
            new_n = int(_us_sec_refresh_state.get("new_events") or 0)
            msg = f"SEC buybacks {done}/{total} • {filings} filings • {new_n} new"
        elif phase == "insider":
            new_n = int(_us_sec_refresh_state.get("new_purchases") or 0)
            msg = f"SEC Form 4 {done}/{total} • {filings} filings • {new_n} new"
        elif phase == "registry":
            msg = "Rebuilding buybacks registry…"
        else:
            msg = f"SEC scan {done}/{total}"
        if ticker:
            msg += f" • {ticker}"
        _us_sec_refresh_state["message"] = msg


def mark_us_sec_refresh_started(max_tickers: int | None = None) -> bool:
    """Mark SEC scan as running (call before spawning background thread)."""
    from data.us_universe import load_us_scan_universe

    universe = load_us_scan_universe()
    if max_tickers:
        universe = universe[: max(1, int(max_tickers))]
    with _us_sec_refresh_lock:
        if _us_sec_refresh_state.get("running"):
            return False
        _us_sec_refresh_state.update({
            "running": True,
            "phase": "starting",
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "error": None,
            "message": "SEC scan starting…",
            "tickers_total": len(universe),
            "tickers_done": 0,
            "current_ticker": "",
            "filings_scanned": 0,
            "new_events": 0,
            "new_purchases": 0,
            "errors": 0,
        })
        return True


def get_us_sec_refresh_status() -> dict:
    with _us_sec_refresh_lock:
        return dict(_us_sec_refresh_state)


def refresh_us_buybacks_data(
    days: int = 365,
    max_tickers: int | None = None,
    reparse_10q: bool = False,
) -> dict:
    from data.us_buybacks import refresh_us_buybacks

    if not _us_sec_refresh_running():
        if not mark_us_sec_refresh_started(max_tickers=max_tickers):
            return {"status": "already_running"}

    try:
        from data.us_insider import refresh_us_insider_purchases

        bb_result = refresh_us_buybacks(
            days_back=days,
            max_tickers=max_tickers,
            reparse_10q=reparse_10q,
            on_progress=_us_sec_progress,
        )
        insider_result = refresh_us_insider_purchases(
            days_back=days,
            max_tickers=max_tickers,
            on_progress=_us_sec_progress,
        )
        _us_sec_progress(phase="registry", message="Rebuilding buybacks registry…")
        _buybacks_cache.clear()
        _registry_set_meta({"us_buyback_rows": "", "us_insider_rows": ""})
        stocks = fetch_buyback_stocks(force_yf=False)
        result = {
            **bb_result,
            "insider": insider_result,
            "status": "ok",
        }
        result["stocks"] = len(stocks)
        result["meta"] = get_buybacks_meta()
        with _us_sec_refresh_lock:
            _us_sec_refresh_state["message"] = (
                f"SEC scan complete • {bb_result.get('new_events', 0)} buybacks • "
                f"{insider_result.get('new_purchases', 0)} insider purchases"
            )
        return result
    except Exception as ex:
        with _us_sec_refresh_lock:
            _us_sec_refresh_state["error"] = str(ex)
            _us_sec_refresh_state["message"] = f"SEC scan failed: {ex}"
        raise
    finally:
        with _us_sec_refresh_lock:
            _us_sec_refresh_state.update({
                "running": False,
                "finished_at": datetime.now().isoformat(),
            })


def rebuild_buybacks_yf_cache(sleep_seconds: float = None):
    agg_syms = sorted({
        _code_to_yf_symbol(c)
        for c in load_tse_events()["Code"].dropna().unique()
    } | {
        _hk_code_to_yf_symbol(c)
        for c in load_hk_events()["Stock code"].dropna().unique()
    } | {
        _hk_code_to_yf_symbol(c)
        for c in load_hk_insider_events()["Stock code"].dropna().unique()
    } | {e["yf_symbol"] for e in load_asx_buyback_events()} | {
        e["yf_symbol"] for e in load_asx_insider_events()
    } | {e["yf_symbol"] for e in load_us_buyback_events()} | {
        e["yf_symbol"] for e in load_us_insider_events()
    })
    syms = filter_active_symbols(agg_syms, confirmed_only=True)
    total = len(syms)
    logger.info("=== REBUILD buybacks YF: %d symbols ===", total)

    with _yf_rebuild_lock:
        if _yf_rebuild_state.get("running"):
            logger.warning("Buybacks YF rebuild already running — skip duplicate start")
            return
        _yf_rebuild_state.update({
            "running": True,
            "done": 0,
            "total": total,
            "fetched": 0,
            "registry_refreshes": 0,
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
            "error": None,
            "message": f"Fetching yfinance for {total} symbols…",
        })

    def _on_progress(done: int, progress_total: int):
        with _yf_rebuild_lock:
            _yf_rebuild_state["done"] = done
            _yf_rebuild_state["total"] = progress_total
            _yf_rebuild_state["message"] = f"YF fetch {done}/{progress_total}"
        _buybacks_cache.clear()
        _refresh_registry_from_cache(lite=True)
        with _yf_rebuild_lock:
            _yf_rebuild_state["registry_refreshes"] = _yf_rebuild_state.get("registry_refreshes", 0) + 1
            _yf_rebuild_state["message"] = (
                f"YF fetch {done}/{progress_total} • registry refreshed"
            )
        logger.info("Buybacks registry refreshed after YF progress %d/%d", done, progress_total)

    try:
        fetched = _fetch_yf_sequential(
            syms,
            sleep_seconds=sleep_seconds,
            progress_every=25,
            retry_tentative_inactive=True,
            progress_label="buybacks yf",
            on_progress=_on_progress,
        )
        _buybacks_cache.clear()
        _registry_set_meta({"tse_csv_mtime": ""})
        fetch_buyback_stocks(force_yf=False, force_registry=True)
        with _yf_rebuild_lock:
            _yf_rebuild_state.update({
                "running": False,
                "done": total,
                "fetched": len(fetched),
                "finished_at": datetime.now().isoformat(),
                "message": f"Complete — {len(fetched)} symbols fetched",
            })
        logger.info("=== Buybacks YF rebuild finished (%d fetched) ===", len(fetched))
    except Exception as ex:
        with _yf_rebuild_lock:
            _yf_rebuild_state.update({
                "running": False,
                "error": str(ex),
                "finished_at": datetime.now().isoformat(),
                "message": f"Failed: {ex}",
            })
        raise


def _load_repurchase_history_for_symbol(
    yf_symbol: str,
    years_back: int = 2,
    limit: int = 200,
) -> list:
    """Repurchase events for detail modal chart (date, shares, price, source)."""
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return []

    cutoff = datetime.now().date() - timedelta(days=365 * years_back)
    events = []

    if sym.endswith(".T"):
        df = load_tse_events()
        if not df.empty:
            sub = df[df["yf_symbol"] == sym].copy()
            sub = sub[sub["Implementation Date"].dt.date >= cutoff]
            for _, r in sub.sort_values("Implementation Date").iterrows():
                impl = r["Implementation Date"]
                shares = r.get("No. of Shares to be Purchased")
                events.append({
                    "date": impl.strftime("%Y-%m-%d") if pd.notna(impl) else "",
                    "shares": int(shares) if pd.notna(shares) else None,
                    "price": _parse_csv_price(r.get("Price")),
                    "source": "TSE",
                })

    elif sym.endswith(".HK"):
        df = load_hk_events()
        if not df.empty:
            sub = df[df["yf_symbol"] == sym].copy()
            sub = sub[sub["Trading Date"].dt.date >= cutoff]
            for _, r in sub.sort_values("Trading Date").iterrows():
                impl = r["Trading Date"]
                shares = r.get("No. of Shares to be Purchased")
                events.append({
                    "date": impl.strftime("%Y-%m-%d") if pd.notna(impl) else "",
                    "shares": int(shares) if pd.notna(shares) else None,
                    "price": _safe_float(r.get("Highest Repurchase Price")),
                    "source": "HKEX",
                })

    elif sym.endswith(".AX"):
        for ev in load_asx_buyback_events():
            if ev.get("yf_symbol") != sym:
                continue
            d = ev.get("date") or ""
            if not d or d < cutoff.isoformat():
                continue
            events.append({
                "date": d,
                "shares": ev.get("shares_purchased"),
                "price": _safe_float(ev.get("price")),
                "source": "ASX",
            })

    else:
        from data.edgar_metrics import is_us_edgar_eligible
        if is_us_edgar_eligible(sym):
            try:
                from data.us_buybacks import load_us_buyback_repurchases
                us_rows = load_us_buyback_repurchases()
            except Exception:
                us_rows = []
            if not us_rows:
                us_rows = [
                    ev for ev in load_us_buyback_events()
                    if ev.get("yf_symbol") == sym and (ev.get("shares") or ev.get("price"))
                ]
            for ev in us_rows:
                if ev.get("yf_symbol") != sym:
                    continue
                d = ev.get("date") or ev.get("period_end") or ""
                if not d or d < cutoff.isoformat():
                    continue
                events.append({
                    "date": d,
                    "shares": ev.get("shares") or ev.get("shares_purchased"),
                    "price": _safe_float(ev.get("price")),
                    "source": "SEC",
                })

    events = [e for e in events if e.get("date")]
    events.sort(key=lambda e: e["date"])
    if len(events) > limit:
        events = events[-limit:]
    return events


def _load_insider_purchases_for_symbol(
    yf_symbol: str,
    years_back: int = 2,
    limit: int = 200,
) -> list:
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return []

    cutoff = datetime.now().date() - timedelta(days=365 * years_back)
    events = []

    if sym.endswith(".HK"):
        df = load_hk_insider_events()
        if not df.empty:
            sub = df[df["yf_symbol"] == sym].copy()
            sub = sub[sub["Event Date"].dt.date >= cutoff]
            for _, r in sub.sort_values("Event Date").iterrows():
                ev = r["Event Date"]
                shares = r.get("Shares")
                events.append({
                    "date": ev.strftime("%Y-%m-%d") if pd.notna(ev) else "",
                    "shares": int(shares) if pd.notna(shares) else None,
                    "price": _safe_float(r.get("Average price")),
                    "source": "HKEX-DI",
                    "director": r.get("Director") or "",
                    "currency": r.get("Currency") or "HKD",
                })

    elif sym.endswith(".AX"):
        for ev in load_asx_insider_events():
            if ev.get("yf_symbol") != sym:
                continue
            d = ev.get("date") or ""
            if not d or d < cutoff.isoformat():
                continue
            events.append({
                "date": d,
                "shares": ev.get("shares"),
                "price": _safe_float(ev.get("price")),
                "source": "ASX",
                "director": ev.get("director") or "",
                "currency": "AUD",
                "headline": ev.get("headline") or "",
                "url": ev.get("url") or "",
            })

    else:
        from data.edgar_metrics import is_us_edgar_eligible
        if is_us_edgar_eligible(sym):
            for ev in load_us_insider_events():
                if ev.get("yf_symbol") != sym:
                    continue
                d = ev.get("date") or ""
                if not d or d < cutoff.isoformat():
                    continue
                events.append({
                    "date": d,
                    "shares": ev.get("shares"),
                    "price": _safe_float(ev.get("price")),
                    "source": "SEC-4",
                    "director": ev.get("director") or ev.get("insider") or "",
                    "currency": "USD",
                    "headline": ev.get("headline") or "",
                    "url": ev.get("url") or "",
                })

    events = [e for e in events if e.get("date")]
    events.sort(key=lambda e: e["date"])
    if len(events) > limit:
        events = events[-limit:]
    return events


_BUYBACK_REGISTRY_FIELDS = (
    "annual_buyback_pct",
    "annual_shares_purchased",
    "buyback_source",
    "buyback_events_year",
    "buyback_events_total",
    "insider_buys_2026",
    "last_insider_date",
    "buyback_date",
    "buyback_announced",
    "last_activity",
    "signals",
    "list_name",
    "us_indices",
)


def _overlay_from_registry_row(registry_row: dict) -> dict:
    overlay = {}
    for key in _BUYBACK_REGISTRY_FIELDS:
        if key == "list_name":
            continue
        val = registry_row.get(key)
        if val is not None and val != "" and val != []:
            overlay[key] = val
    return overlay


def _has_buyback_activity(overlay: dict) -> bool:
    return bool(
        overlay.get("buyback_announced")
        or overlay.get("insider_buys_2026")
        or overlay.get("annual_buyback_pct")
        or overlay.get("buyback_events_total")
    )


def load_buyback_signal_index(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """Ticker -> buyback/insider overlay fields from persistent registry."""
    index = {}
    for row in _load_registry_signals_from_db(db_path=db_path):
        sym = (row.get("ticker") or "").strip().upper()
        if not sym:
            continue
        overlay = _overlay_from_registry_row(row)
        if _has_buyback_activity(overlay):
            index[sym] = overlay
    return index


def enrich_stocks_with_buyback_signals(
    stocks: list,
    *,
    filter_only: bool = False,
    index: dict = None,
) -> list:
    """Merge buyback registry fields onto business-list rows."""
    lookup = index if index is not None else load_buyback_signal_index()
    out = []
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        overlay = lookup.get(sym)
        if not overlay:
            if not filter_only:
                out.append(stock)
            continue
        merged = dict(stock)
        merged.update(overlay)
        merged["_has_buyback_signals"] = True
        out.append(merged)
    return out


def _normalize_buyback_row_for_business(row: dict, list_id: str, display_name: str) -> dict:
    normalized = dict(row)
    normalized["_market"] = "business"
    normalized["_list_id"] = list_id
    normalized["list_name"] = display_name
    return normalized


def fetch_buyback_stocks_for_business_list(force_yf: bool = False) -> list:
    """Buybacks registry exposed as a built-in business list."""
    from data.business_lists import BUILTIN_BUYBACKS_LIST_ID

    display_name = "Buybacks & insider activity"
    rows = fetch_buyback_stocks(force_yf=force_yf)
    return [
        _normalize_buyback_row_for_business(r, BUILTIN_BUYBACKS_LIST_ID, display_name)
        for r in rows
    ]


def sync_builtin_buybacks_list(
    db_path: str = DEFAULT_UNIVERSE_DB,
    conn=None,
    now: str = None,
) -> dict | None:
    """Register buyback-registry symbols as a built-in business list."""
    from data.business_lists import BUILTIN_BUYBACKS_LIST_ID, _lists_conn, _register_builtin_list

    symbols = sorted({
        (r.get("ticker") or "").strip().upper()
        for r in _load_registry_signals_from_db(db_path=db_path)
        if r.get("ticker")
    })
    if not symbols:
        return None

    own_conn = conn is None
    if own_conn:
        conn = _lists_conn(db_path)
    scanned_at = now or datetime.now().isoformat()
    display_name = "Buybacks & insider activity"
    _register_builtin_list(conn, BUILTIN_BUYBACKS_LIST_ID, display_name, symbols, scanned_at)
    if own_conn:
        conn.commit()
        conn.close()
    return {
        "list_id": BUILTIN_BUYBACKS_LIST_ID,
        "filename": BUILTIN_BUYBACKS_LIST_ID,
        "display_name": display_name,
        "ticker_count": len(symbols),
        "scanned_at": scanned_at,
        "builtin": True,
    }


def merge_buyback_registry_into_stock(stock: dict, sym: str = None) -> dict:
    """Overlay buyback registry + detail history onto a stock dict (modal/detail)."""
    sym = (sym or stock.get("ticker") or "").strip().upper()
    if not sym:
        return stock

    registry = _load_registry_row(sym)
    if not registry:
        return stock

    overlay = _overlay_from_registry_row(registry)
    stock.update(overlay)

    registry_signals = registry.get("signals") or []
    if registry_signals and not (
        len(registry_signals) == 1 and registry_signals[0].get("type") == "note"
    ):
        stock["signals"] = registry_signals

    stock["buyback_repurchases"] = _load_repurchase_history_for_symbol(sym)
    stock["insider_purchases"] = _load_insider_purchases_for_symbol(sym)
    shares_out = _shares_outstanding_from_yf(
        load_yf_cached([sym]).get(sym, {}) if sym else {}
    ) or stock.get("shares_outstanding")
    stock["buyback_quarterly"] = build_quarterly_buyback_breakdown(
        sym,
        shares_outstanding=shares_out,
    )
    stock["_has_buyback_signals"] = True
    return stock


def _fetch_single_buyback(yf_symbol: str):
    sym = (yf_symbol or "").strip().upper()
    if not sym:
        return None

    registry = _load_registry_row(sym)
    if not registry:
        fetch_buyback_stocks(force_yf=False, lite=False)
        registry = _load_registry_row(sym)
    if not registry:
        return None

    row = dict(registry)
    try:
        from data.business_lists import _fetch_single_business
        enriched = _fetch_single_business(sym)
        if enriched:
            row.update(enriched)
    except Exception as e:
        logger.warning("Buyback detail yf fetch failed for %s: %s", sym, e)

    for key in _BUYBACK_REGISTRY_FIELDS:
        val = registry.get(key)
        if val is not None and val != "" and val != []:
            row[key] = val

    registry_signals = registry.get("signals") or []
    if registry_signals and not (
        len(registry_signals) == 1
        and registry_signals[0].get("type") == "note"
    ):
        row["signals"] = registry_signals

    row["buyback_repurchases"] = _load_repurchase_history_for_symbol(sym)
    row["insider_purchases"] = _load_insider_purchases_for_symbol(sym)
    shares_out = _shares_outstanding_from_yf(
        load_yf_cached([sym]).get(sym, {}) if sym else {}
    ) or row.get("shares_outstanding")
    row["buyback_quarterly"] = build_quarterly_buyback_breakdown(
        sym,
        shares_outstanding=shares_out,
    )
    row["_market"] = "buybacks"
    row["_pending_yf"] = False
    row["_live"] = True
    if sym.endswith(".AX"):
        row["name"] = asx_official_name(sym, fallback=row.get("name") or sym)
    return row