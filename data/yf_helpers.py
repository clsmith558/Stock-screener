"""
Shared yfinance metric helpers used across ASX, US, and business-list layers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# FX cache: (from, to) -> (rate, fetched_monotonic)
_FX_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_FX_CACHE_TTL_SEC = 3600


def safe_float(val, default=0.0):
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


def safe_round(val, ndigits=1, default=0):
    f = safe_float(val, None)
    if f is None:
        return default
    try:
        return int(round(f)) if ndigits == 0 else round(f, ndigits)
    except (TypeError, ValueError):
        return default


def reconcile_52w_range(
    price, low, high
) -> tuple[float | None, float | None]:
    """
    Ensure 52w low/high are positive and contain current price when present.

    Yahoo (and caches) often lag: last print can sit under fiftyTwoWeekLow,
    which previously produced negative "% from 52w low" (e.g. HIIDQ −86%).
    """
    p = safe_float(price, None)
    lo = safe_float(low, None)
    hi = safe_float(high, None)
    if lo is not None and lo <= 0:
        lo = None
    if hi is not None and hi <= 0:
        hi = None
    if p is not None and p > 0:
        if lo is None or p < lo:
            lo = p
        if hi is None or p > hi:
            hi = p
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def pct_from_52w_low(price, low, *, ndigits: int = 1, default: float = 0.0) -> float:
    """
    % above the 52-week low. Floor at 0% — at the low (or below after reconcile) → 0.0%.
    """
    p = safe_float(price, None)
    lo = safe_float(low, None)
    if p is None or lo is None or p <= 0 or lo <= 0:
        return default
    pct = ((p - lo) / lo) * 100.0
    if pct < 0:
        pct = 0.0
    # Guard divide-by-near-zero junk
    if pct > 1_000_000:
        return default
    return float(safe_round(pct, ndigits, default))


def calc_own_history_cheapness(
    yt,
    *,
    price: float = 0,
    pb: float = 0,
    p_fcf: float = 0,
    fcf_yield: float = 0,
    years: int = 5,
) -> dict:
    """
    Compare current valuation to the stock's *own* ~5y weekly price history.

    Reconstructs approximate historical multiples assuming book equity and FCF
    levels are roughly stable (price path drives the multiple). Not a full
    fundamental history rebuild.

    Returns fields for metrics/UI:
      own_hist_weeks, own_hist_pb_pctile, own_hist_pfcf_pctile,
      own_hist_fcf_yield_pctile, own_hist_cheap (bool), own_hist_label
    Percentiles: 100 = cheapest vs own history (best for value), 0 = richest.
    """
    empty = {
        "own_hist_weeks": 0,
        "own_hist_pb_pctile": None,
        "own_hist_pfcf_pctile": None,
        "own_hist_fcf_yield_pctile": None,
        "own_hist_cheap": False,
        "own_hist_label": "unknown",
    }
    if yt is None:
        return empty
    price = safe_float(price)
    pb = safe_float(pb)
    p_fcf = safe_float(p_fcf)
    fcf_yield = safe_float(fcf_yield)
    if price <= 0:
        return empty
    try:
        period = "5y" if years >= 5 else "2y"
        hist = yt.history(period=period, interval="1wk", auto_adjust=True)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return empty
        closes = hist["Close"].dropna()
        if len(closes) < 26:
            return empty
        # Cap to ~5y of weeks
        closes = closes.tail(min(int(years * 52), len(closes)))
        prices = [float(x) for x in closes.tolist() if float(x) > 0]
        if len(prices) < 26:
            return empty
        n = len(prices)
        out = dict(empty)
        out["own_hist_weeks"] = n

        def _cheaper_pctile(current: float, series: list[float], lower_is_better: bool) -> float | None:
            if current is None or current <= 0 or not series:
                return None
            if lower_is_better:
                # share of history where multiple was >= current (current cheaper or equal)
                better = sum(1 for v in series if v >= current)
            else:
                better = sum(1 for v in series if v <= current)
            return round(100.0 * better / len(series), 1)

        # P/B path: book/share ≈ price/pb today
        if pb > 0:
            book_ps = price / pb
            if book_ps > 0:
                hist_pb = [p / book_ps for p in prices if p / book_ps > 0]
                out["own_hist_pb_pctile"] = _cheaper_pctile(pb, hist_pb, lower_is_better=True)

        # P/FCF path: fcf/share ≈ price/p_fcf
        if p_fcf > 0:
            fcf_ps = price / p_fcf
            if fcf_ps > 0:
                hist_pfcf = [p / fcf_ps for p in prices if p / fcf_ps > 0]
                out["own_hist_pfcf_pctile"] = _cheaper_pctile(p_fcf, hist_pfcf, lower_is_better=True)

        # FCF yield path: yield_t ≈ fcf_yield * price_now / price_t
        if fcf_yield > 0:
            hist_fy = [fcf_yield * price / p for p in prices if p > 0]
            out["own_hist_fcf_yield_pctile"] = _cheaper_pctile(
                fcf_yield, hist_fy, lower_is_better=False
            )

        pctiles = [
            v
            for v in (
                out["own_hist_pb_pctile"],
                out["own_hist_pfcf_pctile"],
                out["own_hist_fcf_yield_pctile"],
            )
            if v is not None
        ]
        if not pctiles:
            out["own_hist_label"] = "unknown"
            return out
        best = max(pctiles)
        # Cheapest quintile vs self on any metric
        cheap = best >= 80
        out["own_hist_cheap"] = cheap
        if cheap:
            out["own_hist_label"] = "cheap_vs_self"
        elif best >= 50:
            out["own_hist_label"] = "mid_vs_self"
        else:
            out["own_hist_label"] = "rich_vs_self"
        return out
    except Exception as e:
        logger.debug("calc_own_history_cheapness failed: %s", e)
        return empty


def calc_last_52w_low_metrics(yt, trading_days: int = 252) -> dict:
    """
    Most recent date the trailing ~52-week low was printed (daily Low).

    Uses the last `trading_days` sessions (default 252 ≈ 1 year). If several
    days share the same low, the **most recent** of those dates is returned
    (latest re-test of the year low).

    Returns:
      last_52w_low_date: 'YYYY-MM-DD' or None
      days_since_52w_low: calendar days from that date to today, or None
    """
    out = {"last_52w_low_date": None, "days_since_52w_low": None}
    if yt is None:
        return out
    try:
        hist = yt.history(period="1y", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return out
        col = "Low" if "Low" in hist.columns else "Close"
        if col not in hist.columns:
            return out
        series = hist[col].dropna()
        if series.empty:
            return out
        window = max(20, min(int(trading_days), len(series)))
        series = series.tail(window)
        min_val = float(series.min())
        if min_val != min_val:  # NaN
            return out
        # Prefer most recent session at the low (re-test counts as latest new low)
        tol = max(abs(min_val) * 1e-6, 1e-8)
        at_low = series[series <= min_val + tol]
        if at_low.empty:
            return out
        idx = at_low.index[-1]
        try:
            low_dt = idx.date() if hasattr(idx, "date") else None
            if low_dt is None:
                import pandas as pd

                low_dt = pd.Timestamp(idx).date()
        except Exception:
            return out
        iso = low_dt.isoformat()
        from datetime import date as date_cls

        days = (date_cls.today() - low_dt).days
        out["last_52w_low_date"] = iso
        out["days_since_52w_low"] = int(days) if days >= 0 else None
        return out
    except Exception as e:
        logger.debug("calc_last_52w_low_metrics failed: %s", e)
        return out


def clamp_pct(val, lo: float = 0.0, hi: float = 100.0, default: float = 0.0) -> float:
    v = safe_float(val, None)
    if v is None:
        return default
    return float(safe_round(min(hi, max(lo, v)), 2, default))


def sanitize_stock_figures(stock: dict | None) -> dict | None:
    """
    In-place logical clamps for dashboard figures.

    - 52w gap ≥ 0; low/high contain current price
    - market_cap, prices ≥ 0
    - ownership / yield percentages in [0, 100] where applicable
    """
    if not stock or not isinstance(stock, dict):
        return stock

    price = safe_float(stock.get("current_price"), None)
    if price is None or price <= 0:
        price = safe_float(stock.get("price"), None)

    lo, hi = reconcile_52w_range(price, stock.get("low_52w"), stock.get("high_52w"))
    if lo is not None:
        stock["low_52w"] = safe_round(lo, 4, 0)
    if hi is not None:
        stock["high_52w"] = safe_round(hi, 4, 0)

    gap = pct_from_52w_low(price if price else stock.get("current_price"), lo if lo is not None else stock.get("low_52w"))
    stock["pct_from_52w_low"] = gap

    for key in ("current_price", "price", "market_cap", "ma_200w"):
        if key in stock and stock[key] is not None:
            v = safe_float(stock[key], None)
            if v is not None and v < 0:
                stock[key] = 0

    # Ownership / yield: never negative; cap ownership at 100
    m = stock.get("metrics")
    if isinstance(m, dict):
        if m.get("insider_ownership_pct") is not None:
            m["insider_ownership_pct"] = clamp_pct(m.get("insider_ownership_pct"), 0, 100, 0)
        if m.get("div_yield") is not None:
            dy = safe_float(m.get("div_yield"), 0)
            m["div_yield"] = safe_round(max(0.0, dy), 2, 0)
        if m.get("market_cap") is not None:
            mc = safe_float(m.get("market_cap"), 0)
            if mc < 0:
                m["market_cap"] = 0
        # PE/PB of 0 or negative often mean N/A for filters; leave negatives for loss-makers
        # but wipe non-finite
        for k in ("pe", "forward_pe", "pb", "p_fcf", "ev_ebitda", "pct_from_200w_ma", "ma_200w"):
            if k in m and m[k] is not None:
                try:
                    fv = float(m[k])
                    if fv != fv or fv in (float("inf"), float("-inf")):  # NaN/inf
                        m[k] = 0
                except (TypeError, ValueError):
                    m[k] = 0

    if stock.get("insider_ownership_pct") is not None:
        stock["insider_ownership_pct"] = clamp_pct(stock.get("insider_ownership_pct"), 0, 100, 0)

    return stock


def sanitize_stocks(stocks: list | None) -> list:
    if not stocks:
        return stocks or []
    for s in stocks:
        sanitize_stock_figures(s)
    return stocks


def normalize_div_yield(val) -> float:
    """yfinance dividendYield: decimal (0.06) on US, often already % (6.0) on ASX."""
    v = safe_float(val)
    if not v or v < 0:
        return 0
    if v > 0.5:
        return safe_round(v, 2)
    return safe_round(v * 100, 2)


def business_summary(info: dict) -> str:
    info = info or {}
    return (info.get("longBusinessSummary") or info.get("description") or "").strip()


def build_price_trend(yt, period: str = "2y", interval: str = "1wk") -> list:
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


RSI_PERIOD = 14


def wilder_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder RSI; first `period` bars are None."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period or period <= 0:
        return out
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        avg_gain += max(d, 0.0)
        avg_loss += max(-d, 0.0)
    avg_gain /= period
    avg_loss /= period

    def _rsi(ag: float, al: float) -> float:
        if al <= 0 and ag <= 0:
            return 50.0
        if al <= 0:
            return 100.0
        if ag <= 0:
            return 0.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = round(_rsi(avg_gain, avg_loss), 1)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = round(_rsi(avg_gain, avg_loss), 1)
    return out


def calc_rsi_metrics(yt, period: int = RSI_PERIOD, with_series: bool = False) -> dict:
    """14-day RSI from ~2y daily auto-adjusted closes."""
    out: dict[str, Any] = {"rsi": None, "rsi_period": period}
    try:
        hist = yt.history(period="2y", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return out
        dates: list[str] = []
        closes: list[float] = []
        for dt, row in hist.iterrows():
            c = row.get("Close") or row.get("Adj Close")
            if c is None or c != c:
                continue
            dates.append(dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10])
            closes.append(float(c))
        series = wilder_rsi(closes, period)
        last = next((v for v in reversed(series) if v is not None), None)
        out["rsi"] = last
        if with_series:
            out["rsi_trend"] = [
                {"date": d, "rsi": v} for d, v in zip(dates, series) if v is not None
            ]
    except Exception as e:
        logger.debug("calc_rsi_metrics failed: %s", e)
    return out


def _norm_ccy(code: Any) -> str:
    c = str(code or "").strip().upper()
    if not c or c in ("NONE", "NULL", "N/A", "NA"):
        return ""
    # yfinance sometimes returns GBp (pence) for LSE — treat as GBP for FX pairs
    if c == "GBP":
        return "GBP"
    if c == "GBX":
        return "GBP"
    return c


def resolve_currencies(info: dict | None = None, ticker: str = "") -> tuple[str, str]:
    """
    Return (quote_currency, financial_currency).

    quote_currency: trading / market-cap currency (info.currency, else ticker suffix).
    financial_currency: balance-sheet / cash-flow units (info.financialCurrency).
    When financialCurrency is missing, assume it matches quote.
    """
    info = info or {}
    quote = _norm_ccy(info.get("currency"))
    fin = _norm_ccy(info.get("financialCurrency"))
    if not quote:
        # Fallback suffixes (keep light — full map lives in asx.infer_currency)
        sym = (ticker or "").upper()
        for sfx, cur in (
            (".AX", "AUD"), (".T", "JPY"), (".HK", "HKD"), (".L", "GBP"),
            (".TO", "CAD"), (".V", "CAD"), (".NS", "INR"), (".BO", "INR"),
            (".SW", "CHF"), (".PA", "EUR"), (".DE", "EUR"), (".MI", "EUR"),
            (".KS", "KRW"), (".KQ", "KRW"), (".SI", "SGD"), (".NZ", "NZD"),
            (".SA", "BRL"), (".MX", "MXN"), (".TW", "TWD"),
        ):
            if sym.endswith(sfx):
                quote = cur
                break
        if not quote:
            quote = "USD"
    if not fin:
        fin = quote
    return quote, fin


def get_fx_rate(from_ccy: str, to_ccy: str) -> float | None:
    """
    Units of *to_ccy* per 1 *from_ccy* (multiply financial amounts by this to get quote units).
    Uses Yahoo FX pairs via yfinance; cached ~1h.
    """
    a = _norm_ccy(from_ccy)
    b = _norm_ccy(to_ccy)
    if not a or not b:
        return None
    if a == b:
        return 1.0

    now = time.monotonic()
    cached = _FX_CACHE.get((a, b))
    if cached and (now - cached[1]) < _FX_CACHE_TTL_SEC:
        return cached[0]
    inv = _FX_CACHE.get((b, a))
    if inv and (now - inv[1]) < _FX_CACHE_TTL_SEC and inv[0]:
        rate = 1.0 / inv[0]
        _FX_CACHE[(a, b)] = (rate, now)
        return rate

    rate = _fetch_fx_rate_yahoo(a, b)
    if rate and rate > 0:
        _FX_CACHE[(a, b)] = (rate, now)
        _FX_CACHE[(b, a)] = (1.0 / rate, now)
        return rate
    return None


def _fetch_fx_rate_yahoo(from_ccy: str, to_ccy: str) -> float | None:
    try:
        import yfinance as yf
    except Exception:
        return None

    pairs = [f"{from_ccy}{to_ccy}=X", f"{to_ccy}{from_ccy}=X"]
    for i, pair in enumerate(pairs):
        try:
            t = yf.Ticker(pair)
            hist = t.history(period="5d")
            px = None
            if hist is not None and not hist.empty:
                px = float(hist["Close"].dropna().iloc[-1])
            if not px or px <= 0:
                info = t.info or {}
                px = safe_float(info.get("regularMarketPrice")) or safe_float(info.get("previousClose"))
            if px and px > 0:
                # first pair is from→to; second is inverse
                return px if i == 0 else (1.0 / px)
        except Exception as e:
            logger.debug("FX fetch %s failed: %s", pair, e)
    return None


def financial_to_quote_fx(info: dict | None = None, ticker: str = "") -> dict[str, Any]:
    """
    Scale factor to convert yfinance *financial* amounts into *quote* (market-cap) currency.

    Yahoo often reports marketCap in trading currency (e.g. USD for PDD) while
    totalCash / totalDebt / freeCashflow stay in financialCurrency (e.g. CNY).
    """
    quote, fin = resolve_currencies(info, ticker)
    if quote == fin:
        return {
            "quote_currency": quote,
            "financial_currency": fin,
            "fx_to_quote": 1.0,
            "currency_converted": False,
            "fx_error": None,
        }
    rate = get_fx_rate(fin, quote)
    if rate is None or rate <= 0:
        logger.warning(
            "FX unavailable %s→%s for %s — leaving financials unconverted",
            fin, quote, ticker or "?",
        )
        return {
            "quote_currency": quote,
            "financial_currency": fin,
            "fx_to_quote": 1.0,
            "currency_converted": False,
            "fx_error": f"no_fx:{fin}->{quote}",
        }
    return {
        "quote_currency": quote,
        "financial_currency": fin,
        "fx_to_quote": rate,
        "currency_converted": True,
        "fx_error": None,
    }


def align_financial_amount(amount: float, info: dict | None = None, ticker: str = "") -> float:
    """Convert one financialCurrency amount into quote currency."""
    fx = financial_to_quote_fx(info, ticker)
    return safe_float(amount) * safe_float(fx.get("fx_to_quote"), 1.0)


def calc_p_fcf(info: dict, price: float, ticker: str = "") -> float:
    """Price / free-cash-flow per share; FCF converted to quote currency when needed."""
    info = info or {}
    try:
        fcf = align_financial_amount(info.get("freeCashflow"), info, ticker)
        shares = safe_float(info.get("sharesOutstanding")) or safe_float(
            info.get("impliedSharesOutstanding")
        )
        if price and fcf and shares > 0:
            fps = fcf / shares
            if fps > 0:
                return safe_round(price / fps, 1)
    except Exception:
        pass
    return 0


def calc_enterprise_value(mcap: float, total_cash: float = 0, total_debt: float = 0) -> float:
    """EV = market cap − cash + debt (all amounts must already share one currency)."""
    m = safe_float(mcap)
    if m <= 0:
        return 0.0
    return m - safe_float(total_cash) + safe_float(total_debt)


def classify_fcf_quality(
    fcf: float = 0,
    ocf: float = 0,
    capex: float = 0,
) -> dict:
    """
    Coarse free-data heuristic: how much operating cash converts to FCF after capex.

    Not owner earnings / maintenance capex. Uses yfinance TTM-style OCF, FCF, and
    capitalExpenditures (spend as a positive number).

    Labels:
      solid       — FCF > 0 and FCF/OCF ≥ 0.70 (most OCF becomes FCF; maintenance-ish or light growth)
      mixed       — FCF > 0 and 0.40 ≤ FCF/OCF < 0.70
      capex_heavy — FCF > 0 and FCF/OCF < 0.40 (or capex/OCF ≥ 0.60 with thin conversion)
      weak        — OCF > 0 but FCF ≤ 0 (capex + WC absorb cash)
      unknown     — missing OCF/FCF inputs
    """
    fcf = safe_float(fcf)
    ocf = safe_float(ocf)
    # Yahoo often reports capitalExpenditures as negative cash outflow
    capex_spend = abs(safe_float(capex))

    fcf_ocf = None
    capex_ocf = None
    if ocf > 0:
        fcf_ocf = fcf / ocf
        if capex_spend > 0:
            capex_ocf = capex_spend / ocf

    label = "unknown"
    if ocf > 0:
        if fcf <= 0:
            label = "weak"
        elif fcf_ocf is not None and fcf_ocf >= 0.70:
            label = "solid"
        elif fcf_ocf is not None and fcf_ocf >= 0.40:
            label = "mixed"
        elif fcf_ocf is not None:
            label = "capex_heavy"
        elif capex_ocf is not None and capex_ocf >= 0.60:
            label = "capex_heavy"
    elif fcf < 0 and ocf <= 0:
        label = "weak"
    elif fcf > 0 and ocf <= 0:
        # Have FCF but no OCF in feed — cannot grade conversion
        label = "unknown"

    return {
        "fcf_quality": label,
        "fcf_ocf_ratio": safe_round(fcf_ocf, 2) if fcf_ocf is not None else None,
        "capex_ocf_ratio": safe_round(capex_ocf, 2) if capex_ocf is not None else None,
    }


def calc_fcf_metrics(
    info: dict,
    mcap: float = 0,
    enterprise_value: float = 0,
    ticker: str = "",
    fcf_override: float | None = None,
    ocf_override: float | None = None,
    capex_override: float | None = None,
) -> dict:
    """
    Free-cash-flow derived metrics from yfinance .info.
    freeCashflow is TTM-style when present; not maintenance-adjusted "true FCF".
    FCF/OCF/capex are converted to quote currency unless overrides are provided.
    """
    info = info or {}
    if fcf_override is not None:
        fcf = safe_float(fcf_override)
    else:
        fcf = align_financial_amount(info.get("freeCashflow"), info, ticker)
    if ocf_override is not None:
        ocf = safe_float(ocf_override)
    else:
        ocf = align_financial_amount(info.get("operatingCashflow"), info, ticker)
    if capex_override is not None:
        capex_raw = safe_float(capex_override)
    else:
        capex_raw = align_financial_amount(info.get("capitalExpenditures"), info, ticker)
    # Spend as positive millions for display
    capex_spend = abs(capex_raw) if capex_raw else 0.0

    mcap = safe_float(mcap) or safe_float(info.get("marketCap"))
    ev = safe_float(enterprise_value)
    quality = classify_fcf_quality(fcf=fcf, ocf=ocf, capex=capex_raw)
    out = {
        "fcf_m": safe_round(fcf / 1_000_000, 1) if fcf else 0,
        "ocf_m": safe_round(ocf / 1_000_000, 1) if ocf else 0,
        "capex_m": safe_round(capex_spend / 1_000_000, 1) if capex_spend else 0,
        "fcf_yield": 0.0,
        "ev_fcf": 0.0,
        "fcf_quality": quality["fcf_quality"],
        "fcf_ocf_ratio": quality["fcf_ocf_ratio"],
        "capex_ocf_ratio": quality["capex_ocf_ratio"],
    }
    if fcf > 0 and mcap > 0:
        out["fcf_yield"] = safe_round(fcf / mcap * 100, 2)
    if fcf > 0 and ev > 0:
        out["ev_fcf"] = safe_round(ev / fcf, 1)
    return out


def calc_leverage_metrics(
    total_debt: float = 0,
    total_cash: float = 0,
    mcap: float = 0,
    enterprise_value: float = 0,
    ev_ebitda: float = 0,
    net_assets: float = 0,
    price_to_book: float = 0,
) -> dict:
    """
    Templeton-style leverage yardsticks (some estimated).

    - net_debt_to_equity: net debt ÷ book equity (equity from net_assets or mcap/P/B)
    - debt_to_ebitda: total debt ÷ estimated TTM EBITDA (EBITDA ≈ EV / EV_EBITDA)
    """
    total_debt = safe_float(total_debt)
    total_cash = safe_float(total_cash)
    mcap = safe_float(mcap)
    ev = safe_float(enterprise_value)
    ev_ebitda = safe_float(ev_ebitda)
    net_assets = safe_float(net_assets)
    pb = safe_float(price_to_book)

    net_debt = total_debt - total_cash
    out = {
        "net_debt_to_equity": 0.0,
        "ebitda_est": 0.0,
        "ebitda_est_m": 0.0,
        "debt_to_ebitda": 0.0,
    }

    book_equity = net_assets
    if book_equity <= 0 and mcap > 0 and pb > 0:
        book_equity = mcap / pb
    if book_equity > 0:
        out["net_debt_to_equity"] = safe_round(net_debt / book_equity, 2)

    # Estimate EBITDA from EV multiple when Yahoo EV/EBITDA is present and positive
    if ev > 0 and ev_ebitda > 0:
        ebitda = ev / ev_ebitda
        out["ebitda_est"] = ebitda
        out["ebitda_est_m"] = safe_round(ebitda / 1_000_000, 1)
        if ebitda > 0 and total_debt > 0:
            out["debt_to_ebitda"] = safe_round(total_debt / ebitda, 2)
    return out


def normalize_growth_rate(val) -> float | None:
    """
    yfinance earningsGrowth is often a decimal (0.12 = 12%).
    EDGAR multi-year growth is already in percent points.
    Returns growth as a decimal fraction (0.12), capped.
    """
    g = safe_float(val, None)
    if g is None:
        return None
    # Heuristic: values > 1.5 are almost certainly percent points (e.g. 12 or 150)
    if abs(g) > 1.5:
        g = g / 100.0
    # Cap extreme analyst growth for long-horizon P/E estimates
    if g < -0.05:
        g = -0.05
    if g > 0.25:
        g = 0.25
    return g


def quote_price(info: dict) -> float:
    info = info or {}
    return safe_float(info.get("currentPrice")) or safe_float(info.get("regularMarketPrice"))
