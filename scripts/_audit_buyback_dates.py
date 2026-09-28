#!/usr/bin/env python3
"""Audit future / absurd buyback_date values in buybacks registry + US SEC tables."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.asx import DEFAULT_UNIVERSE_DB
from data.buybacks import fetch_buyback_stocks_for_business_list


def parse_d(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def main():
    today = date.today()
    print("today", today)
    print("db", DEFAULT_UNIVERSE_DB)

    conn = sqlite3.connect(DEFAULT_UNIVERSE_DB)
    print("\n=== tables ===")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"
    )]
    print(tables)

    # US repurchase schema + MDU
    if "us_buyback_repurchases" in tables:
        print("\n=== us_buyback_repurchases columns ===")
        print(conn.execute("PRAGMA table_info(us_buyback_repurchases)").fetchall())
        print("\n=== MDU repurchases ===")
        rows = conn.execute(
            """
            SELECT yf_symbol, ticker, period_end, period_label, shares, price,
                   filing_date, form, accession, headline
            FROM us_buyback_repurchases
            WHERE upper(ticker)='MDU' OR upper(yf_symbol)='MDU'
            ORDER BY period_end DESC
            LIMIT 30
            """
        ).fetchall()
        for r in rows:
            print(r)

        print("\n=== future period_end rows (sample 40) ===")
        rows = conn.execute(
            """
            SELECT yf_symbol, period_end, period_label, shares, filing_date, form
            FROM us_buyback_repurchases
            WHERE period_end > ?
            ORDER BY period_end DESC
            LIMIT 40
            """,
            (today.isoformat(),),
        ).fetchall()
        for r in rows:
            print(r)
        n = conn.execute(
            "SELECT COUNT(*) FROM us_buyback_repurchases WHERE period_end > ?",
            (today.isoformat(),),
        ).fetchone()[0]
        print("total future repurchase rows:", n)

        years = conn.execute(
            """
            SELECT substr(period_end, 1, 4) AS y, COUNT(*)
            FROM us_buyback_repurchases
            GROUP BY y
            ORDER BY y
            """
        ).fetchall()
        print("period_end year histogram:", years)

    if "us_buyback_events" in tables:
        print("\n=== MDU events ===")
        rows = conn.execute(
            """
            SELECT yf_symbol, date, form, headline, accession
            FROM us_buyback_events
            WHERE upper(ticker)='MDU' OR upper(yf_symbol)='MDU'
            ORDER BY date DESC
            LIMIT 20
            """
        ).fetchall()
        for r in rows:
            print(r)

        n = conn.execute(
            "SELECT COUNT(*) FROM us_buyback_events WHERE date > ?",
            (today.isoformat(),),
        ).fetchone()[0]
        print("future event dates:", n)
        if n:
            for r in conn.execute(
                """
                SELECT yf_symbol, date, form, substr(headline,1,80)
                FROM us_buyback_events WHERE date > ?
                ORDER BY date DESC LIMIT 20
                """,
                (today.isoformat(),),
            ):
                print(r)

    # Registry JSON
    if "buyback_registry" in tables:
        print("\n=== registry future buyback_date ===")
        future = []
        src_c = Counter()
        year_c = Counter()
        for sym, row_json in conn.execute("SELECT yf_symbol, row_json FROM buyback_registry"):
            try:
                row = json.loads(row_json)
            except Exception:
                continue
            d = parse_d(row.get("buyback_date"))
            if d and d > today:
                src = row.get("buyback_source") or row.get("source") or "?"
                future.append((sym, row.get("buyback_date"), src, row.get("name")))
                src_c[src] += 1
                year_c[str(d.year)] += 1
        future.sort(key=lambda x: x[1], reverse=True)
        print("registry future count:", len(future))
        print("by source:", dict(src_c))
        print("by year:", dict(sorted(year_c.items())))
        for r in future[:50]:
            print(r)

    conn.close()

    # Live list (may rebuild registry)
    print("\n=== live list future buyback_date ===")
    stocks = fetch_buyback_stocks_for_business_list(force_yf=False)
    future = []
    src_c = Counter()
    for s in stocks:
        d = parse_d(s.get("buyback_date"))
        if d and d > today:
            src = s.get("buyback_source") or "?"
            future.append(
                (
                    s.get("ticker"),
                    s.get("buyback_date"),
                    src,
                    s.get("name"),
                    s.get("buyback_events_total"),
                    (s.get("buyback_history") or [])[:3],
                )
            )
            src_c[src] += 1
    future.sort(key=lambda x: x[1], reverse=True)
    print("n stocks", len(stocks), "future", len(future), "by source", dict(src_c))
    for t, bd, src, name, n_ev, hist in future[:35]:
        print(f"{t:8} {bd}  src={src}  events={n_ev}  {name}")
        if hist:
            for h in hist:
                print(f"         hist: {h.get('date')} {h.get('source')} {str(h.get('desc') or '')[:90]}")

    # TSE CSV scan for future Implementation Dates
    try:
        import pandas as pd
        from data.jpx_repurchases import DEFAULT_CSV_PATH as TSE_CSV

        if Path(TSE_CSV).exists():
            df = pd.read_csv(TSE_CSV)
            col = "Implementation Date"
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
                fut = df[df[col].dt.date > today]
                print("\n=== TSE CSV future Implementation Date ===")
                print("path", TSE_CSV, "rows", len(df), "future", len(fut))
                if len(fut):
                    print(fut[[c for c in df.columns if c in (
                        "Code", "Issue Name", "Implementation Date",
                        "No. of Shares to be Purchased", "Price",
                    )]].sort_values(col, ascending=False).head(20).to_string())
                    print("year hist future", fut[col].dt.year.value_counts().sort_index().to_dict())
    except Exception as e:
        print("TSE scan failed", e)

    # HK scan
    try:
        import pandas as pd
        from data.hkex_repurchases import DEFAULT_CSV_PATH as HK_CSV

        if Path(HK_CSV).exists():
            df = pd.read_csv(HK_CSV)
            col = "Trading Date"
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
                fut = df[df[col].dt.date > today]
                print("\n=== HK CSV future Trading Date ===")
                print("path", HK_CSV, "rows", len(df), "future", len(fut))
                if len(fut):
                    print(fut.head(10).to_string())
    except Exception as e:
        print("HK scan failed", e)


if __name__ == "__main__":
    main()
