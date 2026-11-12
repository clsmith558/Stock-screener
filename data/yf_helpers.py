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
    hist=None,
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
    if yt is None and hist is None:
        return empty
    price = safe_float(price)
    pb = safe_float(pb)
    p_fcf = safe_float(p_fcf)
    fcf_yield = safe_float(fcf_yield)
    if price <= 0:
        return empty
    try:
        if hist is None:
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


def calc_last_52w_low_metrics(yt, trading_days: int = 252, *, hist=None) -> dict:
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
    if yt is None and hist is None:
        return out
    try:
        if hist is None:
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


def build_price_trend(yt, period: str = "2y", interval: str = "1wk", *, hist=None) -> list:
    """Weekly/daily closes for interactive price chart in detail modal."""
    price_trend = []
    try:
        if hist is None:
            hist = yt.history(period=period, interval=interval, auto_adjust=True)
        elif period == "2y" and interval == "1wk" and len(hist) > 105:
            hist = hist.tail(105)
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


def calc_rsi_metrics(yt, period: int = RSI_PERIOD, with_series: bool = False, *, hist=None) -> dict:
    """14-day RSI from ~2y daily auto-adjusted closes."""
    out: dict[str, Any] = {"rsi": None, "rsi_period": period}
    try:
        if hist is None:
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
            (".HE", "EUR"), (".AT", "EUR"), (".KS", "KRW"), (".KQ", "KRW"),
            (".SI", "SGD"), (".NZ", "NZD"), (".SA", "BRL"), (".MX", "MXN"),
            (".TW", "TWD"), (".JK", "IDR"), (".SS", "CNY"), (".SZ", "CNY"),
            (".KL", "MYR"), (".BK", "THB"), (".VN", "VND"), (".IS", "TRY"),
            (".OL", "NOK"), (".IC", "ISK"), (".TA", "ILS"),
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


def calc_p_fcf(
    info: dict,
    price: float,
    ticker: str = "",
    *,
    fx_to_quote: float | None = None,
) -> float:
    """Price / free-cash-flow per share; FCF converted to quote currency when needed."""
    info = info or {}
    try:
        if fx_to_quote is not None:
            fcf = safe_float(info.get("freeCashflow")) * safe_float(fx_to_quote, 1.0)
        else:
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


