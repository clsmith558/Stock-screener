"""
Join commodity strips + mining unit costs + stock prices for the Commodities tab.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from data.commodity_prices import (
    get_last_price,
    get_series,
    load_commodity_config,
    load_commodity_frame,
    series_stats,
)
from data.yf_helpers import get_fx_rate, pct_from_52w_low, quote_price, reconcile_52w_range, safe_float

logger = logging.getLogger(__name__)

_stock_hist_cache: dict[str, list[dict]] = {}
_hist_lock = threading.Lock()


def _resolve_unit_cost(producer: dict) -> dict[str, Any]:
    """
    Resolve unit cost in a currency suitable for margin vs commodity strip.
    Returns {value, currency, unit, source, raw_value, raw_currency, fx_to_usd, note}
    """
    ticker = (producer.get("ticker") or "").strip().upper()
    source = (producer.get("cost_source") or "yaml").lower()
    out: dict[str, Any] = {
        "value": None,
        "currency": "USD",
        "unit": "",
        "source": None,
        "raw_value": None,
        "raw_currency": None,
        "fx_to_usd": 1.0,
        "note": "",
        "asof": None,
        "snippet": "",
        "metric": "",
    }

    # 1) YAML override
    if producer.get("unit_cost") is not None:
        val = safe_float(producer.get("unit_cost"), None)
        cur = (producer.get("unit_cost_currency") or "USD").upper()
        unit = producer.get("unit_cost_unit") or ""
        out.update(_to_usd_cost(val, cur, unit, "yaml"))
        out["asof"] = producer.get("unit_cost_asof") or None
        out["metric"] = "yaml_break_even"
        return out

    # 2) ASX mining (quarterly / annual PDF parse cache)
    if source in ("asx_mining", "asx") or ticker.endswith(".AX"):
        try:
            from data.asx_mining import get_asx_mining_summary

            s = get_asx_mining_summary(ticker, max_age_days=0)
            if s and s.get("cost_value") is not None:
                cur = "AUD" if "A$" in (s.get("cost_unit") or s.get("cost_display") or "") else "USD"
                unit = "t" if "t" in (s.get("cost_unit") or "t").lower() else "oz"
                if "oz" in (s.get("cost_unit") or "").lower():
                    unit = "oz"
                out.update(
                    _to_usd_cost(
                        safe_float(s.get("cost_value")),
                        cur,
                        unit,
                        "asx_mining",
                    )
                )
                out["note"] = s.get("cost_display") or ""
                out["snippet"] = (s.get("cost_snippet") or "")[:200]
                out["metric"] = s.get("cost_metric") or "asx_reported"
                out["asof"] = (s.get("last_scanned") or "")[:10] or None
                return out
        except Exception as e:
            logger.debug("asx cost resolve %s: %s", ticker, e)

    # 3) US mining (10-K / earnings non-GAAP parse cache)
    if source in ("us_mining", "us", "yaml") or not ticker.endswith(".AX"):
        try:
            from data.mining_costs import get_costs_summary

            base = ticker.replace(".AX", "")
            s = get_costs_summary(base, max_age_days=0)
            if s and s.get("cost_value") is not None:
                unit_s = (s.get("cost_unit") or "").lower()
                unit = "oz" if "oz" in unit_s else ("lb" if "lb" in unit_s else "t")
                out.update(
                    _to_usd_cost(
                        safe_float(s.get("cost_value")),
                        "USD",
                        unit,
                        "us_mining",
                    )
                )
                out["note"] = s.get("cost_display") or s.get("cost_metric") or ""
                out["snippet"] = (s.get("cost_snippet") or "")[:200]
                out["metric"] = s.get("cost_metric") or "us_reported"
                out["asof"] = (s.get("filing_date") or s.get("last_scanned") or "")[:10] or None
                return out
        except Exception as e:
            logger.debug("us cost resolve %s: %s", ticker, e)

    # 4) Latest multi-period history point (iron C1 / coal FOB / AISC series)
    try:
        from data.unit_cost_history import get_cost_history

        hist = get_cost_history(ticker)
        if hist:
            last = hist[-1]
            unit = last.get("cost_unit") or "t"
            out.update(
                _to_usd_cost(
                    safe_float(last.get("cost_value_usd")),
                    "USD",
                    unit,
                    last.get("source") or "cost_history",
                )
            )
            out["note"] = last.get("label") or last.get("cost_metric") or ""
            out["snippet"] = (last.get("snippet") or "")[:200]
            out["metric"] = last.get("cost_metric") or "history"
            out["asof"] = (last.get("period_end") or "")[:10] or None
            return out
    except Exception as e:
        logger.debug("history cost resolve %s: %s", ticker, e)

    return out


def _to_usd_cost(value: float | None, currency: str, unit: str, source: str) -> dict:
    if value is None:
        return {
            "value": None,
            "currency": "USD",
            "unit": unit,
            "source": source,
            "raw_value": None,
            "raw_currency": currency,
            "fx_to_usd": 1.0,
            "note": "",
            "asof": None,
            "snippet": "",
            "metric": "",
        }
    cur = (currency or "USD").upper()
    fx = 1.0
    raw_val = value
    if cur not in ("USD", "USDT"):
        rate = get_fx_rate(cur, "USD")
        if rate and rate > 0:
            fx = rate
            value = value * fx
        else:
            return {
                "value": None,
                "currency": "USD",
                "unit": unit,
                "source": source,
                "raw_value": raw_val,
                "raw_currency": cur,
                "fx_to_usd": None,
                "note": f"FX missing {cur}->USD",
                "asof": None,
                "snippet": "",
                "metric": "",
            }
    return {
        "value": round(value, 4),
        "currency": "USD",
        "unit": unit,
        "source": source,
        "raw_value": round(raw_val, 4),
        "raw_currency": cur,
        "fx_to_usd": round(fx, 6),
        "note": "",
        "asof": None,
        "snippet": "",
        "metric": "",
    }


def _stock_history(ticker: str, days: int = 400) -> list[dict]:
    """Daily close history [{date, close}]."""
    sym = (ticker or "").strip().upper()
    with _hist_lock:
        if sym in _stock_hist_cache:
            return _stock_hist_cache[sym]
    out: list[dict] = []
    try:
        t = yf.Ticker(sym)
        end = datetime.now()
        start = end - timedelta(days=days)
        hist = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"), auto_adjust=True)
        if hist is not None and not hist.empty:
            for dt, row in hist.iterrows():
                c = row.get("Close")
                if c is not None and c == c:
                    out.append({"date": dt.strftime("%Y-%m-%d"), "close": float(c)})
    except Exception as e:
        logger.debug("stock history %s: %s", sym, e)
    with _hist_lock:
        _stock_hist_cache[sym] = out
    return out


def _stock_snapshot(ticker: str) -> dict:
    sym = (ticker or "").strip().upper()
    try:
        from data.business_lists import load_yf_cached
        from data.yf_helpers import pct_from_52w_low as _pct_low

        cached = load_yf_cached([sym]).get(sym)
        if cached and cached.get("price"):
            price = cached.get("price") or 0
            low = cached.get("low_52w") or 0
            high = cached.get("high_52w") or 0
            m = cached.get("metrics") or {}
            return {
                "price": round(float(price), 4) if price else None,
                "name": cached.get("name") or sym,
                "currency": (m.get("currency") or "USD").upper(),
                "low_52w": low or None,
                "high_52w": high or None,
                "pct_from_52w_low": _pct_low(price, low) if price and low else None,
                "sector": cached.get("sector") or m.get("sector") or "",
                "industry": m.get("industry") or "",
            }
    except Exception:
        pass
    try:
        info = yf.Ticker(sym).info or {}
        price = quote_price(info)
        low, high = reconcile_52w_range(
            price, info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
        )
        pct_low = pct_from_52w_low(price, low) if price and low else None
        return {
            "price": round(price, 4) if price else None,
            "name": info.get("shortName") or info.get("longName") or sym,
            "currency": (info.get("currency") or "USD").upper(),
            "low_52w": low or None,
            "high_52w": high or None,
            "pct_from_52w_low": pct_low,
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
        }
    except Exception as e:
        logger.warning("stock snapshot failed %s: %s", sym, e)
        return {
            "price": None,
            "name": sym,
            "currency": "USD",
            "low_52w": None,
            "high_52w": None,
            "pct_from_52w_low": None,
            "sector": "",
            "industry": "",
        }


def _align_ratio_series(
    stock_hist: list[dict],
    cmdty_series: list[dict],
) -> list[dict]:
    """Inner-join by date: ratio = stock_close / commodity_last."""
    c_map = {p["date"]: p["last"] for p in cmdty_series if p.get("last")}
    out = []
    for p in stock_hist:
        d = p["date"]
        if d in c_map and p.get("close") and c_map[d]:
            out.append(
                {
                    "date": d,
                    "stock": p["close"],
                    "commodity": c_map[d],
                    "ratio": p["close"] / c_map[d],
                }
            )
    return out


def _zscore(values: list[float], latest: float) -> float | None:
    if len(values) < 5:
        return None
    try:
        mu = statistics.mean(values)
        sd = statistics.pstdev(values)
        if not sd or sd < 1e-12:
            return 0.0
        return round((latest - mu) / sd, 3)
    except Exception:
        return None


def build_producer_row(producer: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or load_commodity_config()
    ticker = (producer.get("ticker") or "").strip().upper()
    primary = producer.get("primary") or (producer.get("commodities") or [None])[0]
    commodities_meta = cfg.get("commodities") or {}
    meta = commodities_meta.get(primary) or {}

    cmdty_series = get_series(primary) if primary else []
    cmdty_last, cmdty_date = get_last_price(primary) if primary else (None, None)
    cmdty_stats = series_stats(cmdty_series)

    cost = _resolve_unit_cost(producer)
    snap = _stock_snapshot(ticker)
    stock_hist = _stock_history(ticker)
    ratio_series = _align_ratio_series(stock_hist, cmdty_series)

    lookback = int(cfg.get("ratio_z_lookback") or 90)
    ratios = [r["ratio"] for r in ratio_series]
    recent_ratios = ratios[-lookback:] if len(ratios) > lookback else ratios
    ratio_now = ratios[-1] if ratios else None
    ratio_z = _zscore(recent_ratios, ratio_now) if ratio_now is not None else None

    def _ret_hist(hist, key_field, days):
        pts = [{"date": h["date"], "v": h.get(key_field)} for h in hist]
        if len(pts) < 2 or pts[-1].get("v") is None:
            return None
        end = pts[-1]
        try:
            end_d = datetime.fromisoformat(end["date"])
            start_target = (end_d - timedelta(days=int(days * 1.5))).date().isoformat()
        except Exception:
            start_target = pts[max(0, len(pts) - days)]["date"]
        base = next((p for p in pts if p["date"] >= start_target and p.get("v")), None)
        if not base or not base.get("v"):
            return None
        return round((end["v"] / base["v"] - 1) * 100, 2)

    stock_ret_3m = _ret_hist(stock_hist, "close", 63)
    stock_ret_6m = _ret_hist(stock_hist, "close", 126)
    cmdty_ret_3m = (cmdty_stats.get("returns") or {}).get("3m")
    cmdty_ret_6m = (cmdty_stats.get("returns") or {}).get("6m")
    relative_3m = None
    if stock_ret_3m is not None and cmdty_ret_3m is not None:
        relative_3m = round(stock_ret_3m - cmdty_ret_3m, 2)

    unit_margin = None
    margin_pct = None
    above_cost = None
    if cmdty_last is not None and cost.get("value") is not None:
        unit_margin = round(cmdty_last - cost["value"], 4)
        margin_pct = round(unit_margin / cmdty_last * 100, 2) if cmdty_last else None
        above_cost = unit_margin > 0

    uv_z = float(cfg.get("undervalued_ratio_z_max") or -1.0)
    uv_m = float(cfg.get("undervalued_min_margin_pct") or 15.0)
    undervalued = bool(
        ratio_z is not None
        and ratio_z <= uv_z
        and margin_pct is not None
        and margin_pct >= uv_m
    )
    recovery = bool(
        above_cost
        and snap.get("pct_from_52w_low") is not None
        and snap["pct_from_52w_low"] <= 25
        and (relative_3m is not None and relative_3m < 0)
    )
    depressed_vs_cmdty = bool(relative_3m is not None and relative_3m <= -10)

    return {
        "ticker": ticker,
        "name": producer.get("name") or snap.get("name") or ticker,
        "primary_commodity": primary,
        "commodity_display": meta.get("display") or primary,
        "commodity_unit": meta.get("unit") or "",
        "commodities": producer.get("commodities") or ([primary] if primary else []),
        "commodity_last": cmdty_last,
        "commodity_last_date": cmdty_date,
        "commodity_ret_3m": cmdty_ret_3m,
        "commodity_ret_6m": cmdty_ret_6m,
        "unit_cost": cost.get("value"),
        "unit_cost_currency": cost.get("currency"),
        "unit_cost_unit": cost.get("unit"),
        "unit_cost_source": cost.get("source"),
        "unit_cost_note": cost.get("note") or "",
        "unit_cost_fx_to_usd": cost.get("fx_to_usd"),
        "unit_cost_asof": cost.get("asof"),
        "unit_cost_snippet": cost.get("snippet") or "",
        "unit_cost_metric": cost.get("metric") or "",
        "unit_cost_raw": cost.get("raw_value"),
        "unit_cost_raw_currency": cost.get("raw_currency"),
        "unit_margin": unit_margin,
        "margin_pct": margin_pct,
        "above_cost": above_cost,
        "stock_price": snap.get("price"),
        "stock_currency": snap.get("currency"),
        "pct_from_52w_low": snap.get("pct_from_52w_low"),
        "stock_ret_3m": stock_ret_3m,
        "stock_ret_6m": stock_ret_6m,
        "relative_3m": relative_3m,
        "ratio": round(ratio_now, 6) if ratio_now is not None else None,
        "ratio_z": ratio_z,
        "ratio_n": len(ratio_series),
        "depressed_vs_commodity": depressed_vs_cmdty,
        "undervalued_candidate": undervalued,
        "recovery_candidate": recovery,
        "sector": snap.get("sector"),
        "industry": snap.get("industry"),
        # chart series (normalized later in API if needed)
        "ratio_history": ratio_series[-120:],
    }


def list_producers(force: bool = False) -> dict:
    """Full payload for Commodities tab producers table."""
    cfg = load_commodity_config()
    # warm commodity frame
    load_commodity_frame(force=force)
    producers = list(cfg.get("producers") or [])
    rows_by: dict[int, dict] = {}

    def _one(idx: int, prod: dict) -> None:
        try:
            rows_by[idx] = build_producer_row(prod, cfg)
        except Exception as e:
            logger.exception("producer row failed %s: %s", prod.get("ticker"), e)
            rows_by[idx] = {
                "ticker": prod.get("ticker"),
                "name": prod.get("name"),
                "error": str(e),
                "primary_commodity": prod.get("primary"),
            }

    workers = min(8, max(1, len(producers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, i, p) for i, p in enumerate(producers)]
        for fut in as_completed(futs):
            fut.result()
    rows = [rows_by[i] for i in range(len(producers)) if i in rows_by]
    return {
        "producers": rows,
        "count": len(rows),
        "filter_commodities": listed_filter_commodities(cfg),
        "thresholds": {
            "undervalued_ratio_z_max": cfg.get("undervalued_ratio_z_max"),
            "undervalued_min_margin_pct": cfg.get("undervalued_min_margin_pct"),
            "ratio_z_lookback": cfg.get("ratio_z_lookback"),
        },
        "generated_at": datetime.now().isoformat(),
    }


def producer_detail(ticker: str) -> dict:
    """Detail for one producer: row + aligned history for charts."""
    cfg = load_commodity_config()
    t = (ticker or "").strip().upper()
    entry = None
    for p in cfg.get("producers") or []:
        if (p.get("ticker") or "").upper() == t:
            entry = p
            break
    if not entry:
        return {"error": "not in commodity_producers.yaml", "ticker": t}
    row = build_producer_row(entry, cfg)
    primary = entry.get("primary")
    cmdty = get_series(primary) if primary else []
    stock = _stock_history(t)
    # Multi-quarter cost history (forward-filled onto daily chart dates)
    cost_hist = []
    try:
        from data.unit_cost_history import get_cost_history

        cost_hist = get_cost_history(t)
    except Exception as e:
        logger.debug("cost history load %s: %s", t, e)

    # Dual scale: stock as index=100; commodity + reported unit cost in strip units (USD)
    ratio_hist = row.get("ratio_history") or []
    unit_cost_snapshot = row.get("unit_cost")
    chart = []
    if ratio_hist:
        s0 = ratio_hist[0]["stock"]
        c0 = ratio_hist[0]["commodity"]
        for p in ratio_hist:
            cpx = p["commodity"]
            # cost known as of this date: last history point with period_end <= date
            cost_t = _cost_asof(cost_hist, p["date"])
            if cost_t is None:
                cost_t = unit_cost_snapshot
            margin = round(cpx - cost_t, 4) if cost_t is not None and cpx is not None else None
            chart.append(
                {
                    "date": p["date"],
                    "stock": p["stock"],
                    "stock_idx": round(p["stock"] / s0 * 100, 2) if s0 else None,
                    "commodity": cpx,
                    "commodity_idx": round(cpx / c0 * 100, 2) if c0 and cpx else None,
                    "unit_cost": cost_t,
                    "margin": margin,
                    "ratio": p["ratio"],
                }
            )
    has_hist = len(cost_hist) >= 2
    has_cost = unit_cost_snapshot is not None or bool(cost_hist)

    vol_hist = _volume_points(t)
    vol_unit = (vol_hist[-1].get("unit") if vol_hist else "") or ""
    volume_chart = []
    if chart:
        for p in chart:
            volume_chart.append(
                {
                    "date": p["date"],
                    "commodity": p["commodity"],
                    "volume": _volume_asof(vol_hist, p["date"]),
                }
            )
    elif cmdty and vol_hist:
        window = cmdty[-COHORT_CHART_POINTS:]
        for p in window:
            volume_chart.append(
                {
                    "date": p["date"],
                    "commodity": p.get("last"),
                    "volume": _volume_asof(vol_hist, p["date"]),
                }
            )

    corr = _correlations(cmdty, stock, cost_hist, vol_hist)

    return {
        "producer": row,
        "chart": chart,
        "volume_chart": volume_chart,
        "volume_history": vol_hist,
        "volume_unit": vol_unit,
        "correlations": corr,
        "cost_history": [
            {
                "period_end": h["period_end"],
                "cost_value_usd": h["cost_value_usd"],
                "cost_metric": h.get("cost_metric"),
                "source": h.get("source"),
                "label": h.get("label"),
                "snippet": (h.get("snippet") or "")[:160],
            }
            for h in cost_hist
        ],
        "chart_meta": {
            "unit_cost": unit_cost_snapshot,
            "unit_cost_label": _cost_chart_label(row),
            "commodity_unit": row.get("commodity_unit") or "",
            "has_cost": has_cost,
            "has_cost_history": has_hist,
            "cost_history_n": len(cost_hist),
            "has_volume": len(vol_hist) >= 1,
            "volume_n": len(vol_hist),
            "volume_unit": vol_unit,
        },
        "commodity_series_n": len(cmdty),
        "stock_series_n": len(stock),
    }


def _cost_asof(cost_hist: list[dict], date: str) -> float | None:
    """Latest cost with period_end <= date; if none yet, None (caller may use snapshot)."""
    if not cost_hist:
        return None
    best = None
    for h in cost_hist:
        pe = h.get("period_end") or ""
        if pe <= date and h.get("cost_value_usd") is not None:
            best = h["cost_value_usd"]
    # if date before first period, use first known (avoid empty early chart)
    if best is None and cost_hist[0].get("cost_value_usd") is not None:
        if date < (cost_hist[0].get("period_end") or ""):
            return None
    return best


def _cost_chart_label(row: dict) -> str:
    """Human label for the constant cost line."""
    if row.get("unit_cost") is None:
        return "Unit cost (n/a)"
    parts = []
    src = row.get("unit_cost_source") or ""
    metric = row.get("unit_cost_metric") or ""
    note = row.get("unit_cost_note") or ""
    asof = row.get("unit_cost_asof") or ""
    raw = row.get("unit_cost_raw")
    raw_ccy = row.get("unit_cost_raw_currency") or ""
    if note:
        parts.append(str(note))
    elif metric:
        parts.append(str(metric).replace("_", " "))
    else:
        parts.append(f"${row['unit_cost']:.1f} USD")
    if raw is not None and raw_ccy and raw_ccy != "USD":
        parts.append(f"(from {raw_ccy} {raw:.1f})")
    if src:
        parts.append(src)
    if asof:
        parts.append(f"as of {asof}")
    return " · ".join(parts)


COHORT_CHART_POINTS = 400
THIN_N = 8


def producer_lists_commodity(producer: dict, commodity_id: str) -> bool:
    cid = (commodity_id or "").strip()
    if not cid:
        return False
    if (producer.get("primary") or "") == cid:
        return True
    return cid in (producer.get("commodities") or [])


def listed_filter_commodities(cfg: dict | None = None) -> list[dict]:
    """Commodity ids that appear on at least one producer (for the tab filter)."""
    cfg = cfg or load_commodity_config()
    meta = cfg.get("commodities") or {}
    seen: list[str] = []
    for p in cfg.get("producers") or []:
        for cid in [p.get("primary"), *(p.get("commodities") or [])]:
            if cid and cid not in seen:
                seen.append(cid)
    out = []
    for cid in seen:
        m = meta.get(cid) or {}
        out.append(
            {
                "id": cid,
                "display": m.get("display") or cid,
                "unit": m.get("unit") or "",
            }
        )
    return out


def _ols_adj_r2(xs: list[float], ys: list[float]) -> dict:
    n = len(xs)
    empty = {"adj_r2": None, "r2": None, "n": n, "thin": True}
    if n < 3 or n != len(ys):
        return empty
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-18:
        return empty
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    if ss_tot < 1e-18:
        return empty
    r2 = 1.0 - ss_res / ss_tot
    adj = 1.0 - (1.0 - r2) * (n - 1) / (n - 2)
    return {
        "adj_r2": round(adj, 4),
        "r2": round(r2, 4),
        "n": n,
        "thin": n < THIN_N,
    }


def _log_return_pairs(pairs: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for i in range(1, len(pairs)):
        x0, y0 = pairs[i - 1]
        x1, y1 = pairs[i]
        if x0 and y0 and x1 and y1 and x0 > 0 and y0 > 0:
            xs.append(math.log(x1 / x0))
            ys.append(math.log(y1 / y0))
    return xs, ys


def _cmdty_asof(series: list[dict], date: str) -> float | None:
    best = None
    for p in series:
        if p.get("date") and p["date"] <= date and p.get("last") is not None:
            best = p["last"]
    return best


def _volume_points(ticker: str) -> list[dict]:
    try:
        from data.operating_extract import get_operating_history

        hist = get_operating_history(ticker)
        if not any(h.get("field") == "production" for h in hist) and ticker.endswith(".AX"):
            hist = get_operating_history(ticker.replace(".AX", ""))
    except Exception as e:
        logger.debug("volume hist %s: %s", ticker, e)
        return []
    out = []
    for h in hist:
        if h.get("field") != "production" or h.get("value") is None:
            continue
        out.append(
            {
                "period_end": h["period_end"],
                "value": h["value"],
                "unit": h.get("unit") or "",
                "commodity": h.get("commodity") or "",
                "source": h.get("source") or "",
                "snippet": (h.get("snippet") or "")[:160],
            }
        )
    out.sort(key=lambda x: x["period_end"])
    return out


def _volume_asof(vol_hist: list[dict], date: str) -> float | None:
    best = None
    for h in vol_hist:
        pe = h.get("period_end") or ""
        if pe <= date and h.get("value") is not None:
            best = h["value"]
    return best


def _correlations(
    cmdty: list[dict],
    stock_hist: list[dict],
    cost_hist: list[dict],
    vol_hist: list[dict],
) -> dict:
    aligned = _align_ratio_series(stock_hist, cmdty)
    stock_pairs = [(p["commodity"], p["stock"]) for p in aligned]
    sx, sy = _log_return_pairs(stock_pairs)
    price_stock = _ols_adj_r2(sx, sy)

    vol_pairs = []
    for h in vol_hist:
        c = _cmdty_asof(cmdty, h["period_end"])
        if c is not None and h.get("value"):
            vol_pairs.append((c, float(h["value"])))
    vx, vy = _log_return_pairs(vol_pairs)
    price_volume = _ols_adj_r2(vx, vy)

    cost_pairs = []
    for h in cost_hist:
        pe = h.get("period_end") or ""
        c = _cmdty_asof(cmdty, pe)
        cv = h.get("cost_value_usd")
        if c is not None and cv:
            cost_pairs.append((c, float(cv)))
    cx, cy = _log_return_pairs(cost_pairs)
    price_cost = _ols_adj_r2(cx, cy)

    return {
        "price_vs_stock": {**price_stock, "label": "Commodity vs stock (daily log returns)"},
        "price_vs_volume": {
            **price_volume,
            "label": "Commodity vs volume (qtr log returns)",
        },
        "price_vs_cost": {**price_cost, "label": "Commodity vs unit cost (qtr log returns)"},
    }


def commodity_cohort_panel(commodity_id: str) -> dict:
    """Strip + every mapped producer's unit-cost step for the cohort chart."""
    cfg = load_commodity_config()
    cid = (commodity_id or "").strip()
    meta = (cfg.get("commodities") or {}).get(cid) or {}
    if not cid:
        return {"error": "missing commodity id"}
    cmdty = get_series(cid)
    if not cmdty:
        return {"error": f"no series for {cid}", "commodity_id": cid}
    window = cmdty[-COHORT_CHART_POINTS:] if len(cmdty) > COHORT_CHART_POINTS else cmdty
    dates = [p["date"] for p in window]
    c_map = {p["date"]: p["last"] for p in window}

    from data.unit_cost_history import get_cost_history

    producers = []
    for p in cfg.get("producers") or []:
        if not producer_lists_commodity(p, cid):
            continue
        ticker = (p.get("ticker") or "").strip().upper()
        cost = _resolve_unit_cost(p)
        hist = []
        try:
            hist = get_cost_history(ticker)
        except Exception as e:
            logger.debug("cohort cost %s: %s", ticker, e)
        cost_by_date = []
        for d in dates:
            ct = _cost_asof(hist, d)
            if ct is None:
                ct = cost.get("value")
            cost_by_date.append(ct)
        snap = cost.get("value")
        producers.append(
            {
                "ticker": ticker,
                "name": p.get("name") or ticker,
                "primary": p.get("primary"),
                "unit_cost": snap,
                "has_cost": snap is not None or bool(hist),
                "cost_history_n": len(hist),
                "cost_by_date": cost_by_date,
            }
        )

    chart = []
    for i, d in enumerate(dates):
        costs = {pr["ticker"]: pr["cost_by_date"][i] for pr in producers}
        chart.append({"date": d, "commodity": c_map.get(d), "costs": costs})

    src = (window[-1].get("source") if window else "") or "barchart_csv"
    return {
        "commodity_id": cid,
        "display": meta.get("display") or cid,
        "unit": meta.get("unit") or "",
        "source": src,
        "stats": series_stats(cmdty),
        "producers": [
            {k: v for k, v in pr.items() if k != "cost_by_date"} for pr in producers
        ],
        "chart": chart,
        "filter_commodities": listed_filter_commodities(cfg),
        "generated_at": datetime.now().isoformat(),
    }
