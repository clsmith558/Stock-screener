"""
US S&P 500 Data Layer — cache-first via business_lists infrastructure + SEC EDGAR.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from data.yf_helpers import (
    business_summary,
    build_price_trend,
    calc_p_fcf,
    pct_from_52w_low,
    quote_price,
    reconcile_52w_range,
    safe_float,
    safe_round,
    sanitize_stock_figures,
)

logger = logging.getLogger(__name__)

_us_cache = {}
US_CACHE_TTL = 600


def fetch_us_stocks(force_yf: bool = False):
    """
    Load US S&P 500 — cache-first (instant), same pattern as Business Lists.
    yfinance refresh only on force_yf / Rebuild YF. EDGAR from cache only on list load.
    """
    from data.business_lists import BUILTIN_SP500_LIST_ID, fetch_business_list_stocks

    now = __import__("time").time()
    cache_key = f"sp500:{force_yf}"
    if cache_key in _us_cache and now - _us_cache[cache_key].get("ts", 0) < US_CACHE_TTL and not force_yf:
        return _us_cache[cache_key]["data"]

    results = fetch_business_list_stocks(BUILTIN_SP500_LIST_ID, force_yf=force_yf)
    for stock in results:
        stock["_market"] = "us"

    _us_cache[cache_key] = {
        "data": results,
        "ts": now,
        "last_scraped": datetime.now().isoformat(),
    }
    return results


def get_us_meta() -> dict:
    from data.business_lists import BUILTIN_SP500_LIST_ID, get_business_meta
    try:
        meta = get_business_meta(BUILTIN_SP500_LIST_ID)
        meta["data_source"] = "yfinance + SEC EDGAR (ROIC, shares) • S&P 500"
        meta["market"] = "us"
        return meta
    except Exception:
        return {
            "data_source": "yfinance + SEC EDGAR (ROIC, shares) • S&P 500",
            "last_scraped": _us_cache.get("sp500:False", {}).get("last_scraped") or datetime.now().isoformat(),
            "market": "us",
        }


def _fetch_single_us(base_ticker: str):
    """
    Fresh targeted fetch for one US ticker (used by /api/stock/<ticker> for rich modal).
    Includes live yf + insider txns + price trend + earnings history + EDGAR.
    """
    from data.yf_helpers import attach_sec_and_mining_detail, fetch_yf_ticker_snapshot

    t = (base_ticker or "").upper().replace(".AX", "")
    if not t:
        return None

    try:
        snap = fetch_yf_ticker_snapshot(t, ticker_for_fx=t, include_detail=True)
        yt = snap["yt"]
        metrics = snap["metrics"]

        insider_count = 0
        signals = []
        try:
            ins = yt.insider_transactions
            if ins is not None and not ins.empty:
                cutoff = datetime.now() - timedelta(days=365)
                for _, row in ins.iterrows():
                    try:
                        tx_date = pd.to_datetime(row.get("Start Date"))
                        if tx_date < cutoff:
                            continue
                        tx_type = str(row.get("Transaction", ""))
                        if "purchase" in tx_type.lower() or "buy" in tx_type.lower():
                            insider_count += 1
                            signals.append({
                                "date": tx_date.strftime("%Y-%m-%d"),
                                "type": "insider",
                                "desc": f"{row.get('Insider', 'Insider')} - {tx_type} {int(row.get('Shares', 0)):,} sh"
                            })
                    except Exception:
                        continue
        except Exception:
            pass

        stock = {
            "ticker": t,
            "name": snap["name"] or t,
            "sector": snap["sector"],
            "insider_buys_2026": insider_count,
            "buyback_announced": False,
            "buyback_date": None,
            "pct_from_52w_low": snap["pct_from_52w_low"],
            "pct_from_200w_ma": snap["pct_from_200w_ma"],
            "last_52w_low_date": metrics.get("last_52w_low_date"),
            "days_since_52w_low": metrics.get("days_since_52w_low"),
            "own_hist_cheap": bool(metrics.get("own_hist_cheap")),
            "own_hist_label": metrics.get("own_hist_label") or "unknown",
            "last_activity": datetime.now().strftime("%Y-%m-%d"),
            "current_price": snap["price"],
            "low_52w": snap["low_52w"],
            "high_52w": snap["high_52w"],
            "market_cap": metrics.get("market_cap", 0),
            "currency": snap["currency"],
            "pe": metrics.get("pe", 0),
            "pb": metrics.get("pb", 0),
            "p_fcf": snap["p_fcf"],
            "rsi": metrics.get("rsi"),
            "metrics": metrics,
            "signals": signals[:6] if signals else [
                {"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "No recent open-market purchases found"}
            ],
            "summary": snap["summary"],
            "_market": "us",
            "_live": True,
            "price_trend": snap["price_trend"],
            "rsi_trend": snap["rsi_trend"],
            "earnings_history": snap["earnings_history"],
        }
        attach_sec_and_mining_detail(
            stock,
            t,
            include_edgar=True,
            include_sec_mining=True,
            include_asx_mining=False,
        )
        return sanitize_stock_figures(stock)
    except Exception as e:
        logger.warning(f"_fetch_single_us failed for {t}: {e}")
        return None

