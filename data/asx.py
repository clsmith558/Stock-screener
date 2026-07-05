"""
ASX Data Layer - Fully live
- Financial metrics from yfinance
- Signals from asx_announcements scraper
"""

import time
import logging
import sqlite3
import urllib.request
import json
from datetime import datetime, timedelta
from io import StringIO

import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)
# Expected 404s on delisted symbols are handled via asx_inactive_tickers; quiet yfinance noise.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Fallback list (used if universe DB is empty). Replaced at runtime by load_asx_ticker_universe()
# when the periodic CSV update (from ASX directory) has been run at least once.
ASX_MONITORED_TICKERS = [
    "FMG", "BHP", "RIO", "NAB", "WBC", "CSL", "MQG", "WES", "GMG", "TCL",
    "NST", "LYC", "A2M"
]

ASX_UNIVERSE_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
DEFAULT_UNIVERSE_DB = "asx_announcements_cache.db"
UNIVERSE_STALE_DAYS = 180  # ~6 months per user request

YF_CACHE_MAX_AGE_DAYS = 30
YF_FETCH_SLEEP_SECONDS = 2.0  # One call at a time to avoid rate limits. Full ~2000 tickers can take 1-3+ hours. Expect to run fresh ~monthly or every 2 weeks.

_asx_cache = {}
ASX_CACHE_TTL = 600  # 10 minutes


def _safe_float(val, default=0.0):
    """Coerce yfinance info values (sometimes strings) to float."""
    if val is None:
        return default
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s or s.lower() in ("n/a", "na", "none", "-", "inf", "infinity"):
            return default
        try:
            return float(s)
        except ValueError:
            return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_round(val, ndigits=1, default=0):
    f = _safe_float(val, None)
    if f is None:
        return default
    try:
        return int(round(f)) if ndigits == 0 else round(f, ndigits)
    except (TypeError, ValueError):
        return default


def _normalize_div_yield(val) -> float:
    """yfinance dividendYield: decimal (0.06) on US, often already % (6.0) on ASX."""
    v = _safe_float(val)
    if not v:
        return 0
    if v > 0.5:
        return _safe_round(v, 2)
    return _safe_round(v * 100, 2)


def _business_summary(info: dict) -> str:
    info = info or {}
    return (info.get("longBusinessSummary") or info.get("description") or "").strip()


def _build_price_trend(yt, period: str = "2y", interval: str = "1wk") -> list:
    """Weekly/daily closes for interactive price chart in detail modal."""
    price_trend = []
    try:
        hist = yt.history(period=period, interval=interval, auto_adjust=True)
        if hist is not None and not hist.empty:
            for dt, row in hist.iterrows():
                c = row.get("Close") or row.get("Adj Close")
                if c is not None and c == c:
                    price_trend.append({
                        "date": dt.strftime("%Y-%m-%d"),
                        "close": round(float(c), 2),
                    })
    except Exception:
        pass
    return price_trend


def _build_earnings_history(yt) -> list:
    """Quarterly then annual EPS (newest first within each group) from yfinance income statements."""
    quarterly = []
    annual = []
    try:
        qist = getattr(yt, "quarterly_income_stmt", None)
        if qist is not None and not getattr(qist, "empty", True):
            eps_row = None
            for candidate in ["Diluted EPS", "Basic EPS", "Net Income"]:
                if candidate in qist.index:
                    eps_row = candidate
                    break
            if eps_row:
                for period, val in list(qist.loc[eps_row].items())[:6]:
                    try:
                        pstr = str(period)[:7] if hasattr(period, "strftime") else str(period)
                        v = float(val) if val == val else 0
                        quarterly.append({
                            "period": pstr,
                            "eps": round(v, 2) if eps_row.endswith("EPS") else round(v / 1e9, 2),
                            "type": "quarterly",
                            "raw_label": eps_row,
                        })
                    except Exception:
                        continue
    except Exception:
        pass
    try:
        ist = getattr(yt, "income_stmt", None)
        if ist is not None and not getattr(ist, "empty", True):
            eps_row = None
            for candidate in ["Diluted EPS", "Basic EPS", "Net Income"]:
                if candidate in ist.index:
                    eps_row = candidate
                    break
            if eps_row:
                for period, val in list(ist.loc[eps_row].items())[:5]:
                    try:
                        pstr = str(period)[:4] if hasattr(period, "year") else str(period)[:7]
                        v = float(val) if val == val else 0
                        annual.append({
                            "period": pstr,
                            "eps": round(v, 2) if eps_row.endswith("EPS") else round(v / 1e9, 2),
                            "type": "annual",
                            "raw_label": eps_row,
                        })
                    except Exception:
                        continue
    except Exception:
        pass
    return quarterly + annual


_SUFFIX_CURRENCY = {
    ".AX": "AUD", ".T": "JPY", ".HK": "HKD", ".L": "GBP", ".TO": "CAD",
    ".V": "CAD", ".NS": "INR", ".BO": "INR", ".SW": "CHF", ".PA": "EUR",
    ".DE": "EUR", ".MI": "EUR", ".KS": "KRW", ".KQ": "KRW", ".SI": "SGD",
    ".NZ": "NZD", ".SA": "BRL", ".MX": "MXN", ".TW": "TWD",
}


def infer_currency(info: dict = None, ticker: str = "") -> str:
    """Resolve trading/reporting currency from yfinance info or ticker suffix."""
    info = info or {}
    for key in ("currency", "financialCurrency"):
        cur = str(info.get(key) or "").strip().upper()
        if cur and cur not in ("NONE", "NULL"):
            return cur
    sym = (ticker or "").upper()
    for sfx, cur in _SUFFIX_CURRENCY.items():
        if sym.endswith(sfx):
            return cur
    return "USD"


