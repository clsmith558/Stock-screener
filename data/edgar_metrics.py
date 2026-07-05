"""
SEC EDGAR metrics via edgartools — annual ROIC and diluted share count trends.
Generalized from test_edgar.py; cached in SQLite to limit SEC requests.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from config.identity import sec_edgar_identity

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE_DB = "asx_announcements_cache.db"
EDGAR_CACHE_MAX_AGE_DAYS = 30
EDGAR_FETCH_SLEEP_SECONDS = 0.25

# Yahoo symbols with these exchange suffixes are not US SEC filers
_NON_US_YF_SUFFIXES = (
    ".AX", ".T", ".HK", ".L", ".TO", ".V", ".NS", ".BO", ".SW", ".PA",
    ".DE", ".MI", ".KS", ".KQ", ".SI", ".NZ", ".SA", ".MX", ".TW",
)

_identity_set = False


def is_us_edgar_eligible(ticker: str) -> bool:
    """True if ticker is likely a US-listed company with SEC EDGAR filings."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return False
    for sfx in _NON_US_YF_SUFFIXES:
        if sym.endswith(sfx):
            return False
    if "." in sym and "-" not in sym:
        # e.g. BRK.B before normalization — rare in our lists
        tail = sym.rsplit(".", 1)[-1]
        if tail.isalpha() and len(tail) <= 3:
            return False
    return True


def _ensure_identity():
    global _identity_set
    if _identity_set:
        return
    try:
        from edgar import set_identity
        identity = sec_edgar_identity()
        if not identity:
            logger.warning(
                "SEC_EDGAR_IDENTITY (or SCREENER_CONTACT_EMAIL) not set — "
                "EDGAR requests may fail; see .env.example"
            )
            return
        set_identity(identity)
        _identity_set = True
    except Exception as e:
        logger.warning("SEC EDGAR identity setup failed: %s", e)


