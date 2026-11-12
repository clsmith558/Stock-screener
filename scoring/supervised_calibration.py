"""
Section 5: Supervised Learning & Backtesting (Signal Calibration).

Implements an empirical Walk-Forward Supervised Learning & Event-Study Calibration engine
trained on dated corporate signal events (`us_insider_purchases`, `us_buyback_repurchases`,
ASX `announcements`, `buyback_registry`) and point-in-time `monthly_stock_snapshots`:

1. **Point-in-Time Event-Study Dataset (`build_signal_calibration_dataset`)**:
   - Rolls up dated open-market insider purchases (SEC Form 4, ASX 3Y) and completed share
     repurchase executions (SEC 10-Q, ASX Appendix 3C/3E) by `(ticker, YYYY-MM, signal_type)`
     with volume-weighted execution price `P(t_event)`.
   - Reconstructs point-in-time price posture (`event_gap_52w_pct`, `event_ma200w_gap_pct`) at
     the exact execution price alongside fundamental valuation & quality features (`fcf_yield`,
     `ev_ebitda`, `pb`, `pe`, `roic_or_roe`, `net_cash_pct_mcap`, `debt_to_equity`,
     `annual_buyback_pct`, `insider_buys_count`).
   - Computes vintage-quarter de-meaned excess return (`excess_ret_pct = raw_ret_pct - cohort_median`)
     to eliminate market-drift and holding-period bias across vintages.

2. **Walk-Forward Time-Series Cross-Validation (`TimeSeriesSplit`) & Ensemble Models**:
   - Trains an interpretable L2-Regularized `LogisticRegression` + non-linear
     `HistGradientBoostingClassifier` & `HistGradientBoostingRegressor` ensemble.
   - Evaluates strict out-of-sample walk-forward ROC-AUC, Brier Score, Spearman Rank IC,
     Reliability / Calibration Curve, and Quintile Return Spread (`Q5` vs `Q1`).

3. **Empirical Calibration of `opportunity_weights.yaml`**:
   - Optimizes the 4 core dashboard score pillar weights (`valuation`, `price_posture`,
     `insider`, `buyback_at_low`) and posture thresholds (`near_52w_low_pct`,
     `buyback_near_low_pct`) to maximize out-of-sample Rank IC & Top-Quintile Alpha.

4. **Per-Stock Supervised Outperformance Probability & SHAP-Style Feature Attribution**:
   - Predicts `win_prob_pct` (0–100%), `pred_excess_ret_pct`, and per-feature linear log-odds
     contributions explaining the drivers of each stock's calibrated signal conviction.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import numpy as np
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import RobustScaler

from scoring.eda_stats import extract_metric
from scoring.quant_score import get_weights_public

logger = logging.getLogger(__name__)

# 13 Granular Supervised Features
SUPERVISED_FEATURES: list[dict[str, Any]] = [
    {"key": "fcf_yield", "label": "FCF Yield %", "pillar": "valuation", "higher_is_better": True},
    {"key": "earnings_yield_ev", "label": "EBITDA / EV %", "pillar": "valuation", "higher_is_better": True},
    {"key": "book_to_market", "label": "Book / Market (1/PB)", "pillar": "valuation", "higher_is_better": True},
    {"key": "roic_or_roe", "label": "ROIC / ROE %", "pillar": "valuation", "higher_is_better": True},
    {"key": "net_cash_pct_mcap", "label": "Net Cash / MCap %", "pillar": "valuation", "higher_is_better": True},
    {"key": "debt_to_equity_inv", "label": "Low Leverage (-D/E)", "pillar": "valuation", "higher_is_better": True},
    {"key": "near_52w_low_prox", "label": "52w Low Proximity", "pillar": "price_posture", "higher_is_better": True},
    {"key": "below_200w_ma_disc", "label": "200w MA Discount", "pillar": "price_posture", "higher_is_better": True},
    {"key": "oversold_rsi", "label": "Oversold RSI Cushion", "pillar": "price_posture", "higher_is_better": True},
    {"key": "insider_conviction", "label": "Insider Buy Conviction", "pillar": "insider", "higher_is_better": True},
    {"key": "cloner_conviction", "label": "Superinvestor 13F Conviction", "pillar": "insider", "higher_is_better": True},
    {"key": "annual_buyback_pct", "label": "Annual Buyback Yield %", "pillar": "buyback_at_low", "higher_is_better": True},
    {"key": "buyback_at_low_inter", "label": "Buyback x Near-52w-Low", "pillar": "buyback_at_low", "higher_is_better": True},
]

_SUPERVISED_DDL = (
    """
    CREATE TABLE IF NOT EXISTS ml_supervised_cache (
        cache_key TEXT PRIMARY KEY,
        computed_at TEXT,
        payload_json TEXT
    )
    """,
)

_sup_mem_cache: dict[str, Any] = {}
SUPERVISED_CACHE_TTL_SEC = 3600 * 6  # 6 hours


def _sup_conn(db_path: str = "asx_announcements_cache.db"):
    from data.db import connect_db

    return connect_db(db_path, schema_key="ml_supervised", ddl=_SUPERVISED_DDL)


def _safe_float(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _quarter_str(date_str: str) -> str:
    if not date_str or len(date_str) < 7:
        return "Unknown"
    try:
        y = int(date_str[:4])
        m = int(date_str[5:7])
        q = (m - 1) // 3 + 1
        return f"{y}-Q{q}"
    except Exception:
        return "Unknown"


def _extract_feature_vector_from_record(
    *,
    stock: dict[str, Any],
    event_gap_52w_pct: float | None = None,
    event_ma200w_gap_pct: float | None = None,
    insider_count: int | None = None,
    insider_value_usd: float | None = None,
    has_buyback: bool | None = None,
    annual_buyback_pct: float | None = None,
) -> list[float | None]:
    """
    Compute the 12 oriented quantitative features for either a historical signal event
    (using point-in-time `event_gap_52w_pct` / `event_ma200w_gap_pct`) or a present-day stock.
    All features are oriented so HIGHER = more favorable according to financial theory.
    """
    m = stock.get("metrics") or {}

    # 1. FCF Yield %
    fcf_y = _safe_float(m.get("fcf_yield"))
    if fcf_y is None:
        pfcf = _safe_float(stock.get("p_fcf") or m.get("p_fcf"))
        if pfcf and pfcf > 0:
            fcf_y = round(100.0 / pfcf, 2)
    if fcf_y is not None:
        fcf_y = max(-25.0, min(40.0, fcf_y))

    # 2. Earnings Yield (EBITDA / EV %)
    ev_eb = _safe_float(m.get("ev_ebitda"))
    ey_ev = round(100.0 / ev_eb, 2) if (ev_eb and ev_eb > 0.5) else None
    if ey_ev is not None:
        ey_ev = max(0.0, min(50.0, ey_ev))

    # 3. Book-to-Market (1 / PB)
    pb = _safe_float(stock.get("pb") or m.get("pb"))
    btm = round(1.0 / pb, 3) if (pb and pb > 0.05) else None
    if btm is not None:
        btm = max(0.0, min(5.0, btm))

    # 4. ROIC or ROE %
    roic_roe = extract_metric(stock, "roic_or_roe")
    if roic_roe is not None:
        roic_roe = max(-30.0, min(60.0, float(roic_roe)))

    # 5. Net Cash / Market Cap %
    nc_pct = _safe_float(stock.get("net_cash_pct_mcap") or m.get("net_cash_pct_mcap"), 0.0)
    if nc_pct is not None:
        nc_pct = max(-100.0, min(100.0, nc_pct))

    # 6. Low Leverage (-Debt/Equity)
    de = _safe_float(m.get("debt_to_equity") or stock.get("debt_to_equity"), 0.5)
    de_inv = -max(0.0, min(5.0, de)) if de is not None else -0.5

    # 7. 52w Low Proximity: max(0, 60 - pct_from_52w_low)
    gap_52 = event_gap_52w_pct if event_gap_52w_pct is not None else _safe_float(stock.get("pct_from_52w_low"), 35.0)
    gap_52 = max(0.0, min(200.0, gap_52 if gap_52 is not None else 35.0))
    near_52_prox = max(0.0, 60.0 - gap_52)

    # 8. 200w MA Discount: max(-40, -pct_from_200w_ma)
    ma_gap = (
        event_ma200w_gap_pct
        if event_ma200w_gap_pct is not None
        else _safe_float(stock.get("pct_from_200w_ma") or m.get("pct_from_200w_ma"), 0.0)
    )
    ma_gap = max(-80.0, min(150.0, ma_gap if ma_gap is not None else 0.0))
    below_200w_disc = -ma_gap  # Positive when trading below 200w MA

    # 9. Oversold RSI Cushion: (60 - RSI)
    rsi = _safe_float(stock.get("rsi") or m.get("rsi"), 50.0)
    oversold_rsi = 60.0 - max(15.0, min(85.0, rsi if rsi is not None else 50.0))

    # 10. Insider Buy Conviction
    ins_n = insider_count if insider_count is not None else int(stock.get("insider_buys_2026") or 0)
    ins_val = insider_value_usd if insider_value_usd is not None else 0.0
    ins_conv = min(10.0, float(ins_n) * 1.5 + (math.log10(max(1.0, ins_val)) - 3.5 if ins_val > 5000 else 0.0))
    ins_conv = max(0.0, ins_conv)

    # 10b. Superinvestor 13F / ASX Substantial Holder Conviction
    cloner_cnt = int(stock.get("cloner_holder_count") or m.get("cloner_holder_count") or 0)
    cloner_new = int(stock.get("cloner_new_position_flag") or m.get("cloner_new_position_flag") or 0)
    cloner_max_w = _safe_float(stock.get("cloner_max_weight_pct") or m.get("cloner_max_weight_pct"), 0.0) or 0.0
    cloner_conv = min(10.0, float(cloner_cnt) * 2.0 + (2.0 if cloner_new else 0.0) + min(4.0, cloner_max_w * 0.25))

    # 11. Annual Buyback Yield %
    bb_pct = (
        annual_buyback_pct
        if annual_buyback_pct is not None
        else _safe_float(stock.get("annual_buyback_pct") or m.get("annual_buyback_pct"), 0.0)
    )
    bb_pct = max(0.0, min(25.0, bb_pct if bb_pct is not None else 0.0))

    # 12. Buyback x Near-52w-Low Interaction
    is_bb = has_buyback if has_buyback is not None else bool(stock.get("buyback_announced") or bb_pct > 0.25)
    bb_low_inter = (
        (1.0 + min(5.0, bb_pct * 0.4)) * max(0.0, (30.0 - gap_52) / 10.0)
        if is_bb and gap_52 < 30.0
        else 0.0
    )

    return [
        fcf_y,
        ey_ev,
        btm,
        roic_roe,
        nc_pct,
        de_inv,
        near_52_prox,
        below_200w_disc,
        oversold_rsi,
        ins_conv,
        cloner_conv,
        bb_pct,
        bb_low_inter,
    ]


def _compute_pillar_scores_at_event(
    *,
    stock: dict[str, Any],
    event_gap_52w_pct: float,
    event_ma200w_gap_pct: float,
    insider_count: int,
    has_buyback: bool,
    annual_buyback_pct: float,
    near_52w_low_pct: float = 15.0,
    buyback_near_low_pct: float = 20.0,
) -> dict[str, float]:
    """
    Compute the 4 `quant_score` pillar scores (0–100) for a point-in-time event or stock.
    Matches the structure of `scoring/quant_score.py` so calibrated pillar weights
    transfer 1-to-1 into `config/opportunity_weights.yaml`.
    """
    m = stock.get("metrics") or {}
    # 1. Valuation (0–100)
    val_parts: list[float] = []
    pfcf = _safe_float(stock.get("p_fcf") or m.get("p_fcf"))
    if pfcf and pfcf > 0:
        val_parts.append(max(5.0, min(98.0, 100.0 - pfcf * 3.2)))
    ev_eb = _safe_float(m.get("ev_ebitda"))
    if ev_eb and ev_eb > 0:
        val_parts.append(max(5.0, min(98.0, 100.0 - ev_eb * 4.5)))
    fcf_y = _safe_float(m.get("fcf_yield"))
    if fcf_y and fcf_y > 0:
        val_parts.append(max(5.0, min(98.0, fcf_y * 6.5)))
    pb = _safe_float(stock.get("pb") or m.get("pb"))
    if pb and pb > 0:
        val_parts.append(max(5.0, min(98.0, 100.0 - pb * 25.0)))
    val_score = sum(val_parts) / len(val_parts) if val_parts else 50.0

    # 2. Price Posture (0–100)
    post_score = 0.0
    if event_gap_52w_pct < near_52w_low_pct:
        post_score += 60.0 + max(0.0, (near_52w_low_pct - event_gap_52w_pct) * 2.0)
    elif event_gap_52w_pct < 30.0:
        post_score += 40.0
    elif event_gap_52w_pct < 50.0:
        post_score += 20.0
    if event_ma200w_gap_pct < 0:
        post_score += min(25.0, abs(event_ma200w_gap_pct))
    post_score = min(100.0, post_score)

    # 3. Insider (0–100)
    ins_score = min(100.0, float(max(0, insider_count)) * 25.0)

    # 4. Buyback at low (0–100)
    bb_score = 0.0
    near_low = event_gap_52w_pct < buyback_near_low_pct
    if has_buyback or annual_buyback_pct > 0.2:
        if annual_buyback_pct > 1.0 and near_low:
            bb_score = min(100.0, 75.0 + annual_buyback_pct * 3.0)
        elif near_low:
            bb_score = 85.0
        else:
            bb_score = 40.0

    return {
        "valuation": round(val_score, 1),
        "price_posture": round(post_score, 1),
        "insider": round(ins_score, 1),
        "buyback_at_low": round(bb_score, 1),
    }


def build_signal_calibration_dataset(
    db_path: str = "asx_announcements_cache.db",
) -> list[dict[str, Any]]:
    """
    Construct the chronological point-in-time signal event dataset across:
      1. `us_insider_purchases` (SEC Form 4 open-market insider buys with price > 0)
      2. `us_buyback_repurchases` (SEC 10-Q completed monthly share repurchases with price > 0)
      3. `announcements` (ASX daily share buy-back notices with price_avg > 0)
      4. `monthly_stock_snapshots` (any historical snapshot rows with non-zero realized returns)

    Rolls up multiple daily notices within the same `(ticker, YYYY-MM, signal_type)` into a
    single monthly event observation to prevent high-frequency daily buyback filers from
    dominating sample weights.
    """
    from scoring.unsupervised_ml import load_full_global_stock_universe

    stocks = load_full_global_stock_universe(db_path=db_path)
    stock_map: dict[str, dict[str, Any]] = {}
    for s in stocks:
        t = str(s.get("ticker") or "").strip().upper()
        if t and (s.get("current_price") or 0) > 0:
            stock_map[t] = s
            if t.endswith(".AX"):
                stock_map[t[:-3]] = s

    conn = _sup_conn(db_path)
    raw_buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

    now_dt = datetime.now()
    min_date_str = (now_dt - timedelta(days=760)).strftime("%Y-%m-%d")
    max_date_str = (now_dt - timedelta(days=18)).strftime("%Y-%m-%d")

    try:
        # 1. US SEC Form 4 Insider Purchases
        cur = conn.execute(
            """
            SELECT yf_symbol, date, shares, price, insider, position
            FROM us_insider_purchases
            WHERE price > 0.25 AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (min_date_str, max_date_str),
        )
        for yf_sym, dt_s, shares, price, insider, pos in cur.fetchall():
            sym = str(yf_sym or "").strip().upper()
            st = stock_map.get(sym)
            if not st:
                continue
            p_ev = float(price or 0.0)
            p_now = float(st.get("current_price") or 0.0)
            if p_ev <= 0 or p_now <= 0:
                continue
            # Plausibility check vs 52w range to filter preferred-share / option-strike price mismatches
            lo52 = float(st.get("low_52w") or p_now * 0.5)
            hi52 = float(st.get("high_52w") or p_now * 1.5)
            if p_ev < lo52 * 0.30 or p_ev > hi52 * 2.20:
                continue

            ym = str(dt_s)[:7]
            key = (st["ticker"], ym, "insider_buy")
            sh = max(1.0, float(shares or 100.0))
            b = raw_buckets.get(key)
            if not b:
                raw_buckets[key] = {
                    "ticker": st["ticker"],
                    "event_date": str(dt_s)[:10],
                    "year_month": ym,
                    "signal_type": "insider_buy",
                    "cost_sum": p_ev * sh,
                    "shares_sum": sh,
                    "tx_count": 1,
                }
            else:
                b["cost_sum"] += p_ev * sh
                b["shares_sum"] += sh
                b["tx_count"] += 1

        # 2. US SEC 10-Q Monthly Share Repurchases
        cur = conn.execute(
            """
            SELECT yf_symbol, period_end, shares, price
            FROM us_buyback_repurchases
            WHERE price > 0.25 AND period_end >= ? AND period_end <= ?
            ORDER BY period_end ASC
            """,
            (min_date_str, max_date_str),
        )
        for yf_sym, dt_s, shares, price in cur.fetchall():
            sym = str(yf_sym or "").strip().upper()
            st = stock_map.get(sym)
            if not st:
                continue
            p_ev = float(price or 0.0)
            p_now = float(st.get("current_price") or 0.0)
            if p_ev <= 0 or p_now <= 0:
                continue
            lo52 = float(st.get("low_52w") or p_now * 0.5)
            hi52 = float(st.get("high_52w") or p_now * 1.5)
            if p_ev < lo52 * 0.30 or p_ev > hi52 * 2.20:
                continue

            ym = str(dt_s)[:7]
            key = (st["ticker"], ym, "share_buyback")
            sh = max(1.0, float(shares or 1000.0))
            b = raw_buckets.get(key)
            if not b:
                raw_buckets[key] = {
                    "ticker": st["ticker"],
                    "event_date": str(dt_s)[:10],
                    "year_month": ym,
                    "signal_type": "share_buyback",
                    "cost_sum": p_ev * sh,
                    "shares_sum": sh,
                    "tx_count": 1,
                }
            else:
                b["cost_sum"] += p_ev * sh
                b["shares_sum"] += sh
                b["tx_count"] += 1

        # 3. ASX Buyback Announcements with parsed average execution price
        cur = conn.execute(
            """
            SELECT ticker, date, shares_purchased, price_avg
            FROM announcements
            WHERE classification = 'buyback' AND price_avg > 0.02
              AND date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (min_date_str, max_date_str),
        )
        for base_t, dt_s, shares, price_avg in cur.fetchall():
            bt = str(base_t or "").strip().upper()
            st = stock_map.get(f"{bt}.AX") or stock_map.get(bt)
            if not st:
                continue
            p_ev = float(price_avg or 0.0)
            p_now = float(st.get("current_price") or 0.0)
            if p_ev <= 0 or p_now <= 0:
                continue
            lo52 = float(st.get("low_52w") or p_now * 0.5)
            hi52 = float(st.get("high_52w") or p_now * 1.5)
            if p_ev < lo52 * 0.35 or p_ev > hi52 * 2.00:
                continue

            ym = str(dt_s)[:7]
            key = (st["ticker"], ym, "share_buyback")
            sh = max(1.0, float(shares or 1000.0))
            b = raw_buckets.get(key)
            if not b:
                raw_buckets[key] = {
                    "ticker": st["ticker"],
                    "event_date": str(dt_s)[:10],
                    "year_month": ym,
                    "signal_type": "share_buyback",
                    "cost_sum": p_ev * sh,
                    "shares_sum": sh,
                    "tx_count": 1,
                }
            else:
                b["cost_sum"] += p_ev * sh
                b["shares_sum"] += sh
                b["tx_count"] += 1
    finally:
        conn.close()

    # Pre-compute strictly pre-event price history per ticker so we never use post-event 52w lows
    ticker_date_prices: dict[str, list[tuple[str, float]]] = {}
    for b in raw_buckets.values():
        p_ev = b["cost_sum"] / max(1.0, b["shares_sum"])
        if p_ev > 0:
            ticker_date_prices.setdefault(b["ticker"], []).append((b["event_date"], p_ev))
    for tk in ticker_date_prices:
        ticker_date_prices[tk].sort(key=lambda x: x[0])

    # Build enriched observations and compute vintage-quarter cohort medians
    observations: list[dict[str, Any]] = []
    for b in raw_buckets.values():
        t = b["ticker"]
        st = stock_map.get(t)
        if not st:
            continue
        p_ev = b["cost_sum"] / max(1.0, b["shares_sum"])
        p_now = float(st.get("current_price") or 0.0)
        if p_ev <= 0 or p_now <= 0:
            continue

        raw_ret_pct = ((p_now / p_ev) - 1.0) * 100.0
        if not (-85.0 <= raw_ret_pct <= 350.0):
            continue

        # Point-in-time fundamental multiples at P(t_event):
        # Since PB_now = P_now / BVPS, multiplying by (p_ev / p_now) cancels P_now completely
        # and recovers the point-in-time multiple P(t_event) / BVPS at the signal date!
        price_scale_to_event = p_ev / p_now
        m_now = st.get("metrics") or {}
        m_pit = dict(m_now)
        st_pit = dict(st)
        for mult_k in ("pe", "pb", "p_fcf", "ev_ebitda"):
            v_now = _safe_float(st.get(mult_k) or m_now.get(mult_k))
            if v_now and v_now > 0:
                v_pit = round(v_now * price_scale_to_event, 2)
                st_pit[mult_k] = v_pit
                m_pit[mult_k] = v_pit
        fcf_y_now = _safe_float(m_now.get("fcf_yield"))
        if fcf_y_now is not None and price_scale_to_event > 0.1:
            m_pit["fcf_yield"] = round(fcf_y_now / price_scale_to_event, 2)
        nc_now = _safe_float(st.get("net_cash_pct_mcap") or m_now.get("net_cash_pct_mcap"))
        if nc_now is not None and price_scale_to_event > 0.1:
            nc_pit = round(nc_now / price_scale_to_event, 2)
            st_pit["net_cash_pct_mcap"] = nc_pit
            m_pit["net_cash_pct_mcap"] = nc_pit
        st_pit["metrics"] = m_pit

        # Strictly pre-event price posture (no post-event 52w low leakage)
        ma_200w = float(m_now.get("ma_200w") or st.get("ma_200w") or 0.0)
        prior_pts = [px for dt_p, px in ticker_date_prices.get(t, []) if dt_p <= b["event_date"]]
        if len(prior_pts) >= 2:
            pre_low = min(prior_pts)
        elif ma_200w > 0:
            pre_low = min(p_ev, ma_200w * 0.78)
        else:
            pre_low = p_ev * 0.85

        event_gap_52w = max(0.0, ((p_ev - pre_low) / pre_low) * 100.0) if pre_low > 0 else 20.0
        if ma_200w > 0:
            event_ma200w_gap = ((p_ev - ma_200w) / ma_200w) * 100.0
        else:
            event_ma200w_gap = event_gap_52w - 20.0

        is_ins = b["signal_type"] == "insider_buy"
        is_bb = b["signal_type"] == "share_buyback"
        ins_cnt = b["tx_count"] if is_ins else int(st.get("insider_buys_2026") or 0)
        ins_val = b["cost_sum"] if is_ins else 0.0
        ann_bb_pct = float(st.get("annual_buyback_pct") or m_now.get("annual_buyback_pct") or (2.5 if is_bb else 0.0))

        # Neutralize current RSI on historical events so current momentum doesn't leak backward
        st_pit["rsi"] = 50.0 - min(20.0, max(-20.0, -event_ma200w_gap * 0.35))
        m_pit["rsi"] = st_pit["rsi"]

        x_vec = _extract_feature_vector_from_record(
            stock=st_pit,
            event_gap_52w_pct=event_gap_52w,
            event_ma200w_gap_pct=event_ma200w_gap,
            insider_count=ins_cnt,
            insider_value_usd=ins_val,
            has_buyback=is_bb or bool(st.get("buyback_announced")),
            annual_buyback_pct=ann_bb_pct,
        )
        # Require at least 6 non-null features
        if sum(1 for v in x_vec if v is not None) < 6:
            continue

        pillars = _compute_pillar_scores_at_event(
            stock=st_pit,
            event_gap_52w_pct=event_gap_52w,
            event_ma200w_gap_pct=event_ma200w_gap,
            insider_count=ins_cnt,
            has_buyback=is_bb or bool(st.get("buyback_announced")),
            annual_buyback_pct=ann_bb_pct,
        )

        observations.append({
            "ticker": t,
            "name": st.get("name") or t,
            "sector": st.get("sector") or "Unknown",
            "event_date": b["event_date"],
            "year_month": b["year_month"],
            "quarter": _quarter_str(b["event_date"]),
            "signal_type": b["signal_type"],
            "tx_count": b["tx_count"],
            "event_price": round(p_ev, 3),
            "current_price": round(p_now, 3),
            "event_gap_52w_pct": round(event_gap_52w, 1),
            "event_ma200w_gap_pct": round(event_ma200w_gap, 1),
            "raw_ret_pct": round(raw_ret_pct, 2),
            "features": x_vec,
            "pillars": pillars,
        })

    # Sort strictly chronologically by event_date
    observations.sort(key=lambda r: (r["event_date"], r["ticker"]))

    # Compute vintage-quarter median return to de-mean macro/horizon drift
    by_quarter: dict[str, list[float]] = {}
    for obs in observations:
        by_quarter.setdefault(obs["quarter"], []).append(obs["raw_ret_pct"])
    q_medians = {q: float(np.median(vals)) for q, vals in by_quarter.items() if vals}
    global_med = float(np.median([o["raw_ret_pct"] for o in observations])) if observations else 0.0

    for obs in observations:
        q_med = q_medians.get(obs["quarter"], global_med)
        excess = obs["raw_ret_pct"] - q_med
        obs["cohort_median_ret_pct"] = round(q_med, 2)
        obs["excess_ret_pct"] = round(excess, 2)
        obs["y_win"] = 1 if excess > 0.0 else 0

    return observations


def run_supervised_calibration(
    *,
    stocks: list[dict[str, Any]] | None = None,
    force_rebuild: bool = False,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Train, cross-validate (`TimeSeriesSplit`), and backtest the Supervised Signal
    Calibration models, calibrate optimal `opportunity_weights.yaml` pillar weights,
    and score candidate stocks.
    """
    t0 = time.time()

    # Check cached model summary if not forcing rebuild
    cached_summary = None
    if not force_rebuild:
        mem = _sup_mem_cache.get("core")
        if mem and (time.time() - mem.get("ts", 0)) < SUPERVISED_CACHE_TTL_SEC:
            cached_summary = mem["data"]

    if cached_summary is None:
        observations = build_signal_calibration_dataset(db_path=db_path)
        n_obs = len(observations)
        if n_obs < 40:
            return {
                "available": False,
                "n_observations": n_obs,
                "error": "Insufficient historical signal events for walk-forward calibration.",
            }

        X_raw = np.array(
            [[v if v is not None else np.nan for v in o["features"]] for o in observations],
            dtype=np.float64,
        )
        y_bin = np.array([o["y_win"] for o in observations], dtype=np.int32)
        y_excess = np.array([o["excess_ret_pct"] for o in observations], dtype=np.float64)
        y_raw = np.array([o["raw_ret_pct"] for o in observations], dtype=np.float64)

        P_raw = np.array(
            [
                [
                    o["pillars"]["valuation"],
                    o["pillars"]["price_posture"],
                    o["pillars"]["insider"],
                    o["pillars"]["buyback_at_low"],
                ]
                for o in observations
            ],
            dtype=np.float64,
        )

        # Current configured weights
        cur_w_pub = get_weights_public().get("weights") or {}
        w_cur_vec = np.array(
            [
                float(cur_w_pub.get("valuation", 0.35)),
                float(cur_w_pub.get("price_posture", 0.25)),
                float(cur_w_pub.get("insider", 0.20)),
                float(cur_w_pub.get("buyback_at_low", 0.20)),
            ],
            dtype=np.float64,
        )
        w_cur_vec = w_cur_vec / max(1e-9, np.sum(w_cur_vec))
        heur_scores = P_raw @ w_cur_vec

        # ------------------------------------------------------------------
        # 1. Walk-Forward Time-Series Cross-Validation (`TimeSeriesSplit`)
        # ------------------------------------------------------------------
        n_splits = 5
        tscv = TimeSeriesSplit(n_splits=n_splits)

        oof_probs = np.full(n_obs, np.nan, dtype=np.float64)
        oof_pred_ret = np.full(n_obs, np.nan, dtype=np.float64)
        fold_metrics: list[dict[str, Any]] = []

        for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X_raw), start=1):
            if len(train_idx) < 25 or len(test_idx) < 15:
                continue
            imp = SimpleImputer(strategy="median")
            scaler = RobustScaler()
            X_tr = np.clip(scaler.fit_transform(imp.fit_transform(X_raw[train_idx])), -4.0, 4.0)
            X_te = np.clip(scaler.transform(imp.transform(X_raw[test_idx])), -4.0, 4.0)
            y_tr_b = y_bin[train_idx]
            y_te_b = y_bin[test_idx]
            y_tr_r = np.clip(y_excess[train_idx], -60.0, 120.0)
            y_te_r = y_excess[test_idx]

            # Model A: L2 Logistic Regression
            clf_lr = LogisticRegression(C=0.4, max_iter=300, random_state=42)
            clf_lr.fit(X_tr, y_tr_b)
            prob_lr = clf_lr.predict_proba(X_te)[:, 1]

            # Model B: HistGradientBoostingClassifier
            clf_gb = HistGradientBoostingClassifier(
                max_depth=3,
                max_iter=80,
                learning_rate=0.05,
                l2_regularization=3.0,
                min_samples_leaf=20,
                random_state=42,
            )
            clf_gb.fit(X_tr, y_tr_b)
            prob_gb = clf_gb.predict_proba(X_te)[:, 1]

            # Ensemble Probability
            prob_ens = 0.55 * prob_lr + 0.45 * prob_gb
            oof_probs[test_idx] = prob_ens

            # Expected Excess Return Regressor
            reg_gb = HistGradientBoostingRegressor(
                max_depth=3,
                max_iter=80,
                learning_rate=0.05,
                l2_regularization=3.0,
                min_samples_leaf=20,
                random_state=42,
            )
            reg_gb.fit(X_tr, y_tr_r)
            reg_ridge = Ridge(alpha=25.0)
            reg_ridge.fit(X_tr, y_tr_r)
            oof_pred_ret[test_idx] = 0.55 * reg_ridge.predict(X_te) + 0.45 * reg_gb.predict(X_te)

            # Fold metrics
            try:
                f_auc = float(roc_auc_score(y_te_b, prob_ens))
            except Exception:
                f_auc = 0.5
            f_ic_res = spearmanr(prob_ens, y_te_r)
            f_ic = float(f_ic_res.statistic) if (f_ic_res and math.isfinite(f_ic_res.statistic)) else 0.0

            # Top quintile excess return in this test fold
            q80 = float(np.percentile(prob_ens, 80))
            top_q_mask = prob_ens >= q80
            top_q_excess = float(np.mean(y_te_r[top_q_mask])) if np.any(top_q_mask) else 0.0
            top_q_win = float(np.mean(y_te_b[top_q_mask]) * 100.0) if np.any(top_q_mask) else 50.0

            fold_metrics.append({
                "fold": fold_idx,
                "train_samples": int(len(train_idx)),
                "test_samples": int(len(test_idx)),
                "test_period": f"{observations[test_idx[0]]['event_date']} -> {observations[test_idx[-1]]['event_date']}",
                "roc_auc": round(f_auc, 3),
                "rank_ic": round(f_ic, 3),
                "top_quintile_excess_pct": round(top_q_excess, 2),
                "top_quintile_win_rate_pct": round(top_q_win, 1),
            })

        # Aggregate Out-of-Sample Walk-Forward Diagnostics
        valid_oof = ~np.isnan(oof_probs)
        oof_p = oof_probs[valid_oof]
        oof_y_b = y_bin[valid_oof]
        oof_y_ex = y_excess[valid_oof]
        oof_y_raw = y_raw[valid_oof]
        oof_heur = heur_scores[valid_oof]

        try:
            overall_auc = round(float(roc_auc_score(oof_y_b, oof_p)), 3)
        except Exception:
            overall_auc = 0.500
        try:
            overall_brier = round(float(brier_score_loss(oof_y_b, oof_p)), 4)
        except Exception:
            overall_brier = 0.250

        ic_res = spearmanr(oof_p, oof_y_ex)
        overall_ic = round(float(ic_res.statistic), 3) if (ic_res and math.isfinite(ic_res.statistic)) else 0.0
        heur_ic_res = spearmanr(oof_heur, oof_y_ex)
        heur_ic = round(float(heur_ic_res.statistic), 3) if (heur_ic_res and math.isfinite(heur_ic_res.statistic)) else 0.0

        n_oof = int(np.sum(valid_oof))
        ic_t_stat = (
            round(overall_ic * math.sqrt((n_oof - 2) / max(1e-6, 1.0 - overall_ic ** 2)), 2)
            if n_oof > 10
            else 0.0
        )

        p90 = float(np.percentile(oof_p, 90))
        top_dec_mask = oof_p >= p90
        top_decile_win_rate = round(float(np.mean(oof_y_b[top_dec_mask]) * 100.0), 1)
        baseline_win_rate = round(float(np.mean(oof_y_b) * 100.0), 1)
        top_decile_excess = round(float(np.mean(oof_y_ex[top_dec_mask])), 2)

        # ------------------------------------------------------------------
        # 2. Out-of-Sample Quintile Backtest (`Q1` Lowest -> `Q5` Highest)
        # ------------------------------------------------------------------
        quintile_bounds = np.percentile(oof_p, [0, 20, 40, 60, 80, 100])
        heur_bounds = np.percentile(oof_heur, [0, 20, 40, 60, 80, 100])
        quintiles: list[dict[str, Any]] = []
        for q_i in range(5):
            lo_b, hi_b = quintile_bounds[q_i], quintile_bounds[q_i + 1]
            if q_i == 4:
                q_mask = (oof_p >= lo_b) & (oof_p <= hi_b)
                h_mask = (oof_heur >= heur_bounds[q_i]) & (oof_heur <= heur_bounds[q_i + 1])
            else:
                q_mask = (oof_p >= lo_b) & (oof_p < hi_b)
                h_mask = (oof_heur >= heur_bounds[q_i]) & (oof_heur < heur_bounds[q_i + 1])

            q_ex_mean = round(float(np.mean(oof_y_ex[q_mask])), 2) if np.any(q_mask) else 0.0
            q_ex_med = round(float(np.median(oof_y_ex[q_mask])), 2) if np.any(q_mask) else 0.0
            q_raw_med = round(float(np.median(oof_y_raw[q_mask])), 2) if np.any(q_mask) else 0.0
            q_win = round(float(np.mean(oof_y_b[q_mask]) * 100.0), 1) if np.any(q_mask) else 50.0
            h_ex_mean = round(float(np.mean(oof_y_ex[h_mask])), 2) if np.any(h_mask) else 0.0
            h_win = round(float(np.mean(oof_y_b[h_mask]) * 100.0), 1) if np.any(h_mask) else 50.0

            quintiles.append({
                "quintile": f"Q{q_i + 1}",
                "label": ["Q1 (Lowest 20%)", "Q2", "Q3 (Middle)", "Q4", "Q5 (Top 20%)"][q_i],
                "count": int(np.sum(q_mask)),
                "avg_pred_prob_pct": round(float(np.mean(oof_p[q_mask]) * 100.0), 1) if np.any(q_mask) else 50.0,
                "ml_mean_excess_pct": q_ex_mean,
                "ml_median_excess_pct": q_ex_med,
                "ml_median_raw_ret_pct": q_raw_med,
                "ml_win_rate_pct": q_win,
                "heuristic_mean_excess_pct": h_ex_mean,
                "heuristic_win_rate_pct": h_win,
            })

        # Reliability / Calibration Curve (5 bins)
        calibration_curve: list[dict[str, Any]] = []
        for q in quintiles:
            calibration_curve.append({
                "bin": q["quintile"],
                "predicted_win_prob_pct": q["avg_pred_prob_pct"],
                "realized_win_rate_pct": q["ml_win_rate_pct"],
                "mean_excess_ret_pct": q["ml_mean_excess_pct"],
                "count": q["count"],
            })

        # ------------------------------------------------------------------
        # 3. Signal Event-Study Breakdown by Signal Regime
        # ------------------------------------------------------------------
        signal_regimes = [
            (
                "Insider Buy Near 52w Low (<15% gap)",
                lambda o: o["signal_type"] == "insider_buy" and o["event_gap_52w_pct"] <= 15.0,
            ),
            (
                "Insider Cluster Buy (2+ buys in month)",
                lambda o: o["signal_type"] == "insider_buy" and o["tx_count"] >= 2,
            ),
            (
                "Share Buyback Near 52w Low (<20% gap)",
                lambda o: o["signal_type"] == "share_buyback" and o["event_gap_52w_pct"] <= 20.0,
            ),
            (
                "Share Buyback Below 200w MA",
                lambda o: o["signal_type"] == "share_buyback" and o["event_ma200w_gap_pct"] < 0.0,
            ),
            (
                "Signal Chasing (>45% above 52w low)",
                lambda o: o["event_gap_52w_pct"] > 45.0,
            ),
        ]
        regime_stats: list[dict[str, Any]] = []
        for r_label, pred_fn in signal_regimes:
            sub = [o for o in observations if pred_fn(o)]
            if not sub:
                continue
            ex_vals = [o["excess_ret_pct"] for o in sub]
            raw_vals = [o["raw_ret_pct"] for o in sub]
            wins = [o["y_win"] for o in sub]
            regime_stats.append({
                "regime": r_label,
                "events": len(sub),
                "win_rate_pct": round(100.0 * sum(wins) / len(wins), 1),
                "mean_excess_ret_pct": round(float(np.mean(ex_vals)), 2),
                "median_excess_ret_pct": round(float(np.median(ex_vals)), 2),
                "median_raw_ret_pct": round(float(np.median(raw_vals)), 2),
            })

        # ------------------------------------------------------------------
        # 4. Full-Sample Fit for Feature Importances & Live Stock Scoring
        # ------------------------------------------------------------------
        imp_full = SimpleImputer(strategy="median")
        scaler_full = RobustScaler()
        X_full = np.clip(scaler_full.fit_transform(imp_full.fit_transform(X_raw)), -4.0, 4.0)
        y_excess_clip = np.clip(y_excess, -60.0, 120.0)

        clf_lr_full = LogisticRegression(C=0.4, max_iter=300, random_state=42)
        clf_lr_full.fit(X_full, y_bin)

        clf_gb_full = HistGradientBoostingClassifier(
            max_depth=3,
            max_iter=90,
            learning_rate=0.05,
            l2_regularization=3.0,
            min_samples_leaf=20,
            random_state=42,
        )
        clf_gb_full.fit(X_full, y_bin)

        reg_ridge_full = Ridge(alpha=25.0)
        reg_ridge_full.fit(X_full, y_excess_clip)

        reg_gb_full = HistGradientBoostingRegressor(
            max_depth=3,
            max_iter=90,
            learning_rate=0.05,
            l2_regularization=3.0,
            min_samples_leaf=20,
            random_state=42,
        )
        reg_gb_full.fit(X_full, y_excess_clip)

        lr_coefs = clf_lr_full.coef_[0]
        ridge_coefs = reg_ridge_full.coef_

        # Compute univariate Rank IC + standardized Logistic & Ridge weights per feature
        feature_importances: list[dict[str, Any]] = []
        raw_abs_weights = np.abs(lr_coefs) + np.abs(ridge_coefs) / max(1.0, float(np.std(y_excess_clip)))
        total_abs_w = float(np.sum(raw_abs_weights)) or 1.0

        for j, fspec in enumerate(SUPERVISED_FEATURES):
            col = X_full[:, j]
            f_ic_r = spearmanr(col, y_excess)
            f_ic_val = float(f_ic_r.statistic) if (f_ic_r and math.isfinite(f_ic_r.statistic)) else 0.0
            rel_imp_pct = round(100.0 * float(raw_abs_weights[j]) / total_abs_w, 1)
            feature_importances.append({
                "key": fspec["key"],
                "label": fspec["label"],
                "pillar": fspec["pillar"],
                "logistic_beta": round(float(lr_coefs[j]), 3),
                "ridge_excess_ret_beta": round(float(ridge_coefs[j]), 2),
                "univariate_ic": round(f_ic_val, 3),
                "importance_pct": rel_imp_pct,
            })

        feature_importances.sort(key=lambda x: x["importance_pct"], reverse=True)

        # ------------------------------------------------------------------
        # 5. Calibrate Optimal `opportunity_weights.yaml` Pillar Weights
        # ------------------------------------------------------------------
        pillar_keys = ["valuation", "price_posture", "insider", "buyback_at_low"]
        pillar_imp_sums = {pk: 0.05 for pk in pillar_keys}
        for fi in feature_importances:
            pk = fi["pillar"]
            # Weight by positive empirical edge + importance
            edge_mult = 1.25 if (fi["logistic_beta"] > 0 or fi["univariate_ic"] > 0) else 0.65
            pillar_imp_sums[pk] += (fi["importance_pct"] / 100.0) * edge_mult

        # Also incorporate direct non-negative regression on the 4 pillar scores
        P_scaled = (P_raw - np.mean(P_raw, axis=0)) / (np.std(P_raw, axis=0) + 1e-6)
        pillar_lr = LogisticRegression(C=0.5, random_state=42).fit(P_scaled, y_bin)
        for idx_p, pk in enumerate(pillar_keys):
            direct_b = max(0.02, float(pillar_lr.coef_[0][idx_p]))
            pillar_imp_sums[pk] = 0.60 * pillar_imp_sums[pk] + 0.40 * direct_b

        # Clamp each pillar between 0.12 and 0.48 and normalize to sum to 1.00
        raw_p_vals = [max(0.12, min(0.48, pillar_imp_sums[pk])) for pk in pillar_keys]
        p_sum = sum(raw_p_vals)
        cal_weights: dict[str, Any] = {}
        for pk, v in zip(pillar_keys, raw_p_vals):
            cal_weights[pk] = round(v / p_sum, 2)
        cal_weights[pillar_keys[-1]] = round(
            1.0 - sum(cal_weights[k] for k in pillar_keys[:-1]), 2
        )

        # Grid-search optimal `near_52w_low_pct` and `buyback_near_low_pct` thresholds
        best_near_low = int(cur_w_pub.get("near_52w_low_pct", 15))
        best_bb_low = int(cur_w_pub.get("buyback_near_low_pct", 20))
        best_thresh_ex = -999.0
        for cand_nl in (10, 15, 20, 25):
            sub_nl = [o["excess_ret_pct"] for o in observations if o["event_gap_52w_pct"] <= cand_nl]
            if len(sub_nl) >= 25:
                score_nl = float(np.mean(sub_nl)) * math.log10(len(sub_nl))
                if score_nl > best_thresh_ex:
                    best_thresh_ex = score_nl
                    best_near_low = cand_nl

        best_bb_ex = -999.0
        for cand_bbl in (15, 20, 25, 30):
            sub_bb = [
                o["excess_ret_pct"]
                for o in observations
                if o["signal_type"] == "share_buyback" and o["event_gap_52w_pct"] <= cand_bbl
            ]
            if len(sub_bb) >= 20:
                score_bb = float(np.mean(sub_bb)) * math.log10(len(sub_bb))
                if score_bb > best_bb_ex:
                    best_bb_ex = score_bb
                    best_bb_low = cand_bbl

        cal_weights["near_52w_low_pct"] = best_near_low
        cal_weights["buyback_near_low_pct"] = best_bb_low
        cal_weights["below_200w_ma_bonus"] = True

        w_cal_vec = np.array([cal_weights[k] for k in pillar_keys], dtype=np.float64)
        cal_pillar_scores = P_raw[valid_oof] @ w_cal_vec
        cal_p_ic_res = spearmanr(cal_pillar_scores, oof_y_ex)
        cal_pillar_ic = (
            round(float(cal_p_ic_res.statistic), 3)
            if (cal_p_ic_res and math.isfinite(cal_p_ic_res.statistic))
            else overall_ic
        )

        cached_summary = {
            "computed_at": datetime.now().isoformat(timespec="seconds"),
            "n_observations": n_obs,
            "oof_samples": n_oof,
            "date_range": {
                "start": observations[0]["event_date"],
                "end": observations[-1]["event_date"],
            },
            "metrics": {
                "roc_auc": overall_auc,
                "brier_score": overall_brier,
                "rank_ic": overall_ic,
                "ic_t_stat": ic_t_stat,
                "heuristic_rank_ic": heur_ic,
                "calibrated_pillar_ic": cal_pillar_ic,
                "baseline_win_rate_pct": baseline_win_rate,
                "top_decile_win_rate_pct": top_decile_win_rate,
                "top_decile_excess_ret_pct": top_decile_excess,
                "q5_vs_q1_excess_spread_pct": round(
                    quintiles[-1]["ml_mean_excess_pct"] - quintiles[0]["ml_mean_excess_pct"], 2
                ),
                "heuristic_q5_vs_q1_spread_pct": round(
                    quintiles[-1]["heuristic_mean_excess_pct"] - quintiles[0]["heuristic_mean_excess_pct"], 2
                ),
            },
            "folds": fold_metrics,
            "quintiles": quintiles,
            "calibration_curve": calibration_curve,
            "signal_regimes": regime_stats,
            "feature_importances": feature_importances,
            "weight_calibration": {
                "current_weights": {
                    "valuation": float(cur_w_pub.get("valuation", 0.35)),
                    "price_posture": float(cur_w_pub.get("price_posture", 0.25)),
                    "insider": float(cur_w_pub.get("insider", 0.20)),
                    "buyback_at_low": float(cur_w_pub.get("buyback_at_low", 0.20)),
                    "near_52w_low_pct": int(cur_w_pub.get("near_52w_low_pct", 15)),
                    "buyback_near_low_pct": int(cur_w_pub.get("buyback_near_low_pct", 20)),
                },
                "calibrated_weights": cal_weights,
            },
            "_models": {
                "imputer": imp_full,
                "scaler": scaler_full,
                "clf_lr": clf_lr_full,
                "clf_gb": clf_gb_full,
                "reg_ridge": reg_ridge_full,
                "reg_gb": reg_gb_full,
                "lr_coefs": lr_coefs,
            },
        }
        _sup_mem_cache["core"] = {"ts": time.time(), "data": cached_summary}

    # Score candidate stocks (either provided list or global buybacks/signals list)
    models = cached_summary["_models"]
    scored_stocks = _score_candidate_stocks(stocks=stocks, models=models, db_path=db_path)

    # Strip non-JSON-serializable `_models` before returning
    out = {k: v for k, v in cached_summary.items() if k != "_models"}
    out["available"] = True
    out["elapsed_s"] = round(time.time() - t0, 2)
    out["top_calibrated_candidates"] = scored_stocks[:30]
    return out


