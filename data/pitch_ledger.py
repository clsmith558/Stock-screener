"""
Point-in-Time Daily Stock Pitch Ledger & Forward-Outcome Tracker (`data/pitch_ledger.py`).

Records every stock pitched by the Daily Stock Pitcher automation along with a complete
point-in-time snapshot of valuation, short interest, consensus EPS revisions, forensic
balance-sheet survival (Piotroski F-score, SBC-adjusted FCF yield, share count delta,
Capex/D&A, Sloan accruals, interest coverage), microstructure liquidity, and ML scores.

Automatically backfills realized forward returns (`ret_1m_pct`, `ret_3m_pct`, `ret_6m_pct`,
`ret_12m_pct`, `ret_to_now_pct`) and post-pitch Max Favorable / Max Adverse Excursion
(`max_gain_since_pitch_pct`, `max_drawdown_since_pitch_pct`) to build a labeled dataset
for training a 20%+ CAGR deep-value recommender / investing bot.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from data.db import connect_db

logger = logging.getLogger(__name__)

DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "asx_announcements_cache.db")

_PITCH_LEDGER_DDL = (
    """
    CREATE TABLE IF NOT EXISTS pitch_ledger (
        pitch_date TEXT,
        ticker TEXT,
        company TEXT,
        sector TEXT,
        currency TEXT,
        logged_at TEXT,
        price_at_pitch REAL,
        market_cap REAL,
        pe REAL,
        forward_pe REAL,
        pb REAL,
        p_fcf REAL,
        ev_ebitda REAL,
        fcf_yield REAL,
        sbc_adjusted_fcf_yield REAL,
        pct_from_52w_low REAL,
        pct_from_200w_ma REAL,
        rsi REAL,
        short_pct_float REAL,
        short_ratio REAL,
        short_mom_change_pct REAL,
        forward_eps REAL,
        eps_forward_vs_trailing_pct REAL,
        analyst_count INTEGER,
        recommendation_mean REAL,
        upside_to_low_target_pct REAL,
        share_count_5y_change_pct REAL,
        shares_yoy_change_pct REAL,
        sloan_accrual_pct_mcap REAL,
        capex_to_da_ratio REAL,
        interest_coverage REAL,
        piotroski_f_score INTEGER,
        median_dollar_vol_20d REAL,
        volatility_60d_ann_pct REAL,
        max_drawdown_1y_pct REAL,
        quant_score REAL,
        qual_score REAL,
        composite_score REAL,
        vq_score REAL,
        mispriced_outlier_score REAL,
        brief_path TEXT,
        latest_price REAL,
        latest_eval_at TEXT,
        ret_to_now_pct REAL,
        ret_1m_pct REAL,
        ret_3m_pct REAL,
        ret_6m_pct REAL,
        ret_12m_pct REAL,
        max_gain_since_pitch_pct REAL,
        max_drawdown_since_pitch_pct REAL,
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
        cloner_holders_summary TEXT,
        max_pos_usd_5pct_adv REAL,
        suggested_position_weight_pct REAL,
        primary_thesis_type TEXT,
        ai_conviction_score REAL,
        PRIMARY KEY (pitch_date, ticker)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pitch_ledger_ticker ON pitch_ledger(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_pitch_ledger_date ON pitch_ledger(pitch_date)",
)

_EXTRA_PITCH_LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
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
    ("cloner_holders_summary", "TEXT"),
    ("max_pos_usd_5pct_adv", "REAL"),
    ("suggested_position_weight_pct", "REAL"),
    ("primary_thesis_type", "TEXT"),
    ("ai_conviction_score", "REAL"),
)

_migrated_pitch_dbs: set[str] = set()
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def _ledger_conn(db_path: str = DEFAULT_DB):
    conn = connect_db(db_path, schema_key="pitch_ledger", ddl=_PITCH_LEDGER_DDL)
    if db_path not in _migrated_pitch_dbs:
        try:
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(pitch_ledger)").fetchall()
            }
            for col_name, col_type in _EXTRA_PITCH_LEDGER_COLUMNS:
                if col_name not in existing:
                    conn.execute(f"ALTER TABLE pitch_ledger ADD COLUMN {col_name} {col_type}")
            conn.commit()
            _migrated_pitch_dbs.add(db_path)
        except Exception as ex:
            logger.debug("pitch_ledger column migration skipped: %s", ex)
    return conn