def calc_ephemeral_point_in_time_metrics(
    info: dict,
    price: float,
    *,
    mcap: float = 0.0,
    ocf_raw: float = 0.0,
    fx_scale: float = 1.0,
) -> dict[str, Any]:
    """
    Extract ephemeral point-in-time metrics from yfinance `.info` that get overwritten
    every month (short interest, days to cover, consensus EPS trajectory, analyst neglect,
    street price targets, margins, and Sloan accruals).
    """
    info = info or {}
    price = safe_float(price)
    mcap = safe_float(mcap) or safe_float(info.get("marketCap"))
    fx_scale = safe_float(fx_scale, 1.0) or 1.0

    # 1. Short Interest & Positioning
    raw_short_pct = safe_float(info.get("shortPercentOfFloat"), None)
    short_pct_float = None
    if raw_short_pct is not None and raw_short_pct >= 0:
        # Yahoo usually returns decimal (0.085 = 8.5%), guard if already > 1.0
        pct_val = raw_short_pct * 100.0 if raw_short_pct <= 1.5 else raw_short_pct
        if 0 <= pct_val <= 150:
            short_pct_float = safe_round(pct_val, 2, None)

    short_ratio = safe_round(info.get("shortRatio"), 2, None)
    if short_ratio is not None and short_ratio <= 0:
        short_ratio = None

    shares_short = int(safe_float(info.get("sharesShort"), 0)) or None
    shares_short_prior = int(safe_float(info.get("sharesShortPriorMonth"), 0)) or None
    short_mom_change_pct = None
    if shares_short and shares_short_prior and shares_short_prior > 0:
        short_mom_change_pct = safe_round(
            ((shares_short - shares_short_prior) / shares_short_prior) * 100.0, 2, None
        )

    # 2. Consensus Expectations & Analyst Coverage (Templeton Neglect / Revision Inflection)
    forward_eps = safe_round(info.get("forwardEps"), 3, None)
    trailing_eps = safe_round(info.get("trailingEps"), 3, None)
    eps_forward_vs_trailing_pct = None
    if (
        forward_eps is not None
        and trailing_eps is not None
        and abs(trailing_eps) > 0.05
    ):
        raw_eps_chg = ((forward_eps - trailing_eps) / abs(trailing_eps)) * 100.0
        if -500.0 <= raw_eps_chg <= 1000.0:
            eps_forward_vs_trailing_pct = safe_round(raw_eps_chg, 1, None)

    analyst_count = int(safe_float(info.get("numberOfAnalystOpinions"), 0))
    recommendation_mean = safe_round(info.get("recommendationMean"), 2, None)
    target_mean_price = safe_round(info.get("targetMeanPrice"), 2, None)
    target_low_price = safe_round(info.get("targetLowPrice"), 2, None)
    upside_to_low_target_pct = None
    if price > 0 and target_low_price and target_low_price > 0:
        upside_to_low_target_pct = safe_round(
            ((target_low_price - price) / price) * 100.0, 1, None
        )

    # 3. Balance Sheet Survival, Margins & Sloan Accruals
    current_ratio = safe_round(info.get("currentRatio"), 2, None)
    quick_ratio = safe_round(info.get("quickRatio"), 2, None)
    gm_raw = safe_float(info.get("grossMargins"), None)
    gross_margins_pct = safe_round(gm_raw * 100.0, 1, None) if gm_raw is not None else None
    om_raw = safe_float(info.get("operatingMargins"), None)
    operating_margins_pct = safe_round(om_raw * 100.0, 1, None) if om_raw is not None else None
    rev_g_raw = safe_float(info.get("revenueGrowth"), None)
    revenue_growth_pct = safe_round(rev_g_raw * 100.0, 1, None) if rev_g_raw is not None else None
    inst_raw = safe_float(info.get("heldPercentInstitutions"), None)
    held_pct_institutions = safe_round(inst_raw * 100.0, 2, None) if inst_raw is not None else None

    # Sloan Accrual proxy: (Net Income - OCF) / Market Cap %
    # Negative = OCF exceeds Net Income (high cash conversion); high positive = accrual bloat
    ni_raw = safe_float(info.get("netIncomeToCommon"), None)
    if ocf_raw == 0.0:
        ocf_raw = safe_float(info.get("operatingCashflow"), 0.0) * fx_scale
    sloan_accrual_pct_mcap = None
    if ni_raw is not None and ocf_raw != 0.0 and mcap > 0:
        ni_quote = ni_raw * fx_scale
        accrual_pct = ((ni_quote - ocf_raw) / mcap) * 100.0
        if -250.0 <= accrual_pct <= 250.0:
            sloan_accrual_pct_mcap = safe_round(accrual_pct, 2, None)

    return {
        "short_pct_float": short_pct_float,
        "short_ratio": short_ratio,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_mom_change_pct": short_mom_change_pct,
        "forward_eps": forward_eps,
        "trailing_eps": trailing_eps,
        "eps_forward_vs_trailing_pct": eps_forward_vs_trailing_pct,
        "analyst_count": analyst_count,
        "recommendation_mean": recommendation_mean,
        "target_mean_price": target_mean_price,
        "target_low_price": target_low_price,
        "upside_to_low_target_pct": upside_to_low_target_pct,
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "gross_margins_pct": gross_margins_pct,
        "operating_margins_pct": operating_margins_pct,
        "revenue_growth_pct": revenue_growth_pct,
        "held_pct_institutions": held_pct_institutions,
        "sloan_accrual_pct_mcap": sloan_accrual_pct_mcap,
    }


