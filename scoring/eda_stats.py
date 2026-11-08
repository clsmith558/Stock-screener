"""
Statistical & Exploratory Data Analysis (EDA) Engine for Stock Screener.

Implements:
1. Sector-Adjusted Robust Z-Scores using Median and MAD (Median Absolute Deviation)
   with IQR fallback for zero-MAD / heavily tied distributions.
2. Empirical Percentile Ranks (0–100) within each stock's sector (falling back to
   the global list distribution when a sector has fewer than `min_sector_peers`
   valid observations).
3. Multi-Factor Value/Quality Composite Score (`vq_composite_score`, 0–100) combining:
   - Valuation Factor (`val_z`, `val_score`): EV/EBITDA, P/FCF, P/B, P/E, P/5Y Avg EPS
   - Quality & Balance Sheet Factor (`qual_z`, `qual_score`): FCF Yield, ROE/ROIC,
     Net Cash % of Market Cap, Debt/Equity (inverted), Net Assets vs Market Cap %
   - Capitulation & Capital Return Factor (`cap_z`, `cap_score`): % from 52w low,
     RSI(14), Annual Buyback %, Insider Buys
4. Cross-sectional EDA summary statistics per sector and metric (N, median, MAD,
   IQR, skewness proxy, and top statistical outliers).
"""

from __future__ import annotations

import bisect
import math
from typing import Any

# Normal consistency factor for Median Absolute Deviation:
# For X ~ N(mu, sigma^2), E[MAD] = sigma * Phi^-1(0.75) ≈ 0.67448975 * sigma
# => sigma_hat = 1.4826022185 * MAD
MAD_NORMAL_FACTOR = 1.4826022185
IQR_NORMAL_FACTOR = 0.74130110925  # 1 / (2 * Phi^-1(0.75)) ≈ 1 / 1.34898
Z_CLIP = 3.5  # Winsorization bound so a single extreme ratio cannot dominate composite

# Metric specifications:
# (key, label, group, lower_is_better, valid_predicate)
# `lower_is_better=True` means a lower raw metric produces a POSITIVE oriented z-score
# (i.e. +z always means "more attractive / cheaper / higher quality").
METRIC_SPECS: list[dict[str, Any]] = [
    # --- Valuation Pillar (lower multiple = cheaper = positive z) ---
    {
        "key": "ev_ebitda",
        "label": "EV/EBITDA",
        "group": "value",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.30,
    },
    {
        "key": "p_fcf",
        "label": "P/FCF",
        "group": "value",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.25,
    },
    {
        "key": "pb",
        "label": "P/B",
        "group": "value",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.20,
    },
    {
        "key": "pe",
        "label": "P/E",
        "group": "value",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.15,
    },
    {
        "key": "pe_5y_avg_earnings",
        "label": "P/5Y EPS",
        "group": "value",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.10,
    },
    # --- Quality & Balance Sheet Pillar (higher return / cleaner BS = positive z) ---
    {
        "key": "fcf_yield",
        "label": "FCF Yield %",
        "group": "quality",
        "lower_is_better": False,
        "positive_only": False,
        "weight": 0.28,
    },
    {
        "key": "roic_or_roe",
        "label": "ROIC / ROE %",
        "group": "quality",
        "lower_is_better": False,
        "positive_only": False,
        "weight": 0.24,
    },
    {
        "key": "net_cash_pct_mcap",
        "label": "Net Cash / MCap %",
        "group": "quality",
        "lower_is_better": False,
        "positive_only": False,
        "weight": 0.22,
    },
    {
        "key": "debt_to_equity",
        "label": "Debt / Equity",
        "group": "quality",
        "lower_is_better": True,
        "positive_only": False,  # 0 debt is valid and strong
        "non_negative": True,
        "weight": 0.16,
    },
    {
        "key": "net_assets_vs_mcap_pct",
        "label": "(Equity − MCap) %",
        "group": "quality",
        "lower_is_better": False,
        "positive_only": False,
        "weight": 0.10,
    },
    # --- Capitulation / Timing Pillar (closer to 52w low / oversold = positive z) ---
    {
        "key": "pct_from_52w_low",
        "label": "52w Low Gap %",
        "group": "posture",
        "lower_is_better": True,
        "positive_only": False,
        "non_negative": True,
        "weight": 0.45,
    },
    {
        "key": "pct_from_200w_ma",
        "label": "200w MA Gap %",
        "group": "posture",
        "lower_is_better": True,
        "positive_only": False,
        "weight": 0.30,
    },
    {
        "key": "rsi",
        "label": "RSI(14)",
        "group": "posture",
        "lower_is_better": True,
        "positive_only": True,
        "weight": 0.25,
    },
]


