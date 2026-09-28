"""
Major national equity indices — historical prices + valuation proxies.

Price history: yfinance (monthly).
Spot index PE: ETF trailing PE proxy (Yahoo does not expose index PE for most ^symbols).
Historical PE: Multpl S&P 500 monthly PE when available; other markets spot-only.
Optional: equal-weight median PE from local screener universes (S&P 500 / Nasdaq-100 lists).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import requests

from data.asx import DEFAULT_UNIVERSE_DB

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 3600  # 6h in-memory
_indices_cache: dict[str, Any] = {}

# Catalog: yf_symbol for price, pe_etf for current PE proxy, optional local list for constituent PE
INDEX_CATALOG: list[dict[str, Any]] = [
    {
        "id": "sp500",
        "name": "S&P 500",
        "region": "US",
        "yf_symbol": "^GSPC",
        "pe_etf": "SPY",
        "pe_history": "multpl_sp500",
        "universe_list_id": "__builtin__:sp500",
        "note": "Crisis lens: 2000–02, 2008–09; Multpl PE history available",
    },
    {
        "id": "nasdaq",
        "name": "NASDAQ Composite",
        "region": "US",
        "yf_symbol": "^IXIC",
        "pe_etf": "QQQ",
        "universe_list_id": "__builtin__:nasdaq100",
        "note": "Tech-heavy; QQQ PE is Nasdaq-100 proxy",
    },
    {
        "id": "kospi",
        "name": "KOSPI",
        "region": "KR",
        "yf_symbol": "^KS11",
        "pe_etf": "EWY",
        "note": "Yahoo history from late 1996 — Asian crisis drawdown visible",
    },
    {
        "id": "nikkei",
        "name": "Nikkei 225",
        "region": "JP",
        "yf_symbol": "^N225",
        "pe_etf": "EWJ",
        "note": "Price history long; PE via MSCI Japan ETF",
    },
    {
        "id": "topix",
        "name": "TOPIX (ETF 1305.T)",
        "region": "JP",
        "yf_symbol": "1305.T",
        "pe_etf": "1305.T",
        "note": (
            "Yahoo ^TPX is empty. Nomura 1306.T has an unadjusted 10-for-1 split "
            "in Jan 2015 (series drops to ~10%); Daiwa 1305.T is continuous."
        ),
    },
    {
        "id": "hsi",
        "name": "Hang Seng",
        "region": "HK",
        "yf_symbol": "^HSI",
        "pe_etf": "EWH",
    },
    {
        "id": "asx200",
        "name": "S&P/ASX 200",
        "region": "AU",
        "yf_symbol": "^AXJO",
        "pe_etf": "EWA",
    },
    {
        "id": "ftse100",
        "name": "FTSE 100",
        "region": "UK",
        "yf_symbol": "^FTSE",
        "pe_etf": "EWU",
    },
    {
        "id": "dax",
        "name": "DAX",
        "region": "DE",
        "yf_symbol": "^GDAXI",
        "pe_etf": "EWG",
    },
    {
        "id": "sse",
        "name": "SSE Composite",
        "region": "CN",
        "yf_symbol": "000001.SS",
        "pe_etf": "MCHI",
        "note": "China A-share composite; PE via MSCI China ETF",
    },
    {
        "id": "twii",
        "name": "Taiwan Weighted",
        "region": "TW",
        "yf_symbol": "^TWII",
        "pe_etf": "EWT",
    },
    {
        "id": "sensex",
        "name": "BSE Sensex",
        "region": "IN",
        "yf_symbol": "^BSESN",
        "pe_etf": "INDA",
    },
    {
        "id": "bovespa",
        "name": "Bovespa",
        "region": "BR",
        "yf_symbol": "^BVSP",
        "pe_etf": "EWZ",
        "note": "Ibovespa from 1993; PE via iShares MSCI Brazil (EWZ)",
    },
    {
        "id": "jkse",
        "name": "IDX Composite",
        "region": "ID",
        "yf_symbol": "^JKSE",
        "pe_etf": "EIDO",
        "note": "Jakarta Composite from 1990; PE via iShares MSCI Indonesia (EIDO)",
    },
]


def _conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_series_cache (
            index_id TEXT NOT NULL,
            series_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (index_id, series_type)
        )
        """
    )
    conn.commit()
    return conn