def calc_microstructure_and_forensics(
    yt,
    info: dict,
    *,
    price: float = 0.0,
    mcap: float = 0.0,
    fcf_raw: float = 0.0,
    fx_scale: float = 1.0,
    hist_1d=None,
    include_statements: bool = False,
) -> dict[str, Any]:
    """
    Compute execution microstructure metrics from daily history (`median_dollar_vol_20d`,
    `volatility_60d_ann_pct`, `max_drawdown_1y_pct`) at zero extra network cost, plus
    optional deep financial-statement forensics (`sbc_adjusted_fcf_yield`, `capex_to_da_ratio`,
    `shares_yoy_change_pct`, `interest_coverage`, `piotroski_f_score`) when `include_statements=True`.
    """
    import math

    info = info or {}
    price = safe_float(price) or quote_price(info)
    mcap = safe_float(mcap) or safe_float(info.get("marketCap"))
    fx_scale = safe_float(fx_scale, 1.0) or 1.0

    median_dollar_vol_20d = None
    volatility_60d_ann_pct = None
    max_drawdown_1y_pct = None

    try:
        if hist_1d is not None and not hist_1d.empty and "Close" in hist_1d.columns:
            closes = hist_1d["Close"].dropna()
            if "Volume" in hist_1d.columns:
                tail20 = hist_1d[["Close", "Volume"]].dropna().tail(20)
                if not tail20.empty:
                    dvols = (tail20["Close"] * tail20["Volume"]).tolist()
                    dvols = [float(v) for v in dvols if v and float(v) > 0]
                    if dvols:
                        dvols.sort()
                        median_dollar_vol_20d = safe_round(dvols[len(dvols) // 2], 0, None)
            if len(closes) >= 20:
                rets = closes.pct_change().dropna().tail(60)
                if len(rets) >= 15:
                    std_d = float(rets.std())
                    if math.isfinite(std_d) and std_d > 0:
                        volatility_60d_ann_pct = safe_round(std_d * math.sqrt(252.0) * 100.0, 1, None)
            if len(closes) >= 30:
                w252 = closes.tail(252)
                roll_max = w252.cummax()
                dd = ((w252 - roll_max) / roll_max).min()
                if dd is not None and math.isfinite(float(dd)):
                    max_drawdown_1y_pct = safe_round(float(dd) * 100.0, 1, None)
    except Exception:
        pass

    if median_dollar_vol_20d is None and price > 0:
        avg_vol = safe_float(info.get("averageVolume20days")) or safe_float(info.get("averageVolume"))
        if avg_vol and avg_vol > 0:
            median_dollar_vol_20d = safe_round(price * avg_vol, 0, None)

    max_pos_usd_5pct_adv = None
    if median_dollar_vol_20d and median_dollar_vol_20d > 0:
        max_pos_usd_5pct_adv = safe_round(median_dollar_vol_20d * 0.05, 0, None)

    # Net Debt / EBITDA from info when present
    total_debt = safe_float(info.get("totalDebt"), 0.0) * fx_scale
    total_cash = safe_float(info.get("totalCash"), 0.0) * fx_scale
    ebitda_info = safe_float(info.get("ebitda"), 0.0) * fx_scale
    net_debt_to_ebitda = None
    if ebitda_info > 0 and (total_debt > 0 or total_cash > 0):
        net_debt_to_ebitda = safe_round((total_debt - total_cash) / ebitda_info, 2, None)

    out: dict[str, Any] = {
        "median_dollar_vol_20d": median_dollar_vol_20d,
        "max_pos_usd_5pct_adv": max_pos_usd_5pct_adv,
        "volatility_60d_ann_pct": volatility_60d_ann_pct,
        "max_drawdown_1y_pct": max_drawdown_1y_pct,
        "net_debt_to_ebitda": net_debt_to_ebitda,
        "forward_eps_current_yr": None,
        "forward_eps_next_yr": None,
        "eps_rev_30d_pct": None,
        "eps_rev_90d_pct": None,
        "sbc_m": None,
        "sbc_adjusted_fcf_yield": None,
        "capex_to_da_ratio": None,
        "shares_yoy_change_pct": None,
        "shares_3y_cagr_pct": None,
        "interest_coverage": None,
        "current_ratio_yoy_delta": None,
        "gross_margin_yoy_delta": None,
        "inventory_to_sales_delta_pct": None,
        "piotroski_f_score": None,
    }

    if not include_statements or yt is None:
        return out

    # Step 1: Consensus EPS Estimate Trend (`0y` and `+1y` vs 30d/90d ago)
    try:
        et = getattr(yt, "eps_trend", None)
        if et is not None and not getattr(et, "empty", True):
            idx_str = {str(k).strip().lower(): k for k in et.index}
            r_0y = idx_str.get("0y")
            r_1y = idx_str.get("+1y")
            if r_0y is not None and "current" in et.columns:
                out["forward_eps_current_yr"] = safe_round(et.loc[r_0y, "current"], 3, None)
            if r_1y is not None and "current" in et.columns:
                out["forward_eps_next_yr"] = safe_round(et.loc[r_1y, "current"], 3, None)
            # Prefer +1y or 0y row for 30d and 90d revision drift
            rev_row = r_1y if r_1y is not None else r_0y
            if rev_row is not None and "current" in et.columns:
                cur_eps = safe_float(et.loc[rev_row, "current"], None)
                if cur_eps is not None and "30daysAgo" in et.columns:
                    e30 = safe_float(et.loc[rev_row, "30daysAgo"], None)
                    if e30 is not None and abs(e30) > 0.02:
                        out["eps_rev_30d_pct"] = safe_round(((cur_eps - e30) / abs(e30)) * 100.0, 2, None)
                if cur_eps is not None and "90daysAgo" in et.columns:
                    e90 = safe_float(et.loc[rev_row, "90daysAgo"], None)
                    if e90 is not None and abs(e90) > 0.02:
                        out["eps_rev_90d_pct"] = safe_round(((cur_eps - e90) / abs(e90)) * 100.0, 2, None)
    except Exception as ex:
        logger.debug("eps_trend extraction skipped: %s", ex)

    try:
        cf = getattr(yt, "cashflow", None)
        inc = getattr(yt, "financials", None)
        bs = getattr(yt, "balance_sheet", None)

        def _row_vals(df, labels: tuple[str, ...]) -> list[float]:
            if df is None or getattr(df, "empty", True):
                return []
            idx_map = {str(k).strip().lower(): k for k in df.index}
            for lbl in labels:
                k = idx_map.get(lbl.lower())
                if k is not None:
                    s = df.loc[k].dropna()
                    return [float(x) for x in s.tolist() if x is not None and math.isfinite(float(x))]
            return []

        # 1. Stock-Based Compensation & True Owner FCF Yield
        sbc_vals = _row_vals(cf, ("Stock Based Compensation", "Share Based Compensation"))
        ocf_vals = _row_vals(cf, ("Operating Cash Flow", "Total Cash From Operating Activities"))
        capex_vals = _row_vals(cf, ("Capital Expenditure", "Capital Expenditures"))
        da_vals = _row_vals(
            cf,
            (
                "Depreciation And Amortization",
                "Depreciation Amortization Depletion",
                "Depreciation",
            ),
        )
        if not da_vals:
            da_vals = _row_vals(inc, ("Reconciled Depreciation", "Depreciation And Amortization"))

        if fcf_raw == 0.0:
            fcf_raw = safe_float(info.get("freeCashflow"), 0.0) * fx_scale
            if fcf_raw == 0.0 and ocf_vals and capex_vals:
                fcf_raw = (ocf_vals[0] + min(0.0, capex_vals[0])) * fx_scale

        if sbc_vals:
            sbc_quote = max(0.0, sbc_vals[0]) * fx_scale
            out["sbc_m"] = safe_round(sbc_quote / 1_000_000.0, 1, None)
            if mcap > 0 and fcf_raw != 0.0:
                out["sbc_adjusted_fcf_yield"] = safe_round(
                    ((fcf_raw - sbc_quote) / mcap) * 100.0, 2, None
                )
        elif mcap > 0 and fcf_raw != 0.0:
            out["sbc_adjusted_fcf_yield"] = safe_round((fcf_raw / mcap) * 100.0, 2, None)

        # 2. Capital-Cycle Supply Starvation: |Capex| / D&A
        if capex_vals and da_vals and da_vals[0] > 0:
            out["capex_to_da_ratio"] = safe_round(abs(capex_vals[0]) / da_vals[0], 2, None)

        # 3. 1Y & 3Y Share Count Cannibalization / Dilution Velocity
        sh_vals = _row_vals(
            bs,
            ("Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"),
        )
        if len(sh_vals) >= 2 and sh_vals[1] > 0:
            out["shares_yoy_change_pct"] = safe_round(
                ((sh_vals[0] - sh_vals[1]) / sh_vals[1]) * 100.0, 2, None
            )
        if len(sh_vals) >= 4 and sh_vals[3] > 0 and sh_vals[0] > 0:
            out["shares_3y_cagr_pct"] = safe_round(
                (((sh_vals[0] / sh_vals[3]) ** (1.0 / 3.0)) - 1.0) * 100.0, 2, None
            )
        elif len(sh_vals) >= 3 and sh_vals[2] > 0 and sh_vals[0] > 0:
            out["shares_3y_cagr_pct"] = safe_round(
                (((sh_vals[0] / sh_vals[2]) ** (1.0 / 2.0)) - 1.0) * 100.0, 2, None
            )

        # 4. Interest Coverage (EBIT / Interest Expense)
        ebit_vals = _row_vals(inc, ("EBIT", "Operating Income"))
        int_vals = _row_vals(inc, ("Interest Expense", "Interest Expense Non Operating", "Net Interest Income"))
        if ebit_vals and int_vals and abs(int_vals[0]) > 0:
            out["interest_coverage"] = safe_round(ebit_vals[0] / abs(int_vals[0]), 2, None)

        # 5. Piotroski F-Score (0–9) + YoY Current Ratio & Gross Margin Deltas + Inventory-to-Sales Velocity
        ni_vals = _row_vals(inc, ("Net Income", "Net Income Common Stockholders"))
        ta_vals = _row_vals(bs, ("Total Assets",))
        ltd_vals = _row_vals(bs, ("Long Term Debt", "Long Term Debt And Capital Lease Obligation", "Total Debt"))
        ca_vals = _row_vals(bs, ("Current Assets", "Total Current Assets"))
        cl_vals = _row_vals(bs, ("Current Liabilities", "Total Current Liabilities"))
        gp_vals = _row_vals(inc, ("Gross Profit",))
        rev_vals = _row_vals(inc, ("Total Revenue", "Operating Revenue"))
        inv_vals = _row_vals(bs, ("Inventory", "Total Inventory", "Inventories"))

        if len(ca_vals) >= 2 and len(cl_vals) >= 2 and cl_vals[0] > 0 and cl_vals[1] > 0:
            cr_0 = ca_vals[0] / cl_vals[0]
            cr_1 = ca_vals[1] / cl_vals[1]
            out["current_ratio_yoy_delta"] = safe_round(cr_0 - cr_1, 2, None)

        if len(gp_vals) >= 2 and len(rev_vals) >= 2 and rev_vals[0] > 0 and rev_vals[1] > 0:
            gm_0 = (gp_vals[0] / rev_vals[0]) * 100.0
            gm_1 = (gp_vals[1] / rev_vals[1]) * 100.0
            out["gross_margin_yoy_delta"] = safe_round(gm_0 - gm_1, 2, None)

        if len(inv_vals) >= 2 and len(rev_vals) >= 2 and inv_vals[1] > 0 and rev_vals[1] > 0:
            inv_yoy = ((inv_vals[0] - inv_vals[1]) / inv_vals[1]) * 100.0
            rev_yoy = ((rev_vals[0] - rev_vals[1]) / rev_vals[1]) * 100.0
            inv_sales_diff = inv_yoy - rev_yoy
            if -300.0 <= inv_sales_diff <= 300.0:
                out["inventory_to_sales_delta_pct"] = safe_round(inv_sales_diff, 1, None)

        if ni_vals and ta_vals and ta_vals[0] > 0:
            f_score = 0
            roa_0 = ni_vals[0] / ta_vals[0]
            ocf_0 = ocf_vals[0] if ocf_vals else 0.0
            # (1) Positive ROA
            if roa_0 > 0:
                f_score += 1
            # (2) Positive OCF
            if ocf_0 > 0:
                f_score += 1
            # (3) Delta ROA > 0
            if len(ni_vals) >= 2 and len(ta_vals) >= 2 and ta_vals[1] > 0:
                roa_1 = ni_vals[1] / ta_vals[1]
                if roa_0 > roa_1:
                    f_score += 1
            # (4) Accruals: OCF > Net Income
            if ocf_0 > ni_vals[0]:
                f_score += 1
            # (5) Delta Leverage <= 0
            if len(ltd_vals) >= 2 and len(ta_vals) >= 2 and ta_vals[1] > 0:
                if (ltd_vals[0] / ta_vals[0]) <= (ltd_vals[1] / ta_vals[1]):
                    f_score += 1
            elif not ltd_vals or ltd_vals[0] == 0:
                f_score += 1
            # (6) Delta Current Ratio > 0
            if len(ca_vals) >= 2 and len(cl_vals) >= 2 and cl_vals[0] > 0 and cl_vals[1] > 0:
                if (ca_vals[0] / cl_vals[0]) > (ca_vals[1] / cl_vals[1]):
                    f_score += 1
            # (7) No Net Share Dilution
            if len(sh_vals) >= 2 and sh_vals[0] <= sh_vals[1] * 1.005:
                f_score += 1
            # (8) Delta Gross Margin > 0
            if len(gp_vals) >= 2 and len(rev_vals) >= 2 and rev_vals[0] > 0 and rev_vals[1] > 0:
                if (gp_vals[0] / rev_vals[0]) > (gp_vals[1] / rev_vals[1]):
                    f_score += 1
            # (9) Delta Asset Turnover > 0
            if len(rev_vals) >= 2 and len(ta_vals) >= 2 and ta_vals[1] > 0:
                if (rev_vals[0] / ta_vals[0]) > (rev_vals[1] / ta_vals[1]):
                    f_score += 1
            out["piotroski_f_score"] = int(f_score)
    except Exception as ex:
        logger.debug("Statement forensics skipped: %s", ex)

    return out


# ---------------------- SHARED LIVE YFINANCE SNAPSHOT & DETAIL OVERLAYS (2A) ----------------------


def fetch_yf_ticker_snapshot(
    yf_symbol: str,
    *,
    ticker_for_fx: str | None = None,
    include_detail: bool = False,
) -> dict[str, Any]:
    """
    Fetch one ticker's live yfinance quote + history + derived metrics in a single pass.

    Used by both sequential cache builders (`include_detail=False`) and single-stock detail
    endpoints (`include_detail=True`, which adds `summary`, `price_trend`, `rsi_trend`,
    and `earnings_history`).
    """
    import yfinance as yf
    from data.asx import (
        _build_earnings_history,
        _calc_last_52w_low,
        _calc_ma200w_gap,
        _metrics_from_yf_info,
        infer_currency,
    )

    sym = (yf_symbol or "").strip().upper()
    fx_sym = (ticker_for_fx or sym).strip().upper()
    yt = yf.Ticker(sym)
    info = yt.info or {}
    price = quote_price(info)

    if price <= 0 and not include_detail:
        return {
            "yt": yt,
            "info": info,
            "price": 0.0,
            "low_52w": 0.0,
            "high_52w": 0.0,
            "pct_from_52w_low": 0.0,
            "p_fcf": 0.0,
            "ma_200w": 0.0,
            "pct_from_200w_ma": 0.0,
            "metrics": {},
        }

    raw_low = safe_float(info.get("fiftyTwoWeekLow"))
    raw_high = safe_float(info.get("fiftyTwoWeekHigh"))
    low, high = reconcile_52w_range(price, raw_low, raw_high)
    pct_52w = pct_from_52w_low(price, low)
    p_fcf = calc_p_fcf(info, price, ticker=fx_sym)

    try:
        hist_1wk = yt.history(period="5y", interval="1wk", auto_adjust=True)
    except Exception:
        hist_1wk = None
    try:
        hist_1d = yt.history(period="2y", interval="1d", auto_adjust=True)
    except Exception:
        hist_1d = None

    ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price, hist=hist_1wk)
    low52 = _calc_last_52w_low(yt, hist=hist_1d)
    metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, p_fcf, ticker=fx_sym)
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
            hist=hist_1wk,
        )
    )
    rsi_pack = calc_rsi_metrics(yt, with_series=include_detail, hist=hist_1d)
    metrics["rsi"] = rsi_pack.get("rsi")
    metrics["rsi_period"] = rsi_pack.get("rsi_period")

    # Attach microstructure & optional statement forensics
    micro_for = calc_microstructure_and_forensics(
        yt,
        info,
        price=price,
        mcap=safe_float(metrics.get("market_cap")),
        fcf_raw=safe_float(metrics.get("fcf_m")) * 1_000_000.0,
        fx_scale=safe_float(metrics.get("fx_to_quote"), 1.0) or 1.0,
        hist_1d=hist_1d,
        include_statements=include_detail,
    )
    for mk, mv in micro_for.items():
        if mv is not None or mk not in metrics:
            metrics[mk] = mv

    snap: dict[str, Any] = {
        "yt": yt,
        "info": info,
        "price": safe_round(price, 2) if price else 0,
        "low_52w": safe_round(low if include_detail else raw_low, 2) if (low if include_detail else raw_low) else 0,
        "high_52w": safe_round(high if include_detail else raw_high, 2) if (high if include_detail else raw_high) else 0,
        "pct_from_52w_low": pct_52w,
        "ma_200w": ma_200w,
        "pct_from_200w_ma": pct_from_200w_ma,
        "p_fcf": p_fcf,
        "name": info.get("shortName") or info.get("longName") or fx_sym,
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry") or info.get("industryDisp") or metrics.get("industry") or "",
        "currency": metrics.get("currency") or infer_currency(info, sym),
        "metrics": metrics,
    }
    if include_detail:
        snap["summary"] = business_summary(info)
        snap["price_trend"] = build_price_trend(yt, period="2y", interval="1wk", hist=hist_1wk)
        snap["rsi_trend"] = rsi_pack.get("rsi_trend") or []
        snap["earnings_history"] = _build_earnings_history(yt)
    return snap