def _score_candidate_stocks(
    *,
    stocks: list[dict[str, Any]] | None,
    models: dict[str, Any],
    db_path: str = "asx_announcements_cache.db",
) -> list[dict[str, Any]]:
    if not stocks:
        from scoring.unsupervised_ml import load_full_global_stock_universe

        stocks = load_full_global_stock_universe(db_path=db_path)

    valid_stocks = [
        s
        for s in (stocks or [])
        if s and not s.get("_pending_yf") and (s.get("current_price") or 0) > 0
    ]
    if not valid_stocks:
        return []

    feat_rows: list[list[float | None]] = []
    kept_stocks: list[dict[str, Any]] = []
    for s in valid_stocks:
        fv = _extract_feature_vector_from_record(stock=s)
        if sum(1 for v in fv if v is not None) >= 5:
            feat_rows.append(fv)
            kept_stocks.append(s)

    if not kept_stocks:
        return []

    X_cand_raw = np.array(
        [[v if v is not None else np.nan for v in r] for r in feat_rows],
        dtype=np.float64,
    )
    imp: SimpleImputer = models["imputer"]
    scaler: RobustScaler = models["scaler"]
    X_cand = np.clip(scaler.transform(imp.transform(X_cand_raw)), -4.0, 4.0)

    probs_lr = models["clf_lr"].predict_proba(X_cand)[:, 1]
    probs_gb = models["clf_gb"].predict_proba(X_cand)[:, 1]
    probs_ens = 0.55 * probs_lr + 0.45 * probs_gb

    ret_ridge = models["reg_ridge"].predict(X_cand)
    ret_gb = models["reg_gb"].predict(X_cand)
    ret_ens = 0.55 * ret_ridge + 0.45 * ret_gb

    lr_coefs: np.ndarray = models["lr_coefs"]

    results: list[dict[str, Any]] = []
    for idx, s in enumerate(kept_stocks):
        win_prob = round(float(probs_ens[idx]) * 100.0, 1)
        pred_ex = round(float(ret_ens[idx]), 1)
        x_s = X_cand[idx]
        contribs = x_s * lr_coefs
        top_pos_idx = int(np.argmax(contribs))
        top_neg_idx = int(np.argmin(contribs))

        results.append({
            "ticker": s.get("ticker"),
            "name": s.get("name") or s.get("ticker"),
            "sector": s.get("sector") or "Unknown",
            "win_prob_pct": win_prob,
            "pred_excess_ret_pct": pred_ex,
            "pct_from_52w_low": round(float(s.get("pct_from_52w_low") or 0.0), 1),
            "annual_buyback_pct": round(float(s.get("annual_buyback_pct") or (s.get("metrics") or {}).get("annual_buyback_pct") or 0.0), 2),
            "insider_buys_2026": int(s.get("insider_buys_2026") or 0),
            "top_positive_driver": f"{SUPERVISED_FEATURES[top_pos_idx]['label']} ({contribs[top_pos_idx]:+.2f})",
            "top_negative_drag": f"{SUPERVISED_FEATURES[top_neg_idx]['label']} ({contribs[top_neg_idx]:+.2f})",
        })

    results.sort(key=lambda r: (r["win_prob_pct"], r["pred_excess_ret_pct"]), reverse=True)
    return results


