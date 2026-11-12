"""
Monthly Point-in-Time Snapshot Engine & Historical Outlier Return Predictor.

1. **Monthly Point-in-Time Snapshots (`monthly_stock_snapshots` + `monthly_snapshot_runs`)**:
   - Automatically captures once per calendar month (`YYYY-MM`) a complete point-in-time
     snapshot of all ~16,700+ global cached stocks, including:
       * Price & 52w range (`price`, `low_52w`, `high_52w`, `market_cap`, `currency`)
       * Fundamental & Balance-Sheet ratios (`pe`, `pb`, `p_fcf`, `ev_ebitda`, `fcf_yield`,
         `roe`, `roic`, `debt_to_equity`, `net_cash_pct_mcap`, `net_assets_vs_mcap_pct`,
         `pct_from_52w_low`, `pct_from_200w_ma`, `rsi`, `annual_buyback_pct`)
       * Statistical & Unsupervised ML scores (`vq_score`, `val_z`, `qual_z`, `posture_z`,
         `mispriced_outlier_score`, `is_mispriced_outlier`, `anomaly_score`, `is_iso_outlier`,
         `val_residual_z`, `peer_val_diff_z`, `cluster_id`, `cluster_label`, `pca_x`, `pca_y`)
   - Backfills forward realized returns (`ret_1m_pct`, `ret_3m_pct`, `ret_6m_pct`,
     `ret_12m_pct`, `ret_to_now_pct`) on all prior monthly snapshots whenever a new
     snapshot is recorded or evaluated.

2. **Historical Outlier Return Prediction (`predict_returns_from_historical_snapshots`)**:
   - Evaluates historical cohorts of stocks that screened as **Mispriced Quality Outliers**
     (vs. the rest of the market) across past monthly snapshots, tracking their average/median
     forward returns and win-rate (`% > 0` and `% beating median`).
   - Uses a `scikit-learn` k-NN Historical Analog Model + Ridge Regression on historical
     `(val_z, qual_z, post_z, val_residual_z, peer_val_diff_z, mispriced_outlier_score)`
     snapshots with realized forward returns to find **Present-Day Stocks Matching Past
     Winning Outlier Signatures** and estimate their expected forward return (`pred_return_pct`)
     along with their closest historical winning analogs.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from typing import Any

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 4))

import numpy as np
from sklearn.linear_model import HuberRegressor
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import RobustScaler

logger = logging.getLogger(__name__)

_SNAPSHOT_DDL = (
    """
    CREATE TABLE IF NOT EXISTS monthly_snapshot_runs (
        snapshot_month TEXT PRIMARY KEY,
        captured_at TEXT,
        total_stocks INTEGER,
        outlier_count INTEGER,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monthly_stock_snapshots (
        snapshot_month TEXT,
        ticker TEXT,
        captured_at TEXT,
        name TEXT,
        sector TEXT,
        currency TEXT,
        price REAL,
        market_cap REAL,
        pe REAL,
        pb REAL,
        p_fcf REAL,
        ev_ebitda REAL,
        fcf_yield REAL,
        roic_or_roe REAL,
        debt_to_equity REAL,
        net_cash_pct_mcap REAL,
        net_assets_vs_mcap_pct REAL,
        pct_from_52w_low REAL,
        pct_from_200w_ma REAL,
        rsi REAL,
        annual_buyback_pct REAL,
        vq_score REAL,
        val_z REAL,
        qual_z REAL,
        posture_z REAL,
        mispriced_outlier_score REAL,
        is_mispriced_outlier INTEGER,
        anomaly_score REAL,
        is_iso_outlier INTEGER,
        val_residual_z REAL,
        peer_val_diff_z REAL,
        cluster_id INTEGER,
        cluster_label TEXT,
        pca_x REAL,
        pca_y REAL,
        ret_1m_pct REAL,
        ret_3m_pct REAL,
        ret_6m_pct REAL,
        ret_12m_pct REAL,
        ret_to_now_pct REAL,
        forward_pe REAL,
        short_pct_float REAL,
        short_ratio REAL,
        short_mom_change_pct REAL,
        forward_eps REAL,
        eps_forward_vs_trailing_pct REAL,
        analyst_count INTEGER,
        recommendation_mean REAL,
        upside_to_low_target_pct REAL,
        sbc_adjusted_fcf_yield REAL,
        share_count_5y_change_pct REAL,
        shares_yoy_change_pct REAL,
        sloan_accrual_pct_mcap REAL,
        capex_to_da_ratio REAL,
        interest_coverage REAL,
        piotroski_f_score INTEGER,
        median_dollar_vol_20d REAL,
        volatility_60d_ann_pct REAL,
        max_drawdown_1y_pct REAL,
        forward_eps_current_yr REAL,
        forward_eps_next_yr REAL,
        eps_rev_30d_pct REAL,
        eps_rev_90d_pct REAL,
        shares_3y_cagr_pct REAL,
        net_debt_to_ebitda REAL,
        current_ratio_yoy_delta REAL,
        gross_margin_yoy_delta REAL,
        inventory_to_sales_delta_pct REAL,
        sector_capex_to_da_median REAL,
        cloner_holder_count INTEGER,
        cloner_new_position_flag INTEGER,
        cloner_max_weight_pct REAL,
        max_pos_usd_5pct_adv REAL,
        PRIMARY KEY (snapshot_month, ticker)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_msnap_month ON monthly_stock_snapshots(snapshot_month)",
    "CREATE INDEX IF NOT EXISTS idx_msnap_ticker ON monthly_stock_snapshots(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_msnap_outlier ON monthly_stock_snapshots(snapshot_month, is_mispriced_outlier)",
)

_EXTRA_SNAPSHOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("forward_pe", "REAL"),
    ("short_pct_float", "REAL"),
    ("short_ratio", "REAL"),
    ("short_mom_change_pct", "REAL"),
    ("forward_eps", "REAL"),
    ("eps_forward_vs_trailing_pct", "REAL"),
    ("analyst_count", "INTEGER"),
    ("recommendation_mean", "REAL"),
    ("upside_to_low_target_pct", "REAL"),
    ("sbc_adjusted_fcf_yield", "REAL"),
    ("share_count_5y_change_pct", "REAL"),
    ("shares_yoy_change_pct", "REAL"),
    ("sloan_accrual_pct_mcap", "REAL"),
    ("capex_to_da_ratio", "REAL"),
    ("interest_coverage", "REAL"),
    ("piotroski_f_score", "INTEGER"),
    ("median_dollar_vol_20d", "REAL"),
    ("volatility_60d_ann_pct", "REAL"),
    ("max_drawdown_1y_pct", "REAL"),
    ("forward_eps_current_yr", "REAL"),
    ("forward_eps_next_yr", "REAL"),
    ("eps_rev_30d_pct", "REAL"),
    ("eps_rev_90d_pct", "REAL"),
    ("shares_3y_cagr_pct", "REAL"),
    ("net_debt_to_ebitda", "REAL"),
    ("current_ratio_yoy_delta", "REAL"),
    ("gross_margin_yoy_delta", "REAL"),
    ("inventory_to_sales_delta_pct", "REAL"),
    ("sector_capex_to_da_median", "REAL"),
    ("cloner_holder_count", "INTEGER"),
    ("cloner_new_position_flag", "INTEGER"),
    ("cloner_max_weight_pct", "REAL"),
    ("max_pos_usd_5pct_adv", "REAL"),
)

