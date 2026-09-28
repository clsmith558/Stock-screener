#!/usr/bin/env python3
"""
Enrich a single ticker: SEC EDGAR 5yr metrics + yfinance snapshot → Obsidian vault.

Usage (from Stock screener folder):
  python scripts/enrich_ticker.py HCC --export-vault
  python scripts/enrich_ticker.py HCC --export-vault --force-edgar
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_VAULT = Path(r"C:\Obsidian\csmith_test")


def _default_vault() -> Path:
    raw = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_VAULT


def _fmt_millions(val) -> str:
    if val is None:
        return "—"
    try:
        v = float(val)
        if abs(v) >= 1e9:
            return f"${v / 1e9:.2f}B"
        if abs(v) >= 1e6:
            return f"${v / 1e6:.0f}M"
        return f"${v:,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fetch_yfinance(ticker: str) -> dict:
    import yfinance as yf
    from data.business_lists import _calc_ma200w_gap, _metrics_from_yf_info, _safe_float, _safe_round
    from data.yf_helpers import pct_from_52w_low, reconcile_52w_range

    sym = ticker.upper().strip()
    yt = yf.Ticker(sym)
    info = yt.info or {}
    price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice")) or 0
    low, high = reconcile_52w_range(
        price, info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    )
    ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
    from data.yf_helpers import calc_last_52w_low_metrics
    low52 = calc_last_52w_low_metrics(yt)
    metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, 0, ticker=sym)
    metrics["last_52w_low_date"] = low52.get("last_52w_low_date")
    metrics["days_since_52w_low"] = low52.get("days_since_52w_low")
    p_fcf = metrics.get("p_fcf") or 0
    pct_from_low = pct_from_52w_low(price, low)

    return {
        "ticker": sym,
        "name": info.get("shortName") or info.get("longName") or sym,
        "price": _safe_round(price, 2),
        "low_52w": _safe_round(low, 2) if low else 0,
        "high_52w": _safe_round(high, 2) if high else 0,
        "pct_from_52w_low": pct_from_low,
        "pct_from_200w_ma": metrics.get("pct_from_200w_ma", 0),
        "last_52w_low_date": metrics.get("last_52w_low_date"),
        "days_since_52w_low": metrics.get("days_since_52w_low"),
        "market_cap": metrics.get("market_cap", 0),
        "pe": metrics.get("pe", 0),
        "forward_pe": metrics.get("forward_pe", 0),
        "pb": metrics.get("pb", 0),
        "p_fcf": p_fcf,
        "debt_to_equity": metrics.get("debt_to_equity", 0),
        "metrics": metrics,
        "fetched_at": datetime.now().isoformat(),
    }


def _build_annual_table(ticker: str, edgar: dict) -> list[dict]:
    """Merge EDGAR histories into annual rows (last 5 FY)."""
    from data.edgar_metrics import (
        _compute_roic_dataframe,
        _ensure_identity,
        _get_historical_metric_with_fallbacks,
    )

    rows_by_year: dict[int, dict] = {}

    for h in edgar.get("earnings_history") or []:
        yr = h.get("year")
        if yr:
            rows_by_year.setdefault(int(yr), {})["net_income"] = h.get("earnings")

    for h in edgar.get("roic_history") or []:
        yr = h.get("year")
        if yr:
            rows_by_year.setdefault(int(yr), {})["roic"] = h.get("roic")

    try:
        _ensure_identity()
        from edgar import Company

        company = Company(ticker.upper())
        facts = company.get_facts()
        roic_df = _compute_roic_dataframe(facts)
        for year, row in roic_df.sort_index().iterrows():
            try:
                yr = int(year)
            except (TypeError, ValueError):
                continue
            entry = rows_by_year.setdefault(yr, {})
            for src, dst in (
                ("Total_Debt", "total_debt"),
                ("Cash", "cash"),
                ("Equity", "equity"),
                ("EBIT", "ebit"),
                ("NOPAT", "nopat"),
            ):
                v = row.get(src)
                if v == v:
                    entry[dst] = float(v)

        revenue = _get_historical_metric_with_fallbacks(facts, [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ])
        ocf = _get_historical_metric_with_fallbacks(facts, [
            "NetCashProvidedByUsedInOperatingActivities",
        ])
        capex = _get_historical_metric_with_fallbacks(facts, [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "CapitalExpendituresIncurredButNotYetPaid",
        ])
        for series, key in ((revenue, "revenue"), (ocf, "ocf"), (capex, "capex")):
            if series is None or getattr(series, "empty", True):
                continue
            for year, val in series.items():
                try:
                    yr = int(year)
                except (TypeError, ValueError):
                    continue
                if val == val:
                    rows_by_year.setdefault(yr, {})[key] = float(val)
    except Exception as e:
        print(f"WARN: extended EDGAR table failed: {e}", file=sys.stderr)

    annual = []
    for yr in sorted(rows_by_year.keys())[-5:]:
        r = rows_by_year[yr]
        ocf = r.get("ocf")
        capex = r.get("capex")
        fcf = None
        if ocf is not None and capex is not None:
            fcf = ocf - abs(capex)
        debt = r.get("total_debt") or 0
        cash = r.get("cash") or 0
        annual.append({
            "year": yr,
            "revenue": r.get("revenue"),
            "net_income": r.get("net_income"),
            "ocf": ocf,
            "capex": capex,
            "fcf": fcf,
            "total_debt": debt,
            "cash": cash,
            "net_debt": debt - cash if debt is not None and cash is not None else None,
            "roic": r.get("roic"),
        })
    return annual


def _write_vault_exports(vault: Path, ticker: str, payload: dict) -> dict:
    today = date.today().isoformat()
    sym = ticker.upper()
    filings_dir = vault / "raw-sources" / "filings" / sym
    filings_dir.mkdir(parents=True, exist_ok=True)

    json_path = filings_dir / f"{today}-edgar-yf.json"
    md_path = filings_dir / f"{today}-edgar-summary.md"
    source_path = vault / "wiki" / "investing" / "sources" / f"Source-{sym}-EDGAR-{today[:4]}.md"

    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    edgar = payload.get("edgar") or {}
    yf = payload.get("yfinance") or {}
    annual = payload.get("annual") or []
    scores = payload.get("scores") or {}

    lines = [
        "---",
        f"title: EDGAR + yfinance — {sym} ({today})",
        "date: " + today,
        "tags:",
        "  - raw",
        "  - filings",
        f"  - {sym.lower()}",
        f"ticker: {sym}",
        f"fetched_at: {payload.get('fetched_at', today)}",
        "status: processed",
        "---",
        "",
        f"# EDGAR + yfinance capture — {sym}",
        "",
        "> [!quote] Source",
        f"> SEC EDGAR via edgartools · yfinance snapshot · `scripts/enrich_ticker.py`",
        "",
        "## yfinance snapshot",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Price | ${yf.get('price', '—')} |",
        f"| Market cap | {_fmt_millions(yf.get('market_cap'))} |",
        f"| 52w low gap | {yf.get('pct_from_52w_low', '—')}% |",
        f"| 200w MA gap | {yf.get('pct_from_200w_ma', '—')}% |",
        f"| P/E | {yf.get('pe', '—')} |",
        f"| Fwd P/E | {yf.get('forward_pe', '—')} |",
        f"| P/B | {yf.get('pb', '—')} |",
        f"| P/FCF | {yf.get('p_fcf', '—')} |",
        f"| D/E | {yf.get('debt_to_equity', '—')} |",
        "",
        "## Annual financials (last 5 FY, SEC)",
        "",
        "| FY | Revenue | Net income | OCF | CapEx | FCF | Total debt | Cash | Net debt | ROIC |",
        "|----|---------|------------|-----|-------|-----|------------|------|----------|------|",
    ]
    for row in annual:
        lines.append(
            "| {year} | {rev} | {ni} | {ocf} | {capex} | {fcf} | {debt} | {cash} | {net} | {roic} |".format(
                year=row["year"],
                rev=_fmt_millions(row.get("revenue")),
                ni=_fmt_millions(row.get("net_income")),
                ocf=_fmt_millions(row.get("ocf")),
                capex=_fmt_millions(row.get("capex")),
                fcf=_fmt_millions(row.get("fcf")),
                debt=_fmt_millions(row.get("total_debt")),
                cash=_fmt_millions(row.get("cash")),
                net=_fmt_millions(row.get("net_debt")),
                roic=f"{row['roic']:.1f}%" if row.get("roic") is not None else "—",
            )
        )

    lines.extend([
        "",
        "## EDGAR summary",
        "",
        f"- ROIC latest: {edgar.get('roic_latest', '—')}%",
        f"- ROIC 5y avg: {edgar.get('roic_5y_avg', '—')}%",
        f"- Earnings YoY: {edgar.get('earnings_yoy_pct', '—')}%",
        f"- Share count 5y change: {edgar.get('share_count_5y_change_pct', '—')}%",
        "",
        "## Scores",
        "",
        f"- Score (quant only): {scores.get('composite_score', scores.get('quant_score', '—'))}",
        f"- Quant components: {scores.get('quant_score', '—')}",
        f"- Vault qual (not in Score): {scores.get('qual_score', '—')}",
        "",
        f"Machine-readable: `{json_path.relative_to(vault).as_posix()}`",
    ])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    table_start = lines.index("| FY | Revenue | Net income | OCF | CapEx | FCF | Total debt | Cash | Net debt | ROIC |")
    table_end = lines.index("## EDGAR summary")
    table_rows = [ln for ln in lines[table_start:table_end] if isinstance(ln, str)]

    source_lines = [
        "---",
        f"title: Source — {sym} EDGAR {today[:4]}",
        f"date: {today}",
        "tags:",
        "  - investing",
        "  - source",
        "  - filings",
        f"  - {sym.lower()}",
        "sources:",
        f'  - "[[raw-sources/filings/{sym}/{today}-edgar-summary]]"',
        "status: processed",
        "---",
        "",
        f"# {sym} — SEC EDGAR + yfinance ({today})",
        "",
        "> Distilled from immutable raw capture. Regenerate via `python scripts/enrich_ticker.py {sym} --export-vault`.",
        "",
        "## Links",
        "",
        f"- Raw: [[raw-sources/filings/{sym}/{today}-edgar-summary]]",
        f"- Memo: [[opportunities/{sym}]]",
        "",
        "## Annual table",
        "",
        *table_rows,
    ]
    source_path.write_text("\n".join(source_lines) + "\n", encoding="utf-8")

    return {
        "json": str(json_path),
        "raw_md": str(md_path),
        "source_note": str(source_path),
        "raw_wiki_link": f"raw-sources/filings/{sym}/{today}-edgar-summary",
        "source_wiki_link": f"Source-{sym}-EDGAR-{today[:4]}",
    }


def enrich_ticker(ticker: str, force_edgar: bool = False, export_vault: bool = False, vault: Path | None = None) -> dict:
    from data.edgar_metrics import fetch_edgar_metrics
    from data.wiki_bridge import enrich_stock_opportunity, lookup_company

    sym = ticker.upper().strip()
    edgar = fetch_edgar_metrics(sym, force=force_edgar)
    yf = _fetch_yfinance(sym)
    annual = _build_annual_table(sym, edgar)

    stock_row = {
        "ticker": sym,
        "name": yf.get("name") or sym,
        "pct_from_52w_low": yf.get("pct_from_52w_low"),
        "pct_from_200w_ma": yf.get("pct_from_200w_ma"),
        "pb": yf.get("pb"),
        "p_fcf": yf.get("p_fcf"),
        "pe": yf.get("pe"),
        "forward_pe": yf.get("forward_pe"),
        "debt_to_equity": yf.get("debt_to_equity"),
        "metrics": yf.get("metrics") or {},
        "buyback_announced": False,
        "insider_buys_2026": 0,
    }
    enriched = enrich_stock_opportunity(stock_row, peers=[stock_row])
    scores = {
        "quant_score": enriched.get("quant_score"),
        "qual_score": enriched.get("qual_score"),
        "composite_score": enriched.get("composite_score"),
        "coverage_tier": enriched.get("coverage_tier"),
    }

    payload = {
        "ticker": sym,
        "fetched_at": datetime.now().isoformat(),
        "edgar": edgar,
        "yfinance": yf,
        "annual": annual,
        "scores": scores,
        "registry": lookup_company(sym),
    }

    paths = {}
    if export_vault:
        v = vault or _default_vault()
        paths = _write_vault_exports(v, sym, payload)
        try:
            from data.vault_memo import push_enrich_to_memo

            memo = push_enrich_to_memo(sym, payload, vault=v)
            paths["opportunity_memo"] = memo.get("path")
            paths["opportunity_uri"] = memo.get("uri")
            paths["memo_created"] = memo.get("created")
            payload["memo"] = memo
        except Exception as e:
            paths["memo_error"] = str(e)
            print(f"WARN: opportunity memo push failed: {e}", file=sys.stderr)

    payload["vault_paths"] = paths
    return payload


def main():
    parser = argparse.ArgumentParser(description="Enrich ticker with EDGAR + yfinance")
    parser.add_argument("ticker", help="US ticker symbol, e.g. HCC")
    parser.add_argument("--export-vault", action="store_true", help="Write raw + source note to Obsidian vault")
    parser.add_argument("--force-edgar", action="store_true", help="Bypass EDGAR SQLite cache")
    parser.add_argument("--vault", type=Path, default=None, help="Obsidian vault path")
    parser.add_argument("--json", action="store_true", help="Print full JSON to stdout")
    args = parser.parse_args()

    payload = enrich_ticker(
        args.ticker,
        force_edgar=args.force_edgar,
        export_vault=args.export_vault,
        vault=args.vault,
    )
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        yf = payload["yfinance"]
        sc = payload["scores"]
        print(f"{payload['ticker']}: quant={sc.get('quant_score')} qual={sc.get('qual_score')} composite={sc.get('composite_score')}")
        print(f"  price={yf.get('price')} mcap={_fmt_millions(yf.get('market_cap'))} 52w_gap={yf.get('pct_from_52w_low')}%")
        print(f"  P/B={yf.get('pb')} P/E={yf.get('pe')} D/E={yf.get('debt_to_equity')}")
        if payload.get("vault_paths"):
            print(f"  vault: {payload['vault_paths']}")


if __name__ == "__main__":
    main()