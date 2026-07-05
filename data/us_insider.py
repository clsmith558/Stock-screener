"""
US insider open-market purchases via SEC Form 4 (S&P 500 / Russell 1000 / Nasdaq-100 universe).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta

from data.asx import DEFAULT_UNIVERSE_DB
from data.edgar_metrics import EDGAR_FETCH_SLEEP_SECONDS, _ensure_identity
from data.us_buybacks import (
    _fetch_submissions,
    _filing_url,
    load_sec_ticker_cik_map,
)
from data.us_universe import load_us_scan_universe, normalize_us_ticker

logger = logging.getLogger(__name__)

INSIDER_FORM = "4"
PURCHASE_CODES = frozenset({"P"})


def _conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_insider_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            yf_symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            shares INTEGER,
            price REAL,
            insider TEXT,
            position TEXT,
            accession TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            headline TEXT,
            url TEXT,
            source TEXT NOT NULL DEFAULT 'SEC-4',
            created_at TEXT NOT NULL,
            UNIQUE(accession, date, insider, shares, price)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_insider_scan_log (
            accession TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            matched INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_insider_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_ins_ticker ON us_insider_purchases(yf_symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_ins_date ON us_insider_purchases(date)")
    conn.commit()
    return conn


def _meta_set(pairs: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    conn = _conn(db_path)
    for k, v in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO us_insider_meta (key, value) VALUES (?,?)",
            (k, str(v)),
        )
    conn.commit()
    conn.close()


def count_us_insider_purchases(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = _conn(db_path)
        n = conn.execute("SELECT COUNT(*) FROM us_insider_purchases").fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def get_us_insider_meta(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT value FROM us_insider_meta WHERE key='last_fetched'"
    ).fetchone()
    conn.close()
    return {
        "last_fetched": row[0] if row else "",
        "purchase_count": count_us_insider_purchases(db_path),
    }


def _recent_form4_filings(submissions: dict, cutoff) -> list[dict]:
    recent = submissions.get("filings", {}).get("recent") or {}
    names = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    primary = recent.get("primaryDocument") or []
    out = []
    for i, form in enumerate(names):
        if form != INSIDER_FORM:
            continue
        fdate = dates[i] if i < len(dates) else ""
        if not fdate or fdate < cutoff.isoformat():
            continue
        acc = accessions[i] if i < len(accessions) else ""
        if not acc:
            continue
        out.append({
            "filing_date": fdate,
            "accession": acc,
            "primary_document": primary[i] if i < len(primary) else "",
        })
    return out


def _already_scanned(accession: str, db_path: str) -> bool:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT 1 FROM us_insider_scan_log WHERE accession=?", (accession,)
    ).fetchone()
    conn.close()
    return bool(row)


def _log_scan(accession: str, ticker: str, matched: bool, db_path: str):
    conn = _conn(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO us_insider_scan_log (accession, ticker, scanned_at, matched)
           VALUES (?,?,?,?)""",
        (accession, ticker, datetime.now().isoformat(), 1 if matched else 0),
    )
    conn.commit()
    conn.close()


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f) or f <= 0:
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f) or f <= 0:
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _parse_form4_purchases(cik: int, company: str, filing_date: str, accession: str) -> list[dict]:
    from edgar import Filing

    filing = Filing(
        cik=cik,
        company=company,
        form=INSIDER_FORM,
        filing_date=filing_date,
        accession_no=accession,
    )
    obj = filing.obj()
    insider = ""
    position = ""
    try:
        insider = (obj.insider_name or "").strip()
    except Exception:
        pass
    try:
        owners = obj.reporting_owners
        if owners and not insider:
            insider = str(owners).split("\n")[0][:120]
    except Exception:
        pass

    rows = []
    seen = set()

    def _add_row(dt, shares, price, label=""):
        if not dt:
            return
        if hasattr(dt, "strftime"):
            dstr = dt.strftime("%Y-%m-%d")
        else:
            dstr = str(dt)[:10]
        sh = _safe_int(shares)
        pr = _safe_float(price)
        key = (dstr, insider, sh, pr)
        if key in seen:
            return
        seen.add(key)
        headline = f"SEC Form 4: {insider or 'Insider'}"
        if label:
            headline += f" — {label}"
        if sh:
            headline += f" — {sh:,} shares"
        if pr:
            headline += f" @ ${pr:.2f}"
        rows.append({
            "date": dstr,
            "shares": sh,
            "price": pr,
            "insider": insider,
            "position": position,
            "headline": headline,
        })

    try:
        csp = obj.common_stock_purchases
        if csp is not None and hasattr(csp, "iterrows"):
            if not getattr(csp, "empty", True):
                for _, r in csp.iterrows():
                    _add_row(
                        r.get("Date"),
                        r.get("Shares"),
                        r.get("Price") or r.get("Price per Share"),
                        str(r.get("TransactionType") or r.get("Transaction Type") or "Purchase"),
                    )
    except Exception as e:
        logger.debug("common_stock_purchases parse failed: %s", e)

    try:
        import pandas as pd
        df = obj.to_dataframe()
        if df is not None and not df.empty:
            code_col = "Code" if "Code" in df.columns else None
            for _, r in df.iterrows():
                code = str(r.get(code_col, "")).strip().upper() if code_col else ""
                tx_type = str(r.get("Transaction Type") or r.get("TransactionType") or "")
                if code not in PURCHASE_CODES and "purchase" not in tx_type.lower():
                    continue
                if "sale" in tx_type.lower() and code != "P":
                    continue
                _add_row(
                    r.get("Date"),
                    r.get("Shares"),
                    r.get("Price"),
                    tx_type or "Purchase",
                )
    except Exception as e:
        logger.debug("Form4 dataframe parse failed: %s", e)

    return rows