def attach_sec_and_mining_detail(
    stock: dict,
    ticker: str,
    *,
    include_edgar: bool = True,
    include_sec_mining: bool = True,
    include_asx_mining: bool = False,
) -> dict:
    """
    Attach EDGAR fundamentals, bankruptcy/reorg, mining reserves, mining unit costs,
    optional ASX mining summary, and multi-year earnings multiples onto a single-stock detail dict.
    """
    sym = (ticker or stock.get("ticker") or "").strip().upper()
    if include_edgar and sym:
        try:
            from data.edgar_metrics import attach_edgar_to_stock, fetch_edgar_metrics, is_us_edgar_eligible

            if is_us_edgar_eligible(sym):
                stock["_edgar_eligible"] = True
                edgar = fetch_edgar_metrics(sym)
                attach_edgar_to_stock(stock, edgar=edgar, ticker=sym)
            else:
                stock["_edgar_eligible"] = False
        except Exception as ex:
            logger.warning("EDGAR detail fetch failed for %s: %s", sym, ex)

    if include_sec_mining and sym:
        try:
            from data.bankruptcy_reorg import attach_reorg_to_stock

            attach_reorg_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Reorg attach skipped for %s: %s", sym, ex)
        try:
            from data.mining_reserves import attach_reserves_to_stock

            attach_reserves_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Reserves attach skipped for %s: %s", sym, ex)
        try:
            from data.mining_costs import attach_costs_to_stock

            attach_costs_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("Unit-cost attach skipped for %s: %s", sym, ex)

    if include_asx_mining and sym:
        try:
            from data.asx_mining import attach_asx_mining_to_stock, get_asx_mining_summary

            if get_asx_mining_summary(sym, max_age_days=0):
                attach_asx_mining_to_stock(stock, ticker=sym)
        except Exception as ex:
            logger.debug("ASX mining attach skipped for %s: %s", sym, ex)

    try:
        from data.earnings_multiples import attach_earnings_multiples

        attach_earnings_multiples(stock)
    except Exception as ex:
        logger.debug("earnings multiples skipped for %s: %s", sym, ex)

    return stock


