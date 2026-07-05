"""
US S&P 500 Data Layer — cache-first via business_lists infrastructure + SEC EDGAR.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_us_cache = {}
US_CACHE_TTL = 600


def _safe_float(val, default=0.0):
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


def _normalize_div_yield(val) -> float:
    v = _safe_float(val)
    if not v:
        return 0
    if v > 0.5:
        return round(v, 2)
    return round(v * 100, 2)


def _business_summary(info: dict) -> str:
    info = info or {}
    return (info.get("longBusinessSummary") or info.get("description") or "").strip()


def _build_price_trend(yt, period: str = "2y", interval: str = "1wk") -> list:
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


def _calc_ma200w_gap(yt, price=None):
    try:
        from data.asx import _calc_ma200w_gap as _asx_ma
        return _asx_ma(yt, price)
    except Exception:
        pass
    return 0, 0


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
    t = (base_ticker or "").upper().replace(".AX", "")
    if not t:
        return None

    yf_symbol = t
    try:
        yt = yf.Ticker(yf_symbol)
        info = yt.info or {}

        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        low = info.get("fiftyTwoWeekLow") or 0
        high = info.get("fiftyTwoWeekHigh") or 0
        pct = 0.0
        if price and low and low > 0:
            pct = round(((price - low) / low) * 100, 1)

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

        pe = info.get("trailingPE") or info.get("forwardPE") or 0
        pb = info.get("priceToBook") or 0
        cash = (info.get("totalCash") or 0) / 1_000_000 if info.get("totalCash") else 0
        debt = (info.get("totalDebt") or 0) / 1_000_000 if info.get("totalDebt") else 0
        net_cash = cash - debt
        market_cap = int(info.get("marketCap") or 0)
        ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)

        p_fcf = 0
        try:
            fcf = info.get("freeCashflow") or 0
            shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding") or 0
            if price and fcf and shares and shares > 0:
                fps = fcf / shares
                if fps > 0:
                    p_fcf = round(price / fps, 1)
        except Exception:
            pass

        summary = _business_summary(info)
        price_trend = _build_price_trend(yt, period="2y", interval="1wk")

        from data.asx import _build_earnings_history
        earnings_history = _build_earnings_history(yt)

        name = info.get("shortName") or info.get("longName") or t
        sector = info.get("sector") or "Unknown"

        stock = {
            "ticker": t,
            "name": name,
            "sector": sector,
            "insider_buys_2026": insider_count,
            "buyback_announced": False,
            "buyback_date": None,
            "pct_from_52w_low": pct,
            "pct_from_200w_ma": pct_from_200w_ma,
            "last_activity": datetime.now().strftime("%Y-%m-%d"),
            "current_price": round(price, 2) if price else 0,
            "low_52w": round(low, 2) if low else 0,
            "high_52w": round(high, 2) if high else 0,
            "market_cap": market_cap,
            "pe": round(pe, 1) if pe else 0,
            "pb": round(pb, 2) if pb else 0,
            "p_fcf": p_fcf,
            "metrics": {
                "pe": round(pe, 1) if pe else 0,
                "forward_pe": round(info.get("forwardPE", 0), 1) if info.get("forwardPE") else 0,
                "pb": round(pb, 2) if pb else 0,
                "p_fcf": p_fcf,
                "pcf": 0,
                "ev_ebitda": round(info.get("enterpriseToEbitda", 0), 1) if info.get("enterpriseToEbitda") else 0,
                "debt_to_equity": round(info.get("debtToEquity", 0) / 100, 2) if info.get("debtToEquity") else 0,
                "cash_on_hand_m": round(cash, 0),
                "net_cash_m": round(net_cash, 0),
                "roe": round(info.get("returnOnEquity", 0) * 100, 1) if info.get("returnOnEquity") else 0,
                "roic": 0,
                "fcf_yield": 0,
                "div_yield": _normalize_div_yield(info.get("dividendYield")),
                "market_cap": market_cap,
                "ma_200w": ma_200w,
                "pct_from_200w_ma": pct_from_200w_ma,
            },
            "signals": signals[:6] if signals else [
                {"date": datetime.now().strftime("%Y-%m-%d"), "type": "note", "desc": "No recent open-market purchases found"}
            ],
            "summary": summary,
            "_market": "us",
            "_live": True,
            "price_trend": price_trend,
            "earnings_history": earnings_history,
        }
        try:
            from data.edgar_metrics import fetch_edgar_metrics, attach_edgar_to_stock
            edgar = fetch_edgar_metrics(t)
            attach_edgar_to_stock(stock, edgar=edgar, ticker=t)
        except Exception as ex:
            logger.warning("EDGAR detail fetch failed for %s: %s", t, ex)
        return stock
    except Exception as e:
        logger.warning(f"_fetch_single_us failed for {t}: {e}")
        return None