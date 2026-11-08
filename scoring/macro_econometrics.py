"""
Time-Series Econometrics & Macro / Commodity Beta Engine (`scoring/macro_econometrics.py`).

Implements Section 4: **Time-Series Econometrics & Regression (Macro / Commodity Beta)**:
1. **Univariate & Rolling Macro/Commodity Beta (`OLS` + `HuberRegressor`)**:
   - Regresses weekly stock returns against weekly percentage changes in:
     * **Commodities & Energy Margins:** Iron Ore (`iron_ore`), WTI Crude (`crude_oil_wti`),
       Brent Crude (`crude_oil_brent`), Henry Hub Gas (`henry_hub`), 3-2-1 Crack Spread (`crack_321`),
       Copper (`copper`), Gold (`gold`), Thermal Coal (`thermal_coal` / `newcastle_coal`),
       Coking Coal (`coking_coal`), Aluminum (`aluminum`).
     * **Macro Rates, Credit & FX:** 10Y TIPS Real Yield (`tips_10y`, daily bps changes),
       US High-Yield Credit Spread (`hy_oas`, daily bps changes), Broad USD Index (`dxy`),
       AUD/USD (`fx_audusd`), JPY/USD (`fx_jpyusd`), KRW/USD (`fx_krwusd`).
   - Computes:
     * `ols_beta`, `huber_beta` (outlier-robust slope), `t_stat`, `p_value`, `r2`
     * **Up-Market vs. Down-Market Beta Asymmetry (`beta_up`, `beta_down`, `asymmetry`)**:
       measures whether a stock captures upside commodity/macro rallies (`beta_up > beta_down`)
       or suffers disproportionate downside convexity.
     * **26-Week Rolling Beta & Rolling R² series** for interactive time-series visualization.
2. **Multi-Factor Macro & Commodity Attribution (`HuberRegressor` + `OLS`)**:
   - Simultaneously regresses stock returns on its **Primary Commodity/Sector Factor +
     Real Rates (`tips_10y`) + USD (`dxy`) + Credit Spreads (`hy_oas`)** to separate
     pure commodity beta from macro rate/FX drag and estimate annualized **Idiosyncratic Alpha (`alpha_ann_pct`)**.
3. **Cointegration & Mean-Reverting Spread (`Engle-Granger ADF` + `Ornstein-Uhlenbeck Half-Life`)**:
   - Tests whether `log(Stock Price)` and `log(Driver Price)` are cointegrated:
     $$\log(P_{\text{stock}, t}) = \alpha + \gamma \log(P_{\text{driver}, t}) + \varepsilon_t$$
   - Runs an Augmented Dickey-Fuller (`ADF`) test on the residual spread $\varepsilon_t$:
     $$\Delta \varepsilon_t = \phi \varepsilon_{t-1} + c + u_t$$
   - Computes the **ADF t-statistic (`adf_stat`)**, **Ornstein-Uhlenbeck Half-Life of Mean Reversion**
     in trading days (`half_life_days`), and the **Current Spread Z-Score (`spread_z`)**:
     * A deeply negative `spread_z` (e.g. `<= -1.5σ`) on a cointegrated pair means the stock
       has lagged its underlying commodity/macro driver and is statistically compressed
       relative to its equilibrium cointegrating relationship.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sklearn.linear_model import HuberRegressor

logger = logging.getLogger(__name__)

# Catalog of macro & commodity factor series stored in `supporting_series.db` and `commodity_prices.py`
MACRO_FACTOR_CATALOG: list[dict[str, Any]] = [
    {"id": "crude_oil_wti", "label": "Crude Oil WTI", "category": "Energy", "mode": "pct"},
    {"id": "crude_oil_brent", "label": "Crude Oil Brent", "category": "Energy", "mode": "pct"},
    {"id": "henry_hub", "label": "Henry Hub Nat Gas", "category": "Energy", "mode": "pct"},
    {"id": "crack_321", "label": "3-2-1 Refinery Crack", "category": "Energy", "mode": "pct"},
    {"id": "iron_ore", "label": "Iron Ore 62% Fe", "category": "Metals & Mining", "mode": "pct"},
    {"id": "copper", "label": "Copper", "category": "Metals & Mining", "mode": "pct"},
    {"id": "gold", "label": "Gold", "category": "Precious Metals", "mode": "pct"},
    {"id": "silver", "label": "Silver", "category": "Precious Metals", "mode": "pct"},
    {"id": "aluminum", "label": "Aluminum", "category": "Metals & Mining", "mode": "pct"},
    {"id": "coking_coal", "label": "Met / Coking Coal", "category": "Coal", "mode": "pct"},
    {"id": "thermal_coal", "label": "Thermal Coal", "category": "Coal", "mode": "pct"},
    {"id": "dxy", "label": "Broad USD Index (DXY)", "category": "FX & Macro", "mode": "pct"},
    {"id": "fx_audusd", "label": "AUD / USD", "category": "FX & Macro", "mode": "pct"},
    {"id": "fx_jpyusd", "label": "JPY / USD", "category": "FX & Macro", "mode": "pct"},
    {"id": "tips_10y", "label": "US 10Y TIPS Real Yield", "category": "Rates & Credit", "mode": "diff"},
    {"id": "hy_oas", "label": "US High-Yield Credit OAS", "category": "Rates & Credit", "mode": "diff"},
    {"id": "aemo_nsw_price", "label": "AEMO NSW Power Price", "category": "Power", "mode": "pct"},
]

_factor_weekly_cache: dict[str, Any] = {"ts": 0.0, "series": {}}
_FACTOR_CACHE_TTL = 1800  # 30 minutes


def _iso_week_key(date_str: str) -> str | None:
    """Convert YYYY-MM-DD to its Friday (or ISO year-week) bucket so daily/weekly series align cleanly."""
    try:
        dt = datetime.fromisoformat(str(date_str)[:10]).date()
        # Snap to the Friday of that ISO week so dates are human-readable YYYY-MM-DD
        friday = dt + timedelta(days=(4 - dt.weekday()))
        return friday.isoformat()
    except Exception:
        return None


def _load_raw_factor_points(factor_id: str) -> list[dict[str, Any]]:
    """Fetch daily/weekly points for `factor_id` from `supporting_series` or `commodity_prices`."""
    try:
        from data.supporting_series import get_series as get_supp

        pts = get_supp(factor_id) or []
        if len(pts) >= 30:
            return pts
    except Exception:
        pass
    try:
        from data.commodity_prices import get_series as get_cmdty

        pts = get_cmdty(factor_id) or []
        if pts:
            return pts
    except Exception:
        pass
    return []


def get_weekly_factor_table(force: bool = False) -> dict[str, dict[str, Any]]:
    """
    Build and cache weekly-sampled (`YYYY-MM-DD` Friday keys) levels and returns/changes
    for all macro & commodity factors in `MACRO_FACTOR_CATALOG`.
    """
    now = time.time()
    if not force and _factor_weekly_cache["series"] and (now - _factor_weekly_cache["ts"] < _FACTOR_CACHE_TTL):
        return _factor_weekly_cache["series"]

    cutoff = (datetime.now().date() - timedelta(days=365 * 3 + 30)).isoformat()
    out: dict[str, dict[str, Any]] = {}

    for fmeta in MACRO_FACTOR_CATALOG:
        fid = fmeta["id"]
        mode = fmeta.get("mode", "pct")
        pts = _load_raw_factor_points(fid)
        if not pts:
            continue

        # Bucket by ISO week Friday (last observation in each week wins)
        week_levels: dict[str, float] = {}
        for p in pts:
            d = str(p.get("date") or "")[:10]
            if not d or d < cutoff:
                continue
            val = p.get("last") if p.get("last") is not None else p.get("value")
            if val is None:
                continue
            try:
                vf = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(vf):
                continue
            if mode == "pct" and vf <= 0:
                continue
            wk = _iso_week_key(d)
            if wk:
                week_levels[wk] = vf

        weeks_sorted = sorted(week_levels.keys())
        if len(weeks_sorted) < 20:
            continue

        week_rets: dict[str, float] = {}
        for idx in range(1, len(weeks_sorted)):
            w_prev = weeks_sorted[idx - 1]
            w_curr = weeks_sorted[idx]
            v_prev = week_levels[w_prev]
            v_curr = week_levels[w_curr]
            if mode == "diff":
                # For yields/spreads (in % points), express weekly change in percentage points * 10 (or 100 bps = 1%)
                # Here we use percentage-point change * 10 so a +0.10% (+10 bps) yield jump = +1.0 unit
                chg = (v_curr - v_prev) * 10.0
            else:
                if v_prev <= 0:
                    continue
                chg = ((v_curr / v_prev) - 1.0) * 100.0
            if math.isfinite(chg) and abs(chg) <= 80.0:
                week_rets[w_curr] = chg

        out[fid] = {
            **fmeta,
            "levels": week_levels,
            "returns": week_rets,
            "weeks": weeks_sorted,
            "last_week": weeks_sorted[-1],
            "last_level": round(week_levels[weeks_sorted[-1]], 4),
            "n_weeks": len(week_rets),
        }

    _factor_weekly_cache["ts"] = now
    _factor_weekly_cache["series"] = out
    return out


def _normal_two_sided_pvalue(t_stat: float) -> float:
    """Fast normal/Student-t approximation for two-sided p-value."""
    z = abs(float(t_stat))
    return round(math.erfc(z / math.sqrt(2.0)), 4)


def _univariate_beta_stats(
    x: np.ndarray,
    y: np.ndarray,
) -> dict[str, float]:
    """
    Compute OLS beta, Huber robust beta, t-stat, p-value, R², correlation,
    and Up-Market / Down-Market beta asymmetry (`beta_up`, `beta_down`).
    """
    n = len(x)
    if n < 12:
        return {}
    mx = float(np.mean(x))
    my = float(np.mean(y))
    dx = x - mx
    dy = y - my
    sxx = float(np.sum(dx * dx))
    syy = float(np.sum(dy * dy))
    if sxx <= 1e-9 or syy <= 1e-9:
        return {}

    sxy = float(np.sum(dx * dy))
    ols_beta = sxy / sxx
    alpha = my - ols_beta * mx
    y_hat = alpha + ols_beta * x
    resid = y - y_hat
    ssr = float(np.sum(resid * resid))
    r2 = max(0.0, min(1.0, 1.0 - (ssr / syy)))
    corr = max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))

    dof = max(1, n - 2)
    se_beta = math.sqrt(max(1e-12, (ssr / dof) / sxx))
    t_stat = ols_beta / se_beta if se_beta > 1e-9 else 0.0
    p_val = _normal_two_sided_pvalue(t_stat)

    # Huber Robust Regression Beta (resistant to earnings/announcement jump weeks)
    try:
        huber = HuberRegressor(max_iter=150)
        huber.fit(x.reshape(-1, 1), y)
        huber_beta = float(huber.coef_[0])
        huber_alpha = float(huber.intercept_)
    except Exception:
        huber_beta = ols_beta
        huber_alpha = alpha

    # Up-market vs Down-market conditional betas
    up_mask = x > 0
    dn_mask = x < 0
    beta_up = ols_beta
    beta_down = ols_beta
    if int(np.sum(up_mask)) >= 6:
        xu, yu = x[up_mask], y[up_mask]
        denom_u = float(np.sum((xu - np.mean(xu)) ** 2))
        if denom_u > 1e-6:
            beta_up = float(np.sum((xu - np.mean(xu)) * (yu - np.mean(yu))) / denom_u)
    if int(np.sum(dn_mask)) >= 6:
        xd, yd = x[dn_mask], y[dn_mask]
        denom_d = float(np.sum((xd - np.mean(xd)) ** 2))
        if denom_d > 1e-6:
            beta_down = float(np.sum((xd - np.mean(xd)) * (yd - np.mean(yd))) / denom_d)

    return {
        "n_weeks": int(n),
        "ols_beta": round(ols_beta, 3),
        "huber_beta": round(huber_beta, 3),
        "alpha_weekly_pct": round(huber_alpha, 3),
        "alpha_ann_pct": round(huber_alpha * 52.0, 1),
        "r2": round(r2, 3),
        "corr": round(corr, 3),
        "t_stat": round(t_stat, 2),
        "p_value": p_val,
        "beta_up": round(beta_up, 2),
        "beta_down": round(beta_down, 2),
        "convexity_diff": round(beta_up - beta_down, 2),
    }


def _cointegration_and_spread(
    weeks: list[str],
    stock_levels: np.ndarray,
    driver_levels: np.ndarray,
    *,
    factor_mode: str = "pct",
) -> dict[str, Any]:
    """
    Engle-Granger Cointegration & Ornstein-Uhlenbeck Mean-Reversion analysis:
    1. Regress `log(Stock_t) = a + gamma * DriverTerm_t + eps_t`
       (where `DriverTerm_t = log(Driver_t)` for price series or raw level for yield series).
    2. Run Augmented Dickey-Fuller (ADF) test on `eps_t`:
       `Delta eps_t = phi * eps_{t-1} + c + u_t`
    3. Estimate Ornstein-Uhlenbeck Half-Life (in weeks & trading days) and current Z-score `spread_z`.
    """
    n = len(weeks)
    if n < 24 or np.any(stock_levels <= 0):
        return {}

    y_log = np.log(stock_levels)
    if factor_mode == "pct":
        if np.any(driver_levels <= 0):
            return {}
        x_term = np.log(driver_levels)
    else:
        x_term = driver_levels.astype(np.float64)

    # Cointegrating regression via Huber (so temporary blowouts don't skew equilibrium line)
    try:
        huber = HuberRegressor(max_iter=200)
        huber.fit(x_term.reshape(-1, 1), y_log)
        gamma = float(huber.coef_[0])
        intercept = float(huber.intercept_)
    except Exception:
        mx, my = float(np.mean(x_term)), float(np.mean(y_log))
        sxx = float(np.sum((x_term - mx) ** 2))
        if sxx <= 1e-9:
            return {}
        gamma = float(np.sum((x_term - mx) * (y_log - my)) / sxx)
        intercept = my - gamma * mx

    eq_log = intercept + gamma * x_term
    spread = y_log - eq_log  # log-price residual (approx % deviation when multiplied by 100)

    # Robust Z-score of the spread over trailing 52 weeks (or full sample)
    lookback = spread[-52:] if len(spread) >= 52 else spread
    med = float(np.median(lookback))
    mad = float(np.median(np.abs(lookback - med)))
    robust_sd = mad * 1.4826 if mad > 1e-6 else float(np.std(lookback))
    if robust_sd <= 1e-6:
        return {}
    spread_z_series = (spread - med) / robust_sd
    current_spread_z = round(float(spread_z_series[-1]), 2)
    current_pct_mispricing = round((math.exp(float(spread[-1])) - 1.0) * 100.0, 1)

    # Augmented Dickey-Fuller (ADF) regression on spread: d_eps_t = phi * eps_{t-1} + c + b * d_eps_{t-1}
    d_eps = np.diff(spread)
    eps_lag = spread[:-1]
    if len(d_eps) < 15:
        return {}

    # Design matrix [eps_{t-1}, d_eps_{t-1}, 1]
    X_adf = np.column_stack([eps_lag[1:], d_eps[:-1], np.ones(len(d_eps) - 1)])
    y_adf = d_eps[1:]
    try:
        beta_adf, residuals, _, _ = np.linalg.lstsq(X_adf, y_adf, rcond=None)
        phi = float(beta_adf[0])
        y_adf_hat = X_adf @ beta_adf
        u_adf = y_adf - y_adf_hat
        dof = max(1, len(y_adf) - 3)
        sigma2 = float(np.sum(u_adf * u_adf)) / dof
        cov_beta = sigma2 * np.linalg.inv(X_adf.T @ X_adf + np.eye(3) * 1e-9)
        se_phi = math.sqrt(max(1e-12, float(cov_beta[0, 0])))
        adf_t_stat = round(phi / se_phi, 2)
    except Exception:
        phi = -0.05
        adf_t_stat = 0.0

    # Ornstein-Uhlenbeck Half-Life: lambda = -log(1 + phi) per week
    half_life_weeks: float | None = None
    half_life_days: float | None = None
    if -0.95 < phi < -0.005:
        lam = -math.log(1.0 + phi)
        if lam > 1e-6:
            hl_w = math.log(2.0) / lam
            if 0.5 <= hl_w <= 104.0:
                half_life_weeks = round(hl_w, 1)
                half_life_days = round(hl_w * 5.0, 0)

    # Engle-Granger 2-variable critical values: 5% is ~ -3.34, 10% is ~ -3.04
    is_cointegrated = bool(adf_t_stat <= -2.85 and (half_life_weeks is not None and half_life_weeks <= 40.0))

    return {
        "hedge_ratio_gamma": round(gamma, 3),
        "adf_t_stat": adf_t_stat,
        "is_cointegrated": is_cointegrated,
        "half_life_weeks": half_life_weeks,
        "half_life_days": half_life_days,
        "spread_z": current_spread_z,
        "pct_vs_equilibrium": current_pct_mispricing,
        "spread_z_series": [round(float(z), 2) for z in spread_z_series],
        "equilibrium_price_series": [round(math.exp(float(v)), 3) for v in eq_log],
    }


def compute_stock_macro_econometrics(
    stock: dict[str, Any],
    *,
    primary_factor_override: str | None = None,
    rolling_window_weeks: int = 26,
) -> dict[str, Any]:
    """
    Compute the full Time-Series Econometrics & Macro/Commodity Beta profile for `stock`:
    1. Scans all 17 macro & commodity factors in `MACRO_FACTOR_CATALOG` against the stock's
       weekly return history (`price_trend`).
    2. Selects the Primary Driver (using explicit override, or `supporting_series` exposure
       mapping if available, or the factor with the highest statistical significance `|t_stat|`).
    3. Computes 26-week Rolling Beta & Rolling R², Engle-Granger Cointegration (`ADF t-stat`,
       `Half-Life`, `Spread Z-Score`), and a Multi-Factor Macro Attribution model
       (`Primary Driver + 10Y TIPS Real Yield + Broad USD + HY Credit Spread`).
    """
    ticker = str(stock.get("ticker") or "").strip().upper()
    trend = stock.get("price_trend") or []
    if len(trend) < 20:
        return {
            "ticker": ticker,
            "available": False,
            "note": "Need at least 20 weekly price observations to run time-series econometrics.",
        }

    # Build weekly stock levels and weekly % returns
    stock_levels: dict[str, float] = {}
    for pt in trend:
        d = str(pt.get("date") or "")[:10]
        p = pt.get("price") if pt.get("price") is not None else pt.get("close")
        if not d or p is None:
            continue
        try:
            pf = float(p)
        except (TypeError, ValueError):
            continue
        if pf > 0 and math.isfinite(pf):
            wk = _iso_week_key(d)
            if wk:
                stock_levels[wk] = pf

    s_weeks = sorted(stock_levels.keys())
    if len(s_weeks) < 20:
        return {
            "ticker": ticker,
            "available": False,
            "note": "Insufficient weekly price history.",
        }

    stock_rets: dict[str, float] = {}
    for idx in range(1, len(s_weeks)):
        w0, w1 = s_weeks[idx - 1], s_weeks[idx]
        p0, p1 = stock_levels[w0], stock_levels[w1]
        if p0 > 0:
            r = ((p1 / p0) - 1.0) * 100.0
            if math.isfinite(r) and abs(r) <= 85.0:
                stock_rets[w1] = r

    factors = get_weekly_factor_table()
    if not factors:
        return {
            "ticker": ticker,
            "available": False,
            "note": "No macro or commodity time-series loaded in supporting_series.db.",
        }

    # 1. Evaluate Univariate Beta against every available Macro / Commodity factor
    factor_betas: list[dict[str, Any]] = []
    aligned_cache: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}

    for fid, fdata in factors.items():
        f_rets = fdata["returns"]
        common_weeks = [w for w in s_weeks if w in stock_rets and w in f_rets]
        if len(common_weeks) < 18:
            continue
        x_arr = np.array([f_rets[w] for w in common_weeks], dtype=np.float64)
        y_arr = np.array([stock_rets[w] for w in common_weeks], dtype=np.float64)
        stats = _univariate_beta_stats(x_arr, y_arr)
        if not stats:
            continue
        aligned_cache[fid] = (common_weeks, x_arr, y_arr)
        factor_betas.append({
            "factor_id": fid,
            "label": fdata["label"],
            "category": fdata["category"],
            "mode": fdata["mode"],
            **stats,
        })

    if not factor_betas:
        return {
            "ticker": ticker,
            "available": False,
            "note": "No overlapping dates between stock price history and macro/commodity series.",
        }

    # Sort by absolute t-statistic (most statistically significant macro/commodity sensitivity first)
    factor_betas.sort(key=lambda b: abs(b.get("t_stat") or 0.0), reverse=True)

    # 2. Determine Primary Factor
    mapped_primary = None
    if primary_factor_override and primary_factor_override in aligned_cache:
        mapped_primary = primary_factor_override
    else:
        drv = stock.get("driver") or {}
        if drv.get("primary") and drv.get("primary") in aligned_cache:
            mapped_primary = drv["primary"]
        else:
            try:
                from data.supporting_series import get_exposure

                exp = get_exposure(ticker)
                if exp and exp.get("primary") in aligned_cache:
                    mapped_primary = exp["primary"]
            except Exception:
                pass

    primary_beta_row = next(
        (b for b in factor_betas if b["factor_id"] == mapped_primary),
        factor_betas[0],
    )
    primary_id = primary_beta_row["factor_id"]
    p_weeks, p_x, p_y = aligned_cache[primary_id]

    # 3. Compute Rolling 26-Week Beta & R² + Cointegration Spread on Primary Factor
    win = min(max(12, int(rolling_window_weeks)), max(12, len(p_weeks) // 2))
    rolling_series: list[dict[str, Any]] = []
    for idx in range(win, len(p_weeks) + 1):
        w_end = p_weeks[idx - 1]
        xw = p_x[idx - win : idx]
        yw = p_y[idx - win : idx]
        st_w = _univariate_beta_stats(xw, yw)
        if st_w:
            rolling_series.append({
                "date": w_end,
                "beta": st_w["huber_beta"],
                "ols_beta": st_w["ols_beta"],
                "r2": st_w["r2"],
            })

    # Cointegration & Mean-Reverting Spread vs Primary Factor
    f_levels_map = factors[primary_id]["levels"]
    lvl_weeks = [w for w in s_weeks if w in stock_levels and w in f_levels_map]
    coint_info: dict[str, Any] = {}
    chart_series: list[dict[str, Any]] = []
    if len(lvl_weeks) >= 24:
        s_lvl_arr = np.array([stock_levels[w] for w in lvl_weeks], dtype=np.float64)
        f_lvl_arr = np.array([f_levels_map[w] for w in lvl_weeks], dtype=np.float64)
        coint_info = _cointegration_and_spread(
            lvl_weeks,
            s_lvl_arr,
            f_lvl_arr,
            factor_mode=factors[primary_id]["mode"],
        )
        sz_list = coint_info.pop("spread_z_series", [])
        eq_list = coint_info.pop("equilibrium_price_series", [])
        roll_by_date = {r["date"]: r for r in rolling_series}
        for idx, wk in enumerate(lvl_weeks):
            rl = roll_by_date.get(wk) or {}
            chart_series.append({
                "date": wk,
                "stock_price": round(float(s_lvl_arr[idx]), 3),
                "eq_price": eq_list[idx] if idx < len(eq_list) else None,
                "driver_level": round(float(f_lvl_arr[idx]), 3),
                "spread_z": sz_list[idx] if idx < len(sz_list) else None,
                "rolling_beta": rl.get("beta"),
                "rolling_r2": rl.get("r2"),
            })

    # 4. Multi-Factor Macro Attribution:
    # Regress Stock Returns ~ Primary Commodity/Factor + DXY (USD) + 10Y TIPS Real Yield + HY Credit Spread
    macro_core_ids = [primary_id]
    for cand_m in ("dxy", "tips_10y", "hy_oas", "fx_audusd" if ticker.endswith(".AX") else "crude_oil_wti"):
        if cand_m not in macro_core_ids and cand_m in factors:
            macro_core_ids.append(cand_m)
        if len(macro_core_ids) >= 4:
            break

    mf_weeks = [
        w
        for w in s_weeks
        if w in stock_rets and all(w in factors[mid]["returns"] for mid in macro_core_ids)
    ]
    multifactor_model: dict[str, Any] = {}
    if len(mf_weeks) >= 24:
        X_mf = np.column_stack(
            [[factors[mid]["returns"][w] for w in mf_weeks] for mid in macro_core_ids]
        )
        y_mf = np.array([stock_rets[w] for w in mf_weeks], dtype=np.float64)
        try:
            huber_mf = HuberRegressor(max_iter=200)
            huber_mf.fit(X_mf, y_mf)
            coefs = huber_mf.coef_
            intercept_mf = float(huber_mf.intercept_)
        except Exception:
            X_aug = np.column_stack([X_mf, np.ones(len(mf_weeks))])
            b_ols, _, _, _ = np.linalg.lstsq(X_aug, y_mf, rcond=None)
            coefs = b_ols[:-1]
            intercept_mf = float(b_ols[-1])

        y_mf_hat = X_mf @ coefs + intercept_mf
        syy_mf = float(np.sum((y_mf - np.mean(y_mf)) ** 2))
        ssr_mf = float(np.sum((y_mf - y_mf_hat) ** 2))
        r2_mf = round(max(0.0, min(1.0, 1.0 - ssr_mf / max(1e-9, syy_mf))), 3)

        mf_exposures = []
        for j, mid in enumerate(macro_core_ids):
            mf_exposures.append({
                "factor_id": mid,
                "label": factors[mid]["label"],
                "category": factors[mid]["category"],
                "beta": round(float(coefs[j]), 3),
            })
        multifactor_model = {
            "n_weeks": len(mf_weeks),
            "r2": r2_mf,
            "idiosyncratic_alpha_weekly_pct": round(intercept_mf, 3),
            "idiosyncratic_alpha_ann_pct": round(intercept_mf * 52.0, 1),
            "factors": mf_exposures,
        }

    return {
        "ticker": ticker,
        "available": True,
        "primary_factor": {
            **primary_beta_row,
            "mapped_from_catalog": bool(mapped_primary == primary_id),
        },
        "cointegration": coint_info,
        "multifactor": multifactor_model,
        "top_factors": factor_betas[:10],
        "available_factors": [
            {"id": b["factor_id"], "label": b["label"], "category": b["category"], "t_stat": b["t_stat"], "huber_beta": b["huber_beta"]}
            for b in factor_betas
        ],
        "rolling_window_weeks": win,
        "chart": chart_series[-104:],  # Up to 2 years of weekly points
    }