# ---------------------- SHARED YF CACHE / INACTIVE / SEQUENTIAL FETCH PLUMBING (2B) ----------------------

TENTATIVE_INACTIVE_REASONS = frozenset({"cached_zero_price"})


def is_yf_fresh(last_updated: str, max_days: int = 30) -> bool:
    if not last_updated:
        return False
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(last_updated)
        return (datetime.now() - dt).days < max_days
    except Exception:
        return False


def detect_yf_inactive(info: dict, price: float) -> str:
    """
    Return a reason string if Yahoo data indicates this ticker is inactive/delisted.
    Uses .info only — never calls history (avoids duplicate 404 log spam).
    """
    info = info or {}
    price = safe_float(price)
    if price > 0:
        return ""

    has_name = bool(info.get("shortName") or info.get("longName"))
    has_symbol = bool(info.get("symbol") or info.get("underlyingSymbol"))
    rm = safe_float(info.get("regularMarketPrice"), None)
    cp = safe_float(info.get("currentPrice"), None)
    if (rm and rm > 0) or (cp and cp > 0):
        return ""

    if not info or len(info) <= 3:
        return "quote_not_found"
    if not has_name and not has_symbol:
        return "quote_not_found"
    return "zero_price"


def inactive_from_yf_error(msg: str) -> str:
    """Map yfinance exception text to inactive reason, or '' if likely transient."""
    m = (msg or "").lower()
    if any(x in m for x in ("rate", "too many", "429", "timed out", "timeout", "connection", "__round__")):
        return ""
    if any(
        x in m
        for x in (
            "404",
            "not found",
            "no data",
            "delisted",
            "invalid",
            "possibly delisted",
            "quote not found",
        )
    ):
        return "quote_not_found"
    return ""