def _clean_num(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
        return f if math.isfinite(f) else None
    if isinstance(val, str):
        # Strip $, %, x, ~, commas
        s = re.sub(r"[,$%~xX\s+]", "", val.strip())
        if not s or s.lower() in ("na", "n/a", "none", "-"):
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def parse_pitch_frontmatter(md_path: str | Path) -> dict[str, Any]:
    p = Path(md_path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8", errors="replace")
    m = _FM_RE.match(text)
    fm: dict[str, Any] = {}
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                fm = loaded
        except Exception:
            pass
    # Fallback date/ticker from filename `YYYY-MM-DD - TICKER - Company.md`
    parts = [x.strip() for x in p.stem.split(" - ")]
    if not fm.get("date") and parts and re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
        fm["date"] = parts[0]
    if not fm.get("ticker") and len(parts) >= 2:
        fm["ticker"] = parts[1].upper()
    if not fm.get("company") and len(parts) >= 3:
        fm["company"] = parts[2]
    return fm


def record_pitch_from_markdown(
    md_path: str | Path,
    *,
    fetch_live_yf: bool = True,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    """
    Parse a daily brief Markdown file, enrich the pitched ticker with full point-in-time
    ephemeral, forensic, and ML metrics, persist to `pitch_ledger`, and update forward
    outcomes on all historical pitches.
    """
    p = Path(md_path).resolve()
    fm = parse_pitch_frontmatter(p)
    ticker = str(fm.get("ticker") or "").strip().upper()
    if not ticker:
        raise ValueError(f"Could not determine ticker from pitch file: {p}")

    pitch_date = str(fm.get("date") or datetime.now().strftime("%Y-%m-%d")).strip()[:10]
    company = str(fm.get("company") or fm.get("title") or ticker).strip()

    # 1. Load cached metrics from global universe + EDGAR + ML profiles
    from scoring.unsupervised_ml import load_full_global_stock_universe, load_persisted_global_ml

    universe = load_full_global_stock_universe(db_path=db_path)
    uni_map = {str(s.get("ticker") or "").upper(): s for s in universe}
    stock_row = uni_map.get(ticker) or uni_map.get(f"{ticker}.AX") or {}
    cached_m = dict(stock_row.get("metrics") or {})

    ml_payload = load_persisted_global_ml(db_path=db_path) or {}
    ml_profiles = ml_payload.get("profiles") or {}
    ml_prof = ml_profiles.get(ticker) or ml_profiles.get(ticker.replace(".AX", "")) or {}

    # 2. Check Obsidian opportunity memo for quant/qual/composite scores
    quant_score = _clean_num(fm.get("quant_score"))
    qual_score = _clean_num(fm.get("qual_score"))
    composite_score = _clean_num(fm.get("composite_score"))
    try:
        from data.vault_memo import memo_path

        mp = memo_path(ticker)
        if mp and mp.exists():
            memo_fm = parse_pitch_frontmatter(mp)
            if quant_score is None:
                quant_score = _clean_num(memo_fm.get("quant_score"))
            if qual_score is None:
                qual_score = _clean_num(memo_fm.get("qual_score"))
            if composite_score is None:
                composite_score = _clean_num(memo_fm.get("composite_score"))
    except Exception:
        pass

    # 3. Fetch live yfinance snapshot with statement forensics (`include_detail=True`)
    live_m: dict[str, Any] = {}
    live_price: float | None = None
    live_sector: str | None = None
    live_ccy: str | None = None
    live_pct_52w: float | None = None
    live_pct_200w: float | None = None
    if fetch_live_yf:
        try:
            from data.yf_helpers import fetch_yf_ticker_snapshot

            snap = fetch_yf_ticker_snapshot(ticker, ticker_for_fx=ticker, include_detail=True)
            if snap and (snap.get("price") or 0) > 0:
                live_price = _clean_num(snap.get("price"))
                live_sector = snap.get("sector")
                live_ccy = snap.get("currency")
                live_pct_52w = _clean_num(snap.get("pct_from_52w_low"))
                live_pct_200w = _clean_num(snap.get("pct_from_200w_ma"))
                live_m = dict(snap.get("metrics") or {})
        except Exception as ex:
            logger.warning("Live yf snapshot skipped during pitch ledger logging for %s: %s", ticker, ex)

    # Merge existing pitch_ledger row <- cached <- live so transient yf TLS errors never wipe prior forensics
    existing_row: dict[str, Any] = {}
    try:
        conn_ex = _ledger_conn(db_path)
        try:
            cur_ex = conn_ex.execute(
                "SELECT * FROM pitch_ledger WHERE pitch_date = ? AND ticker = ?",
                (pitch_date, ticker),
            )
            r_ex = cur_ex.fetchone()
            if r_ex:
                cols_ex = [d[0] for d in cur_ex.description]
                existing_row = {k: v for k, v in zip(cols_ex, r_ex) if v is not None}
        finally:
            conn_ex.close()
    except Exception:
        pass

    merged_m = {
        **existing_row,
        **cached_m,
        **{k: v for k, v in live_m.items() if v is not None},
    }

    # Also attach cached EDGAR metrics if eligible
    try:
        from data.edgar_metrics import _cache_get, is_us_edgar_eligible

        if is_us_edgar_eligible(ticker):
            ed = _cache_get(ticker, max_days=0, db_path=db_path) or {}
            if ed.get("share_count_5y_change_pct") is not None and merged_m.get("share_count_5y_change_pct") is None:
                merged_m["share_count_5y_change_pct"] = ed.get("share_count_5y_change_pct")
    except Exception:
        pass

    # Attach Superinvestor 13F & ASX substantial holder summary (Step 5)
    cloner_info: dict[str, Any] = {}
    try:
        from data.superinvestor_13f import get_superinvestor_summary_for_ticker

        cloner_info = get_superinvestor_summary_for_ticker(ticker, db_path=db_path) or {}
    except Exception:
        pass

    price_at_pitch = (
        _clean_num(fm.get("current_price"))
        or live_price
        or _clean_num(stock_row.get("current_price"))
    )
    sector = str(fm.get("sector") or live_sector or stock_row.get("sector") or "Unknown")
    currency = str(fm.get("currency") or live_ccy or stock_row.get("currency") or merged_m.get("currency") or "USD")
    now_iso = datetime.now().isoformat(timespec="seconds")

    # Compute sector median capex_to_da_ratio across cached universe (Step 4)
    sec_capex_vals = [
        float((s.get("metrics") or {}).get("capex_to_da_ratio"))
        for s in universe
        if str(s.get("sector") or "Unknown") == sector
        and _clean_num((s.get("metrics") or {}).get("capex_to_da_ratio")) is not None
        and 0.0 < float((s.get("metrics") or {}).get("capex_to_da_ratio")) < 15.0
    ]
    sec_capex_vals.sort()
    sector_capex_median = (
        round(sec_capex_vals[len(sec_capex_vals) // 2], 2) if sec_capex_vals else None
    )

    pb_val = _clean_num(fm.get("pb_ratio")) or _clean_num(merged_m.get("pb"))
    cda_val = _clean_num(merged_m.get("capex_to_da_ratio"))
    sbc_fcf_val = _clean_num(merged_m.get("sbc_adjusted_fcf_yield")) or _clean_num(merged_m.get("fcf_yield"))
    f_score_val = int(merged_m["piotroski_f_score"]) if _clean_num(merged_m.get("piotroski_f_score")) is not None else None
    cloner_cnt = int(cloner_info.get("cloner_holder_count") or merged_m.get("cloner_holder_count") or 0)
    cloner_new = int(cloner_info.get("cloner_new_position_flag") or merged_m.get("cloner_new_position_flag") or 0)
    cloner_max_w = _clean_num(cloner_info.get("cloner_max_weight_pct") or merged_m.get("cloner_max_weight_pct"))
    cloner_sum_str = str(cloner_info.get("cloner_holders_summary") or merged_m.get("cloner_holders_summary") or "")

    # Infer primary_thesis_type (Step 7: cigar_butt | capital_cycle | spinoff_reorg | compounder)
    raw_thesis = str(fm.get("primary_thesis_type") or fm.get("thesis_type") or "").strip().lower()
    if raw_thesis not in ("cigar_butt", "capital_cycle", "spinoff_reorg", "compounder"):
        body_lower = p.read_text(encoding="utf-8", errors="replace").lower() if p.exists() else ""
        if any(w in body_lower for w in ("spin-off", "spinoff", "demerger", "carve-out", "restructuring")):
            raw_thesis = "spinoff_reorg"
        elif (cda_val is not None and cda_val < 0.85) or "capital cycle" in body_lower or "supply starvation" in body_lower:
            raw_thesis = "capital_cycle"
        elif (pb_val is not None and pb_val < 0.85) or "cigar butt" in body_lower or "net-net" in body_lower or "liquidation" in body_lower:
            raw_thesis = "cigar_butt"
        else:
            raw_thesis = "compounder"

    # Compute AI Conviction Score (0–100) & Volatility-Adjusted Position Weight % (Steps 6 & 7)
    vq_val = _clean_num(ml_prof.get("vq_score"))
    base_conv = composite_score or vq_val or quant_score or 65.0
    conv_bonus = 0.0
    if f_score_val is not None:
        conv_bonus += (f_score_val - 5.0) * 2.5
    if sbc_fcf_val is not None:
        conv_bonus += min(10.0, max(-8.0, (sbc_fcf_val - 6.0) * 0.6))
    if cloner_cnt > 0:
        conv_bonus += min(8.0, cloner_cnt * 2.5 + (2.0 if cloner_new else 0.0))
    ai_conviction = _clean_num(fm.get("ai_conviction_score")) or round(max(25.0, min(98.0, float(base_conv) + conv_bonus)), 1)

    vol_60d = _clean_num(merged_m.get("volatility_60d_ann_pct")) or 35.0
    # Volatility-parity / Half-Kelly inspired position weight: target ~1.2% annualized portfolio vol contribution, scaled by conviction
    vol_target_w = (32.0 / max(15.0, min(90.0, vol_60d))) * (ai_conviction / 75.0) * 3.5
    suggested_pos_w = round(max(1.0, min(8.0, vol_target_w)), 2)

    record = {
        "pitch_date": pitch_date,
        "ticker": ticker,
        "company": company,
        "sector": sector,
        "currency": currency,
        "logged_at": now_iso,
        "price_at_pitch": price_at_pitch,
        "market_cap": _clean_num(merged_m.get("market_cap")) or _clean_num(stock_row.get("market_cap")),
        "pe": _clean_num(fm.get("pe_ratio")) or _clean_num(merged_m.get("pe")),
        "forward_pe": _clean_num(fm.get("forward_pe")) or _clean_num(merged_m.get("forward_pe")),
        "pb": pb_val,
        "p_fcf": _clean_num(fm.get("p_fcf")) or _clean_num(merged_m.get("p_fcf")),
        "ev_ebitda": _clean_num(fm.get("ev_ebitda")) or _clean_num(merged_m.get("ev_ebitda")),
        "fcf_yield": _clean_num(merged_m.get("fcf_yield")),
        "sbc_adjusted_fcf_yield": _clean_num(merged_m.get("sbc_adjusted_fcf_yield")),
        "pct_from_52w_low": _clean_num(fm.get("pct_from_52w_low")) or live_pct_52w or _clean_num(stock_row.get("pct_from_52w_low")),
        "pct_from_200w_ma": live_pct_200w or _clean_num(merged_m.get("pct_from_200w_ma")),
        "rsi": _clean_num(merged_m.get("rsi")),
        "short_pct_float": _clean_num(merged_m.get("short_pct_float")),
        "short_ratio": _clean_num(merged_m.get("short_ratio")),
        "short_mom_change_pct": _clean_num(merged_m.get("short_mom_change_pct")),
        "forward_eps": _clean_num(merged_m.get("forward_eps")),
        "eps_forward_vs_trailing_pct": _clean_num(merged_m.get("eps_forward_vs_trailing_pct")),
        "analyst_count": int(merged_m["analyst_count"]) if _clean_num(merged_m.get("analyst_count")) is not None else None,
        "recommendation_mean": _clean_num(merged_m.get("recommendation_mean")),
        "upside_to_low_target_pct": _clean_num(merged_m.get("upside_to_low_target_pct")),
        "share_count_5y_change_pct": _clean_num(merged_m.get("share_count_5y_change_pct")),
        "shares_yoy_change_pct": _clean_num(merged_m.get("shares_yoy_change_pct")),
        "sloan_accrual_pct_mcap": _clean_num(merged_m.get("sloan_accrual_pct_mcap")),
        "capex_to_da_ratio": cda_val,
        "interest_coverage": _clean_num(merged_m.get("interest_coverage")),
        "piotroski_f_score": f_score_val,
        "median_dollar_vol_20d": _clean_num(merged_m.get("median_dollar_vol_20d")),
        "volatility_60d_ann_pct": _clean_num(merged_m.get("volatility_60d_ann_pct")),
        "max_drawdown_1y_pct": _clean_num(merged_m.get("max_drawdown_1y_pct")),
        "quant_score": quant_score,
        "qual_score": qual_score,
        "composite_score": composite_score,
        "vq_score": vq_val,
        "mispriced_outlier_score": _clean_num(ml_prof.get("mispriced_outlier_score")),
        "brief_path": str(p),
        "latest_price": live_price or price_at_pitch,
        "latest_eval_at": now_iso,
        "ret_to_now_pct": 0.0,
        "ret_1m_pct": None,
        "ret_3m_pct": None,
        "ret_6m_pct": None,
        "ret_12m_pct": None,
        "max_gain_since_pitch_pct": 0.0,
        "max_drawdown_since_pitch_pct": 0.0,
        "forward_eps_current_yr": _clean_num(merged_m.get("forward_eps_current_yr")),
        "forward_eps_next_yr": _clean_num(merged_m.get("forward_eps_next_yr")),
        "eps_rev_30d_pct": _clean_num(merged_m.get("eps_rev_30d_pct")),
        "eps_rev_90d_pct": _clean_num(merged_m.get("eps_rev_90d_pct")),
        "shares_3y_cagr_pct": _clean_num(merged_m.get("shares_3y_cagr_pct")),
        "net_debt_to_ebitda": _clean_num(merged_m.get("net_debt_to_ebitda")),
        "current_ratio_yoy_delta": _clean_num(merged_m.get("current_ratio_yoy_delta")),
        "gross_margin_yoy_delta": _clean_num(merged_m.get("gross_margin_yoy_delta")),
        "inventory_to_sales_delta_pct": _clean_num(merged_m.get("inventory_to_sales_delta_pct")),
        "sector_capex_to_da_median": sector_capex_median,
        "cloner_holder_count": cloner_cnt,
        "cloner_new_position_flag": cloner_new,
        "cloner_max_weight_pct": cloner_max_w,
        "cloner_holders_summary": cloner_sum_str,
        "max_pos_usd_5pct_adv": _clean_num(merged_m.get("max_pos_usd_5pct_adv")),
        "suggested_position_weight_pct": suggested_pos_w,
        "primary_thesis_type": raw_thesis,
        "ai_conviction_score": ai_conviction,
    }

    cols = list(record.keys())
    placeholders = ",".join("?" for _ in cols)
    col_Clause = ",".join(cols)

    conn = _ledger_conn(db_path)
    try:
        with conn:
            conn.execute(
                f"INSERT OR REPLACE INTO pitch_ledger ({col_Clause}) VALUES ({placeholders})",
                tuple(record[c] for c in cols),
            )
    finally:
        conn.close()

    # Backfill outcomes on all prior pitches in the ledger
    update_pitch_ledger_outcomes(db_path=db_path)
    return record


def update_pitch_ledger_outcomes(db_path: str = DEFAULT_DB) -> int:
    """
    Update `latest_price`, `ret_to_now_pct`, and milestone returns (`ret_1m_pct`,
    `ret_3m_pct`, `ret_6m_pct`, `ret_12m_pct`) across all rows in `pitch_ledger`.
    """
    from scoring.unsupervised_ml import load_full_global_stock_universe

    conn = _ledger_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT pitch_date, ticker, price_at_pitch, ret_1m_pct, ret_3m_pct, ret_6m_pct, ret_12m_pct,
                   max_gain_since_pitch_pct, max_drawdown_since_pitch_pct
            FROM pitch_ledger
            WHERE price_at_pitch IS NOT NULL AND price_at_pitch > 0
            """
        ).fetchall()
        if not rows:
            return 0

        universe = load_full_global_stock_universe(db_path=db_path)
        price_map = {
            str(s.get("ticker") or "").upper(): float(s.get("current_price") or 0.0)
            for s in universe
            if (s.get("current_price") or 0) > 0
        }

        now = datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        updates: list[tuple[Any, ...]] = []

        for (
            p_date,
            ticker,
            p0,
            r1m,
            r3m,
            r6m,
            r12m,
            max_gain,
            max_dd,
        ) in rows:
            sym = str(ticker or "").upper()
            cur_p = price_map.get(sym) or price_map.get(f"{sym}.AX")
            if not cur_p or cur_p <= 0 or not p0 or p0 <= 0:
                continue
            ret_now = round(((cur_p - p0) / p0) * 100.0, 2)
            new_max_gain = max(float(max_gain or 0.0), ret_now)
            new_max_dd = min(float(max_dd or 0.0), ret_now)

            days_elapsed = 0
            try:
                dt0 = datetime.strptime(str(p_date)[:10], "%Y-%m-%d")
                days_elapsed = (now - dt0).days
            except Exception:
                pass

            if days_elapsed >= 28 and r1m is None:
                r1m = ret_now
            if days_elapsed >= 88 and r3m is None:
                r3m = ret_now
            if days_elapsed >= 178 and r6m is None:
                r6m = ret_now
            if days_elapsed >= 360 and r12m is None:
                r12m = ret_now

            updates.append(
                (
                    cur_p,
                    now_iso,
                    ret_now,
                    r1m,
                    r3m,
                    r6m,
                    r12m,
                    new_max_gain,
                    new_max_dd,
                    p_date,
                    ticker,
                )
            )

        if updates:
            with conn:
                conn.executemany(
                    """
                    UPDATE pitch_ledger
                    SET latest_price = ?,
                        latest_eval_at = ?,
                        ret_to_now_pct = ?,
                        ret_1m_pct = ?,
                        ret_3m_pct = ?,
                        ret_6m_pct = ?,
                        ret_12m_pct = ?,
                        max_gain_since_pitch_pct = ?,
                        max_drawdown_since_pitch_pct = ?
                    WHERE pitch_date = ? AND ticker = ?
                    """,
                    updates,
                )
        return len(updates)
    finally:
        conn.close()


DEFAULT_PITCHES_DIR = Path(r"C:\Obsidian\csmith_test\wiki\investing\pitches")


def list_pitch_ledger(db_path: str = DEFAULT_DB) -> list[dict[str, Any]]:
    """Return all rows from `pitch_ledger` ordered newest-first."""
    conn = _ledger_conn(db_path)
    try:
        conn.row_factory = None
        cur = conn.execute("SELECT * FROM pitch_ledger ORDER BY pitch_date DESC, ticker ASC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def sync_obsidian_pitches(
    pitches_dir: Path | str = DEFAULT_PITCHES_DIR,
    *,
    fetch_live_yf: bool = True,
    max_live_fetch: int = 5,
    db_path: str = DEFAULT_DB,
) -> int:
    """Ensure any `.md` pitch files in the Obsidian pitches folder are logged in `pitch_ledger`."""
    pdir = Path(pitches_dir)
    if not pdir.is_dir():
        return 0
    existing = {
        (str(r.get("pitch_date") or "")[:10], str(r.get("ticker") or "").upper())
        for r in list_pitch_ledger(db_path=db_path)
    }
    added = 0
    for md in sorted(pdir.glob("*.md"), reverse=True):
        if md.name.startswith("_"):
            continue
        fm = parse_pitch_frontmatter(md)
        sym = str(fm.get("ticker") or "").strip().upper()
        pdate = str(fm.get("date") or "").strip()[:10]
        if sym and pdate and (pdate, sym) not in existing:
            try:
                use_live = fetch_live_yf and added < max_live_fetch
                record_pitch_from_markdown(md, fetch_live_yf=use_live, db_path=db_path)
                existing.add((pdate, sym))
                added += 1
            except Exception as ex:
                logger.debug("Skipping unparsed pitch file %s: %s", md.name, ex)
    return added


def backfill_pitch_forensics(
    *,
    force: bool = False,
    db_path: str = DEFAULT_DB,
) -> int:
    """
    Populate or refresh live yfinance forensic metrics (`sbc_adjusted_fcf_yield`,
    `short_pct_float`, `piotroski_f_score`, `capex_to_da_ratio`, `interest_coverage`,
    `shares_yoy_change_pct`, `latest_price`, `ret_to_now_pct`) on `pitch_ledger` rows.
    """
    rows = list_pitch_ledger(db_path=db_path)
    updated = 0
    for r in rows:
        needs = force or (
            r.get("sbc_adjusted_fcf_yield") is None and r.get("piotroski_f_score") is None
        )
        if not needs:
            continue
        bp = r.get("brief_path")
        if bp and Path(bp).exists():
            try:
                record_pitch_from_markdown(bp, fetch_live_yf=True, db_path=db_path)
                updated += 1
            except Exception as ex:
                logger.warning("backfill_pitch_forensics failed for %s: %s", r.get("ticker"), ex)
    return updated


_PITCH_METRIC_KEYS = (
    "forward_pe",
    "sbc_adjusted_fcf_yield",
    "short_pct_float",
    "short_ratio",
    "short_mom_change_pct",
    "forward_eps",
    "eps_forward_vs_trailing_pct",
    "analyst_count",
    "recommendation_mean",
    "upside_to_low_target_pct",
    "share_count_5y_change_pct",
    "shares_yoy_change_pct",
    "sloan_accrual_pct_mcap",
    "capex_to_da_ratio",
    "interest_coverage",
    "piotroski_f_score",
    "median_dollar_vol_20d",
    "volatility_60d_ann_pct",
    "max_drawdown_1y_pct",
    "forward_eps_current_yr",
    "forward_eps_next_yr",
    "eps_rev_30d_pct",
    "eps_rev_90d_pct",
    "shares_3y_cagr_pct",
    "net_debt_to_ebitda",
    "current_ratio_yoy_delta",
    "gross_margin_yoy_delta",
    "inventory_to_sales_delta_pct",
    "sector_capex_to_da_median",
    "cloner_holder_count",
    "cloner_new_position_flag",
    "cloner_max_weight_pct",
    "cloner_holders_summary",
    "max_pos_usd_5pct_adv",
    "suggested_position_weight_pct",
    "primary_thesis_type",
    "ai_conviction_score",
)


def fetch_pitched_stocks(db_path: str = DEFAULT_DB) -> list[dict[str, Any]]:
    """
    Build enriched stock rows for the Screener's `Pitches` tab.
    Combines local YF caches (`business_list_yf_cache` / `asx_yf_cache`) with
    point-in-time pitch & forensic metrics from `pitch_ledger`.
    """
    try:
        sync_obsidian_pitches(db_path=db_path)
    except Exception as ex:
        logger.debug("sync_obsidian_pitches skipped: %s", ex)

    entries = list_pitch_ledger(db_path=db_path)
    if not entries:
        return []

    from data.watchlist import _infer_market
    from data.yf_helpers import sanitize_stocks

    symbols = list({str(e["ticker"]).strip().upper() for e in entries if e.get("ticker")})
    cached: dict[str, Any] = {}
    try:
        from data.business_lists import load_yf_cached

        cached = load_yf_cached(symbols, max_days=3650) or {}
    except Exception as ex:
        logger.debug("fetch_pitched_stocks load_yf_cached: %s", ex)

    out: list[dict[str, Any]] = []
    for e in entries:
        t = str(e.get("ticker") or "").strip().upper()
        if not t:
            continue
        mkt = _infer_market(t)
        ydat = cached.get(t)
        row: dict[str, Any] | None = None
        if ydat:
            try:
                from data.business_lists import _stock_row

                row = _stock_row(
                    t,
                    ydat,
                    {"list_id": "pitches", "display_name": "Pitches", "_market": mkt},
                )
                row["_market"] = mkt
                row["_pending_yf"] = not bool(ydat.get("price"))
            except Exception as ex:
                logger.debug("fetch_pitched_stocks _stock_row(%s): %s", t, ex)

        if row is None:
            row = {
                "ticker": t,
                "name": e.get("company") or t,
                "sector": e.get("sector") or "Unknown",
                "currency": e.get("currency") or "USD",
                "current_price": e.get("latest_price") or e.get("price_at_pitch") or 0,
                "market_cap": e.get("market_cap") or 0,
                "pct_from_52w_low": e.get("pct_from_52w_low") or 0,
                "pct_from_200w_ma": e.get("pct_from_200w_ma") or 0,
                "low_52w": 0,
                "high_52w": 0,
                "pe": e.get("pe") or 0,
                "pb": e.get("pb") or 0,
                "p_fcf": e.get("p_fcf") or 0,
                "rsi": e.get("rsi"),
                "insider_buys_2026": 0,
                "buyback_announced": False,
                "metrics": {},
                "signals": [],
                "_market": mkt,
                "_pending_yf": not bool(e.get("latest_price") or e.get("price_at_pitch")),
            }

        metrics = row.setdefault("metrics", {})
        if not row.get("current_price") and (e.get("latest_price") or e.get("price_at_pitch")):
            row["current_price"] = e.get("latest_price") or e.get("price_at_pitch")
        if not row.get("market_cap") and e.get("market_cap"):
            row["market_cap"] = e.get("market_cap")
            metrics["market_cap"] = e.get("market_cap")
        if (not row.get("sector") or row.get("sector") == "Unknown") and e.get("sector"):
            row["sector"] = e.get("sector")
        if not row.get("currency") and e.get("currency"):
            row["currency"] = e.get("currency")
            metrics["currency"] = e.get("currency")

        for k in ("pe", "pb", "p_fcf", "ev_ebitda", "fcf_yield", "pct_from_200w_ma", "rsi"):
            if e.get(k) is not None and (not metrics.get(k)):
                metrics[k] = e.get(k)
            if k in row and not row.get(k) and e.get(k) is not None:
                row[k] = e.get(k)

        if not row.get("pct_from_52w_low") and e.get("pct_from_52w_low") is not None:
            row["pct_from_52w_low"] = e.get("pct_from_52w_low")

        # Attach pitch metadata & return outcomes
        row["pitch_date"] = e.get("pitch_date")
        row["price_at_pitch"] = e.get("price_at_pitch")
        row["ret_to_now_pct"] = e.get("ret_to_now_pct")
        row["ret_1m_pct"] = e.get("ret_1m_pct")
        row["ret_3m_pct"] = e.get("ret_3m_pct")
        row["ret_6m_pct"] = e.get("ret_6m_pct")
        row["ret_12m_pct"] = e.get("ret_12m_pct")
        row["max_gain_since_pitch_pct"] = e.get("max_gain_since_pitch_pct")
        row["max_drawdown_since_pitch_pct"] = e.get("max_drawdown_since_pitch_pct")
        row["brief_path"] = e.get("brief_path")
        row["_pitched"] = True

        # Attach ephemeral + forensic metrics to both row and row["metrics"]
        for k in _PITCH_METRIC_KEYS:
            val = e.get(k)
            if val is not None:
                row[k] = val
                metrics[k] = val

        # Recompute live ret_to_now_pct if cached price is newer than ledger
        cur_p = row.get("current_price") or 0
        p0 = e.get("price_at_pitch") or 0
        if cur_p > 0 and p0 > 0:
            row["ret_to_now_pct"] = round(((cur_p - p0) / p0) * 100.0, 2)

        out.append(row)

    return sanitize_stocks(out)
