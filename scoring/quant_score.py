"""
Quant opportunity scoring for stock screener.
Combines valuation percentile, price posture, insider buys, buyback-at-low.

Dashboard Score = quant_score only (see docs/DASHBOARD_FORMULAS.md).
Vault/Obsidian qual does not enter the score.
"""

from __future__ import annotations

import bisect
import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "opportunity_weights.yaml"


def default_weights() -> dict:
    return {
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


def _load_weights() -> dict:
    defaults = default_weights()
    if not _CONFIG_PATH.exists():
        return defaults
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        defaults.update({k: raw[k] for k in defaults if k in raw})
    except Exception:
        pass
    return defaults


def get_weights_public() -> dict:
    """JSON-safe weights for the dashboard editor."""
    w = _load_weights()
    return {
        "weights": w,
        "component_keys": ["valuation", "price_posture", "insider", "buyback_at_low"],
        "path": str(_CONFIG_PATH),
        "notes": {
            "valuation": "Peer-relative P/FCF, EV/EBITDA, FCF yield",
            "price_posture": "Near 52w low + optional below 200w MA bonus",
            "insider": "Points per open-market buy (capped)",
            "buyback_at_low": "Buyback signal while near 52w low",
        },
    }


def save_weights(updates: dict) -> dict:
    """
    Merge updates into opportunity_weights.yaml.
    Component weights valuation/price_posture/insider/buyback_at_low are
    re-normalized to sum to 1.0 when any of them is provided.
    """
    cur = _load_weights()
    if not updates:
        return get_weights_public()
    component_keys = ["valuation", "price_posture", "insider", "buyback_at_low"]
    for k, v in updates.items():
        if k not in cur:
            continue
        if k == "below_200w_ma_bonus":
            cur[k] = bool(v)
            continue
        try:
            cur[k] = float(v)
        except (TypeError, ValueError):
            continue
    # Normalize the four score weights if any touched
    if any(k in updates for k in component_keys):
        vals = [max(0.0, float(cur[k])) for k in component_keys]
        s = sum(vals)
        if s <= 0:
            for k in component_keys:
                cur[k] = 0.25
        else:
            for k, v in zip(component_keys, vals):
                cur[k] = round(v / s, 4)
            # Fix float drift on last component
            cur[component_keys[-1]] = round(
                1.0 - sum(cur[k] for k in component_keys[:-1]), 4
            )
    # Clamp thresholds
    cur["near_52w_low_pct"] = max(1, min(50, int(round(cur["near_52w_low_pct"]))))
    cur["buyback_near_low_pct"] = max(1, min(50, int(round(cur["buyback_near_low_pct"]))))
    cur["insider_points_per_buy"] = max(1, min(100, int(round(cur["insider_points_per_buy"]))))
    cur["insider_cap"] = max(10, min(100, int(round(cur["insider_cap"]))))

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Preserve comments-ish structure with a short header
    lines = [
        "# Quant score component weights (must sum to 1.0)",
        f"valuation: {cur['valuation']}",
        f"price_posture: {cur['price_posture']}",
        f"insider: {cur['insider']}",
        f"buyback_at_low: {cur['buyback_at_low']}",
        "",
        "# Price posture thresholds",
        f"near_52w_low_pct: {cur['near_52w_low_pct']}",
        f"below_200w_ma_bonus: {str(bool(cur['below_200w_ma_bonus'])).lower()}",
        "",
        "# Buyback-at-low: pct_from_52w_low must be below this",
        f"buyback_near_low_pct: {cur['buyback_near_low_pct']}",
        "",
        "# Insider: points per buy transaction (capped)",
        f"insider_points_per_buy: {cur['insider_points_per_buy']}",
        f"insider_cap: {cur['insider_cap']}",
        "",
    ]
    _CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    return get_weights_public()


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
    clean.sort()
    return _percentile_in_sorted(value, clean, lower_is_better=lower_is_better)


def _percentile_in_sorted(
    value: float, sorted_vals: list[float], lower_is_better: bool = True
) -> float:
    if not sorted_vals or value is None or value <= 0:
        return 50.0
    n = len(sorted_vals)
    if lower_is_better:
        i = bisect.bisect_left(sorted_vals, value)
        rank = (n - i) / n
    else:
        i = bisect.bisect_right(sorted_vals, value)
        rank = i / n
    return round(rank * 100, 1)


def build_peer_value_lists(peers: list[dict] | None) -> dict[str, list[float]]:
    """Sort unique-enough metric lists once per list load (O(n log n))."""
    pfcf: list[float] = []
    ev: list[float] = []
    fcf: list[float] = []
    for s in peers or []:
        m = s.get("metrics") or {}
        v = _safe_float(s.get("p_fcf") or m.get("p_fcf"))
        if v and v > 0:
            pfcf.append(v)
        v = _safe_float(m.get("ev_ebitda"))
        if v and v > 0:
            ev.append(v)
        v = _safe_float(m.get("fcf_yield"))
        if v and v > 0:
            fcf.append(v)
    pfcf.sort()
    ev.sort()
    fcf.sort()
    return {"pfcf": pfcf, "ev": ev, "fcf": fcf}


def _valuation_score(
    stock: dict,
    peers: list[dict],
    weights: dict,
    peer_vals: dict[str, list[float]] | None = None,
) -> float:
    m = stock.get("metrics") or {}
    p_fcf = _safe_float(stock.get("p_fcf") or m.get("p_fcf"))
    ev_ebitda = _safe_float(m.get("ev_ebitda"))
    fcf_yield = _safe_float(m.get("fcf_yield"))

    pv = peer_vals or build_peer_value_lists(peers)
    parts = []
    if p_fcf and p_fcf > 0:
        parts.append(_percentile_in_sorted(p_fcf, pv.get("pfcf") or [], lower_is_better=True))
    if ev_ebitda and ev_ebitda > 0:
        parts.append(_percentile_in_sorted(ev_ebitda, pv.get("ev") or [], lower_is_better=True))
    if fcf_yield and fcf_yield > 0:
        parts.append(_percentile_in_sorted(fcf_yield, pv.get("fcf") or [], lower_is_better=False))
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


def compute_quant_score(
    stock: dict,
    peers: list[dict] | None = None,
    weights: dict | None = None,
    peer_vals: dict[str, list[float]] | None = None,
) -> dict:
    """Return quant_score 0-100 and component breakdown."""
    w = weights or _load_weights()
    peers = peers or []
    components = {
        "valuation": _valuation_score(stock, peers, w, peer_vals=peer_vals),
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
    peer_vals = build_peer_value_lists(stocks)
    out = []
    for s in stocks:
        row = dict(s)
        extra = compute_quant_score(row, peers=stocks, peer_vals=peer_vals)
        row.update(extra)
        out.append(row)
    return out