def load_inactive_set_generic(
    conn_fn,
    table: str,
    key_col: str,
    *,
    db_path: str,
    confirmed_only: bool = False,
) -> set[str]:
    try:
        conn = conn_fn(db_path)
        if confirmed_only:
            ph = ",".join("?" for _ in TENTATIVE_INACTIVE_REASONS)
            cur = conn.execute(
                f"SELECT {key_col} FROM {table} WHERE reason NOT IN ({ph})",
                tuple(TENTATIVE_INACTIVE_REASONS),
            )
        else:
            cur = conn.execute(f"SELECT {key_col} FROM {table}")
        out = {row[0] for row in cur.fetchall()}
        conn.close()
        return out
    except Exception as e:
        logger.warning("load_inactive_set_generic(%s) failed: %s", table, e)
        return set()


def mark_inactive_generic(
    conn_fn,
    table: str,
    key_col: str,
    key_val: str,
    reason: str,
    *,
    source: str = "yf_fetch",
    db_path: str,
) -> None:
    if not key_val:
        return
    try:
        from datetime import datetime

        conn = conn_fn(db_path)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({key_col}, reason, marked_at, source) VALUES (?,?,?,?)",
            (key_val, reason or "unknown", datetime.now().isoformat(), source),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("mark_inactive_generic(%s, %s) failed: %s", table, key_val, e)


