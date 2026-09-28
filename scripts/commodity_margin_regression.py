#!/usr/bin/env python3
"""
Offline research: constant unit-cost margin & stock/commodity ratio vs forward returns.

Usage (from Stock screener folder):
  python scripts/commodity_margin_regression.py
  python scripts/commodity_margin_regression.py --horizons 63,126,252

Writes:
  data/commodity_research/panel.csv
  data/commodity_research/latest_summary.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

OUT_DIR = PROJECT_ROOT / "data" / "commodity_research"


def _fwd_return(hist: list[dict], i: int, horizon_days: int) -> float | None:
    """Calendar-ish forward return from hist[i] over ~horizon trading days."""
    if i >= len(hist) - 1:
        return None
    start = hist[i]
    try:
        d0 = datetime.fromisoformat(start["date"])
        target = (d0 + timedelta(days=int(horizon_days * 1.45))).date().isoformat()
    except Exception:
        j = min(len(hist) - 1, i + horizon_days)
        if hist[j]["close"] and start["close"]:
            return (hist[j]["close"] / start["close"] - 1) * 100
        return None
    end = None
    for p in hist[i + 1 :]:
        if p["date"] >= target:
            end = p
            break
    if end is None:
        end = hist[-1]
        # require enough calendar span
        try:
            if (datetime.fromisoformat(end["date"]) - d0).days < horizon_days * 0.7:
                return None
        except Exception:
            return None
    if not start.get("close") or not end.get("close"):
        return None
    return (end["close"] / start["close"] - 1) * 100


def _bucket_stats(rows: list[dict], key: str, ret_key: str, n_buckets: int = 3) -> list[dict]:
    vals = [(r[key], r[ret_key]) for r in rows if r.get(key) is not None and r.get(ret_key) is not None]
    if len(vals) < n_buckets * 3:
        return []
    vals.sort(key=lambda x: x[0])
    size = len(vals) // n_buckets
    out = []
    for b in range(n_buckets):
        chunk = vals[b * size : (b + 1) * size if b < n_buckets - 1 else len(vals)]
        rets = [c[1] for c in chunk]
        out.append(
            {
                "bucket": b + 1,
                "n": len(chunk),
                "x_min": chunk[0][0],
                "x_max": chunk[-1][0],
                "mean_forward_return_pct": round(statistics.mean(rets), 2),
                "median_forward_return_pct": round(statistics.median(rets), 2),
            }
        )
    return out


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx < 1e-12 or deny < 1e-12:
        return None
    return round(num / (denx * deny), 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--horizons",
        default="63,126",
        help="Comma-separated trading-day horizons (63≈3m, 126≈6m, 252≈1y)",
    )
    args = parser.parse_args()
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]

    from data.commodity_prices import get_series, load_commodity_config
    from data.commodity_producers import _align_ratio_series, _resolve_unit_cost, _stock_history

    cfg = load_commodity_config()
    panel_rows = []

    for prod in cfg.get("producers") or []:
        ticker = (prod.get("ticker") or "").upper()
        primary = prod.get("primary")
        if not ticker or not primary:
            continue
        cost = _resolve_unit_cost(prod)
        c_val = cost.get("value")
        cmdty = get_series(primary)
        stock = _stock_history(ticker, days=500)
        if not cmdty or not stock:
            continue
        aligned = _align_ratio_series(stock, cmdty)
        if len(aligned) < 20:
            continue
        # map stock hist for forward returns
        stock_by_date = {p["date"]: p for p in stock}
        stock_list = stock  # ordered

        for i, pt in enumerate(aligned):
            # find stock index for this date
            si = next((j for j, s in enumerate(stock_list) if s["date"] == pt["date"]), None)
            if si is None:
                continue
            margin = (pt["commodity"] - c_val) if c_val is not None else None
            margin_pct = (margin / pt["commodity"] * 100) if margin is not None and pt["commodity"] else None
            row = {
                "ticker": ticker,
                "commodity": primary,
                "date": pt["date"],
                "stock": pt["stock"],
                "commodity_price": pt["commodity"],
                "ratio": pt["ratio"],
                "unit_cost": c_val,
                "margin": margin,
                "margin_pct": margin_pct,
            }
            for h in horizons:
                row[f"fwd_ret_{h}d"] = _fwd_return(stock_list, si, h)
            panel_rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel_path = OUT_DIR / "panel.csv"
    try:
        import pandas as pd

        pd.DataFrame(panel_rows).to_csv(panel_path, index=False)
    except Exception:
        # minimal CSV without pandas
        if panel_rows:
            keys = list(panel_rows[0].keys())
            with open(panel_path, "w", encoding="utf-8") as f:
                f.write(",".join(keys) + "\n")
                for r in panel_rows:
                    f.write(",".join(str(r.get(k, "")) for k in keys) + "\n")

    summary: dict = {
        "generated_at": datetime.now().isoformat(),
        "n_panel_rows": len(panel_rows),
        "horizons": horizons,
        "note": (
            "Unit cost held constant at latest snapshot (not historical AISC path). "
            "Commodity sample is short (~1y); 1y-forward n may be tiny. Research only."
        ),
        "by_horizon": {},
    }

    for h in horizons:
        key = f"fwd_ret_{h}d"
        usable = [r for r in panel_rows if r.get(key) is not None]
        # weekly subsample to reduce overlap (every 5 rows per ticker)
        by_t: dict[str, list] = {}
        for r in usable:
            by_t.setdefault(r["ticker"], []).append(r)
        subsample = []
        for t, rows in by_t.items():
            subsample.extend(rows[::5])

        block = {
            "n_rows_with_forward": len(usable),
            "n_subsample": len(subsample),
            "corr_margin_pct": _corr(
                [r["margin_pct"] for r in subsample if r.get("margin_pct") is not None],
                [r[key] for r in subsample if r.get("margin_pct") is not None],
            ),
            "corr_ratio": _corr(
                [r["ratio"] for r in subsample if r.get("ratio") is not None],
                [r[key] for r in subsample if r.get("ratio") is not None],
            ),
            "margin_pct_terciles": _bucket_stats(
                [r for r in subsample if r.get("margin_pct") is not None],
                "margin_pct",
                key,
            ),
            "ratio_terciles": _bucket_stats(subsample, "ratio", key),
        }
        summary["by_horizon"][str(h)] = block

    summary_path = OUT_DIR / "latest_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Panel rows: {len(panel_rows)} → {panel_path}")
    print(f"Summary → {summary_path}")
    print(summary["note"])
    for h, block in summary["by_horizon"].items():
        print(
            f"  horizon {h}d: n={block['n_rows_with_forward']} "
            f"subsample={block['n_subsample']} "
            f"corr(margin%,ret)={block['corr_margin_pct']} "
            f"corr(ratio,ret)={block['corr_ratio']}"
        )
        for b in block.get("margin_pct_terciles") or []:
            print(
                f"    margin tercile {b['bucket']}: n={b['n']} "
                f"mean fwd={b['mean_forward_return_pct']}%"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