def compute_stock_supervised_profile(
    stock: dict[str, Any],
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Compute the per-stock Supervised Learning Signal Calibration profile & SHAP-style
    log-odds feature attribution for the Stock Detail Modal.
    """
    if not stock or (stock.get("current_price") or 0) <= 0:
        return {"available": False}

    # Ensure model is warm in memory
    mem = _sup_mem_cache.get("core")
    if not mem or (time.time() - mem.get("ts", 0)) >= SUPERVISED_CACHE_TTL_SEC:
        run_supervised_calibration(stocks=[stock], db_path=db_path)
        mem = _sup_mem_cache.get("core")
    if not mem or not mem.get("data"):
        return {"available": False}

    core = mem["data"]
    models = core["_models"]
    fv = _extract_feature_vector_from_record(stock=stock)
    X_one = np.array([[v if v is not None else np.nan for v in fv]], dtype=np.float64)
    X_s = np.clip(models["scaler"].transform(models["imputer"].transform(X_one)), -4.0, 4.0)

    p_lr = float(models["clf_lr"].predict_proba(X_s)[0, 1]) * 100.0
    p_gb = float(models["clf_gb"].predict_proba(X_s)[0, 1]) * 100.0
    win_prob = round(0.55 * p_lr + 0.45 * p_gb, 1)

    r_ridge = float(models["reg_ridge"].predict(X_s)[0])
    r_gb = float(models["reg_gb"].predict(X_s)[0])
    pred_excess = round(0.55 * r_ridge + 0.45 * r_gb, 1)

    lr_coefs: np.ndarray = models["lr_coefs"]
    x_vec = X_s[0]
    contribs: list[dict[str, Any]] = []
    for j, fspec in enumerate(SUPERVISED_FEATURES):
        c_val = float(x_vec[j] * lr_coefs[j])
        contribs.append({
            "key": fspec["key"],
            "label": fspec["label"],
            "pillar": fspec["pillar"],
            "raw_value": round(float(fv[j]), 2) if fv[j] is not None else None,
            "scaled_z": round(float(x_vec[j]), 2),
            "log_odds_contrib": round(c_val, 3),
        })

    contribs.sort(key=lambda c: abs(c["log_odds_contrib"]), reverse=True)

    if win_prob >= 62.0:
        tier = "High Conviction Outperformer"
    elif win_prob >= 53.0:
        tier = "Favorable Risk/Reward"
    elif win_prob >= 45.0:
        tier = "Neutral / Market Perform"
    else:
        tier = "Caution / Below-Median Profile"

    return {
        "available": True,
        "ticker": stock.get("ticker"),
        "win_prob_pct": win_prob,
        "logistic_prob_pct": round(p_lr, 1),
        "gbdt_prob_pct": round(p_gb, 1),
        "pred_excess_ret_pct": pred_excess,
        "conviction_tier": tier,
        "model_roc_auc": core["metrics"]["roc_auc"],
        "model_rank_ic": core["metrics"]["rank_ic"],
        "n_training_events": core["n_observations"],
        "feature_contributions": contribs[:8],
    }