def _num(val: Any) -> float | None:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def extract_metric(stock: dict[str, Any], key: str) -> float | None:
    """Extract a numeric metric from a stock dict or its nested `metrics`/`edgar` dicts."""
    if not stock or stock.get("_pending_yf"):
        return None
    m = stock.get("metrics") or {}
    ed = stock.get("edgar") or {}

    if key == "roic_or_roe":
        for candidate in (
            stock.get("roic_5y_avg"),
            ed.get("roic_5y_avg"),
            m.get("roic_5y_avg"),
            stock.get("roic_latest"),
            ed.get("roic_latest"),
            m.get("roic_latest"),
            m.get("roe"),
            stock.get("roe"),
        ):
            v = _num(candidate)
            if v is not None and v != 0:
                return v
        return None

    if key == "fcf_yield":
        v = _num(m.get("fcf_yield") if m.get("fcf_yield") is not None else stock.get("fcf_yield"))
        if v is not None and v != 0:
            return v
        pfcf = _num(stock.get("p_fcf") or m.get("p_fcf"))
        if pfcf is not None and pfcf > 0:
            return round(100.0 / pfcf, 2)
        return None

    if key == "debt_to_equity":
        v = _num(stock.get("debt_to_equity") if stock.get("debt_to_equity") is not None else m.get("debt_to_equity"))
        if v is not None and v >= 0:
            if v == 0:
                # Only treat 0 D/E as confirmed zero-debt if net_cash_m is known >= 0
                nc = _num(stock.get("net_cash_m") if stock.get("net_cash_m") is not None else m.get("net_cash_m"))
                return 0.0 if (nc is not None and nc >= 0) else None
            return v
        return None

    for src in (stock.get(key), m.get(key), ed.get(key)):
        v = _num(src)
        if v is not None:
            return v
    return None


def is_valid_metric_value(val: float | None, spec: dict[str, Any]) -> bool:
    if val is None:
        return False
    if spec.get("positive_only") and val <= 0:
        return False
    if spec.get("non_negative") and val < 0:
        return False
    # Exclude placeholder zeros on ratios where 0 means missing
    if val == 0 and spec["key"] not in ("debt_to_equity", "pct_from_52w_low", "net_cash_pct_mcap", "net_assets_vs_mcap_pct"):
        return False
    return True


