"""
Price relative to multi-year earnings (Burry-style / cyclically adjusted-ish).

- pe_5y_avg_earnings: price / average annual EPS over last ≤5 years
- pe_peak_earnings:   price / peak annual EPS over last ≤5 years
"""

from __future__ import annotations

from typing import Any


def _safe_float(val, default=None):
    if val is None:
        return default
    try:
        f = float(val)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default


def _annual_eps_from_yf_history(earnings_history: list | None) -> list[float]:
    """Extract annual EPS series oldest→newest from yfinance-style history."""
    if not earnings_history:
        return []
    annual = [
        h for h in earnings_history
        if (h.get("type") or "").lower() == "annual" and h.get("eps") is not None
    ]
    # yfinance builder returns newest-first within groups; reverse for chrono
    annual = list(reversed(annual))
    out = []
    for h in annual:
        eps = _safe_float(h.get("eps"))
        if eps is not None:
            out.append(eps)
    return out


def _annual_eps_from_edgar(
    earnings_history: list | None,
    share_count_history: list | None = None,
    fallback_shares: float | None = None,
) -> list[float]:
    """
    Convert EDGAR annual net-income history to EPS using share-count history
    when available, else current shares outstanding.
    """
    if not earnings_history:
        return []
    shares_by_year: dict[int, float] = {}
    for h in share_count_history or []:
        yr = h.get("year")
        sh = _safe_float(h.get("shares"))
        if yr is not None and sh and sh > 0:
            try:
                shares_by_year[int(yr)] = sh
            except (TypeError, ValueError):
                pass

    rows = sorted(
        [h for h in earnings_history if h.get("earnings") is not None and h.get("year") is not None],
        key=lambda x: int(x["year"]),
    )
    out = []
    for h in rows:
        try:
            yr = int(h["year"])
        except (TypeError, ValueError):
            continue
        ni = _safe_float(h.get("earnings"))
        if ni is None:
            continue
        sh = shares_by_year.get(yr) or _safe_float(fallback_shares)
        if not sh or sh <= 0:
            continue
        out.append(ni / sh)
    return out


def compute_price_to_multi_year_earnings(
    price: float | None,
    annual_eps: list[float] | None,
    lookback_years: int = 5,
) -> dict[str, Any]:
    """
    Return pe_5y_avg_earnings, pe_peak_earnings, and the EPS inputs used.
    Only positive average/peak produce ratios (losses → no multiple).
    """
    price = _safe_float(price)
    if not price or price <= 0 or not annual_eps:
        return {}

    series = [e for e in annual_eps if e is not None]
    if not series:
        return {}
    recent = series[-lookback_years:] if len(series) > lookback_years else series
    if not recent:
        return {}

    avg_eps = sum(recent) / len(recent)
    peak_eps = max(recent)
    loss_years = sum(1 for e in recent if e < 0)
    out: dict[str, Any] = {
        "eps_5y_avg": round(avg_eps, 4),
        "eps_peak_5y": round(peak_eps, 4),
        "eps_years_used": len(recent),
        "eps_latest": round(series[-1], 4),
        "loss_years_5y": loss_years,
    }
    if avg_eps > 0:
        out["pe_5y_avg_earnings"] = round(price / avg_eps, 1)
    if peak_eps > 0:
        out["pe_peak_earnings"] = round(price / peak_eps, 1)
    return out


def _growth_decimal_from_stock(stock: dict) -> float | None:
    """Growth as decimal for long-horizon EPS power estimates."""
    from data.yf_helpers import normalize_growth_rate

    m = stock.get("metrics") or {}
    for src in (
        m.get("earnings_growth_pct"),
        stock.get("earnings_growth_pct"),
        stock.get("earnings_5y_growth_pct"),
        m.get("earnings_5y_growth_pct"),
        (stock.get("edgar") or {}).get("earnings_5y_growth_pct"),
    ):
        g = normalize_growth_rate(src)
        if g is not None:
            return g
    return None


def compute_long_horizon_pe(
    price: float | None,
    latest_eps: float | None,
    growth_decimal: float | None,
) -> dict[str, Any]:
    """
    Templeton-style P/E on year-5 / year-10 EPS (estimated).
    Book framing: prefer ~≤5× year-5 EPS and ~≤2× year-10 EPS.
    """
    price = _safe_float(price)
    eps = _safe_float(latest_eps)
    if not price or price <= 0 or not eps or eps <= 0 or growth_decimal is None:
        return {}
    out: dict[str, Any] = {"growth_used_pct": round(growth_decimal * 100, 1)}
    for years, key in ((5, "pe_year5_est"), (10, "pe_year10_est")):
        future = eps * ((1.0 + growth_decimal) ** years)
        if future > 0:
            out[key] = round(price / future, 2)
    return out


def earnings_multiples_for_stock(stock: dict) -> dict[str, Any]:
    """
    Best-effort multiples from data already on the stock dict.
    Prefers yfinance annual EPS; falls back to EDGAR NI / shares.
    """
    price = _safe_float(stock.get("current_price"))
    yf_eps = _annual_eps_from_yf_history(stock.get("earnings_history"))
    series = yf_eps
    if not series:
        edgar = stock.get("edgar") or {}
        m = stock.get("metrics") or {}
        shares = (
            _safe_float(m.get("shares_outstanding"))
            or _safe_float(stock.get("shares_outstanding"))
            or _safe_float(m.get("market_cap") and price and m.get("market_cap") / price)
        )
        if not shares and price and m.get("market_cap"):
            mcap = _safe_float(m.get("market_cap"))
            if mcap and price:
                shares = mcap / price

        series = _annual_eps_from_edgar(
            edgar.get("earnings_history") or stock.get("edgar_earnings_history"),
            edgar.get("share_count_history"),
            fallback_shares=shares,
        )

    out = compute_price_to_multi_year_earnings(price, series)
    if out:
        g = _growth_decimal_from_stock(stock)
        out.update(compute_long_horizon_pe(price, out.get("eps_latest"), g))
    return out


def attach_earnings_multiples(stock: dict) -> dict:
    """Mutate stock + metrics with multi-year P/E fields when computable."""
    mult = earnings_multiples_for_stock(stock)
    if not mult:
        return stock
    metrics = stock.setdefault("metrics", {})
    for k, v in mult.items():
        metrics[k] = v
        stock[k] = v
    return stock