def _cache_get(index_id: str, series_type: str, max_age_hours: float = 24, db_path: str = DEFAULT_UNIVERSE_DB):
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT payload_json, fetched_at FROM index_series_cache WHERE index_id=? AND series_type=?",
            (index_id, series_type),
        ).fetchone()
        conn.close()
        if not row:
            return None
        fetched = datetime.fromisoformat(row[1])
        age_h = (datetime.now() - fetched).total_seconds() / 3600
        if age_h > max_age_hours:
            return None
        return json.loads(row[0])
    except Exception as e:
        logger.debug("index cache get failed: %s", e)
        return None


def _cache_put(index_id: str, series_type: str, payload: Any, db_path: str = DEFAULT_UNIVERSE_DB):
    try:
        conn = _conn(db_path)
        conn.execute(
            """
            INSERT OR REPLACE INTO index_series_cache (index_id, series_type, payload_json, fetched_at)
            VALUES (?,?,?,?)
            """,
            (index_id, series_type, json.dumps(payload), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("index cache put failed: %s", e)


def _repair_scale_breaks(
    prices: list[dict],
    factors: tuple[float, ...] = (10.0, 5.0, 2.0),
    tol: float = 0.15,
) -> tuple[list[dict], int]:
    """Fix one-month ~Nx jumps when Yahoo missed an ETF split (1306.T Jan 2015)."""
    if len(prices) < 3:
        return prices, 0
    out = [dict(p) for p in prices]
    nfix = 0
    for i in range(len(out) - 1, 0, -1):
        older, newer = out[i - 1].get("close"), out[i].get("close")
        if not older or not newer:
            continue
        ratio = older / newer
        for f in factors:
            if abs(ratio / f - 1.0) <= tol:
                for j in range(i):
                    out[j]["close"] = round(out[j]["close"] / f, 2)
                nfix += 1
                break
            if abs(ratio * f - 1.0) <= tol:
                for j in range(i):
                    out[j]["close"] = round(out[j]["close"] * f, 2)
                nfix += 1
                break
    return out, nfix


def _fetch_price_history(yf_symbol: str, start: str = "1990-01-01") -> list[dict]:
    import yfinance as yf

    tk = yf.Ticker(yf_symbol)
    hist = tk.history(start=start, interval="1mo", auto_adjust=True)
    if hist is None or hist.empty:
        return []
    out = []
    for dt, row in hist.iterrows():
        c = row.get("Close")
        if c is None or c != c:
            continue
        out.append({
            "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
            "close": round(float(c), 2),
        })
    repaired, nfix = _repair_scale_breaks(out)
    if nfix:
        logger.info("scale-break repair %s n=%s", yf_symbol, nfix)
    return repaired


def _fetch_etf_pe(etf_symbol: str) -> dict:
    import yfinance as yf

    info = yf.Ticker(etf_symbol).info or {}
    pe = info.get("trailingPE")
    fwd = info.get("forwardPE")
    try:
        pe = float(pe) if pe is not None else None
    except (TypeError, ValueError):
        pe = None
    try:
        fwd = float(fwd) if fwd is not None else None
    except (TypeError, ValueError):
        fwd = None
    return {
        "trailing_pe": round(pe, 2) if pe and pe == pe else None,
        "forward_pe": round(fwd, 2) if fwd and fwd == fwd else None,
        "source": f"ETF {etf_symbol}",
        "name": info.get("shortName") or etf_symbol,
    }


def _fetch_multpl_sp500_pe() -> list[dict]:
    """Monthly S&P 500 PE from multpl.com (long history)."""
    url = "https://www.multpl.com/s-p-500-pe-ratio/table/by-month"
    try:
        resp = requests.get(
            url,
            timeout=45,
            headers={
                "User-Agent": "SignalScreener/1.0 (personal research; local)",
                "Accept": "text/html",
            },
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Multpl SP500 PE fetch failed: %s", e)
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("bs4 missing for Multpl parse")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="datatable") or soup.find("table")
    if not table:
        return []

    series = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2 or cells[0].lower() == "date":
            continue
        date_raw, val_raw = cells[0], cells[1]
        val_raw = re.sub(r"[†*]", "", val_raw).replace(",", "").strip()
        try:
            pe = float(val_raw)
        except ValueError:
            continue
        # Multpl dates like "Jul 13, 2026" or "Jun 1, 2026"
        dt = None
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_raw, fmt)
                break
            except ValueError:
                continue
        if not dt:
            continue
        series.append({"date": dt.strftime("%Y-%m-%d"), "pe": round(pe, 2)})
    series.sort(key=lambda x: x["date"])
    return series


def _constituent_median_pe(list_id: str) -> dict | None:
    """Equal-weight median trailing PE from local business-list YF cache."""
    if not list_id:
        return None
    try:
        from data.business_lists import load_list_symbols, load_yf_cached

        symbols = load_list_symbols(list_id)
        if not symbols:
            return None
        cached = load_yf_cached(symbols)
        pes = []
        for _sym, data in cached.items():
            m = (data or {}).get("metrics") or {}
            pe = m.get("pe") or data.get("pe")
            try:
                pe = float(pe)
            except (TypeError, ValueError):
                continue
            if pe and pe > 0 and pe < 200:
                pes.append(pe)
        if len(pes) < 10:
            return None
        pes.sort()
        mid = len(pes) // 2
        med = pes[mid] if len(pes) % 2 else (pes[mid - 1] + pes[mid]) / 2
        return {
            "median_pe": round(med, 2),
            "mean_pe": round(sum(pes) / len(pes), 2),
            "n": len(pes),
            "source": f"local list {list_id} (YF cache)",
        }
    except Exception as e:
        logger.debug("constituent PE failed for %s: %s", list_id, e)
        return None


def _drawdown_stats(prices: list[dict]) -> dict:
    if not prices:
        return {}
    closes = [p["close"] for p in prices]
    peak = closes[0]
    max_dd = 0.0
    max_dd_date = prices[0]["date"]
    peak_date = prices[0]["date"]
    for p in prices:
        c = p["close"]
        if c > peak:
            peak = c
            peak_date = p["date"]
        dd = (c / peak - 1.0) * 100 if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
            max_dd_date = p["date"]
    last = closes[-1]
    ath = max(closes)
    from_ath = (last / ath - 1.0) * 100 if ath > 0 else 0
    # 5y window
    last_date = prices[-1]["date"]
    try:
        y = int(last_date[:4])
        cutoff_5y = f"{y - 5}-01-01"
        sub5 = [p for p in prices if p["date"] >= cutoff_5y]
    except ValueError:
        sub5 = prices[-60:]
    peak5 = max(p["close"] for p in sub5) if sub5 else ath
    from_5y_high = (last / peak5 - 1.0) * 100 if peak5 > 0 else 0
    ret_1y = None
    if len(closes) >= 13:
        ret_1y = round((closes[-1] / closes[-13] - 1) * 100, 1)
    ret_5y = None
    if len(closes) >= 61:
        ret_5y = round((closes[-1] / closes[-61] - 1) * 100, 1)
    return {
        "last_close": round(last, 2),
        "all_time_high": round(ath, 2),
        "pct_from_ath": round(from_ath, 1),
        "pct_from_5y_high": round(from_5y_high, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "max_drawdown_date": max_dd_date,
        "ath_date": peak_date if peak == ath else None,
        "return_1y_pct": ret_1y,
        "return_5y_pct": ret_5y,
        "history_start": prices[0]["date"],
        "history_end": prices[-1]["date"],
        "n_months": len(prices),
    }


def _load_index_detail(meta: dict, force: bool = False) -> dict:
    iid = meta["id"]
    cached = None if force else _cache_get(iid, "detail", max_age_hours=18)
    if cached and cached.get("yf_symbol") != meta.get("yf_symbol"):
        cached = None
    if cached:
        return cached

    prices = _fetch_price_history(meta["yf_symbol"])
    pe_spot = _fetch_etf_pe(meta["pe_etf"]) if meta.get("pe_etf") else {}
    pe_history = []
    if meta.get("pe_history") == "multpl_sp500":
        pe_history = _cache_get("sp500", "pe_history", max_age_hours=48) or []
        if not pe_history or force:
            pe_history = _fetch_multpl_sp500_pe()
            if pe_history:
                _cache_put("sp500", "pe_history", pe_history)

    constituent = None
    if meta.get("universe_list_id"):
        constituent = _constituent_median_pe(meta["universe_list_id"])

    stats = _drawdown_stats(prices)
    # PE percentile vs history when available
    pe_pctile = None
    pe_now = pe_spot.get("trailing_pe")
    if pe_history and pe_now:
        vals = [p["pe"] for p in pe_history if p.get("pe")]
        if vals:
            below = sum(1 for v in vals if v < pe_now)
            pe_pctile = round(100 * below / len(vals), 1)

    detail = {
        **meta,
        "prices": prices,
        "pe_spot": pe_spot,
        "pe_history": pe_history,
        "constituent_pe": constituent,
        "stats": stats,
        "pe_percentile_vs_history": pe_pctile,
        "fetched_at": datetime.now().isoformat(),
    }
    # Slim cache without huge duplication — store full detail
    _cache_put(iid, "detail", detail)
    return detail


def get_indices_overview(force: bool = False) -> dict:
    """Summary rows for all indices (no full price series in list payload)."""
    global _indices_cache
    now = time.time()
    if (
        not force
        and _indices_cache.get("overview")
        and now - _indices_cache.get("overview_ts", 0) < CACHE_TTL_SECONDS
    ):
        return _indices_cache["overview"]

    def _row_from_detail(meta: dict, detail: dict) -> dict:
        pe_spot = detail.get("pe_spot") or {}
        cons = detail.get("constituent_pe") or {}
        stats = detail.get("stats") or {}
        return {
            "id": meta["id"],
            "name": meta["name"],
            "region": meta["region"],
            "yf_symbol": meta["yf_symbol"],
            "note": meta.get("note") or "",
            "last_close": stats.get("last_close"),
            "pct_from_ath": stats.get("pct_from_ath"),
            "pct_from_5y_high": stats.get("pct_from_5y_high"),
            "max_drawdown_pct": stats.get("max_drawdown_pct"),
            "max_drawdown_date": stats.get("max_drawdown_date"),
            "return_1y_pct": stats.get("return_1y_pct"),
            "return_5y_pct": stats.get("return_5y_pct"),
            "history_start": stats.get("history_start"),
            "history_end": stats.get("history_end"),
            "pe_etf": pe_spot.get("trailing_pe"),
            "pe_etf_source": pe_spot.get("source"),
            "pe_constituent_median": cons.get("median_pe"),
            "pe_constituent_n": cons.get("n"),
            "pe_percentile_vs_history": detail.get("pe_percentile_vs_history"),
            "has_pe_history": bool(detail.get("pe_history")),
        }

    by_id: dict[str, dict] = {}
    workers = min(8, max(1, len(INDEX_CATALOG)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_load_index_detail, meta, force): meta for meta in INDEX_CATALOG}
        for fut in as_completed(futs):
            meta = futs[fut]
            try:
                by_id[meta["id"]] = _row_from_detail(meta, fut.result())
            except Exception as e:
                logger.exception("index overview failed for %s", meta["id"])
                by_id[meta["id"]] = {
                    "id": meta["id"],
                    "name": meta["name"],
                    "region": meta["region"],
                    "error": str(e),
                }
    rows = [by_id[m["id"]] for m in INDEX_CATALOG if m["id"] in by_id]

    # Rank by deepest drawdown from ATH (more negative = more "pessimism" on price)
    ranked = sorted(
        [r for r in rows if r.get("pct_from_ath") is not None],
        key=lambda x: x.get("pct_from_ath") or 0,
    )
    for i, r in enumerate(ranked):
        r["pessimism_rank"] = i + 1  # 1 = furthest below ATH

    payload = {
        "indices": rows,
        "count": len(rows),
        "meta": {
            "data_source": "yfinance monthly prices · ETF trailing PE proxy · Multpl S&P PE history",
            "caveat": (
                "Yahoo indices rarely expose PE. Spot PE uses country/style ETFs. "
                "Historical PE only for S&P 500 (Multpl). "
                "Price drawdown is the main historical pessimism lens (e.g. KOSPI 1997–98)."
            ),
            "fetched_at": datetime.now().isoformat(),
        },
    }
    _indices_cache["overview"] = payload
    _indices_cache["overview_ts"] = now
    return payload


def get_index_detail(index_id: str, force: bool = False) -> dict | None:
    meta = next((m for m in INDEX_CATALOG if m["id"] == index_id), None)
    if not meta:
        return None
    detail = _load_index_detail(meta, force=force)
    # Attach summary stats already inside
    return detail