def remove_inactive_generic(
    conn_fn,
    table: str,
    key_col: str,
    key_val: str,
    *,
    db_path: str,
) -> None:
    if not key_val:
        return
    try:
        conn = conn_fn(db_path)
        conn.execute(f"DELETE FROM {table} WHERE {key_col}=?", (key_val,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("remove_inactive_generic(%s, %s) failed: %s", table, key_val, e)


def sync_inactive_from_cache_generic(
    conn_fn,
    cache_table: str,
    inactive_table: str,
    key_col: str,
    mark_fn,
    *,
    db_path: str,
) -> int:
    try:
        conn = conn_fn(db_path)
        cur = conn.execute(
            f"""
            SELECT c.{key_col} FROM {cache_table} c
            LEFT JOIN {inactive_table} i ON c.{key_col} = i.{key_col}
            WHERE (c.price IS NULL OR c.price <= 0) AND i.{key_col} IS NULL
            """
        )
        to_mark = [row[0] for row in cur.fetchall()]
        conn.close()
        for k in to_mark:
            mark_fn(k, "cached_zero_price", source="cache_scan", db_path=db_path)
        if to_mark:
            logger.info("Added %d zero-price rows from %s to %s", len(to_mark), cache_table, inactive_table)
        return len(to_mark)
    except Exception as e:
        logger.warning("sync_inactive_from_cache_generic(%s) failed: %s", cache_table, e)
        return 0


def load_yf_cache_generic(
    conn_fn,
    cache_table: str,
    key_col: str,
    keys: list[str],
    *,
    max_days: int = 30,
    db_path: str,
) -> dict[str, dict]:
    if not keys:
        return {}
    import json

    conn = conn_fn(db_path)
    ph = ",".join("?" for _ in keys)
    cur = conn.execute(
        f"""SELECT {key_col}, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf
            FROM {cache_table} WHERE {key_col} IN ({ph})""",
        keys,
    )
    out: dict[str, dict] = {}
    for row in cur.fetchall():
        k, last_up, price, low, high, name, sector, mjson, pf = row
        if is_yf_fresh(last_up, max_days):
            try:
                metrics = json.loads(mjson) if mjson else {}
            except Exception:
                metrics = {}
            out[k] = {
                "price": price or 0,
                "low_52w": low or 0,
                "high_52w": high or 0,
                "name": name or k,
                "sector": sector or "Unknown",
                "metrics": metrics,
                "p_fcf": pf or 0,
            }
    conn.close()
    return out


def save_yf_cache_generic(
    conn_fn,
    cache_table: str,
    key_col: str,
    key_val: str,
    data: dict,
    *,
    db_path: str,
) -> None:
    import json
    from datetime import datetime

    conn = conn_fn(db_path)
    metrics_json = json.dumps(data.get("metrics") or {})
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {cache_table}
        ({key_col}, last_updated, price, low_52w, high_52w, name, sector, metrics_json, p_fcf)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            key_val,
            datetime.now().isoformat(),
            data.get("price") or 0,
            data.get("low_52w") or 0,
            data.get("high_52w") or 0,
            data.get("name") or key_val,
            data.get("sector") or "Unknown",
            metrics_json,
            data.get("p_fcf") or 0,
        ),
    )
    conn.commit()
    conn.close()