def _edgar_conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_edgar_cache (
            ticker TEXT PRIMARY KEY,
            last_updated TEXT,
            metrics_json TEXT
        )
    """)
    conn.commit()
    return conn


def _cache_get(ticker: str, max_days: int = EDGAR_CACHE_MAX_AGE_DAYS, db_path: str = DEFAULT_UNIVERSE_DB) -> dict | None:
    try:
        conn = _edgar_conn(db_path)
        row = conn.execute(
            "SELECT last_updated, metrics_json FROM us_edgar_cache WHERE ticker=?",
            (ticker.upper(),),
        ).fetchone()
        conn.close()
        if not row:
            return None
        last_updated, raw = row
        if max_days > 0 and last_updated:
            try:
                lu = datetime.fromisoformat(last_updated)
                if datetime.now() - lu > timedelta(days=max_days):
                    return None
            except Exception:
                pass
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("EDGAR cache read failed for %s: %s", ticker, e)
        return None


def _cache_put(ticker: str, payload: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    try:
        conn = _edgar_conn(db_path)
        conn.execute(
            """INSERT OR REPLACE INTO us_edgar_cache (ticker, last_updated, metrics_json)
               VALUES (?,?,?)""",
            (ticker.upper(), datetime.now().isoformat(), json.dumps(payload)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("EDGAR cache write failed for %s: %s", ticker, e)


def _get_historical_metric(facts, concept_name: str) -> pd.Series:
    """Extract clean multi-year annual data for a GAAP concept."""
    try:
        df = facts.query().by_concept(concept_name, exact=True).to_dataframe()
        if df.empty and not concept_name.startswith("us-gaap:"):
            df = facts.query().by_concept(f"us-gaap:{concept_name}", exact=True).to_dataframe()
        if df.empty:
            clean_name = concept_name.replace("us-gaap:", "")
            df = facts.query().by_concept(clean_name).to_dataframe()
        if df.empty:
            return pd.Series(dtype=float)

        form_col = "form_type" if "form_type" in df.columns else "form"
        fy_col = "fiscal_year" if "fiscal_year" in df.columns else "fy"
        val_col = "value" if "value" in df.columns else ("numeric_value" if "numeric_value" in df.columns else "val")
        period_col = "fiscal_period" if "fiscal_period" in df.columns else "fp"

        df_annual = df[df[form_col].isin(["10-K", "10-K/A"])].copy()
        if df_annual.empty:
            df_annual = df.copy()

        if period_col in df_annual.columns:
            df_fy = df_annual[df_annual[period_col] == "FY"]
            if not df_fy.empty:
                df_annual = df_fy

        if df_annual.empty:
            return pd.Series(dtype=float)

        sort_col = "filing_date" if "filing_date" in df_annual.columns else fy_col
        df_annual = df_annual.sort_values(by=[fy_col, sort_col])
        df_annual = df_annual.drop_duplicates(subset=[fy_col], keep="last")
        return df_annual.set_index(fy_col)[val_col]
    except Exception:
        return pd.Series(dtype=float)


def _get_historical_metric_with_fallbacks(facts, concept_names) -> pd.Series:
    if isinstance(concept_names, str):
        concept_names = [concept_names]
    for name in concept_names:
        series = _get_historical_metric(facts, name)
        if not series.empty:
            return series
    return pd.Series(dtype=float)


def _sum_debt_series(facts) -> pd.Series:
    parts = [
        _get_historical_metric(facts, "ShortTermBorrowings"),
        _get_historical_metric(facts, "OtherShortTermBorrowings"),
        _get_historical_metric(facts, "CommercialPaper"),
        _get_historical_metric(facts, "LongTermDebtCurrent"),
        _get_historical_metric(facts, "LongTermDebtNoncurrent"),
    ]
    total = pd.Series(dtype=float)
    for s in parts:
        if not s.empty:
            total = total.add(s, fill_value=0)
    if total.empty or total.sum() == 0:
        total = _get_historical_metric(facts, "LongTermDebt")
    return total


def _is_likely_split_ratio(ratio: float) -> bool:
    if ratio <= 0:
        return False
    for r in (2, 3, 4, 5, 10, 20):
        if abs(ratio - r) / r < 0.15:
            return True
        inv = 1.0 / r
        if abs(ratio - inv) / inv < 0.15:
            return True
    return False


def _detect_split_years(years: list[int], shares: list[float]) -> set[int]:
    """Years where YoY share change looks like a stock split (post-split year)."""
    split_years: set[int] = set()
    for i in range(1, len(years)):
        prev, curr = shares[i - 1], shares[i]
        if not prev or not curr or prev <= 0:
            continue
        ratio = curr / prev
        pct = (ratio - 1) * 100
        if abs(pct) >= 50 and (_is_likely_split_ratio(ratio) or abs(pct) >= 75):
            split_years.add(years[i])
    return split_years


def _baseline_for_share_change(years: list[int], shares: list[float], lookback_years: int = 5) -> tuple[int | None, float | None]:
    """
    Pick baseline year/shares for multi-year share count change.
    Uses earliest available if < lookback years; advances past stock splits.
    """
    if not years or not shares:
        return None, None

    latest_idx = len(years) - 1
    latest_year = years[latest_idx]
    target_year = latest_year - lookback_years

    baseline_idx = 0
    for i, y in enumerate(years):
        if y >= target_year:
            baseline_idx = i
            break

    split_years = _detect_split_years(years, shares)
    for i in range(baseline_idx + 1, latest_idx + 1):
        if years[i] in split_years:
            baseline_idx = i

    return years[baseline_idx], shares[baseline_idx]


def _build_share_history(shares: pd.Series) -> list[dict[str, Any]]:
    if shares.empty:
        return []

    df = pd.DataFrame({"shares": shares}).sort_index()
    df["yoy_abs"] = df["shares"].diff()
    df["yoy_pct"] = df["shares"].pct_change() * 100

    history = []
    for year, row in df.iterrows():
        try:
            yr = int(year)
        except (TypeError, ValueError):
            continue
        entry: dict[str, Any] = {
            "year": yr,
            "shares": int(row["shares"]) if row["shares"] == row["shares"] else None,
        }
        if row["yoy_abs"] == row["yoy_abs"]:
            entry["yoy_abs"] = int(row["yoy_abs"])
        if row["yoy_pct"] == row["yoy_pct"]:
            entry["yoy_pct"] = round(float(row["yoy_pct"]), 2)
        history.append(entry)
    return history


def _build_earnings_history(net_income: pd.Series) -> list[dict[str, Any]]:
    if net_income.empty:
        return []

    df = pd.DataFrame({"earnings": net_income}).sort_index()
    df["yoy_abs"] = df["earnings"].diff()
    df["yoy_pct"] = df["earnings"].pct_change() * 100

    history = []
    for year, row in df.iterrows():
        try:
            yr = int(year)
        except (TypeError, ValueError):
            continue
        val = row["earnings"]
        if val != val:
            continue
        entry: dict[str, Any] = {
            "year": yr,
            "earnings": int(val) if abs(val) > 1e6 else round(float(val), 2),
        }
        if row["yoy_abs"] == row["yoy_abs"]:
            entry["yoy_abs"] = int(row["yoy_abs"]) if abs(row["yoy_abs"]) > 1e6 else round(float(row["yoy_abs"]), 2)
        if row["yoy_pct"] == row["yoy_pct"]:
            entry["yoy_pct"] = round(float(row["yoy_pct"]), 2)
        history.append(entry)
    return history


def _earnings_growth_metrics(history: list[dict[str, Any]], lookback_years: int = 5) -> tuple[float, float]:
    """Return (latest YoY %, multi-year growth % vs lookback)."""
    if not history:
        return 0.0, 0.0
    valid = [h for h in history if h.get("earnings") is not None]
    if not valid:
        return 0.0, 0.0

    latest = valid[-1]
    yoy_pct = float(latest.get("yoy_pct") or 0)

    years = [h["year"] for h in valid]
    vals = [h["earnings"] for h in valid]
    latest_year = years[-1]
    latest_val = vals[-1]
    target_year = latest_year - lookback_years
    baseline_idx = 0
    for i, y in enumerate(years):
        if y >= target_year:
            baseline_idx = i
            break
    baseline_val = vals[baseline_idx]
    growth_pct = 0.0
    if baseline_val and baseline_val != 0:
        growth_pct = round(((latest_val - baseline_val) / abs(baseline_val)) * 100, 2)
    return yoy_pct, growth_pct


def _build_roic_history(roic_df: pd.DataFrame) -> list[dict[str, Any]]:
    if roic_df.empty or "ROIC (%)" not in roic_df.columns:
        return []

    history = []
    for year, row in roic_df.sort_index().iterrows():
        try:
            yr = int(year)
        except (TypeError, ValueError):
            continue
        roic_val = row.get("ROIC (%)")
        if roic_val != roic_val:
            continue
        entry: dict[str, Any] = {
            "year": yr,
            "roic": round(float(roic_val), 2),
        }
        for col, key in (("NOPAT", "nopat"), ("Invested_Capital", "invested_capital"), ("EBIT", "ebit")):
            v = row.get(col)
            if v == v:
                entry[key] = int(v) if abs(v) > 1e6 else round(float(v), 2)
        history.append(entry)
    return history


def _compute_roic_dataframe(facts) -> pd.DataFrame:
    ebit = _get_historical_metric_with_fallbacks(facts, ["OperatingIncomeLoss", "OperatingProfitLoss"])
    ebt = _get_historical_metric_with_fallbacks(facts, [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeBeforeIncomeTaxExpenseBenefit",
    ])
    tax_expense = _get_historical_metric_with_fallbacks(facts, [
        "IncomeTaxExpenseBenefit",
        "IncomeTaxExpenseBenefitContinuingOperations",
    ])
    equity = _get_historical_metric_with_fallbacks(facts, [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ])
    cash = _get_historical_metric_with_fallbacks(facts, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndCashEquivalentsAtCarryingValueWithFinancialInstitutions",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ])
    total_debt = _sum_debt_series(facts)

    roic_df = pd.DataFrame({
        "EBIT": ebit,
        "EBT": ebt,
        "Tax_Expense": tax_expense,
        "Total_Debt": total_debt,
        "Equity": equity,
        "Cash": cash,
    }).dropna(how="any")

    if roic_df.empty:
        return roic_df

    roic_df["Effective_Tax_Rate"] = roic_df["Tax_Expense"] / roic_df["EBT"]
    roic_df.loc[roic_df["EBT"] <= 0, "Effective_Tax_Rate"] = 0
    roic_df["Effective_Tax_Rate"] = roic_df["Effective_Tax_Rate"].clip(0, 0.5)
    roic_df["NOPAT"] = roic_df["EBIT"] * (1 - roic_df["Effective_Tax_Rate"])
    roic_df["Invested_Capital"] = roic_df["Total_Debt"] + roic_df["Equity"] - roic_df["Cash"]
    roic_df = roic_df[roic_df["Invested_Capital"] > 0]
    roic_df["ROIC (%)"] = (roic_df["NOPAT"] / roic_df["Invested_Capital"]) * 100
    return roic_df


def fetch_edgar_metrics(ticker: str, force: bool = False) -> dict:
    """
    Fetch SEC EDGAR metrics for a US-listed ticker.
    Returns dict with summary fields + full histories for dashboard/modal.
    """
    sym = (ticker or "").strip().upper().replace(".AX", "")
    if not sym:
        return _empty_edgar_payload("invalid_ticker")

    if not force:
        cached = _cache_get(sym)
        if cached:
            cached["_from_cache"] = True
            return cached

    _ensure_identity()
    try:
        from edgar import Company
        company = Company(sym)
        facts = company.get_facts()
    except Exception as e:
        logger.warning("EDGAR fetch failed for %s: %s", sym, e)
        cached = _cache_get(sym, max_days=0)
        if cached:
            cached["_from_cache"] = True
            cached["_fetch_error"] = str(e)
            return cached
        return _empty_edgar_payload(str(e))

    shares = _get_historical_metric_with_fallbacks(facts, [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ])
    share_history = _build_share_history(shares)

    net_income = _get_historical_metric_with_fallbacks(facts, [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ])
    earnings_history = _build_earnings_history(net_income)
    earnings_yoy_pct, earnings_5y_growth_pct = _earnings_growth_metrics(earnings_history)

    roic_df = _compute_roic_dataframe(facts)
    roic_history = _build_roic_history(roic_df)

    roic_latest = 0.0
    roic_5y_avg = 0.0
    if roic_history:
        roic_latest = roic_history[-1]["roic"]
        recent = [h["roic"] for h in roic_history[-5:]]
        roic_5y_avg = round(sum(recent) / len(recent), 2) if recent else 0.0

    share_count_5y_change_pct = 0.0
    baseline_year = None
    latest_year = None
    latest_shares = None
    baseline_shares = None

    if share_history:
        years = [h["year"] for h in share_history if h.get("shares")]
        share_vals = [h["shares"] for h in share_history if h.get("shares")]
        if years and share_vals:
            latest_year = years[-1]
            latest_shares = share_vals[-1]
            baseline_year, baseline_shares = _baseline_for_share_change(years, share_vals, lookback_years=5)
            if baseline_shares and baseline_shares > 0 and latest_shares is not None:
                share_count_5y_change_pct = round(
                    ((latest_shares - baseline_shares) / baseline_shares) * 100, 2
                )

    payload = {
        "ticker": sym,
        "roic_latest": roic_latest,
        "roic_5y_avg": roic_5y_avg,
        "share_count_5y_change_pct": share_count_5y_change_pct,
        "share_count_baseline_year": baseline_year,
        "share_count_latest_year": latest_year,
        "share_count_baseline": baseline_shares,
        "share_count_latest": latest_shares,
        "share_count_history": share_history,
        "roic_history": roic_history,
        "earnings_history": earnings_history,
        "earnings_yoy_pct": earnings_yoy_pct,
        "earnings_5y_growth_pct": earnings_5y_growth_pct,
        "fetched_at": datetime.now().isoformat(),
        "_from_cache": False,
    }
    _cache_put(sym, payload)
    return payload


def _empty_edgar_payload(reason: str = "") -> dict:
    return {
        "ticker": "",
        "roic_latest": 0,
        "roic_5y_avg": 0,
        "share_count_5y_change_pct": 0,
        "share_count_baseline_year": None,
        "share_count_latest_year": None,
        "share_count_baseline": None,
        "share_count_latest": None,
        "share_count_history": [],
        "roic_history": [],
        "earnings_history": [],
        "earnings_yoy_pct": 0,
        "earnings_5y_growth_pct": 0,
        "fetched_at": None,
        "_error": reason,
    }


def attach_edgar_to_stock(stock: dict, edgar: dict | None = None, ticker: str | None = None) -> dict:
    """Merge EDGAR summary fields onto a US stock dict for list + modal views."""
    sym = ticker or stock.get("ticker") or ""
    edgar = edgar or fetch_edgar_metrics(sym)
    stock["edgar"] = {
        "roic_latest": edgar.get("roic_latest") or 0,
        "roic_5y_avg": edgar.get("roic_5y_avg") or 0,
        "share_count_5y_change_pct": edgar.get("share_count_5y_change_pct") or 0,
        "share_count_baseline_year": edgar.get("share_count_baseline_year"),
        "share_count_latest_year": edgar.get("share_count_latest_year"),
        "share_count_history": edgar.get("share_count_history") or [],
        "roic_history": edgar.get("roic_history") or [],
        "earnings_history": edgar.get("earnings_history") or [],
        "earnings_yoy_pct": edgar.get("earnings_yoy_pct") or 0,
        "earnings_5y_growth_pct": edgar.get("earnings_5y_growth_pct") or 0,
        "fetched_at": edgar.get("fetched_at"),
    }
    metrics = stock.setdefault("metrics", {})
    metrics["roic"] = edgar.get("roic_latest") or metrics.get("roic") or 0
    metrics["roic_5y_avg"] = edgar.get("roic_5y_avg") or 0
    metrics["share_count_5y_change_pct"] = edgar.get("share_count_5y_change_pct") or 0
    metrics["earnings_yoy_pct"] = edgar.get("earnings_yoy_pct") or 0
    metrics["earnings_5y_growth_pct"] = edgar.get("earnings_5y_growth_pct") or 0
    stock["roic_latest"] = metrics["roic"]
    stock["roic_5y_avg"] = metrics["roic_5y_avg"]
    stock["share_count_5y_change_pct"] = metrics["share_count_5y_change_pct"]
    stock["earnings_yoy_pct"] = metrics["earnings_yoy_pct"]
    stock["earnings_5y_growth_pct"] = metrics["earnings_5y_growth_pct"]
    return stock


def enrich_us_stocks_edgar_cached(stocks: list) -> list:
    """Attach EDGAR metrics from cache only — no SEC calls (instant list load)."""
    return enrich_stocks_edgar_cached(stocks)


def enrich_stocks_edgar_cached(stocks: list) -> list:
    """Attach cached SEC EDGAR metrics for US-eligible tickers only."""
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        if not is_us_edgar_eligible(sym):
            stock["_edgar_eligible"] = False
            continue
        stock["_edgar_eligible"] = True
        cached = _cache_get(sym.replace(".AX", ""))
        attach_edgar_to_stock(stock, edgar=cached or _empty_edgar_payload(), ticker=sym)
    return stocks


def enrich_us_stocks_with_edgar(stocks: list, force: bool = False, sleep_seconds: float = EDGAR_FETCH_SLEEP_SECONDS) -> list:
    """Attach EDGAR metrics to each US stock (cache-first, sequential SEC calls for misses)."""
    out = []
    for i, stock in enumerate(stocks):
        sym = (stock.get("ticker") or "").upper()
        try:
            edgar = fetch_edgar_metrics(sym, force=force)
            attach_edgar_to_stock(stock, edgar=edgar, ticker=sym)
        except Exception as e:
            logger.warning("EDGAR enrich failed for %s: %s", sym, e)
            attach_edgar_to_stock(stock, edgar=_empty_edgar_payload(str(e)), ticker=sym)
        out.append(stock)
        if sleep_seconds and i < len(stocks) - 1 and not edgar.get("_from_cache"):
            time.sleep(sleep_seconds)
    return out