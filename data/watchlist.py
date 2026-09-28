"""
Personal watchlist — local SQLite CRUD.

Tickers stored as full Yahoo-style symbols (NHC.AX, AAPL) so ASX/US never collide.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(_PROJECT_ROOT / "data" / "watchlist.db")


def _norm_ticker(ticker: str) -> str:
    return (ticker or "").strip().upper()


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            notes TEXT,
            added_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchlist_added ON watchlist(added_at DESC)"
    )
    conn.commit()
    return conn


def list_watchlist(db_path: str = DEFAULT_DB) -> list[dict[str, Any]]:
    conn = _conn(db_path)
    rows = conn.execute(
        """
        SELECT ticker, name, market, notes, added_at
        FROM watchlist
        ORDER BY added_at DESC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "ticker": r[0],
            "name": r[1] or r[0],
            "market": r[2] or "",
            "notes": r[3] or "",
            "added_at": r[4],
            "on_watchlist": True,
        }
        for r in rows
    ]


def watchlist_tickers(db_path: str = DEFAULT_DB) -> set[str]:
    return {e["ticker"] for e in list_watchlist(db_path)}


def is_watched(ticker: str, db_path: str = DEFAULT_DB) -> bool:
    t = _norm_ticker(ticker)
    if not t:
        return False
    conn = _conn(db_path)
    row = conn.execute("SELECT 1 FROM watchlist WHERE ticker=?", (t,)).fetchone()
    conn.close()
    return bool(row)


def add_ticker(
    ticker: str,
    *,
    name: str = "",
    market: str = "",
    notes: str = "",
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    t = _norm_ticker(ticker)
    if not t:
        raise ValueError("ticker required")
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    conn.execute(
        """
        INSERT INTO watchlist (ticker, name, market, notes, added_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            name=COALESCE(NULLIF(excluded.name, ''), watchlist.name),
            market=COALESCE(NULLIF(excluded.market, ''), watchlist.market),
            notes=COALESCE(NULLIF(excluded.notes, ''), watchlist.notes)
        """,
        (
            t,
            (name or "").strip(),
            (market or "").strip().lower(),
            (notes or "").strip(),
            now,
        ),
    )
    conn.commit()
    conn.close()
    return {
        "ticker": t,
        "name": (name or "").strip() or t,
        "market": (market or "").strip().lower(),
        "notes": (notes or "").strip(),
        "added_at": now,
        "on_watchlist": True,
    }


def remove_ticker(ticker: str, db_path: str = DEFAULT_DB) -> bool:
    t = _norm_ticker(ticker)
    if not t:
        return False
    conn = _conn(db_path)
    cur = conn.execute("DELETE FROM watchlist WHERE ticker=?", (t,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n > 0


def _infer_market(ticker: str, market: str = "") -> str:
    m = (market or "").strip().lower()
    if m and m not in ("watchlist",):
        return m
    t = _norm_ticker(ticker)
    if t.endswith(".AX"):
        return "asx"
    if any(
        t.endswith(s)
        for s in (
            ".HK",
            ".T",
            ".L",
            ".TO",
            ".PA",
            ".DE",
            ".SW",
            ".SI",
            ".NS",
            ".BO",
        )
    ):
        return "business"
    return "us"


def _thin_row(entry: dict) -> dict:
    t = entry["ticker"]
    market = _infer_market(t, entry.get("market") or "")
    return {
        "ticker": t,
        "name": entry.get("name") or t,
        "sector": "Watchlist",
        "current_price": 0,
        "market_cap": 0,
        "pct_from_52w_low": 0,
        "pct_from_200w_ma": 0,
        "low_52w": 0,
        "high_52w": 0,
        "pe": 0,
        "pb": 0,
        "p_fcf": 0,
        "insider_buys_2026": 0,
        "buyback_announced": False,
        "metrics": {},
        "signals": [],
        "on_watchlist": True,
        "watchlist_added_at": entry.get("added_at"),
        "watchlist_notes": entry.get("notes") or "",
        "_market": market,
        "_watchlist": True,
        "_pending_yf": True,
    }


def _try_enrich_from_caches(entries: list[dict]) -> list[dict]:
    """Overlay price/metrics from business/ASX YF caches when available (no network)."""
    by_ticker = {e["ticker"]: _thin_row(e) for e in entries}
    if not by_ticker:
        return []

    symbols = list(by_ticker.keys())
    cached: dict = {}
    try:
        from data.business_lists import load_yf_cached

        # Allow stale rows so watchlist still shows last known prices
        cached = load_yf_cached(symbols, max_days=3650) or {}
    except Exception as e:
        logger.debug("watchlist load_yf_cached: %s", e)

    for t, ydat in cached.items():
        if t not in by_ticker or not ydat:
            continue
        row = by_ticker[t]
        try:
            from data.business_lists import _stock_row

            enriched = _stock_row(
                t,
                ydat,
                {
                    "list_id": "watchlist",
                    "display_name": "Watchlist",
                    "_market": row.get("_market"),
                },
            )
            enriched["on_watchlist"] = True
            enriched["watchlist_added_at"] = row.get("watchlist_added_at")
            enriched["watchlist_notes"] = row.get("watchlist_notes") or ""
            enriched["_watchlist"] = True
            enriched["_market"] = row.get("_market") or "business"
            enriched["_pending_yf"] = not bool(ydat.get("price"))
            by_ticker[t] = enriched
        except Exception as e:
            logger.debug("watchlist row build %s: %s", t, e)
            if ydat.get("price"):
                row["current_price"] = ydat.get("price") or 0
                row["name"] = ydat.get("name") or row["name"]
                row["sector"] = ydat.get("sector") or row["sector"]
                row["low_52w"] = ydat.get("low_52w") or 0
                row["high_52w"] = ydat.get("high_52w") or 0
                row["_pending_yf"] = False

    return [by_ticker[e["ticker"]] for e in entries if e["ticker"] in by_ticker]


def fetch_watchlist_stocks(db_path: str = DEFAULT_DB) -> list[dict]:
    """Rows for Watchlist tab: thin + cache overlay."""
    entries = list_watchlist(db_path)
    if not entries:
        return []
    try:
        rows = _try_enrich_from_caches(entries)
    except Exception as e:
        logger.warning("watchlist enrich failed: %s", e)
        rows = [_thin_row(e) for e in entries]
    from data.yf_helpers import sanitize_stocks

    return sanitize_stocks(rows)


def annotate_on_watchlist(stocks: list[dict], db_path: str = DEFAULT_DB) -> list[dict]:
    """Set on_watchlist flag on stock dicts for star UI."""
    watched = watchlist_tickers(db_path)
    for s in stocks or []:
        t = _norm_ticker(s.get("ticker") or "")
        s["on_watchlist"] = t in watched
    return stocks or []