_snapshot_lock = threading.Lock()
_monthly_scheduler_started = False
_migrated_dbs: set[str] = set()


def _snap_conn(db_path: str = "asx_announcements_cache.db"):
    from data.db import connect_db

    conn = connect_db(db_path, schema_key="monthly_snapshots", ddl=_SNAPSHOT_DDL)
    if db_path not in _migrated_dbs:
        try:
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(monthly_stock_snapshots)").fetchall()
            }
            for col_name, col_type in _EXTRA_SNAPSHOT_COLUMNS:
                if col_name not in existing:
                    conn.execute(
                        f"ALTER TABLE monthly_stock_snapshots ADD COLUMN {col_name} {col_type}"
                    )
            conn.commit()
            _migrated_dbs.add(db_path)
        except Exception as ex:
            logger.debug("monthly_stock_snapshots column migration skipped: %s", ex)
    return conn


def _safe_num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _month_diff(m_earlier: str, m_later: str) -> int:
    """Return number of calendar months between two 'YYYY-MM' strings."""
    try:
        y1, mo1 = int(m_earlier[:4]), int(m_earlier[5:7])
        y2, mo2 = int(m_later[:4]), int(m_later[5:7])
        return (y2 - y1) * 12 + (mo2 - mo1)
    except Exception:
        return 0


def current_snapshot_month() -> str:
    return datetime.now().strftime("%Y-%m")


def has_snapshot_for_month(
    snapshot_month: str | None = None,
    db_path: str = "asx_announcements_cache.db",
) -> bool:
    month_str = snapshot_month or current_snapshot_month()
    conn = _snap_conn(db_path)
    try:
        row = conn.execute(
            "SELECT total_stocks FROM monthly_snapshot_runs WHERE snapshot_month = ?",
            (month_str,),
        ).fetchone()
        return bool(row and (row[0] or 0) > 0)
    finally:
        conn.close()