def run_sequential_yf_fetch(
    keys: list[str],
    *,
    to_yf_symbol=lambda k: k,
    sleep_seconds: float = 2.0,
    progress_every: int = 50,
    retry_tentative_inactive: bool = False,
    progress_label: str = "yf sequential",
    on_progress=None,
    sync_inactive_fn,
    filter_active_fn,
    load_inactive_fn,
    mark_inactive_fn,
    remove_inactive_fn,
    save_cache_fn,
) -> dict[str, dict]:
    """
    Shared sequential, rate-limited yfinance fetcher used by both `data/asx.py`
    and `data/business_lists.py`.
    """
    sync_inactive_fn()
    keys = filter_active_fn(keys, confirmed_only=retry_tentative_inactive)
    inactive_skip = load_inactive_fn(confirmed_only=retry_tentative_inactive)
    out: dict[str, dict] = {}
    total = len(keys)
    if not total:
        logger.info("%s: no active symbols to fetch.", progress_label)
        return out

    if progress_every is None or progress_every < 1:
        progress_every = 10
    progress_every = min(progress_every, 25)
    est_min = (total * float(sleep_seconds)) / 60.0
    logger.info(
        "%s: starting %d tickers (sleep=%.1fs, ~%.0f min)",
        progress_label,
        total,
        sleep_seconds,
        est_min,
    )
    t0 = time.monotonic()
    done_ok = 0

    for i, k in enumerate(keys):
        n = i + 1
        if k in inactive_skip:
            if n == 1 or n % progress_every == 0 or n == total:
                logger.info("  %s progress: %d/%d (ok=%d) skip-inactive %s", progress_label, n, total, done_ok, k)
            continue

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
                k,
            )
            if on_progress:
                try:
                    on_progress(n, total)
                except Exception as ex:
                    logger.warning("on_progress callback failed: %s", ex)

        yf_sym = to_yf_symbol(k)
        try:
            snap = fetch_yf_ticker_snapshot(yf_sym, ticker_for_fx=k, include_detail=False)
            price = snap["price"]
            info = snap["info"]
            if price <= 0:
                reason = detect_yf_inactive(info, price) or "quote_not_found"
                mark_inactive_fn(k, reason, source="yf_fetch")
                inactive_skip.add(k)
                logger.info("  %s: inactive (%s) — added to skip list", k, reason)
                time.sleep(sleep_seconds)
                continue

            data = {
                "price": snap["price"],
                "low_52w": snap["low_52w"],
                "high_52w": snap["high_52w"],
                "name": snap["name"] or k,
                "sector": snap["sector"],
                "industry": snap["industry"],
                "metrics": snap["metrics"],
                "p_fcf": snap["p_fcf"],
            }
            out[k] = data
            save_cache_fn(k, data)
            remove_inactive_fn(k)
            inactive_skip.discard(k)
            done_ok += 1
            time.sleep(sleep_seconds)
        except Exception as e:
            msg = str(e)
            logger.warning("  %s error for %s: %s", progress_label, k, msg[:120])
            inactive_reason = inactive_from_yf_error(msg)
            if inactive_reason:
                mark_inactive_fn(k, inactive_reason, source="yf_error")
                inactive_skip.add(k)
                logger.info("  %s: inactive (%s) from yf error", k, inactive_reason)
            else:
                logger.info("  %s: transient error, will retry later", k)
            sleep = 30 if ("Rate" in msg or "Too Many" in msg or "429" in msg) else max(1.0, sleep_seconds / 2)
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

