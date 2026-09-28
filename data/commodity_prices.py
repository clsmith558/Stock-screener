"""
Load and clean daily commodity prices from the local Barchart collection CSV.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "commodity_producers.yaml"

_DEFAULT_CSV = Path(
    r"C:\Python scripts\Commodity data collection\daily_commodity_price_data.csv"
)

_config_cache: dict | None = None
_series_cache: dict[str, Any] = {}
_SERIES_CACHE_TTL = 300  # seconds

# SGX TSI 62% Fe (USD/dmt). Barchart TS*0 is Tokyo Topix-Mini, not ore.
_IRON_YF_SYMBOL = "TIO=F"


def load_commodity_config(force: bool = False) -> dict:
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache
    defaults = {
        "csv_path": str(_DEFAULT_CSV),
        "stale_after_days": 5,
        "ratio_z_lookback": 90,
        "undervalued_ratio_z_max": -1.0,
        "undervalued_min_margin_pct": 15.0,
        "commodities": {},
        "producers": [],
    }
    if not _CONFIG_PATH.exists():
        _config_cache = defaults
        return defaults
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k, v in defaults.items():
            if k not in raw:
                raw[k] = v
        _config_cache = raw
        return raw
    except Exception as e:
        logger.warning("commodity config load failed: %s", e)
        _config_cache = defaults
        return defaults


def resolve_csv_path() -> Path:
    env = (os.environ.get("COMMODITY_DATA_CSV") or "").strip()
    if env:
        return Path(env)
    cfg = load_commodity_config()
    return Path(cfg.get("csv_path") or _DEFAULT_CSV)


def clean_date(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Excel/export corruption: 2025+AC0-05+AC0-26
    s = s.replace("+AC0-", "-")
    s = re.sub(r"[^0-9\-]", "", s) if re.search(r"\d{4}.\d{2}.\d{2}", s) else s
    # After stripping non-digits-dashes we may have 2025-05-26
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m2 = re.search(r"(\d{4})(\d{2})(\d{2})", s)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    try:
        return datetime.fromisoformat(str(raw)[:10]).date().isoformat()
    except Exception:
        return None


def clean_price(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    s = re.sub(r"[^0-9.\-]", "", s)  # drop trailing 's' etc.
    if not s or s in (".", "-", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _csv_name_to_id(cfg: dict) -> dict[str, str]:
    """Map CSV commodity label → config id."""
    out = {}
    for cid, meta in (cfg.get("commodities") or {}).items():
        name = (meta.get("csv_name") or cid).strip().lower()
        out[name] = cid
    return out


def load_commodity_frame(force: bool = False) -> dict[str, Any]:
    """
    Return cleaned long-form data:
      {
        "rows": [{date, commodity_id, commodity_label, last, open, high, low}, ...],
        "by_id": {id: sorted list of {date, last, ...}},
        "path": str,
        "loaded_at": iso,
        "error": optional
      }
    """
    global _series_cache
    now = datetime.now().timestamp()
    if not force and _series_cache.get("ts") and now - _series_cache["ts"] < _SERIES_CACHE_TTL:
        return _series_cache["data"]

    path = resolve_csv_path()
    cfg = load_commodity_config()
    name_map = _csv_name_to_id(cfg)
    result: dict[str, Any] = {
        "rows": [],
        "by_id": {},
        "path": str(path),
        "loaded_at": datetime.now().isoformat(),
        "error": None,
    }
    if not path.exists():
        result["error"] = f"CSV not found: {path}"
        _series_cache = {"ts": now, "data": result}
        return result

    try:
        import pandas as pd

        df = pd.read_csv(path)
    except Exception as e:
        result["error"] = str(e)
        _series_cache = {"ts": now, "data": result}
        return result

    # normalize columns
    cols = {c.lower().strip(): c for c in df.columns}
    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    c_date = col("date")
    c_last = col("lastprice", "last", "close")
    c_cmd = col("commodity")
    c_open = col("openprice", "open")
    c_high = col("highprice", "high")
    c_low = col("lowprice", "low")
    if not c_date or not c_last or not c_cmd:
        result["error"] = "CSV missing date/lastPrice/commodity columns"
        _series_cache = {"ts": now, "data": result}
        return result

    rows = []
    by_id: dict[str, list] = {}
    for _, r in df.iterrows():
        d = clean_date(r.get(c_date))
        last = clean_price(r.get(c_last))
        label = str(r.get(c_cmd) or "").strip()
        if not d or last is None or not label:
            continue
        cid = name_map.get(label.lower())
        if not cid:
            # allow unmapped labels as slug
            cid = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        bounds = _plausible_bounds(cid)
        if bounds and last is not None and not (bounds[0] <= last <= bounds[1]):
            continue
        row = {
            "date": d,
            "commodity_id": cid,
            "commodity_label": label,
            "last": last,
            "open": clean_price(r.get(c_open)) if c_open else None,
            "high": clean_price(r.get(c_high)) if c_high else None,
            "low": clean_price(r.get(c_low)) if c_low else None,
        }
        rows.append(row)
        by_id.setdefault(cid, []).append(row)

    for cid, series in by_id.items():
        series.sort(key=lambda x: x["date"])
        # dedupe keep last
        dedup = {}
        for pt in series:
            dedup[pt["date"]] = pt
        by_id[cid] = [dedup[k] for k in sorted(dedup.keys())]

    result["rows"] = rows
    result["by_id"] = by_id
    _series_cache = {"ts": now, "data": result}
    return result


def series_stats(series: list[dict], lookbacks: tuple[int, ...] = (21, 63, 126)) -> dict:
    """Stats for one commodity series (list of {date, last})."""
    if not series:
        return {
            "last": None,
            "last_date": None,
            "n": 0,
            "returns": {},
            "drawdown_from_peak_pct": None,
            "peak": None,
            "peak_date": None,
        }
    lasts = [p["last"] for p in series if p.get("last") is not None]
    last = lasts[-1]
    last_date = series[-1]["date"]
    peak = max(lasts)
    peak_i = lasts.index(peak)
    peak_date = series[peak_i]["date"]
    dd = ((last / peak) - 1) * 100 if peak else None

    def ret(days: int) -> float | None:
        if len(series) < 2:
            return None
        # approx trading days: walk back
        target = series[-1]
        # find point ~days before by calendar
        try:
            end = datetime.fromisoformat(target["date"])
            start_d = (end - timedelta(days=int(days * 1.5))).date().isoformat()
        except Exception:
            start_d = series[max(0, len(series) - days - 1)]["date"]
        base = None
        for p in series:
            if p["date"] >= start_d:
                base = p
                break
        if not base or not base.get("last"):
            base = series[max(0, len(series) - days - 1)]
        if not base.get("last"):
            return None
        return round((last / base["last"] - 1) * 100, 2)

    returns = {}
    for d in lookbacks:
        returns[f"{d}d"] = ret(d)
    # also calendar-ish labels
    returns["1m"] = returns.get("21d")
    returns["3m"] = returns.get("63d")
    returns["6m"] = returns.get("126d")

    return {
        "last": last,
        "last_date": last_date,
        "n": len(series),
        "returns": returns,
        "drawdown_from_peak_pct": round(dd, 2) if dd is not None else None,
        "peak": peak,
        "peak_date": peak_date,
    }


def _supporting_points(commodity_id: str) -> list[dict]:
    """Official EIA/FRED points when Phase-1 collector has this id."""
    try:
        from data.supporting_series import get_series as get_official

        pts = get_official(commodity_id) or []
    except Exception:
        return []
    out = []
    for p in pts:
        last = p.get("last") if p.get("last") is not None else p.get("value")
        if last is None or not p.get("date"):
            continue
        out.append(
            {
                "date": p["date"],
                "last": last,
                "source": p.get("source") or "supporting",
                "source_id": p.get("source_id") or "",
            }
        )
    return out


def _plausible_bounds(commodity_id: str) -> tuple[float, float] | None:
    cfg = load_commodity_config()
    meta = (cfg.get("commodities") or {}).get(commodity_id) or {}
    lo, hi = meta.get("plausible_min"), meta.get("plausible_max")
    if lo is None or hi is None:
        return None
    try:
        return float(lo), float(hi)
    except (TypeError, ValueError):
        return None


def series_is_plausible(commodity_id: str, series: list[dict]) -> bool:
    """Reject mis-mapped contracts (Barchart TS*0 Topix Mini as 'iron ore')."""
    bounds = _plausible_bounds(commodity_id)
    if not bounds or not series:
        return bool(series)
    lo, hi = bounds
    vals = [p["last"] for p in series if p.get("last") is not None]
    if not vals:
        return False
    sample = vals[-60:] if len(vals) > 60 else vals
    try:
        import statistics

        med = statistics.median(sample)
    except statistics.StatisticsError:
        med = sample[-1]
    return lo <= med <= hi


def _yfinance_daily(symbol: str, commodity_id: str) -> list[dict]:
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(period="max", auto_adjust=True)
    except Exception as e:
        logger.warning("yfinance %s failed: %s", symbol, e)
        return []
    if hist is None or hist.empty:
        return []
    out = []
    for dt, row in hist.iterrows():
        try:
            d = dt.strftime("%Y-%m-%d")
        except Exception:
            d = str(dt)[:10]
        last = clean_price(row.get("Close"))
        if not d or last is None:
            continue
        out.append(
            {
                "date": d,
                "last": last,
                "source": "yfinance",
                "source_id": symbol,
                "commodity_id": commodity_id,
            }
        )
    return out


def get_series(commodity_id: str, force: bool = False) -> list[dict]:
    cid = (commodity_id or "").strip()
    official = _supporting_points(cid)
    if official and series_is_plausible(cid, official):
        return official

    data = load_commodity_frame(force=force)
    rows = list((data.get("by_id") or {}).get(cid) or [])
    for r in rows:
        r.setdefault("source", "barchart_csv")
    if series_is_plausible(cid, rows):
        return rows

    if official:
        logger.warning(
            "official %s failed plausibility (n=%d last=%s); trying other sources",
            cid,
            len(official),
            official[-1].get("last") if official else None,
        )
    if rows:
        last = rows[-1].get("last")
        logger.warning(
            "Barchart %s looks like the wrong contract (last=%s); ignoring CSV",
            cid,
            last,
        )

    if cid == "iron_ore":
        yf_rows = _yfinance_daily(_IRON_YF_SYMBOL, cid)
        if series_is_plausible(cid, yf_rows):
            return yf_rows

    return []


def commodities_overview(force: bool = False) -> dict:
    cfg = load_commodity_config()
    data = load_commodity_frame(force=force)
    meta_by_id = cfg.get("commodities") or {}
    stale_days = int(cfg.get("stale_after_days") or 5)
    today = datetime.now().date()

    ids = list(meta_by_id.keys())
    for cid in data.get("by_id") or {}:
        if cid not in ids:
            ids.append(cid)

    series_list = []
    for cid in ids:
        series = get_series(cid, force=force)
        meta = meta_by_id.get(cid) or {}
        st = series_stats(series)
        age_days = None
        if st.get("last_date"):
            try:
                age_days = (today - datetime.fromisoformat(st["last_date"]).date()).days
            except Exception:
                pass
        src = ""
        if series:
            src = series[-1].get("source") or "barchart_csv"
        series_list.append(
            {
                "id": cid,
                "display": meta.get("display") or (meta.get("csv_name") or cid),
                "csv_name": meta.get("csv_name") or (
                    series[0].get("commodity_label") if series else cid
                ),
                "unit": meta.get("unit") or "",
                "source": src,
                **st,
                "age_days": age_days,
                "stale": age_days is not None and age_days > stale_days,
            }
        )

    return {
        "path": data.get("path"),
        "loaded_at": data.get("loaded_at"),
        "error": data.get("error"),
        "stale_after_days": stale_days,
        "commodities": series_list,
        "count": len(series_list),
    }


def get_last_price(commodity_id: str) -> tuple[float | None, str | None]:
    series = get_series(commodity_id)
    if not series:
        return None, None
    return series[-1].get("last"), series[-1].get("date")
