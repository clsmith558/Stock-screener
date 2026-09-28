"""
Inactive symbol hygiene — keep YF jobs lean.

Steps (default full pass):
  1. Promote zero-price cache rows → inactive (business + ASX)
  2. Optionally re-probe *tentative* inactive (cached_zero_price) via yfinance
  3. Purge *confirmed* inactive from business_list_entries (+ registry counts)
  4. Optionally rewrite Business lists CSV sources without confirmed dead symbols
  5. Prune orphan inactive rows (no longer on any list / ASX universe)
  6. Never auto-remove watchlist tickers (user intent)

Confirmed vs tentative:
  - tentative: reason in {cached_zero_price} — may be transient
  - confirmed: quote_not_found, zero_price, delisted, etc. — permanent skip
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_LISTS_DIR = _PROJECT_ROOT / "Business lists"


def _iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def list_inactive_business(db_path: str | None = None) -> list[dict]:
    from data.asx import DEFAULT_UNIVERSE_DB
    from data.business_lists import TENTATIVE_INACTIVE_REASONS, _lists_conn

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _lists_conn(db_path)
    rows = conn.execute(
        "SELECT yf_symbol, reason, marked_at, source FROM business_list_inactive ORDER BY yf_symbol"
    ).fetchall()
    conn.close()
    out = []
    for sym, reason, marked, source in rows:
        out.append(
            {
                "symbol": sym,
                "reason": reason,
                "marked_at": marked,
                "source": source,
                "confirmed": (reason or "") not in TENTATIVE_INACTIVE_REASONS,
            }
        )
    return out


def list_inactive_asx(db_path: str | None = None) -> list[dict]:
    from data.asx import DEFAULT_UNIVERSE_DB, TENTATIVE_INACTIVE_REASONS, _get_universe_conn

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _get_universe_conn(db_path)
    rows = conn.execute(
        "SELECT ticker, reason, marked_at, source FROM asx_inactive_tickers ORDER BY ticker"
    ).fetchall()
    conn.close()
    out = []
    for t, reason, marked, source in rows:
        out.append(
            {
                "symbol": f"{t}.AX",
                "base": t,
                "reason": reason,
                "marked_at": marked,
                "source": source,
                "confirmed": (reason or "") not in TENTATIVE_INACTIVE_REASONS,
            }
        )
    return out


def revalidate_tentative_business(
    *,
    limit: int = 80,
    sleep_seconds: float = 1.5,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Re-check tentative business inactive symbols with a live yfinance quote.
    Promote to confirmed or clear if price is healthy.
    """
    import yfinance as yf

    from data.asx import DEFAULT_UNIVERSE_DB, _detect_yf_inactive
    from data.business_lists import (
        TENTATIVE_INACTIVE_REASONS,
        _lists_conn,
        mark_symbol_inactive,
        remove_inactive_symbol,
    )
    from data.yf_helpers import quote_price

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _lists_conn(db_path)
    ph = ",".join("?" * len(TENTATIVE_INACTIVE_REASONS))
    rows = conn.execute(
        f"""
        SELECT yf_symbol FROM business_list_inactive
        WHERE reason IN ({ph})
        ORDER BY marked_at
        LIMIT ?
        """,
        (*tuple(TENTATIVE_INACTIVE_REASONS), limit),
    ).fetchall()
    conn.close()
    symbols = [r[0] for r in rows]
    cleared = 0
    confirmed = 0
    errors = 0
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
            price = quote_price(info)
            reason = _detect_yf_inactive(info, price)
            if price and price > 0 and not reason:
                remove_inactive_symbol(sym, db_path=db_path)
                cleared += 1
                logger.info("inactive hygiene: revived %s", sym)
            else:
                mark_symbol_inactive(
                    sym,
                    reason or "quote_not_found",
                    source="hygiene_revalidate",
                    db_path=db_path,
                )
                confirmed += 1
            time.sleep(sleep_seconds)
        except Exception as e:
            errors += 1
            logger.debug("revalidate %s: %s", sym, e)
            time.sleep(max(0.5, sleep_seconds / 2))
    return {
        "probed": len(symbols),
        "cleared": cleared,
        "confirmed": confirmed,
        "errors": errors,
    }


