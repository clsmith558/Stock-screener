"""
Quant opportunity scoring for stock screener.
Combines valuation percentile, price posture, insider buys, buyback-at-low.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "opportunity_weights.yaml"


def _load_weights() -> dict:
    defaults = {
        "valuation": 0.35,
        "price_posture": 0.25,
        "insider": 0.20,
        "buyback_at_low": 0.20,
        "near_52w_low_pct": 15,
        "below_200w_ma_bonus": True,
        "buyback_near_low_pct": 20,
        "insider_points_per_buy": 25,
        "insider_cap": 100,
    }
    if not _CONFIG_PATH.exists():
        return defaults
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        defaults.update({k: raw[k] for k in defaults if k in raw})
    except Exception:
        pass
    return defaults


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


def _percentile_rank(value: float, population: list[float], lower_is_better: bool = True) -> float:
    """Return 0-100 score where higher = better opportunity."""
    clean = [v for v in population if v is not None and v > 0]
    if not clean or value is None or value <= 0:
        return 50.0
    if lower_is_better:
        rank = sum(1 for v in clean if v >= value) / len(clean)
    else:
        rank = sum(1 for v in clean if v <= value) / len(clean)
    return round(rank * 100, 1)


def _valuation_score(stock: dict, peers: list[dict], weights: dict) -> float:
    m = stock.get("metrics") or {}
    p_fcf = _safe_float(stock.get("p_fcf") or m.get("p_fcf"))
    ev_ebitda = _safe_float(m.get("ev_ebitda"))
    fcf_yield = _safe_float(m.get("fcf_yield"))

    pfcf_vals = [_safe_float(s.get("p_fcf") or (s.get("metrics") or {}).get("p_fcf")) for s in peers]
    ev_vals = [_safe_float((s.get("metrics") or {}).get("ev_ebitda")) for s in peers]
    fcf_vals = [_safe_float((s.get("metrics") or {}).get("fcf_yield")) for s in peers]

    parts = []
    if p_fcf and p_fcf > 0:
        parts.append(_percentile_rank(p_fcf, pfcf_vals, lower_is_better=True))
    if ev_ebitda and ev_ebitda > 0:
        parts.append(_percentile_rank(ev_ebitda, ev_vals, lower_is_better=True))
    if fcf_yield and fcf_yield > 0:
        parts.append(_percentile_rank(fcf_yield, fcf_vals, lower_is_better=False))
    return round(sum(parts) / max(len(parts), 1), 1) if parts else 50.0


def _price_posture_score(stock: dict, weights: dict) -> float:
    gap = _safe_float(stock.get("pct_from_52w_low"), 999)
    ma_gap = _safe_float(stock.get("pct_from_200w_ma") or (stock.get("metrics") or {}).get("pct_from_200w_ma"), 0)
    score = 0.0
    near = weights.get("near_52w_low_pct", 15)
    if gap < near:
        score += 60 + max(0, (near - gap) * 2)
    elif gap < 30:
        score += 40
    elif gap < 50:
        score += 20
    if weights.get("below_200w_ma_bonus") and ma_gap is not None and ma_gap < 0:
        score += min(25, abs(ma_gap))
    return min(100.0, round(score, 1))


def _insider_score(stock: dict, weights: dict) -> float:
    n = int(stock.get("insider_buys_2026") or 0)
    if n <= 0:
        return 0.0
    per = weights.get("insider_points_per_buy", 25)
    cap = weights.get("insider_cap", 100)
    return min(cap, n * per)


def _buyback_at_low_score(stock: dict, weights: dict) -> float:
    gap = _safe_float(stock.get("pct_from_52w_low"), 999)
    threshold = weights.get("buyback_near_low_pct", 20)
    has_buyback = bool(stock.get("buyback_announced"))
    annual_pct = _safe_float(stock.get("annual_buyback_pct"), 0)
    hk_jp_buyback = (stock.get("buyback_source") or "").upper() in ("HKEX", "TSE", "HK", "JP", "ASX")

    if not has_buyback and not annual_pct and not hk_jp_buyback:
        if not stock.get("buyback_date"):
            return 0.0

    near_low = gap < threshold
    if has_buyback and near_low:
        return 90.0
    if annual_pct and annual_pct > 1 and near_low:
        return min(100.0, 70 + annual_pct * 3)
    if hk_jp_buyback and near_low:
        return 75.0
    if has_buyback or annual_pct or hk_jp_buyback:
        return 40.0
    return 0.0


def compute_quant_score(stock: dict, peers: list[dict] | None = None, weights: dict | None = None) -> dict:
    """Return quant_score 0-100 and component breakdown."""
    w = weights or _load_weights()
    peers = peers or []
    components = {
        "valuation": _valuation_score(stock, peers, w),
        "price_posture": _price_posture_score(stock, w),
        "insider": _insider_score(stock, w),
        "buyback_at_low": _buyback_at_low_score(stock, w),
    }
    total = (
        components["valuation"] * w["valuation"]
        + components["price_posture"] * w["price_posture"]
        + components["insider"] * w["insider"]
        + components["buyback_at_low"] * w["buyback_at_low"]
    )
    return {
        "quant_score": round(total, 1),
        "quant_components": components,
    }


def enrich_stocks_batch(stocks: list[dict]) -> list[dict]:
    """Attach quant_score to each stock dict (mutates copies)."""
    if not stocks:
        return stocks
    out = []
    for s in stocks:
        row = dict(s)
        extra = compute_quant_score(row, peers=stocks)
        row.update(extra)
        out.append(row)
    return out