def capture_monthly_snapshot(
    *,
    snapshot_month: str | None = None,
    force: bool = False,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Capture a full point-in-time snapshot of all cached global stocks + their EDA &
    Unsupervised ML scores for `snapshot_month` (defaults to current `YYYY-MM`).
    Also updates realized forward returns (`ret_1m_pct`, `ret_3m_pct`, `ret_6m_pct`,
    `ret_12m_pct`, `ret_to_now_pct`) on all prior monthly snapshots.
    """
    month_str = snapshot_month or current_snapshot_month()
    if not force and has_snapshot_for_month(month_str, db_path=db_path):
        # Still refresh `ret_to_now_pct` on historical snapshots if any exist
        update_forward_returns_on_snapshots(db_path=db_path)
        return {
            "status": "already_exists",
            "snapshot_month": month_str,
            "summary": get_snapshots_overview(db_path=db_path),
        }

    if not _snapshot_lock.acquire(blocking=False):
        return {"status": "busy", "snapshot_month": month_str}

    t0 = time.time()
    try:
        from scoring.eda_stats import extract_metric
        from scoring.unsupervised_ml import (
            load_full_global_stock_universe,
            load_persisted_global_ml,
            rebuild_global_unsupervised_ml,
        )

        stocks = load_full_global_stock_universe(db_path=db_path)
        ml_payload = load_persisted_global_ml(db_path=db_path)
        if not ml_payload or not ml_payload.get("profiles"):
            ml_payload = rebuild_global_unsupervised_ml(db_path=db_path)
        profiles = (ml_payload and ml_payload.get("profiles")) or {}

        now_iso = datetime.now().isoformat(timespec="seconds")
        rows_to_insert: list[tuple[Any, ...]] = []
        outlier_count = 0

        # Precompute sector median capex_to_da_ratio across universe (Step 4)
        sector_capex_lists: dict[str, list[float]] = {}
        for s in stocks:
            sec_k = str(s.get("sector") or "Unknown")
            cda = _safe_num((s.get("metrics") or {}).get("capex_to_da_ratio"))
            if cda is not None and 0.0 < cda < 15.0:
                sector_capex_lists.setdefault(sec_k, []).append(cda)
        sector_capex_medians = {
            sec_k: round(float(np.median(vals)), 2)
            for sec_k, vals in sector_capex_lists.items()
            if vals
        }

        # Load prior monthly snapshots' forward_eps to derive 30d/90d EPS revision drift when not in live cache
        prior_feps_1m: dict[str, float] = {}
        prior_feps_3m: dict[str, float] = {}
        conn_pre = _snap_conn(db_path)
        try:
            prev_months = [
                r[0]
                for r in conn_pre.execute(
                    "SELECT DISTINCT snapshot_month FROM monthly_stock_snapshots WHERE snapshot_month < ? ORDER BY snapshot_month DESC LIMIT 4",
                    (month_str,),
                ).fetchall()
            ]
            for pm in prev_months:
                mdiff = _month_diff(pm, month_str)
                if mdiff in (1, 2) and not prior_feps_1m:
                    for tk_p, fe_p in conn_pre.execute(
                        "SELECT ticker, forward_eps FROM monthly_stock_snapshots WHERE snapshot_month = ? AND forward_eps IS NOT NULL",
                        (pm,),
                    ).fetchall():
                        if fe_p and abs(float(fe_p)) > 0.01:
                            prior_feps_1m[str(tk_p).upper()] = float(fe_p)
                if mdiff in (3, 4) and not prior_feps_3m:
                    for tk_p, fe_p in conn_pre.execute(
                        "SELECT ticker, forward_eps FROM monthly_stock_snapshots WHERE snapshot_month = ? AND forward_eps IS NOT NULL",
                        (pm,),
                    ).fetchall():
                        if fe_p and abs(float(fe_p)) > 0.01:
                            prior_feps_3m[str(tk_p).upper()] = float(fe_p)
        except Exception:
            pass
        finally:
            conn_pre.close()

        for s in stocks:
            t = str(s.get("ticker") or "").strip().upper()
            if not t:
                continue
            price = _safe_num(s.get("current_price"))
            if price is None or price <= 0:
                continue
            m = s.get("metrics") or {}
            mp = profiles.get(t) or profiles.get(t.replace(".AX", "")) or {}

            is_out = 1 if mp.get("is_mispriced_outlier") else 0
            if is_out:
                outlier_count += 1

            is_iso = 1 if mp.get("is_iso_outlier") else 0
            roic_roe = extract_metric(s, "roic_or_roe")
            sec_str = str(s.get("sector") or "Unknown")
            feps_cur = _safe_num(m.get("forward_eps"))
            eps_r30 = _safe_num(m.get("eps_rev_30d_pct"))
            if eps_r30 is None and feps_cur is not None and t in prior_feps_1m:
                p_fe1 = prior_feps_1m[t]
                if abs(p_fe1) > 0.01:
                    eps_r30 = round(((feps_cur - p_fe1) / abs(p_fe1)) * 100.0, 2)
            eps_r90 = _safe_num(m.get("eps_rev_90d_pct"))
            if eps_r90 is None and feps_cur is not None and t in prior_feps_3m:
                p_fe3 = prior_feps_3m[t]
                if abs(p_fe3) > 0.01:
                    eps_r90 = round(((feps_cur - p_fe3) / abs(p_fe3)) * 100.0, 2)

            rows_to_insert.append(
                (
                    month_str,
                    t,
                    now_iso,
                    str(s.get("name") or t),
                    sec_str,
                    str(s.get("currency") or m.get("currency") or "USD"),
                    price,
                    _safe_num(s.get("market_cap") or m.get("market_cap")),
                    _safe_num(s.get("pe") or m.get("pe")),
                    _safe_num(s.get("pb") or m.get("pb")),
                    _safe_num(s.get("p_fcf") or m.get("p_fcf")),
                    _safe_num(m.get("ev_ebitda")),
                    _safe_num(m.get("fcf_yield")),
                    _safe_num(roic_roe),
                    _safe_num(m.get("debt_to_equity")),
                    _safe_num(s.get("net_cash_pct_mcap") or m.get("net_cash_pct_mcap")),
                    _safe_num(s.get("net_assets_vs_mcap_pct") or m.get("net_assets_vs_mcap_pct")),
                    _safe_num(s.get("pct_from_52w_low")),
                    _safe_num(s.get("pct_from_200w_ma") or m.get("pct_from_200w_ma")),
                    _safe_num(s.get("rsi") or m.get("rsi")),
                    _safe_num(s.get("annual_buyback_pct") or m.get("annual_buyback_pct")),
                    _safe_num(mp.get("vq_score")),
                    _safe_num(mp.get("val_z")),
                    _safe_num(mp.get("qual_z")),
                    _safe_num(mp.get("posture_z")),
                    _safe_num(mp.get("mispriced_outlier_score")),
                    is_out,
                    _safe_num(mp.get("anomaly_score")),
                    is_iso,
                    _safe_num(mp.get("val_residual_z")),
                    _safe_num(mp.get("peer_val_diff_z")),
                    mp.get("cluster_id"),
                    mp.get("cluster_label"),
                    _safe_num(mp.get("pca_x")),
                    _safe_num(mp.get("pca_y")),
                    None,  # ret_1m_pct
                    None,  # ret_3m_pct
                    None,  # ret_6m_pct
                    None,  # ret_12m_pct
                    0.0,   # ret_to_now_pct
                    _safe_num(m.get("forward_pe")),
                    _safe_num(m.get("short_pct_float")),
                    _safe_num(m.get("short_ratio")),
                    _safe_num(m.get("short_mom_change_pct")),
                    feps_cur,
                    _safe_num(m.get("eps_forward_vs_trailing_pct")),
                    int(m["analyst_count"]) if _safe_num(m.get("analyst_count")) is not None else None,
                    _safe_num(m.get("recommendation_mean")),
                    _safe_num(m.get("upside_to_low_target_pct")),
                    _safe_num(m.get("sbc_adjusted_fcf_yield")),
                    _safe_num(m.get("share_count_5y_change_pct")),
                    _safe_num(m.get("shares_yoy_change_pct")),
                    _safe_num(m.get("sloan_accrual_pct_mcap")),
                    _safe_num(m.get("capex_to_da_ratio")),
                    _safe_num(m.get("interest_coverage")),
                    int(m["piotroski_f_score"]) if _safe_num(m.get("piotroski_f_score")) is not None else None,
                    _safe_num(m.get("median_dollar_vol_20d")),
                    _safe_num(m.get("volatility_60d_ann_pct")),
                    _safe_num(m.get("max_drawdown_1y_pct")),
                    _safe_num(m.get("forward_eps_current_yr")),
                    _safe_num(m.get("forward_eps_next_yr")),
                    eps_r30,
                    eps_r90,
                    _safe_num(m.get("shares_3y_cagr_pct")),
                    _safe_num(m.get("net_debt_to_ebitda")),
                    _safe_num(m.get("current_ratio_yoy_delta")),
                    _safe_num(m.get("gross_margin_yoy_delta")),
                    _safe_num(m.get("inventory_to_sales_delta_pct")),
                    sector_capex_medians.get(sec_str),
                    int(s.get("cloner_holder_count") or m.get("cloner_holder_count") or 0),
                    int(s.get("cloner_new_position_flag") or m.get("cloner_new_position_flag") or 0),
                    _safe_num(s.get("cloner_max_weight_pct") or m.get("cloner_max_weight_pct")),
                    _safe_num(m.get("max_pos_usd_5pct_adv")),
                )
            )

        conn = _snap_conn(db_path)
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO monthly_stock_snapshots (
                        snapshot_month, ticker, captured_at, name, sector, currency,
                        price, market_cap, pe, pb, p_fcf, ev_ebitda, fcf_yield,
                        roic_or_roe, debt_to_equity, net_cash_pct_mcap, net_assets_vs_mcap_pct,
                        pct_from_52w_low, pct_from_200w_ma, rsi, annual_buyback_pct,
                        vq_score, val_z, qual_z, posture_z,
                        mispriced_outlier_score, is_mispriced_outlier,
                        anomaly_score, is_iso_outlier, val_residual_z, peer_val_diff_z,
                        cluster_id, cluster_label, pca_x, pca_y,
                        ret_1m_pct, ret_3m_pct, ret_6m_pct, ret_12m_pct, ret_to_now_pct,
                        forward_pe, short_pct_float, short_ratio, short_mom_change_pct,
                        forward_eps, eps_forward_vs_trailing_pct, analyst_count, recommendation_mean,
                        upside_to_low_target_pct, sbc_adjusted_fcf_yield, share_count_5y_change_pct,
                        shares_yoy_change_pct, sloan_accrual_pct_mcap, capex_to_da_ratio,
                        interest_coverage, piotroski_f_score, median_dollar_vol_20d,
                        volatility_60d_ann_pct, max_drawdown_1y_pct,
                        forward_eps_current_yr, forward_eps_next_yr, eps_rev_30d_pct, eps_rev_90d_pct,
                        shares_3y_cagr_pct, net_debt_to_ebitda, current_ratio_yoy_delta,
                        gross_margin_yoy_delta, inventory_to_sales_delta_pct, sector_capex_to_da_median,
                        cloner_holder_count, cloner_new_position_flag, cloner_max_weight_pct,
                        max_pos_usd_5pct_adv
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    rows_to_insert,
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO monthly_snapshot_runs
                    (snapshot_month, captured_at, total_stocks, outlier_count, notes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        month_str,
                        now_iso,
                        len(rows_to_insert),
                        outlier_count,
                        f"Point-in-time monthly snapshot ({len(rows_to_insert)} stocks, {outlier_count} strict outliers)",
                    ),
                )
        finally:
            conn.close()

        # Reconcile forward returns on any older snapshots using latest prices
        update_forward_returns_on_snapshots(db_path=db_path)

        elapsed = round(time.time() - t0, 2)
        logger.info(
            "Captured monthly stock snapshot for %s: %d stocks (%d outliers) in %.2fs",
            month_str,
            len(rows_to_insert),
            outlier_count,
            elapsed,
        )
        return {
            "status": "captured",
            "snapshot_month": month_str,
            "captured_at": now_iso,
            "total_stocks": len(rows_to_insert),
            "outlier_count": outlier_count,
            "elapsed_s": elapsed,
            "summary": get_snapshots_overview(db_path=db_path),
        }
    finally:
        _snapshot_lock.release()


def update_forward_returns_on_snapshots(db_path: str = "asx_announcements_cache.db") -> int:
    """
    Compute realized forward returns (`ret_1m_pct`, `ret_3m_pct`, `ret_6m_pct`,
    `ret_12m_pct`, `ret_to_now_pct`) for all historical monthly snapshots by comparing
    each snapshot's recorded `price` with subsequent monthly snapshots and current cached prices.
    """
    from scoring.unsupervised_ml import load_full_global_stock_universe

    conn = _snap_conn(db_path)
    try:
        months = [
            r[0]
            for r in conn.execute(
                "SELECT snapshot_month FROM monthly_snapshot_runs ORDER BY snapshot_month ASC"
            ).fetchall()
        ]
        if not months:
            return 0

        # Current live/cached prices map
        current_stocks = load_full_global_stock_universe(db_path=db_path)
        now_prices: dict[str, float] = {}
        for s in current_stocks:
            t = str(s.get("ticker") or "").upper()
            p = _safe_num(s.get("current_price"))
            if t and p and p > 0:
                now_prices[t] = p

        # Load price matrix by month
        month_prices: dict[str, dict[str, float]] = {}
        for m in months:
            cur = conn.execute(
                "SELECT ticker, price FROM monthly_stock_snapshots WHERE snapshot_month = ? AND price > 0",
                (m,),
            )
            month_prices[m] = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] and r[1] > 0}

        updates: list[tuple[float | None, float | None, float | None, float | None, float | None, str, str]] = []
        for idx, m_base in enumerate(months):
            base_map = month_prices.get(m_base) or {}
            if not base_map:
                continue

            # Find closest matching later months for +1m, +3m, +6m, +12m
            later_months = { _month_diff(m_base, m_lat): m_lat for m_lat in months[idx + 1:] }
            m_1 = later_months.get(1)
            m_3 = later_months.get(3) or later_months.get(2)
            m_6 = later_months.get(6) or later_months.get(5) or later_months.get(7)
            m_12 = later_months.get(12) or later_months.get(11) or later_months.get(13)

            for ticker, p0 in base_map.items():
                if p0 <= 0:
                    continue

                def _calc_ret(target_month: str | None) -> float | None:
                    if not target_month:
                        return None
                    p1 = (month_prices.get(target_month) or {}).get(ticker)
                    if not p1 or p1 <= 0:
                        return None
                    ret = ((p1 / p0) - 1.0) * 100.0
                    # Guard against unadjusted splits / data glitches (> +1000% or < -98%)
                    return round(ret, 2) if -98.0 <= ret <= 1000.0 else None

                r1 = _calc_ret(m_1)
                r3 = _calc_ret(m_3)
                r6 = _calc_ret(m_6)
                r12 = _calc_ret(m_12)

                p_now = now_prices.get(ticker)
                r_now = None
                if p_now and p_now > 0:
                    raw_now = ((p_now / p0) - 1.0) * 100.0
                    if -98.0 <= raw_now <= 1000.0:
                        r_now = round(raw_now, 2)

                updates.append((r1, r3, r6, r12, r_now, m_base, ticker))

        if updates:
            with conn:
                conn.executemany(
                    """
                    UPDATE monthly_stock_snapshots
                    SET ret_1m_pct = ?, ret_3m_pct = ?, ret_6m_pct = ?, ret_12m_pct = ?, ret_to_now_pct = ?
                    WHERE snapshot_month = ? AND ticker = ?
                    """,
                    updates,
                )
        return len(updates)
    finally:
        conn.close()


def get_snapshots_overview(
    *,
    predict_for_stocks: list[dict[str, Any]] | None = None,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Return a comprehensive summary of:
    1. All recorded monthly point-in-time snapshots (`runs`) and per-cohort performance
       comparing **Mispriced Quality Outliers** vs. **Market Baseline**.
    2. If historical snapshots with realized non-zero returns exist, fits a k-NN Historical
       Analog + Huber Regression return predictor to rank **Present-Day Stocks** whose
       outlier profile best matches past winning outliers (`predicted_winners`).
    """
    conn = _snap_conn(db_path)
    try:
        runs_rows = conn.execute(
            """
            SELECT snapshot_month, captured_at, total_stocks, outlier_count, notes
            FROM monthly_snapshot_runs
            ORDER BY snapshot_month DESC
            """
        ).fetchall()

        runs: list[dict[str, Any]] = []
        cohort_studies: list[dict[str, Any]] = []

        for s_month, cap_at, tot_s, out_c, notes in runs_rows:
            # Compute cohort return stats for this snapshot month
            cur = conn.execute(
                """
                SELECT is_mispriced_outlier, mispriced_outlier_score,
                       ret_1m_pct, ret_3m_pct, ret_6m_pct, ret_12m_pct, ret_to_now_pct
                FROM monthly_stock_snapshots
                WHERE snapshot_month = ?
                """,
                (s_month,),
            )
            all_rows = cur.fetchall()
            out_rets: list[float] = []
            mkt_rets: list[float] = []
            for is_out, sc, r1, r3, r6, r12, rnow in all_rows:
                # Pick best available realized return horizon (prefer 3m -> 1m -> to_now)
                r_use = r3 if r3 is not None else (r1 if r1 is not None else rnow)
                if r_use is None or abs(r_use) < 1e-6:
                    continue
                mkt_rets.append(float(r_use))
                if is_out or (sc is not None and float(sc) >= 70.0):
                    out_rets.append(float(r_use))

            has_realized = len(mkt_rets) >= 10
            out_mean = round(float(np.mean(out_rets)), 2) if out_rets else None
            out_med = round(float(np.median(out_rets)), 2) if out_rets else None
            mkt_mean = round(float(np.mean(mkt_rets)), 2) if mkt_rets else None
            mkt_med = round(float(np.median(mkt_rets)), 2) if mkt_rets else None
            alpha_med = round(out_med - mkt_med, 2) if (out_med is not None and mkt_med is not None) else None
            win_rate = (
                round(100.0 * sum(1 for r in out_rets if r > 0) / len(out_rets), 1)
                if out_rets
                else None
            )

            run_info = {
                "snapshot_month": s_month,
                "captured_at": cap_at,
                "total_stocks": int(tot_s or 0),
                "outlier_count": int(out_c or 0),
                "notes": notes,
                "has_realized_returns": has_realized,
                "outlier_mean_ret_pct": out_mean,
                "outlier_median_ret_pct": out_med,
                "market_mean_ret_pct": mkt_mean,
                "market_median_ret_pct": mkt_med,
                "outlier_excess_median_pct": alpha_med,
                "outlier_win_rate_pct": win_rate,
            }
            runs.append(run_info)
            if has_realized:
                cohort_studies.append(run_info)

        # Predict returns for current stocks using historical snapshots with realized returns
        predictions = _predict_from_historical_analogs(conn, predict_for_stocks=predict_for_stocks, db_path=db_path)

        return {
            "current_month": current_snapshot_month(),
            "current_month_captured": any(r["snapshot_month"] == current_snapshot_month() for r in runs),
            "total_snapshots": len(runs),
            "snapshots_with_realized_returns": len(cohort_studies),
            "runs": runs,
            "predictions": predictions,
        }
    finally:
        conn.close()


def _predict_from_historical_analogs(
    conn,
    *,
    predict_for_stocks: list[dict[str, Any]] | None = None,
    db_path: str = "asx_announcements_cache.db",
) -> dict[str, Any]:
    """
    Train a k-NN Historical Analog + Robust Regression model on all historical
    snapshot rows that have non-zero realized returns (`ret_3m_pct`, `ret_1m_pct`, or
    `ret_to_now_pct`), and score today's outlier candidates.

    Even when the first monthly snapshot was just taken today (0 months elapsed, so
    multi-month snapshots are still accumulating), we also support a **52w-Low / 200w-MA
    Rebound Empirical Calibration** fallback or live mark-to-market drift so the panel
    is immediately informative while monthly snapshots accumulate over time.
    """
    from scoring.unsupervised_ml import load_persisted_global_ml

    # 1. Fetch historical snapshot rows with realized returns != 0
    hist_rows = conn.execute(
        """
        SELECT snapshot_month, ticker, name, sector,
               val_z, qual_z, posture_z, val_residual_z, peer_val_diff_z, mispriced_outlier_score,
               COALESCE(ret_3m_pct, ret_1m_pct, ret_to_now_pct) AS realized_ret
        FROM monthly_stock_snapshots
        WHERE val_z IS NOT NULL AND qual_z IS NOT NULL
          AND COALESCE(ret_3m_pct, ret_1m_pct, ret_to_now_pct) IS NOT NULL
          AND ABS(COALESCE(ret_3m_pct, ret_1m_pct, ret_to_now_pct)) > 0.01
        """
    ).fetchall()

    ml_payload = load_persisted_global_ml(db_path=db_path) or {}
    profiles = ml_payload.get("profiles") or {}

    if not profiles:
        return {
            "mode": "awaiting_ml",
            "training_samples": 0,
            "candidates": [],
            "note": "Global ML profiles are still building.",
        }

    # Filter candidate stocks to score today (either passed list or global top outliers)
    if predict_for_stocks:
        cand_tickers = [
            str(s.get("ticker") or "").upper()
            for s in predict_for_stocks
            if s and not s.get("_pending_yf")
        ]
    else:
        cand_tickers = [t for t, p in profiles.items() if not t.isalpha() or "." in t or p.get("mispriced_outlier_score", 0) >= 60]

    # Deduplicate candidate profiles
    cand_profiles: list[dict[str, Any]] = []
    seen_t: set[str] = set()
    for t in cand_tickers:
        mp = profiles.get(t) or profiles.get(t + ".AX")
        if not mp:
            continue
        canon_t = str(mp.get("ticker") or t).upper()
        if canon_t in seen_t:
            continue
        if (mp.get("mispriced_outlier_score") or 0) < 58.0:
            continue
        seen_t.add(canon_t)
        cand_profiles.append(mp)

    cand_profiles.sort(key=lambda p: float(p.get("mispriced_outlier_score") or 0.0), reverse=True)
    cand_profiles = cand_profiles[:40]

    if len(hist_rows) >= 25:
        # Full Historical Snapshot Analog k-NN + Huber Regression Mode!
        X_train = np.array(
            [
                [
                    float(r[4] or 0.0),
                    float(r[5] or 0.0),
                    float(r[6] or 0.0),
                    float(r[7] or 0.0),
                    float(r[8] or 0.0),
                    float(r[9] or 50.0) / 25.0,
                ]
                for r in hist_rows
            ],
            dtype=np.float64,
        )
        y_train = np.array([float(r[10]) for r in hist_rows], dtype=np.float64)

        scaler = RobustScaler()
        X_train_s = np.clip(scaler.fit_transform(X_train), -4.0, 4.0)

        k_analog = min(10, len(hist_rows) - 1)
        nn = NearestNeighbors(n_neighbors=k_analog, metric="euclidean")
        nn.fit(X_train_s)

        try:
            reg = HuberRegressor(max_iter=200)
            reg.fit(X_train_s, y_train)
            has_reg = True
        except Exception:
            has_reg = False

        scored_candidates: list[dict[str, Any]] = []
        for mp in cand_profiles:
            x_vec = np.array(
                [
                    [
                        float(mp.get("val_z") or 0.0),
                        float(mp.get("qual_z") or 0.0),
                        float(mp.get("posture_z") or 0.0),
                        float(mp.get("val_residual_z") or 0.0),
                        float(mp.get("peer_val_diff_z") or 0.0),
                        float(mp.get("mispriced_outlier_score") or 50.0) / 25.0,
                    ]
                ],
                dtype=np.float64,
            )
            x_s = np.clip(scaler.transform(x_vec), -4.0, 4.0)
            dists, idxs = nn.kneighbors(x_s)
            d0 = dists[0]
            i0 = idxs[0]
            weights = np.exp(-0.5 * d0)
            weights /= np.sum(weights) + 1e-9
            knn_ret = float(np.sum(weights * y_train[i0]))
            reg_ret = float(reg.predict(x_s)[0]) if has_reg else knn_ret
            pred_ret = round(0.65 * knn_ret + 0.35 * reg_ret, 2)
            win_prob = round(100.0 * float(np.mean(y_train[i0] > 0)), 1)

            analogs = []
            for rank_i in range(min(3, len(i0))):
                hr = hist_rows[int(i0[rank_i])]
                analogs.append({
                    "snapshot_month": hr[0],
                    "ticker": hr[1],
                    "name": hr[2],
                    "realized_ret_pct": round(float(hr[10]), 1),
                })

            scored_candidates.append({
                "ticker": mp["ticker"],
                "name": mp.get("name") or mp["ticker"],
                "sector": mp.get("sector") or "Unknown",
                "cluster_label": mp.get("cluster_label") or "—",
                "mispriced_outlier_score": mp.get("mispriced_outlier_score"),
                "val_z": mp.get("val_z"),
                "qual_z": mp.get("qual_z"),
                "val_residual_z": mp.get("val_residual_z"),
                "peer_val_diff_z": mp.get("peer_val_diff_z"),
                "pred_return_pct": pred_ret,
                "analog_win_rate_pct": win_prob,
                "historical_analogs": analogs,
            })

        scored_candidates.sort(key=lambda x: x["pred_return_pct"], reverse=True)
        return {
            "mode": "historical_snapshots_knn",
            "training_samples": len(hist_rows),
            "candidates": scored_candidates[:20],
            "note": f"Trained on {len(hist_rows):,} historical monthly snapshot observations with realized forward returns.",
        }

    # Baseline / First-Month Calibration Mode (when <25 historical realized-return rows exist yet):
    # Surfaces today's strongest Mispriced Quality Outliers alongside their factor-implied
    # mean-reversion upside (`val_residual_z` + `peer_val_diff_z`) so the table is useful on Day 1
    # while monthly snapshots accumulate each month.
    baseline_candidates: list[dict[str, Any]] = []
    for mp in cand_profiles[:20]:
        res_z = float(mp.get("val_residual_z") or 0.0)
        pdiff_z = float(mp.get("peer_val_diff_z") or 0.0)
        qual_z = float(mp.get("qual_z") or 0.0)
        # Conservative factor-reversion estimate (1 sigma valuation discount ~ 12% re-rating potential)
        implied_rerating_pct = round(max(0.0, (0.50 * res_z + 0.35 * pdiff_z + 0.15 * qual_z) * 12.5), 1)
        baseline_candidates.append({
            "ticker": mp["ticker"],
            "name": mp.get("name") or mp["ticker"],
            "sector": mp.get("sector") or "Unknown",
            "cluster_label": mp.get("cluster_label") or "—",
            "mispriced_outlier_score": mp.get("mispriced_outlier_score"),
            "val_z": mp.get("val_z"),
            "qual_z": mp.get("qual_z"),
            "val_residual_z": res_z,
            "peer_val_diff_z": pdiff_z,
            "pred_return_pct": implied_rerating_pct,
            "analog_win_rate_pct": None,
            "historical_analogs": [],
        })

    return {
        "mode": "accumulating_snapshots",
        "training_samples": len(hist_rows),
        "candidates": baseline_candidates,
        "note": (
            "Baseline snapshot captured for this month! Forward 1m / 3m / 6m / 12m realized returns "
            "and k-NN historical analog return predictions will automatically populate as subsequent "
            "monthly snapshots are recorded (and as current prices move relative to this baseline)."
        ),
    }


def start_monthly_snapshot_scheduler(db_path: str = "asx_announcements_cache.db") -> None:
    """
    Start a background daemon thread that checks on startup (and once every 12 hours)
    whether a point-in-time snapshot exists for the current calendar month (`YYYY-MM`).
    If missing, automatically captures the monthly snapshot and updates forward returns
    on all prior snapshots.
    """
    global _monthly_scheduler_started
    if _monthly_scheduler_started:
        return
    _monthly_scheduler_started = True

    def _loop():
        # Wait briefly so Global Unsupervised ML finishes or loads first
        time.sleep(4.0)
        while True:
            try:
                month_str = current_snapshot_month()
                if not has_snapshot_for_month(month_str, db_path=db_path):
                    logger.info(
                        "Monthly Snapshot Scheduler: no snapshot found for %s — capturing point-in-time snapshot now.",
                        month_str,
                    )
                    capture_monthly_snapshot(snapshot_month=month_str, force=False, db_path=db_path)
                else:
                    # Keep `ret_to_now_pct` fresh against latest cached prices
                    update_forward_returns_on_snapshots(db_path=db_path)
            except Exception as e:
                logger.warning("Monthly Snapshot Scheduler check failed: %s", e)
            # Check every 12 hours
            time.sleep(12 * 3600)

    threading.Thread(target=_loop, name="monthly-snapshot-scheduler", daemon=True).start()