def revalidate_tentative_asx(
    *,
    limit: int = 80,
    sleep_seconds: float = 1.5,
    db_path: str | None = None,
) -> dict[str, Any]:
    import yfinance as yf

    from data.asx import (
        DEFAULT_UNIVERSE_DB,
        TENTATIVE_INACTIVE_REASONS,
        _detect_yf_inactive,
        _get_universe_conn,
        mark_ticker_inactive,
        remove_inactive_ticker,
    )
    from data.yf_helpers import quote_price

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _get_universe_conn(db_path)
    ph = ",".join("?" * len(TENTATIVE_INACTIVE_REASONS))
    rows = conn.execute(
        f"""
        SELECT ticker FROM asx_inactive_tickers
        WHERE reason IN ({ph})
        ORDER BY marked_at
        LIMIT ?
        """,
        (*tuple(TENTATIVE_INACTIVE_REASONS), limit),
    ).fetchall()
    conn.close()
    cleared = 0
    confirmed = 0
    errors = 0
    for (t,) in rows:
        try:
            info = yf.Ticker(f"{t}.AX").info or {}
            price = quote_price(info)
            reason = _detect_yf_inactive(info, price)
            if price and price > 0 and not reason:
                remove_inactive_ticker(t, db_path=db_path)
                cleared += 1
                logger.info("inactive hygiene: revived %s.AX", t)
            else:
                mark_ticker_inactive(
                    t,
                    reason or "quote_not_found",
                    source="hygiene_revalidate",
                    db_path=db_path,
                )
                confirmed += 1
            time.sleep(sleep_seconds)
        except Exception as e:
            errors += 1
            logger.debug("revalidate ASX %s: %s", t, e)
            time.sleep(max(0.5, sleep_seconds / 2))
    return {
        "probed": len(rows),
        "cleared": cleared,
        "confirmed": confirmed,
        "errors": errors,
    }


