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
    from data.asx import (
        _build_earnings_history,
        _calc_ma200w_gap,
        _metrics_from_yf_info,
        infer_currency,
    )

    t = (base_ticker or "").upper().replace(".AX", "")
    if not t:
        return None

    try:
        yt = yf.Ticker(t)
        info = yt.info or {}

        price = quote_price(info)
        low, high = reconcile_52w_range(
            price, info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
        )
        pct = pct_from_52w_low(price, low)

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

        p_fcf = calc_p_fcf(info, price, ticker=t)
        ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
        from data.yf_helpers import calc_last_52w_low_metrics, calc_own_history_cheapness, calc_rsi_metrics
        low52 = calc_last_52w_low_metrics(yt)
        metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=t)
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
        market_cap = metrics.get("market_cap", 0)

        stock = {
            "ticker": t,
            "name": info.get("shortName") or info.get("longName") or t,
            "sector": info.get("sector") or "Unknown",
            "insider_buys_2026": insider_count,
            "buyback_announced": False,
            "buyback_date": None,
            "pct_from_52w_low": pct,
            "pct_from_200w_ma": pct_from_200w_ma,
            "last_52w_low_date": metrics.get("last_52w_low_date"),
            "days_since_52w_low": metrics.get("days_since_52w_low"),
            "own_hist_cheap": bool(metrics.get("own_hist_cheap")),
            "own_hist_label": metrics.get("own_hist_label") or "unknown",
            "last_activity": datetime.now().strftime("%Y-%m-%d"),
            "current_price": safe_round(price, 2) if price else 0,
            "low_52w": safe_round(low, 2) if low else 0,
            "high_52w": safe_round(high, 2) if high else 0,
            "market_cap": market_cap,
            "currency": metrics.get("currency") or infer_currency(info, t),
            "pe": metrics.get("pe", 0),
            "pb": metrics.get("pb", 0),
            "p_fcf": p_fcf,
            "rsi": metrics.get("rsi"),
            "metrics": metrics,
            "signals": signals[:6] if signals else [
                {"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "No recent open-market purchases found"}
            ],
            "summary": business_summary(info),
            "_market": "us",
            "_live": True,
            "price_trend": build_price_trend(yt, period="2y", interval="1wk"),
            "rsi_trend": rsi_pack.get("rsi_trend") or [],
            "earnings_history": _build_earnings_history(yt),
        }
        try:
            from data.edgar_metrics import attach_edgar_to_stock, fetch_edgar_metrics
            edgar = fetch_edgar_metrics(t)
            attach_edgar_to_stock(stock, edgar=edgar, ticker=t)
        except Exception as ex:
            logger.warning("EDGAR detail fetch failed for %s: %s", t, ex)
        try:
            from data.bankruptcy_reorg import attach_reorg_to_stock
            attach_reorg_to_stock(stock, ticker=t)
        except Exception as ex:
            logger.debug("Reorg attach skipped for %s: %s", t, ex)
        try:
            from data.mining_reserves import attach_reserves_to_stock
            attach_reserves_to_stock(stock, ticker=t)
        except Exception as ex:
            logger.debug("Reserves attach skipped for %s: %s", t, ex)
        try:
            from data.mining_costs import attach_costs_to_stock
            attach_costs_to_stock(stock, ticker=t)
        except Exception as ex:
            logger.debug("Unit-cost attach skipped for %s: %s", t, ex)
        try:
            from data.earnings_multiples import attach_earnings_multiples
            attach_earnings_multiples(stock)
        except Exception:
            pass
        return sanitize_stock_figures(stock)
    except Exception as e:
        logger.warning(f"_fetch_single_us failed for {t}: {e}")
        return None