def _upsert_purchases(
    sym: str,
    filing_date: str,
    accession: str,
    url: str,
    purchases: list[dict],
    db_path: str,
) -> int:
    conn = _conn(db_path)
    inserted = 0
    now = datetime.now().isoformat()
    for p in purchases:
        cur = conn.execute(
            """INSERT OR IGNORE INTO us_insider_purchases
               (ticker, yf_symbol, date, shares, price, insider, position,
                accession, filing_date, headline, url, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sym,
                sym,
                p.get("date"),
                p.get("shares"),
                p.get("price"),
                p.get("insider") or "",
                p.get("position") or "",
                accession,
                filing_date,
                p.get("headline") or "",
                url,
                "SEC-4",
                now,
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    conn.close()
    return inserted


def load_us_insider_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list[dict]:
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            """
            SELECT ticker, yf_symbol, date, shares, price, insider, position,
                   accession, filing_date, headline, url, source
            FROM us_insider_purchases
            ORDER BY date DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("US insider load failed: %s", e)
        return []

    return [
        {
            "yf_symbol": yf,
            "ticker": ticker,
            "date": date,
            "shares": int(shares) if shares is not None else None,
            "price": float(price) if price is not None else None,
            "director": insider or "",
            "insider": insider or "",
            "position": position or "",
            "headline": headline or "",
            "url": url or "",
            "source": source or "SEC-4",
            "accession": accession,
            "filing_date": filing_date,
            "currency": "USD",
        }
        for ticker, yf, date, shares, price, insider, position, accession, filing_date, headline, url, source in rows
    ]


def refresh_us_insider_purchases(
    tickers: list[str] | None = None,
    days_back: int = 365,
    sleep_seconds: float | None = None,
    db_path: str = DEFAULT_UNIVERSE_DB,
    max_tickers: int | None = None,
    on_progress=None,
) -> dict:
    _ensure_identity()
    sleep = sleep_seconds if sleep_seconds is not None else EDGAR_FETCH_SLEEP_SECONDS
    universe = tickers or load_us_scan_universe()
    if max_tickers:
        universe = universe[: max(1, int(max_tickers))]

    cik_map = load_sec_ticker_cik_map()
    cutoff = (datetime.now() - timedelta(days=max(1, days_back))).date()

    scanned_filings = 0
    new_purchases = 0
    tickers_scanned = 0
    tickers_missing = 0
    errors = 0

    sym = ""

    def _report():
        if on_progress:
            on_progress(
                phase="insider",
                tickers_done=tickers_scanned,
                tickers_total=len(universe),
                current_ticker=sym,
                filings_scanned=scanned_filings,
                new_purchases=new_purchases,
                errors=errors,
            )

    for ticker in universe:
        sym = normalize_us_ticker(ticker)
        entry = cik_map.get(sym) or cik_map.get(ticker.upper())
        if not entry:
            tickers_missing += 1
            continue

        cik = int(entry["cik"])
        company = entry.get("title") or sym
        tickers_scanned += 1
        _report()

        try:
            submissions = _fetch_submissions(cik)
            candidates = _recent_form4_filings(submissions, cutoff)
        except Exception as e:
            logger.warning("US insider submissions failed for %s: %s", sym, e)
            errors += 1
            time.sleep(sleep)
            continue

        for cand in candidates:
            acc = cand["accession"]
            if _already_scanned(acc, db_path):
                continue
            scanned_filings += 1
            matched = False
            try:
                purchases = _parse_form4_purchases(
                    cik,
                    company,
                    cand["filing_date"],
                    acc,
                )
                if purchases:
                    matched = True
                    url = _filing_url(cik, acc, cand.get("primary_document") or "")
                    new_purchases += _upsert_purchases(
                        sym,
                        cand["filing_date"],
                        acc,
                        url,
                        purchases,
                        db_path,
                    )
            except Exception as e:
                logger.debug("US Form 4 parse failed %s %s: %s", sym, acc, e)
                errors += 1
            finally:
                _log_scan(acc, sym, matched, db_path)
            time.sleep(sleep)

        time.sleep(sleep * 0.35)

    now = datetime.now().isoformat()
    _meta_set({
        "last_fetched": now,
        "last_scan_tickers": tickers_scanned,
        "purchase_count": count_us_insider_purchases(db_path),
    }, db_path)

    result = {
        "tickers_requested": len(universe),
        "tickers_scanned": tickers_scanned,
        "tickers_missing_cik": tickers_missing,
        "filings_scanned": scanned_filings,
        "new_purchases": new_purchases,
        "total_purchases": count_us_insider_purchases(db_path),
        "errors": errors,
        "last_fetched": now,
    }
    logger.info("US insider refresh: %s", result)
    return result