def _net_assets_raw(info: dict, mcap: int = 0) -> float:
    """Shareholders' equity (net assets) in raw currency units."""
    info = info or {}
    for key in ("totalStockholderEquity", "totalEquityGrossMinorityInterest"):
        val = _safe_float(info.get(key))
        if val > 0:
            return val
    bv = _safe_float(info.get("bookValue"))
    shares = _safe_float(info.get("sharesOutstanding")) or _safe_float(info.get("impliedSharesOutstanding"))
    if bv > 0 and shares > 0:
        return bv * shares
    pb = _safe_float(info.get("priceToBook"))
    if mcap > 0 and pb > 0:
        return mcap / pb
    return 0.0


def _net_assets_vs_mcap_metrics(net_assets: float, mcap: int) -> dict:
    """Net assets vs market cap — positive spread = equity exceeds market value."""
    if not net_assets or not mcap:
        return {
            "net_assets_m": 0,
            "net_assets_vs_mcap_m": 0,
            "net_assets_vs_mcap_pct": 0.0,
        }
    net_assets_m = _safe_round(net_assets / 1_000_000, 0)
    spread = net_assets - mcap
    return {
        "net_assets_m": net_assets_m,
        "net_assets_vs_mcap_m": _safe_round(spread / 1_000_000, 0),
        "net_assets_vs_mcap_pct": _safe_round(spread / mcap * 100, 2),
    }


def _metrics_from_yf_info(info: dict, price: float, ma_200w=0, pct_from_200w_ma=0, p_fcf=0, ticker: str = "") -> dict:
    """Build metrics dict from yfinance .info, tolerant of string/null fields."""
    info = info or {}
    pe = _safe_float(info.get("trailingPE")) or _safe_float(info.get("forwardPE"))
    pb = _safe_float(info.get("priceToBook"))
    cash = _safe_float(info.get("totalCash")) / 1_000_000
    debt = _safe_float(info.get("totalDebt")) / 1_000_000
    net_cash_m = cash - debt
    dte = _safe_float(info.get("debtToEquity"))
    roe = _safe_float(info.get("returnOnEquity"))
    held_insiders = _safe_float(info.get("heldPercentInsiders"))
    insider_ownership_pct = _safe_round(held_insiders * 100, 2) if held_insiders else 0
    mcap = int(_safe_float(info.get("marketCap")))
    net_cash_pct_mcap = 0.0
    if mcap > 0:
        net_cash_pct_mcap = _safe_round((net_cash_m * 1_000_000) / mcap * 100, 2)
    na_metrics = _net_assets_vs_mcap_metrics(_net_assets_raw(info, mcap), mcap)
    currency = infer_currency(info, ticker)
    return {
        "pe": _safe_round(pe, 1),
        "forward_pe": _safe_round(info.get("forwardPE"), 1),
        "pb": _safe_round(pb, 2),
        "p_fcf": p_fcf,
        "pcf": 0,
        "ev_ebitda": _safe_round(info.get("enterpriseToEbitda"), 1),
        "debt_to_equity": _safe_round(dte / 100, 2) if dte else 0,
        "cash_on_hand_m": _safe_round(cash, 0),
        "net_cash_m": _safe_round(net_cash_m, 0),
        "net_cash_pct_mcap": net_cash_pct_mcap,
        "net_assets_m": na_metrics["net_assets_m"],
        "net_assets_vs_mcap_m": na_metrics["net_assets_vs_mcap_m"],
        "net_assets_vs_mcap_pct": na_metrics["net_assets_vs_mcap_pct"],
        "currency": currency,
        "roe": _safe_round(roe * 100, 1) if roe else 0,
        "insider_ownership_pct": insider_ownership_pct,
        "roic": 0,
        "fcf_yield": 0,
        "div_yield": _normalize_div_yield(info.get("dividendYield")),
        "market_cap": mcap,
        "ma_200w": ma_200w,
        "pct_from_200w_ma": pct_from_200w_ma,
    }


def _calc_ma200w_gap(yt, price=None):
    """Compute 200-week SMA and % gap from current price. Returns (ma_200w, pct_from_200w_ma)."""
    if price is not None and _safe_float(price) <= 0:
        return 0, 0
    try:
        hist = yt.history(period="max", interval="1wk", auto_adjust=True)
        if hist is None or hist.empty:
            return 0, 0
        closes = hist["Close"].dropna()
        if len(closes) < 10:
            return 0, 0
        window = closes.tail(min(200, len(closes)))
        ma = float(window.mean())
        cur = float(price) if price else float(closes.iloc[-1])
        if ma > 0 and cur > 0:
            return round(ma, 2), round(((cur - ma) / ma) * 100, 1)
    except Exception:
        pass
    return 0, 0


# ---------------------- UNIVERSE (CSV) MANAGEMENT ----------------------