def purge_confirmed_from_list_entries(db_path: str | None = None) -> dict[str, Any]:
    """Remove confirmed-inactive symbols from business_list_entries; fix registry counts."""
    from data.asx import DEFAULT_UNIVERSE_DB
    from data.business_lists import TENTATIVE_INACTIVE_REASONS, _lists_conn

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _lists_conn(db_path)
    ph = ",".join("?" * len(TENTATIVE_INACTIVE_REASONS))
    inactive = {
        r[0]
        for r in conn.execute(
            f"""
            SELECT yf_symbol FROM business_list_inactive
            WHERE reason NOT IN ({ph})
            """,
            tuple(TENTATIVE_INACTIVE_REASONS),
        ).fetchall()
    }
    if not inactive:
        conn.close()
        return {"removed_entries": 0, "lists_touched": 0, "inactive_n": 0}

    # Never strip watchlist — not stored in business_list_entries
    placeholders = ",".join("?" * len(inactive))
    before = conn.execute(
        f"SELECT COUNT(*) FROM business_list_entries WHERE yf_symbol IN ({placeholders})",
        tuple(inactive),
    ).fetchone()[0]
    lists = [
        r[0]
        for r in conn.execute(
            f"""
            SELECT DISTINCT list_id FROM business_list_entries
            WHERE yf_symbol IN ({placeholders})
            """,
            tuple(inactive),
        ).fetchall()
    ]
    conn.execute(
        f"DELETE FROM business_list_entries WHERE yf_symbol IN ({placeholders})",
        tuple(inactive),
    )
    # Refresh ticker_count per touched list
    for lid in lists:
        n = conn.execute(
            "SELECT COUNT(*) FROM business_list_entries WHERE list_id=?",
            (lid,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE business_list_registry SET ticker_count=? WHERE list_id=?",
            (n, lid),
        )
    conn.commit()
    conn.close()
    logger.info(
        "Purged %d list entries for %d confirmed inactive symbols (%d lists)",
        before,
        len(inactive),
        len(lists),
    )
    return {
        "removed_entries": before,
        "lists_touched": len(lists),
        "inactive_n": len(inactive),
        "list_ids": lists,
    }


def rewrite_business_csvs_without_confirmed(
    db_path: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Rewrite user Business lists/*.csv files excluding confirmed inactive symbols.
    Skips *.meta.csv. Built-in lists are DB-only (no CSV rewrite).
    """
    from data.asx import DEFAULT_UNIVERSE_DB
    from data.business_lists import (
        BUILTIN_LIST_IDS,
        TENTATIVE_INACTIVE_REASONS,
        _lists_conn,
        _parse_csv_tickers,
    )

    db_path = db_path or DEFAULT_UNIVERSE_DB
    conn = _lists_conn(db_path)
    ph = ",".join("?" * len(TENTATIVE_INACTIVE_REASONS))
    inactive = {
        r[0]
        for r in conn.execute(
            f"SELECT yf_symbol FROM business_list_inactive WHERE reason NOT IN ({ph})",
            tuple(TENTATIVE_INACTIVE_REASONS),
        ).fetchall()
    }
    conn.close()
    if not inactive or not BUSINESS_LISTS_DIR.is_dir():
        return {"files_rewritten": 0, "symbols_removed": 0, "files": []}

    files_out = []
    removed_total = 0
    for path in sorted(BUSINESS_LISTS_DIR.glob("*.csv*")):
        name = path.name
        if name.endswith(".meta.csv") or "meta.csv" in name:
            continue
        if name in BUILTIN_LIST_IDS:
            continue
        # Skip pure meta sidecars
        if name.endswith(".meta.csv"):
            continue
        try:
            original = _parse_csv_tickers(path)
        except Exception as e:
            logger.warning("parse %s failed: %s", path, e)
            continue
        if not original:
            continue
        kept = [s for s in original if s not in inactive]
        dropped = len(original) - len(kept)
        if dropped <= 0:
            continue
        if dry_run:
            files_out.append({"path": str(path), "dropped": dropped, "kept": len(kept)})
            removed_total += dropped
            continue
        # Preserve format: Japan-style single comma line if original was one line blob
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) <= 2 and text.count(",") > 10:
            body = ",".join(kept) + "\n"
        else:
            body = "\n".join(kept) + ("\n" if kept else "")
        path.write_text(body, encoding="utf-8")
        files_out.append({"path": str(path), "dropped": dropped, "kept": len(kept)})
        removed_total += dropped
        logger.info("Rewrote %s: removed %d inactive, kept %d", path.name, dropped, len(kept))

    return {
        "files_rewritten": len(files_out) if not dry_run else 0,
        "files_would_rewrite": len(files_out) if dry_run else 0,
        "symbols_removed": removed_total,
        "files": files_out,
        "dry_run": dry_run,
    }


def prune_orphan_inactive(db_path: str | None = None) -> dict[str, Any]:
    """
    Drop stale inactive rows that no longer need a skip entry.

    Business: remove inactive symbols not on any list and marked >90 days ago
    (keeps recent skip memory so re-scans of CSVs before rewrite do not re-hit YF).
    ASX: remove inactive bases no longer on the official universe CSV.
    """
    from data.asx import DEFAULT_UNIVERSE_DB, _get_universe_conn
    from data.business_lists import _lists_conn

    db_path = db_path or DEFAULT_UNIVERSE_DB

    conn = _lists_conn(db_path)
    biz_before = conn.execute("SELECT COUNT(*) FROM business_list_inactive").fetchone()[0]
    pruned_biz = conn.execute(
        """
        DELETE FROM business_list_inactive
        WHERE yf_symbol NOT IN (SELECT DISTINCT yf_symbol FROM business_list_entries)
          AND (marked_at IS NULL OR date(substr(marked_at, 1, 10)) < date('now', '-90 days'))
        """
    ).rowcount
    conn.commit()
    biz_after = conn.execute("SELECT COUNT(*) FROM business_list_inactive").fetchone()[0]
    conn.close()

    conn = _get_universe_conn(db_path)
    # Only prune if universe table has rows (avoid wiping skip list on empty universe)
    univ_n = conn.execute("SELECT COUNT(*) FROM asx_universe").fetchone()[0] or 0
    if univ_n > 0:
        pruned_asx = conn.execute(
            """
            DELETE FROM asx_inactive_tickers
            WHERE ticker NOT IN (SELECT ticker FROM asx_universe)
            """
        ).rowcount
    else:
        pruned_asx = 0
    conn.commit()
    conn.close()

    logger.info(
        "Pruned orphan inactive: business=%s asx=%s",
        pruned_biz,
        pruned_asx,
    )
    return {
        "business_pruned": pruned_biz,
        "asx_pruned": pruned_asx,
        "business_remaining": biz_after,
        "business_before": biz_before,
    }


def run_inactive_hygiene(
    *,
    revalidate: bool = True,
    revalidate_limit: int = 60,
    purge_entries: bool = True,
    rewrite_csvs: bool = True,
    prune_orphans: bool = True,
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Full hygiene pass. Safe for scheduled runs."""
    from data.asx import DEFAULT_UNIVERSE_DB, sync_inactive_from_cache as asx_sync
    from data.business_lists import sync_inactive_from_cache as biz_sync

    db_path = db_path or DEFAULT_UNIVERSE_DB
    report: dict[str, Any] = {
        "started_at": _iso(),
        "dry_run": dry_run,
    }

    report["asx_cache_sync"] = asx_sync(db_path=db_path)
    report["business_cache_sync"] = biz_sync(db_path=db_path)

    if revalidate and not dry_run:
        report["revalidate_business"] = revalidate_tentative_business(
            limit=revalidate_limit, db_path=db_path
        )
        report["revalidate_asx"] = revalidate_tentative_asx(
            limit=revalidate_limit, db_path=db_path
        )
    elif revalidate and dry_run:
        report["revalidate_business"] = {"skipped": True, "reason": "dry_run"}
        report["revalidate_asx"] = {"skipped": True, "reason": "dry_run"}

    if purge_entries:
        if dry_run:
            from data.business_lists import TENTATIVE_INACTIVE_REASONS, _lists_conn

            conn = _lists_conn(db_path)
            ph = ",".join("?" * len(TENTATIVE_INACTIVE_REASONS))
            inactive = [
                r[0]
                for r in conn.execute(
                    f"SELECT yf_symbol FROM business_list_inactive WHERE reason NOT IN ({ph})",
                    tuple(TENTATIVE_INACTIVE_REASONS),
                ).fetchall()
            ]
            n = 0
            if inactive:
                placeholders = ",".join("?" * len(inactive))
                n = conn.execute(
                    f"SELECT COUNT(*) FROM business_list_entries WHERE yf_symbol IN ({placeholders})",
                    tuple(inactive),
                ).fetchone()[0]
            conn.close()
            report["purge_entries"] = {
                "removed_entries": 0,
                "would_remove_entries": n,
                "dry_run": True,
                "inactive_n": len(inactive),
            }
        else:
            report["purge_entries"] = purge_confirmed_from_list_entries(db_path=db_path)

    if rewrite_csvs:
        report["rewrite_csvs"] = rewrite_business_csvs_without_confirmed(
            db_path=db_path, dry_run=dry_run
        )

    if prune_orphans and not dry_run:
        report["prune_orphans"] = prune_orphan_inactive(db_path=db_path)
    elif prune_orphans:
        report["prune_orphans"] = {"skipped": True, "reason": "dry_run"}

    # Summary counts
    biz = list_inactive_business(db_path)
    asx = list_inactive_asx(db_path)
    report["summary"] = {
        "business_inactive_total": len(biz),
        "business_inactive_confirmed": sum(1 for x in biz if x["confirmed"]),
        "business_inactive_tentative": sum(1 for x in biz if not x["confirmed"]),
        "asx_inactive_total": len(asx),
        "asx_inactive_confirmed": sum(1 for x in asx if x["confirmed"]),
        "asx_inactive_tentative": sum(1 for x in asx if not x["confirmed"]),
    }
    report["finished_at"] = _iso()
    return report