def _quantile_sorted(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation quantile on a pre-sorted non-empty list."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def compute_distribution_stats(values: list[float]) -> dict[str, float] | None:
    """
    Compute robust location & scale statistics for a 1D sample:
    - median, q25, q75, iqr
    - mad (Median Absolute Deviation from the median)
    - robust_sigma = max(1.4826 * MAD, 0.7413 * IQR)
    - bowley_skew = ((Q75 - Median) - (Median - Q25)) / IQR  (robust skewness in [-1, 1])
    """
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    med = _quantile_sorted(s, 0.5)
    q25 = _quantile_sorted(s, 0.25)
    q75 = _quantile_sorted(s, 0.75)
    iqr = max(0.0, q75 - q25)

    abs_devs = sorted(abs(x - med) for x in s)
    mad = _quantile_sorted(abs_devs, 0.5)
    sigma_mad = MAD_NORMAL_FACTOR * mad
    sigma_iqr = IQR_NORMAL_FACTOR * iqr
    robust_sigma = sigma_mad if sigma_mad > 1e-9 else sigma_iqr

    bowley_skew = 0.0
    if iqr > 1e-9:
        bowley_skew = ((q75 - med) - (med - q25)) / iqr

    mean = sum(s) / n
    return {
        "n": n,
        "mean": round(mean, 3),
        "median": round(med, 3),
        "q25": round(q25, 3),
        "q75": round(q75, 3),
        "iqr": round(iqr, 3),
        "mad": round(mad, 3),
        "robust_sigma": round(robust_sigma, 4),
        "bowley_skew": round(bowley_skew, 3),
        "_sorted": s,
    }


def score_value_against_dist(
    val: float,
    dist: dict[str, Any],
    lower_is_better: bool,
) -> tuple[float, float, float]:
    """
    Return `(raw_z, oriented_z, oriented_pctile)` for `val` against `dist`:
    - `raw_z`: robust z-score `(val - median) / robust_sigma` clipped to `[-Z_CLIP, +Z_CLIP]`
    - `oriented_z`: `-raw_z` if `lower_is_better` else `+raw_z` (so `> 0` is always better)
    - `oriented_pctile`: `0..100` where `100` is the best in the peer group
    """
    med = dist["median"]
    sigma = dist["robust_sigma"]
    sorted_vals = dist["_sorted"]
    n = len(sorted_vals)

    if sigma > 1e-9:
        raw_z = (val - med) / sigma
    else:
        raw_z = 0.0
    raw_z = max(-Z_CLIP, min(Z_CLIP, raw_z))
    oriented_z = -raw_z if lower_is_better else raw_z

    # Mid-rank empirical percentile
    lo_idx = bisect.bisect_left(sorted_vals, val)
    hi_idx = bisect.bisect_right(sorted_vals, val)
    mid_rank = 0.5 * (lo_idx + hi_idx)
    raw_pct = (mid_rank / n) * 100.0 if n > 0 else 50.0
    oriented_pct = (100.0 - raw_pct) if lower_is_better else raw_pct

    return round(raw_z, 2), round(oriented_z, 2), round(max(0.0, min(100.0, oriented_pct)), 1)


def z_to_0_100(z: float) -> float:
    """
    Smooth logistic/normal-like mapping from robust z-score `[-3, +3]` to `0..100`,
    centered at `50` (`z = 0 -> 50`, `z = +1 -> 73`, `z = +2 -> 88`, `z = -1 -> 27`).
    """
    return round(100.0 / (1.0 + math.exp(-1.0 * z)), 1)


def compute_cross_sectional_eda(
    stocks: list[dict[str, Any]],
    *,
    min_sector_peers: int = 5,
    value_weight: float = 0.45,
    quality_weight: float = 0.40,
    posture_weight: float = 0.15,
) -> dict[str, Any]:
    """
    Run cross-sectional EDA across `stocks`:
    1. Builds global and per-sector robust distributions for every metric in `METRIC_SPECS`.
    2. Computes per-stock sector-adjusted robust z-scores, percentiles, and the
       Multi-Factor Value/Quality Composite Score (`vq_score`, `val_z`, `qual_z`, `posture_z`).
    3. Returns a summary dictionary containing sector summaries, global distributions,
       and per-ticker factor profiles.
    """
    valid_stocks = [s for s in (stocks or []) if s and not s.get("_pending_yf")]

    # 1. Collect raw metric arrays globally and by sector
    global_vals: dict[str, list[float]] = {spec["key"]: [] for spec in METRIC_SPECS}
    sector_vals: dict[str, dict[str, list[float]]] = {}
    sector_counts: dict[str, int] = {}

    for s in valid_stocks:
        sec = str(s.get("sector") or "Unknown").strip() or "Unknown"
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        if sec not in sector_vals:
            sector_vals[sec] = {spec["key"]: [] for spec in METRIC_SPECS}
        for spec in METRIC_SPECS:
            k = spec["key"]
            v = extract_metric(s, k)
            if is_valid_metric_value(v, spec):
                global_vals[k].append(float(v))
                sector_vals[sec][k].append(float(v))

    # 2. Compute distribution stats
    global_dists: dict[str, dict[str, Any]] = {}
    for spec in METRIC_SPECS:
        k = spec["key"]
        st = compute_distribution_stats(global_vals[k])
        if st:
            global_dists[k] = st

    sector_dists: dict[str, dict[str, dict[str, Any]]] = {}
    for sec, by_metric in sector_vals.items():
        sector_dists[sec] = {}
        for spec in METRIC_SPECS:
            k = spec["key"]
            if len(by_metric[k]) >= min_sector_peers:
                st = compute_distribution_stats(by_metric[k])
                if st:
                    sector_dists[sec][k] = st

    # Normalize pillar weights
    w_sum = max(1e-9, value_weight + quality_weight + posture_weight)
    w_val = value_weight / w_sum
    w_qual = quality_weight / w_sum
    w_post = posture_weight / w_sum

    # 3. Score each stock
    profiles: dict[str, dict[str, Any]] = {}
    ranked_rows: list[dict[str, Any]] = []

    for s in valid_stocks:
        ticker = str(s.get("ticker") or "").upper()
        if not ticker:
            continue
        sec = str(s.get("sector") or "Unknown").strip() or "Unknown"
        sec_map = sector_dists.get(sec) or {}

        metric_scores: dict[str, dict[str, Any]] = {}
        group_weighted_z: dict[str, float] = {"value": 0.0, "quality": 0.0, "posture": 0.0}
        group_weight_sum: dict[str, float] = {"value": 0.0, "quality": 0.0, "posture": 0.0}

        for spec in METRIC_SPECS:
            k = spec["key"]
            v = extract_metric(s, k)
            if not is_valid_metric_value(v, spec):
                continue
            dist = sec_map.get(k) or global_dists.get(k)
            if not dist:
                continue
            scope = "sector" if k in sec_map else "list"
            raw_z, oriented_z, pctile = score_value_against_dist(
                float(v), dist, spec["lower_is_better"]
            )
            metric_scores[k] = {
                "value": round(float(v), 3),
                "raw_z": raw_z,
                "oriented_z": oriented_z,
                "pctile": pctile,
                "median": dist["median"],
                "mad": dist["mad"],
                "iqr": dist["iqr"],
                "n": dist["n"],
                "scope": scope,
            }
            grp = spec["group"]
            w = float(spec["weight"])
            group_weighted_z[grp] += oriented_z * w
            group_weight_sum[grp] += w

        val_z = (
            round(group_weighted_z["value"] / group_weight_sum["value"], 2)
            if group_weight_sum["value"] > 0
            else None
        )
        qual_z = (
            round(group_weighted_z["quality"] / group_weight_sum["quality"], 2)
            if group_weight_sum["quality"] > 0
            else None
        )
        posture_z = (
            round(group_weighted_z["posture"] / group_weight_sum["posture"], 2)
            if group_weight_sum["posture"] > 0
            else None
        )

        # Capital return bonus (buybacks + insider buys) in z-space (up to +0.50 sigma)
        cap_bonus_z = 0.0
        bb_pct = _num(s.get("annual_buyback_pct")) or 0.0
        if bb_pct > 0:
            cap_bonus_z += min(0.30, bb_pct * 0.05)
        elif s.get("buyback_announced"):
            cap_bonus_z += 0.15
        ins_buys = int(s.get("insider_buys_2026") or 0)
        if ins_buys > 0:
            cap_bonus_z += min(0.25, ins_buys * 0.10)

        # Combine available pillars into composite z-score
        comp_num = 0.0
        comp_den = 0.0
        if val_z is not None:
            comp_num += val_z * w_val
            comp_den += w_val
        if qual_z is not None:
            comp_num += qual_z * w_qual
            comp_den += w_qual
        if posture_z is not None:
            comp_num += posture_z * w_post
            comp_den += w_post

        if comp_den > 0:
            composite_z = round((comp_num / comp_den) + cap_bonus_z, 2)
            vq_score = z_to_0_100(composite_z)
        else:
            composite_z = None
            vq_score = None

        profile = {
            "ticker": ticker,
            "sector": sec,
            "vq_score": vq_score,
            "composite_z": composite_z,
            "val_z": val_z,
            "val_score": z_to_0_100(val_z) if val_z is not None else None,
            "qual_z": qual_z,
            "qual_score": z_to_0_100(qual_z) if qual_z is not None else None,
            "posture_z": posture_z,
            "posture_score": z_to_0_100(posture_z) if posture_z is not None else None,
            "cap_bonus_z": round(cap_bonus_z, 2),
            "metrics_used": len(metric_scores),
            "metrics": metric_scores,
        }
        profiles[ticker] = profile
        if vq_score is not None:
            ranked_rows.append(
                {
                    "ticker": ticker,
                    "name": s.get("name") or ticker,
                    "sector": sec,
                    "vq_score": vq_score,
                    "composite_z": composite_z,
                    "val_z": val_z,
                    "qual_z": qual_z,
                    "posture_z": posture_z,
                }
            )

    ranked_rows.sort(key=lambda r: (r["vq_score"], r["composite_z"]), reverse=True)

    # 4. Strip internal `_sorted` arrays from public distribution summary
    def _clean_dist(d: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in d.items() if k != "_sorted"}

    public_global = {k: _clean_dist(d) for k, d in global_dists.items()}
    public_sectors = {
        sec: {
            "count": sector_counts.get(sec, 0),
            "metrics": {k: _clean_dist(d) for k, d in m_map.items()},
        }
        for sec, m_map in sorted(sector_dists.items(), key=lambda x: -sector_counts.get(x[0], 0))
    }

    return {
        "total_stocks": len(stocks or []),
        "valid_stocks": len(valid_stocks),
        "min_sector_peers": min_sector_peers,
        "metric_specs": [
            {
                "key": sp["key"],
                "label": sp["label"],
                "group": sp["group"],
                "lower_is_better": sp["lower_is_better"],
                "weight": sp["weight"],
            }
            for sp in METRIC_SPECS
        ],
        "global_distributions": public_global,
        "sector_distributions": public_sectors,
        "top_outliers": ranked_rows[:25],
        "profiles": profiles,
    }