def _get_universe_conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asx_universe (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            gics TEXT,
            as_of TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asx_universe_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asx_yf_cache (
            ticker TEXT PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS asx_inactive_tickers (
            ticker TEXT PRIMARY KEY,
            reason TEXT,
            marked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------- INACTIVE / DELISTED TICKER SKIP LIST ----------------------
# Tickers with no live Yahoo quote (delisted, suspended, renamed) are persisted here
# so we never waste yfinance calls on them again.
# "cached_zero_price" is tentative (may be a transient fetch error); confirmed reasons are permanent skips.

TENTATIVE_INACTIVE_REASONS = frozenset({"cached_zero_price"})


def load_inactive_tickers(db_path: str = DEFAULT_UNIVERSE_DB, confirmed_only: bool = False) -> set:
    """Return set of base tickers (e.g. 'ABC') on the inactive skip list."""
    try:
        conn = _get_universe_conn(db_path)
        if confirmed_only:
            placeholders = ",".join("?" for _ in TENTATIVE_INACTIVE_REASONS)
            cur = conn.execute(
                f"SELECT ticker FROM asx_inactive_tickers WHERE reason NOT IN ({placeholders})",
                tuple(TENTATIVE_INACTIVE_REASONS),
            )
        else:
            cur = conn.execute("SELECT ticker FROM asx_inactive_tickers")
        out = {row[0] for row in cur.fetchall()}
        conn.close()
        return out
    except Exception as e:
        logger.warning(f"Failed loading inactive ticker list: {e}")
        return set()


def remove_inactive_ticker(ticker: str, db_path: str = DEFAULT_UNIVERSE_DB):
    """Remove a ticker from the inactive skip list (e.g. after a successful live quote)."""
    t = (ticker or "").upper().replace(".AX", "")
    if not t:
        return
    try:
        conn = _get_universe_conn(db_path)
        conn.execute("DELETE FROM asx_inactive_tickers WHERE ticker=?", (t,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed removing {t} from inactive list: {e}")


def mark_ticker_inactive(ticker: str, reason: str, source: str = "yf_fetch", db_path: str = DEFAULT_UNIVERSE_DB):
    """Add a ticker to the inactive skip list (idempotent)."""
    t = (ticker or "").upper().replace(".AX", "")
    if not t:
        return
    try:
        conn = _get_universe_conn(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO asx_inactive_tickers (ticker, reason, marked_at, source) VALUES (?,?,?,?)",
            (t, reason or "unknown", datetime.now().isoformat(), source)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed marking {t} inactive: {e}")


def filter_active_tickers(tickers: list, db_path: str = DEFAULT_UNIVERSE_DB, confirmed_only: bool = False) -> list:
    """Remove inactive/delisted tickers from a list before any yfinance work."""
    inactive = load_inactive_tickers(db_path, confirmed_only=confirmed_only)
    if not inactive:
        return list(tickers)
    active = [t for t in tickers if t not in inactive]
    skipped = len(tickers) - len(active)
    if skipped:
        label = "confirmed inactive/delisted" if confirmed_only else "inactive/delisted"
        logger.info(f"Skipping {skipped} {label} tickers (on persistent skip list)")
    return active


def sync_inactive_from_cache(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    """Promote zero-price yf cache rows to the inactive skip list (one-time backfill + ongoing)."""
    try:
        conn = _get_universe_conn(db_path)
        cur = conn.execute("""
            SELECT c.ticker FROM asx_yf_cache c
            LEFT JOIN asx_inactive_tickers i ON c.ticker = i.ticker
            WHERE (c.price IS NULL OR c.price <= 0) AND i.ticker IS NULL
        """)
        to_mark = [row[0] for row in cur.fetchall()]
        conn.close()
        for t in to_mark:
            mark_ticker_inactive(t, "cached_zero_price", source="cache_scan", db_path=db_path)
        if to_mark:
            logger.info(f"Added {len(to_mark)} zero-price cached tickers to inactive skip list")
        return len(to_mark)
    except Exception as e:
        logger.warning(f"sync_inactive_from_cache failed: {e}")
        return 0


def _detect_yf_inactive(info: dict, price: float) -> str:
    """
    Return a reason string if Yahoo data indicates this ticker is inactive/delisted.
    Uses .info only — never calls history (avoids duplicate 404 log spam).
    """
    info = info or {}
    price = _safe_float(price)
    if price > 0:
        return ""

    has_name = bool(info.get("shortName") or info.get("longName"))
    has_symbol = bool(info.get("symbol") or info.get("underlyingSymbol"))
    rm = _safe_float(info.get("regularMarketPrice"), None)
    cp = _safe_float(info.get("currentPrice"), None)
    if (rm and rm > 0) or (cp and cp > 0):
        return ""

    if not info or len(info) <= 3:
        return "quote_not_found"
    if not has_name and not has_symbol:
        return "quote_not_found"
    return "zero_price"


def _inactive_from_yf_error(msg: str) -> str:
    """Map yfinance exception text to inactive reason, or '' if likely transient."""
    m = (msg or "").lower()
    if any(x in m for x in ("rate", "too many", "429", "timed out", "timeout", "connection", "__round__")):
        return ""
    if any(x in m for x in ("404", "not found", "no data", "delisted", "invalid", "possibly delisted", "quote not found")):
        return "quote_not_found"
    return ""


def _set_meta(conn, key: str, value: str):
    conn.execute("INSERT OR REPLACE INTO asx_universe_meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _get_meta(conn, key: str, default=None):
    cur = conn.execute("SELECT value FROM asx_universe_meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def load_asx_ticker_universe(db_path: str = DEFAULT_UNIVERSE_DB) -> list:
    """Return list of base ASX codes (e.g. ['BHP','FMG']) from the persisted CSV-derived universe."""
    try:
        conn = _get_universe_conn(db_path)
        cur = conn.execute("SELECT ticker FROM asx_universe ORDER BY ticker")
        tickers = [row[0] for row in cur.fetchall()]
        conn.close()
        return tickers
    except Exception as e:
        logger.warning(f"Failed loading ASX universe from DB: {e}")
        return []


def load_asx_universe_details(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """ticker -> {'name': str, 'gics': str, 'as_of': str} for name/GICS enrichment."""
    try:
        conn = _get_universe_conn(db_path)
        cur = conn.execute("SELECT ticker, name, gics, as_of FROM asx_universe")
        details = {row[0]: {"name": row[1], "gics": row[2], "as_of": row[3]} for row in cur.fetchall()}
        conn.close()
        return details
    except Exception:
        return {}


def asx_official_name(yf_symbol: str, univ_details: dict = None, fallback: str = "") -> str:
    """Prefer ASX CSV company name for .AX symbols (e.g. TLS.AX → TELSTRA GROUP LIMITED)."""
    sym = (yf_symbol or "").strip().upper()
    if not sym.endswith(".AX"):
        return fallback or sym
    base = sym.replace(".AX", "")
    details = univ_details if univ_details is not None else load_asx_universe_details()
    return details.get(base, {}).get("name") or fallback or sym


def update_asx_company_list(force: bool = False, db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    """
    Download 'All ASX listed companies' CSV from the ASX directory page (direct file link)
    and persist into SQLite (shared with announcements cache).
    Safe to call ~once every 6 months. Returns status dict with count, as_of, etc.
    """
    conn = _get_universe_conn(db_path)
    last_update = _get_meta(conn, "universe_last_update")
    as_of_prev = _get_meta(conn, "universe_as_of")

    if not force and last_update:
        try:
            last_dt = datetime.fromisoformat(last_update)
            if datetime.now() - last_dt < timedelta(days=UNIVERSE_STALE_DAYS):
                count = conn.execute("SELECT COUNT(*) FROM asx_universe").fetchone()[0]
                conn.close()
                return {
                    "updated": False,
                    "reason": "not_stale",
                    "count": count,
                    "last_update": last_update,
                    "as_of": as_of_prev,
                    "source": ASX_UNIVERSE_URL
                }
        except Exception:
            pass  # fall through to force update on bad date

    logger.info("Updating ASX company universe from official CSV...")
    try:
        req = urllib.request.Request(
            ASX_UNIVERSE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SignalScreener/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8", errors="ignore")

        # The file starts with a title line, blank, then CSV header.
        # pd.read_csv(skiprows=2) makes the real header the column names.
        df = pd.read_csv(StringIO(content), skiprows=2)
        # Normalize
        cols = {c.strip().lower(): c for c in df.columns}
        code_col = cols.get("asx code") or list(df.columns)[1]
        name_col = cols.get("company name") or list(df.columns)[0]
        gics_col = cols.get("gics industry group") or (list(df.columns)[2] if len(df.columns) > 2 else None)

        # Extract as_of timestamp from title line if present
        as_of = None
        first = content.splitlines()[0] if content else ""
        if "as at" in first.lower():
            as_of = first.split("as at", 1)[1].strip()

        df = df.dropna(subset=[code_col])
        now_iso = datetime.now().isoformat()
        rows = []
        for _, r in df.iterrows():
            code = str(r[code_col]).strip().upper().replace(".AX", "")
            if not code or len(code) > 10 or any(ch in code for ch in " ./"):
                continue
            name = str(r.get(name_col, "")).strip().strip('"\'')
            gics = str(r.get(gics_col, "") if gics_col else "").strip()
            rows.append((code, name, gics, as_of, now_iso))

        # Replace strategy (simple + correct for periodic full refresh)
        conn.execute("DELETE FROM asx_universe")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO asx_universe (ticker, name, gics, as_of, updated_at) VALUES (?,?,?,?,?)",
                rows
            )
        _set_meta(conn, "universe_last_update", now_iso)
        _set_meta(conn, "universe_as_of", as_of or now_iso)
        _set_meta(conn, "universe_count", str(len(rows)))
        # Drop inactive entries for tickers no longer in the official CSV
        pruned = conn.execute("""
            DELETE FROM asx_inactive_tickers
            WHERE ticker NOT IN (SELECT ticker FROM asx_universe)
        """).rowcount
        conn.commit()
        count = len(rows)
        conn.close()

        if pruned:
            logger.info(f"Pruned {pruned} inactive entries for tickers removed from ASX CSV")
        logger.info(f"ASX universe updated successfully: {count} companies (as of {as_of})")
        return {
            "updated": True,
            "count": count,
            "as_of": as_of,
            "last_update": now_iso,
            "source": ASX_UNIVERSE_URL
        }
    except Exception as e:
        logger.error(f"ASX universe update failed: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return {"updated": False, "error": str(e), "source": ASX_UNIVERSE_URL}


def get_asx_meta() -> dict:
    """Return current data source + scrape times + universe metadata for UI badges."""
    u_count = 0
    u_as_of = None
    u_updated = None
    yf_count = 0
    yf_oldest = None
    yf_newest = None
    inactive_count = 0
    try:
        u_tickers = load_asx_ticker_universe()
        u_count = len(u_tickers)
        conn = _get_universe_conn()
        u_as_of = _get_meta(conn, "universe_as_of")
        u_updated = _get_meta(conn, "universe_last_update")

        cur = conn.execute("SELECT COUNT(*), MIN(last_updated), MAX(last_updated) FROM asx_yf_cache")
        row = cur.fetchone()
        if row:
            yf_count = row[0] or 0
            yf_oldest = row[1]
            yf_newest = row[2]
        inactive_count = conn.execute("SELECT COUNT(*) FROM asx_inactive_tickers").fetchone()[0] or 0
        conn.close()
    except Exception:
        u_count = len(ASX_MONITORED_TICKERS)

    meta = {
        "data_source": "yfinance + ASX Announcements (Markit Digital, market-wide)",
        "last_scraped": _asx_cache.get("last_scraped") or datetime.now().isoformat(),
        "universe_count": u_count,
        "universe_as_of": u_as_of,
        "universe_last_update": u_updated,
        "yf_cached_count": yf_count,
        "yf_oldest": yf_oldest,
        "yf_newest": yf_newest,
        "inactive_count": inactive_count,
    }
    return meta


# ---------------------- PERSISTENT YFINANCE CACHE (rate-limited, long-lived) ----------------------
# Fresh calls to yfinance are expensive and rate-limited. We persist per-ticker data in the
# shared DB and only refetch stale/missing entries (default 30 days). Full universe sequential
# fetch is acceptable (hours) because it only happens ~monthly.

def _is_yf_fresh(last_updated: str, max_days: int = None) -> bool:
    if not last_updated:
        return False
    if max_days is None:
        max_days = YF_CACHE_MAX_AGE_DAYS
    try:
        dt = datetime.fromisoformat(last_updated)
        return (datetime.now() - dt).days < max_days
    except Exception:
        return False


def load_yf_cached_data(tickers: list, max_days: int = None) -> dict:
    """Return {base_ticker: {price, low_52w, high_52w, name, sector, metrics, p_fcf}} for fresh entries only."""
    if not tickers:
        return {}
    if max_days is None:
        max_days = YF_CACHE_MAX_AGE_DAYS
    conn = _get_universe_conn()
    placeholders = ",".join("?" for _ in tickers)
    cur = conn.execute(
        f"""SELECT ticker, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf
            FROM asx_yf_cache
            WHERE ticker IN ({placeholders})""",
        tickers
    )
    out = {}
    for row in cur.fetchall():
        t, last_up, price, low, high, name, sector, mjson, pf = row
        if _is_yf_fresh(last_up, max_days):
            try:
                metrics = json.loads(mjson) if mjson else {}
            except Exception:
                metrics = {}
            out[t] = {
                "price": price or 0,
                "low_52w": low or 0,
                "high_52w": high or 0,
                "name": name or t,
                "sector": sector or "Unknown",
                "metrics": metrics,
                "p_fcf": pf or 0
            }
    conn.close()
    return out


def save_yf_data(ticker: str, data: dict):
    """Persist one ticker's yf snapshot."""
    conn = _get_universe_conn()
    metrics_json = json.dumps(data.get("metrics") or {})
    conn.execute("""
        INSERT OR REPLACE INTO asx_yf_cache
        (ticker, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        ticker,
        datetime.now().isoformat(),
        data.get("price") or 0,
        data.get("low_52w") or 0,
        data.get("high_52w") or 0,
        data.get("name") or ticker,
        data.get("sector") or "Unknown",
        metrics_json,
        data.get("p_fcf") or 0
    ))
    conn.commit()
    conn.close()


def _fetch_yf_data_sequentially(
    base_tickers: list,
    sleep_seconds: float = None,
    progress_every: int = 50,
    retry_tentative_inactive: bool = False,
) -> dict:
    """
    VERY conservative sequential fetch. One Ticker() at a time + long sleep.
    Returns {base_ticker: data_dict} and saves each to persistent cache immediately.
    Intended for initial population or forced monthly rebuild. Can take hours for full list.
    """
    if sleep_seconds is None:
        sleep_seconds = YF_FETCH_SLEEP_SECONDS
    sync_inactive_from_cache()
    base_tickers = filter_active_tickers(
        base_tickers,
        confirmed_only=retry_tentative_inactive,
    )
    inactive_skip = load_inactive_tickers(confirmed_only=retry_tentative_inactive)
    out = {}
    total = len(base_tickers)
    if total == 0:
        logger.info("Sequential yfinance fetch: no active tickers to fetch (all inactive or empty).")
        return out
    logger.info(f"Sequential yfinance fetch starting for {total} active tickers (sleep={sleep_seconds}s per call)...")
    for i, t in enumerate(base_tickers):
        if t in inactive_skip:
            continue
        if i and i % progress_every == 0:
            logger.info(f"  yf sequential progress: {i}/{total}")
        try:
            yt = yf.Ticker(f"{t}.AX")
            info = yt.info or {}
            price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice"))

            if price <= 0:
                reason = _detect_yf_inactive(info, price) or "quote_not_found"
                mark_ticker_inactive(t, reason, source="yf_fetch")
                inactive_skip.add(t)
                logger.info(f"  {t}: inactive ({reason}) — added to skip list, no future yf calls")
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
            metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=t)

            data = {
                "price": _safe_round(price, 2),
                "low_52w": _safe_round(low, 2),
                "high_52w": _safe_round(high, 2),
                "name": info.get("shortName") or info.get("longName") or t,
                "sector": info.get("sector") or "Unknown",
                "metrics": metrics,
                "p_fcf": p_fcf
            }
            out[t] = data
            save_yf_data(t, data)
            remove_inactive_ticker(t)
            inactive_skip.discard(t)
            time.sleep(sleep_seconds)
        except Exception as e:
            msg = str(e)
            logger.warning(f"  yf sequential fetch error for {t}: {msg[:120]}")
            inactive_reason = _inactive_from_yf_error(msg)
            if inactive_reason:
                mark_ticker_inactive(t, inactive_reason, source="yf_error")
                inactive_skip.add(t)
                logger.info(f"  {t}: inactive ({inactive_reason}) from yf error — added to skip list")
            else:
                logger.info(f"  {t}: transient error, will retry on next stale cycle (not marking inactive)")
            # Be extra nice on rate limits / errors
            sleep = 30 if 'Rate' in msg or 'Too Many' in msg or '429' in msg else max(1.0, sleep_seconds / 2)
            time.sleep(sleep)
    logger.info(f"Sequential yf fetch complete for {len(out)} / {total} tickers.")
    return out


def rebuild_asx_yf_cache(sleep_seconds: float = None):
    """Manual / CLI entrypoint for full yf cache rebuild. Run this ~every 2-4 weeks.
    Example: python -c "from data.asx import rebuild_asx_yf_cache; rebuild_asx_yf_cache()"
    """
    sync_inactive_from_cache()
    tickers = filter_active_tickers(
        load_asx_ticker_universe() or ASX_MONITORED_TICKERS,
        confirmed_only=True,
    )
    logger.info(f"=== REBUILD ASX YF CACHE for {len(tickers)} active tickers (this will take hours) ===")
    _fetch_yf_data_sequentially(tickers, sleep_seconds=sleep_seconds, retry_tentative_inactive=True)
    logger.info("=== YF cache rebuild finished ===")


# ---------------------- MAIN FETCH ----------------------

def _batch_price_and_low(base_tickers, chunk_size: int = 150):
    """
    Efficiently fetch recent prices + 52-week lows for many tickers using yf.download in chunks.
    Returns {base_ticker: {"price": float, "low_52w": float}}
    Much faster than one Ticker() per symbol for the quote + history part.
    """
    out = {}
    if not base_tickers:
        return out
    for i in range(0, len(base_tickers), chunk_size):
        chunk = base_tickers[i : i + chunk_size]
        syms = [f"{t}.AX" for t in chunk]
        try:
            df = yf.download(
                tickers=syms,
                period="1y",
                interval="1d",
                progress=False,
                threads=True,
                timeout=90,
            )
            for t in chunk:
                sym = f"{t}.AX"
                try:
                    sub = None
                    if isinstance(getattr(df, "columns", None), pd.MultiIndex):
                        lvl0 = df.columns.get_level_values(0)
                        if sym in lvl0:
                            sub = df[sym]
                    else:
                        sub = df
                    if sub is not None and not sub.empty:
                        # yfinance may return Close or Adj Close
                        closes = sub.get("Close") if "Close" in sub.columns else sub.get("Adj Close")
                        lows = sub.get("Low")
                        if closes is not None:
                            closes = closes.dropna()
                            if len(closes) > 0:
                                price = float(closes.iloc[-1])
                                low = float(lows.dropna().min()) if (lows is not None and len(lows.dropna()) > 0) else price
                                out[t] = {"price": round(price, 2), "low_52w": round(low, 2)}
                except Exception:
                    continue
            time.sleep(0.8)  # polite between chunks
        except Exception as e:
            logger.warning(f"Batch price chunk {i}-{i+len(chunk)} failed: {e}")
            # Fallback per ticker for this chunk (slower but keeps data)
            for t in chunk:
                try:
                    yt = yf.Ticker(f"{t}.AX")
                    info = yt.info or {}
                    p = info.get("currentPrice") or info.get("regularMarketPrice") or 0
                    l = info.get("fiftyTwoWeekLow") or 0
                    if p:
                        out[t] = {"price": round(p, 2), "low_52w": round(l, 2)}
                    time.sleep(0.6)
                except Exception:
                    pass
    return out


def fetch_asx_stocks(full: bool = False, force_yf: bool = False):
    """
    Live ASX data (with heavy persistent caching for yfinance to avoid rate limits).
    - If full=False (default): only tickers with signals (the buyback/insider subset).
    - If full=True: full current ASX listed universe from CSV, sorted by gap to 52w low (lowest first).
    - yfinance data comes from SQLite cache (default 30 days old). Only stale/missing are fetched,
      and *only sequentially, one ticker at a time, with long sleeps* (can take hours for first full
      population or forced rebuild -- user expects this ~monthly).
    - force_yf=True ignores age and refetches the needed tickers slowly (for the "rebuild" button).
    - Signals (scraper) are separate and can be refreshed more often.
    """
    cache_key = "full_data" if full else "data"
    now = time.time()
    if cache_key in _asx_cache and now - _asx_cache.get("ts", 0) < ASX_CACHE_TTL and not force_yf:
        cached = _asx_cache[cache_key]
        if full:
            return sorted(list(cached), key=lambda s: (s.get("pct_from_52w_low") or 9999))
        return list(cached)

    results = []
    scraper_available = False
    signals_by_ticker = {}

    # 1. Market-wide signals (works for both full list and signals-only)
    base_universe = load_asx_ticker_universe() or ASX_MONITORED_TICKERS
    try:
        from asx_announcements import get_relevant_market_signals
        # For full list we can skip expensive PDF to keep initial load reasonable; signals are still useful as tags.
        use_pdf_for_signals = not full
        signals_by_ticker = get_relevant_market_signals(
            days=365,
            use_pdf=use_pdf_for_signals,
            use_cache=True
        )
        scraper_available = True
        univ_set = set(base_universe)
        if univ_set:
            signals_by_ticker = {k: v for k, v in signals_by_ticker.items() if k in univ_set}
        logger.info(f"Market-wide signals: {len(signals_by_ticker)} qualifying tickers (full={full})")
    except Exception as e:
        logger.warning(f"Market-wide signals failed: {e}")

    # 2. Determine target tickers
    if full:
        tickers = list(base_universe)
    else:
        tickers = list(signals_by_ticker.keys()) or base_universe

    # Promote known dead symbols from cache, then exclude inactive from all yf work
    sync_inactive_from_cache()
    tickers = filter_active_tickers(tickers)

    univ_details = load_asx_universe_details()

    # 3. Use persistent long-lived yf cache (30 days default). Only hit yfinance (sequentially, slowly)
    # for tickers whose data is missing or stale. This means after the initial slow population,
    # normal loads (even full list) do ZERO yf calls unless something is >30 days old.
    # force_yf=True forces refetch of needed ones (for monthly rebuild).
    max_yf_age = 0 if force_yf else YF_CACHE_MAX_AGE_DAYS

    cached_yf = load_yf_cached_data(tickers, max_days=max_yf_age)
    stale = [t for t in tickers if t not in cached_yf]

    if stale:
        mode = "FORCED REBUILD" if force_yf else "normal (stale/missing)"
        logger.info(f"[{mode}] Fetching fresh yf data *sequentially* for {len(stale)} tickers (sleep ~{YF_FETCH_SLEEP_SECONDS}s; expect 1-3h for full universe).")
        newly = _fetch_yf_data_sequentially(
            stale,
            sleep_seconds=YF_FETCH_SLEEP_SECONDS,
            progress_every=25,
            retry_tentative_inactive=force_yf,
        )
        cached_yf.update(newly)
    else:
        logger.info(f"All {len(tickers)} tickers served from persistent yf cache (age < {YF_CACHE_MAX_AGE_DAYS} days). No yfinance calls this run.")

    for ticker in tickers:
        try:
            ydat = cached_yf.get(ticker, {})
            price = ydat.get("price", 0)
            low = ydat.get("low_52w", 0)
            high = ydat.get("high_52w", 0)

            # Use cached metrics if present (avoids any live .info)
            m = ydat.get("metrics", {}) or {}
            p_fcf = ydat.get("p_fcf", m.get("p_fcf", 0) or 0)

            pct_from_low = 0.0
            if price and low and low > 0:
                pct_from_low = round(((price - low) / low) * 100, 1)

            real_signals = signals_by_ticker.get(ticker, [])
            insider_count = len([s for s in real_signals if s.get("type") == "insider"])
            has_buyback = any(s.get("type") == "buyback" for s in real_signals)

            formatted_signals = []
            for sig in real_signals[:6]:
                formatted_signals.append({
                    "date": sig.get("date"),
                    "type": sig.get("type"),
                    "desc": sig.get("headline", "")[:120]
                })

            if not formatted_signals:
                formatted_signals = [{
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "type": "note",
                    "desc": "No recent relevant announcements found"
                }]

            # Prefer authoritative name/GICS from the official ASX CSV universe
            detail = univ_details.get(ticker, {})
            csv_name = detail.get("name")
            csv_gics = detail.get("gics")
            y_name = ydat.get("name")
            y_sector = ydat.get("sector")

            stock = {
                "ticker": f"{ticker}.AX",
                "name": csv_name or y_name or ticker,
                "sector": csv_gics or y_sector or "Unknown",
                "insider_buys_2026": insider_count,
                "buyback_announced": has_buyback,
                "buyback_date": None,
                "pct_from_52w_low": pct_from_low,
                "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
                "last_activity": real_signals[0]["date"] if real_signals else datetime.now().strftime("%Y-%m-%d"),
                "current_price": round(price, 2) if price else 0,
                "low_52w": round(low, 2) if low else 0,
                "high_52w": round(high, 2) if high else 0,
                "market_cap": m.get("market_cap", 0),
                "currency": m.get("currency") or infer_currency(ticker=f"{ticker}.AX"),
                "net_cash_pct_mcap": m.get("net_cash_pct_mcap", 0),
                "pe": m.get("pe", 0),
                "pb": m.get("pb", 0),
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
                    "currency": m.get("currency") or infer_currency(ticker=f"{ticker}.AX"),
                    "roe": m.get("roe", 0),
                    "roic": m.get("roic", 0),
                    "fcf_yield": m.get("fcf_yield", 0),
                    "div_yield": m.get("div_yield", 0),
                    "market_cap": m.get("market_cap", 0),
                    "ma_200w": m.get("ma_200w", 0),
                    "pct_from_200w_ma": m.get("pct_from_200w_ma", 0),
                },
                # top-level shortcuts for table columns (full list view)
                "p_fcf": p_fcf,
                "signals": formatted_signals,
                "_market": "asx",
                "_live": scraper_available
            }
            results.append(stock)

        except Exception as e:
            results.append({
                "ticker": f"{ticker}.AX",
                "name": ticker,
                "sector": "Unknown",
                "insider_buys_2026": 0,
                "buyback_announced": False,
                "buyback_date": None,
                "pct_from_52w_low": 0,
                "last_activity": datetime.now().strftime("%Y-%m-%d"),
                "current_price": 0,
                "low_52w": 0,
                "high_52w": 0,
                "metrics": {k: 0 for k in ["pe","forward_pe","pb","p_fcf","pcf","ev_ebitda","debt_to_equity","cash_on_hand_m","net_cash_m","roe","roic","fcf_yield","div_yield"]},
                "p_fcf": 0,
                "signals": [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "Data temporarily unavailable"}],
                "_market": "asx",
                "_live": False
            })

    # For full list: sort by gap to 52w low, lowest first (biggest relative discounts / closest to lows at top)
    if full:
        # Drop rows that have no usable price data (some tickers in the official CSV are delisted/suspended)
        before = len(results)
        results = [r for r in results if (r.get("current_price") or 0) > 0]
        if len(results) != before:
            logger.info(f"Full list: filtered {before - len(results)} tickers with no price data (delisted etc)")

        results.sort(key=lambda s: (s.get("pct_from_52w_low") or 9999))

        # Post-pass: enrich full metrics (PE/PB/P/FCF/...) for the top N by gap (user sees these first)
        # plus any that have signals (even if farther from low). This gives "key metrics alongside"
        # without paying the cost for all 1979 on cold start.
        TOP_ENRICH = 60
        to_enrich = set()
        for i, r in enumerate(results[:TOP_ENRICH]):
            to_enrich.add(r["ticker"].replace(".AX", ""))
        for r in results:
            if (r.get("insider_buys_2026", 0) > 0 or r.get("buyback_announced")):
                to_enrich.add(r["ticker"].replace(".AX", ""))

        if to_enrich:
            # Live post-enrich disabled to prevent rate limits. Metrics come from the yf_cache layer above.
            # If a top ticker was stale this run, _fetch_yf_data_sequentially already gave it full metrics.
            logger.info(f"Full list: {len(to_enrich)} top/signaled tickers would have been candidates for extra enrichment (now served from cache).")
    else:
        # Ensure signals-only view only contains rows that actually have a signal (if we fell back to base list)
        if signals_by_ticker:
            results = [r for r in results if (r.get("insider_buys_2026", 0) > 0 or r.get("buyback_announced"))]

    # Cache under mode-specific key so signals view and full view can coexist in the 10-min TTL cache
    cache_key = "full_data" if full else "data"
    _asx_cache[cache_key] = results
    _asx_cache["ts"] = now
    _asx_cache["last_scraped"] = datetime.now().isoformat()
    _asx_cache["last_mode"] = "full" if full else "signals"

    # Mirror universe stats...
    try:
        _asx_cache["universe_count"] = len(load_asx_ticker_universe())
        conn = _get_universe_conn()
        _asx_cache["universe_as_of"] = _get_meta(conn, "universe_as_of")
        _asx_cache["universe_last_update"] = _get_meta(conn, "universe_last_update")
        conn.close()
    except Exception:
        _asx_cache["universe_count"] = len(base_universe)
    return results


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv or "-f" in sys.argv
    print("ASX company list (universe) update from", ASX_UNIVERSE_URL)
    res = update_asx_company_list(force=force)
    print(res)
    if res.get("updated"):
        print("Done. Next: run the app and use the 'Update ASX company list' button or /api/refresh to pick up signals for new names.")
    elif not res.get("error"):
        print("List is fresh (use --force to override 6-month staleness check).")


# ---------------------- SINGLE TICKER HELPERS (for detail modal) ----------------------

def _fetch_single_asx(base_ticker: str, use_pdf: bool = False):
    """
    Fresh targeted fetch for one ASX ticker (used by /api/stock/<ticker>).
    Returns a fully enriched stock dict with latest yf metrics + any signals for it.
    Much faster than full list.
    """
    t = (base_ticker or "").upper().replace(".AX", "")
    if not t:
        return None

    signals = {}
    try:
        from asx_announcements import get_relevant_signals
        # Per-ticker for one is acceptable; use_pdf=False to keep detail snappy unless user really wants it
        signals = get_relevant_signals([t], days=365, use_pdf=use_pdf, use_cache=True)
    except Exception as e:
        logger.debug(f"signals for single {t} failed: {e}")

    real_signals = signals.get(t, []) if signals else []
    insider_count = len([s for s in real_signals if s.get("type") == "insider"])
    has_buyback = any(s.get("type") == "buyback" for s in real_signals)

    yf_symbol = f"{t}.AX"
    try:
        yt = yf.Ticker(yf_symbol)
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
        metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf)
        market_cap = metrics.get("market_cap", 0)
        pe = metrics.get("pe", 0)
        pb = metrics.get("pb", 0)

        summary = _business_summary(info)
        price_trend = _build_price_trend(yt, period="2y", interval="1wk")
        earnings_history = _build_earnings_history(yt)

        detail = load_asx_universe_details().get(t, {})
        name = detail.get("name") or info.get("shortName") or info.get("longName") or t
        gics = detail.get("gics") or info.get("sector") or "Unknown"

        formatted = []
        for sig in real_signals[:6]:
            formatted.append({"date": sig.get("date"), "type": sig.get("type"), "desc": (sig.get("headline") or "")[:120]})
        if not formatted:
            formatted = [{"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "No recent relevant announcements"}]

        stock = {
            "ticker": f"{t}.AX",
            "name": name,
            "sector": gics,
            "insider_buys_2026": insider_count,
            "buyback_announced": has_buyback,
            "buyback_date": None,
            "pct_from_52w_low": pct,
            "pct_from_200w_ma": pct_from_200w_ma,
            "last_activity": real_signals[0]["date"] if real_signals else datetime.now().strftime("%Y-%m-%d"),
            "current_price": _safe_round(price, 2),
            "low_52w": _safe_round(low, 2),
            "high_52w": _safe_round(high, 2),
            "market_cap": market_cap,
            "currency": metrics.get("currency") or infer_currency(ticker=f"{t}.AX"),
            "net_cash_pct_mcap": metrics.get("net_cash_pct_mcap", 0),
            "pe": pe,
            "pb": pb,
            "p_fcf": p_fcf,
            "metrics": metrics,
            "signals": formatted,
            "summary": summary,
            "_market": "asx",
            "_live": True,
            "price_trend": price_trend,
            "earnings_history": earnings_history,
        }
        return stock
    except Exception as e:
        logger.warning(f"_fetch_single_asx failed for {t}: {e}")
        return None