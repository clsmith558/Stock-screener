"""
Phase-1: Bankruptcy-cleaned survivor signals from SEC Form 8-K Item 1.03.

Indexes EDGAR 8-K filings with Item 1.03 (Bankruptcy or Receivership) for a
US mining pilot universe, classifies filing vs plan confirmation / emergence,
and attaches summary flags for pariah capital-cycle enrichment.

Does NOT prove equity survived reorg or that the current ticker is post-cleanse
equity — those require manual review (see manual_reorg in universe YAML).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from config.identity import sec_edgar_identity

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = _PROJECT_ROOT / "config" / "us_mining_universe.yaml"
DEFAULT_DB = str(_PROJECT_ROOT / "asx_announcements_cache.db")

SUMMARY_CACHE_MAX_AGE_DAYS = 30
EDGAR_FETCH_SLEEP_DEFAULT = 0.35

# Classification patterns (priority applied in classify_item_103_text)
_RE_CONFIRMATION = re.compile(
    r"\b(confirmation order|order confirming|confirming the\s+(?:debtors['’]?\s+)?"
    r"(?:joint\s+|amended\s+)*plan|plan of reorganization[^.…]{0,80}confirm|"
    r"confirmed the\s+(?:debtors['’]?\s+)?(?:joint\s+|amended\s+)*plan|"
    r"court\s+confirmed)\b",
    re.I,
)
_RE_EMERGENCE = re.compile(
    r"\b(emerged from(?:\s+chapter\s*11)?|emergence from(?:\s+chapter\s*11)?|"
    r"plan became effective|effective date of the\s+(?:confirmed\s+)?plan|"
    r"exit(?:ed|ing)\s+from\s+(?:chapter\s*11|bankruptcy))\b",
    re.I,
)
_RE_LIQUIDATION = re.compile(
    r"\b(chapter\s*7|liquidating plan|plan of liquidation|converted to chapter\s*7)\b",
    re.I,
)
_RE_FILING = re.compile(
    r"\b(voluntary petitions?|filed(?:\s+voluntary)?\s+petitions?|"
    r"commenced?\s+(?:the\s+)?chapter\s*11|"
    r"protection under chapter\s*11|filed for\s+(?:chapter\s*11|bankruptcy)|"
    r"debtors?\s+filed|"
    r"restructuring support agreement|"
    r"rsa\b|"
    r"chapter\s*11\s+cases?\s+(?:were\s+)?commenced)\b",
    re.I,
)

_DATE_IN_TEXT = re.compile(
    r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})\b",
    re.I,
)

from data.db import connect_db
from data.sec_pilot_common import (
    ensure_sec_identity as _ensure_identity,
    load_mining_universe as _shared_load_mining_universe,
    norm_cik as _norm_cik,
    resolve_edgar_company as _resolve_company,
)

_REORG_DDL = (
    """
    CREATE TABLE IF NOT EXISTS mining_item103_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT,
        cik TEXT NOT NULL,
        accession TEXT NOT NULL,
        filing_date TEXT,
        period_of_report TEXT,
        items TEXT,
        event_class TEXT,
        snippet TEXT,
        filing_url TEXT,
        company_name TEXT,
        fetched_at TEXT,
        UNIQUE(cik, accession)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mining_reorg_summary (
        ticker TEXT PRIMARY KEY,
        cik TEXT,
        company_name TEXT,
        has_item_103 INTEGER,
        has_filing INTEGER,
        has_confirmation INTEGER,
        has_emergence INTEGER,
        has_liquidation INTEGER,
        first_filing_date TEXT,
        confirmation_date TEXT,
        emerge_date TEXT,
        last_event_date TEXT,
        event_count INTEGER,
        survivor_candidate INTEGER,
        manual_reorg INTEGER,
        manual_note TEXT,
        commodities_json TEXT,
        events_json TEXT,
        last_scanned TEXT,
        scan_error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_item103_ticker ON mining_item103_events(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_item103_cik ON mining_item103_events(cik)",
)


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    return connect_db(db_path, schema_key="mining_reorg", ddl=_REORG_DDL)


def load_mining_universe(force: bool = False) -> dict:
    """Load pilot universe YAML."""
    return _shared_load_mining_universe(force=force, universe_path=UNIVERSE_PATH)


def universe_tickers() -> list[str]:
    cfg = load_mining_universe()
    out = []
    for c in cfg.get("companies") or []:
        t = (c.get("ticker") or "").strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def classify_item_103_text(text: str) -> str:
    """
    Classify Item 1.03 body text.
    Returns: confirmation | emergence | liquidation | filing | other

    Priority: confirmation > filing (petition/RSA) > emergence > liquidation > other.
    Filing is ranked above emergence so RSA/petition 8-Ks are not mis-tagged when
    residual \"effective date\" language appears in attached RSA exhibits.
    """
    if not text:
        return "other"
    # Prefer the Item 1.03 section when present
    section = text
    m = re.search(
        r"Item\s*1\.03(.{0,8000}?)(?:Item\s*\d|SIGNATURES|SIGNATURE\b|$)",
        text,
        re.I | re.S,
    )
    if m:
        section = m.group(1)
    blob = section[:12000]
    full = text[:20000]

    def _hit(pat: re.Pattern) -> bool:
        return bool(pat.search(blob) or pat.search(full))

    if _hit(_RE_CONFIRMATION):
        return "confirmation"
    if _hit(_RE_FILING):
        return "filing"
    if _hit(_RE_EMERGENCE):
        return "emergence"
    if _hit(_RE_LIQUIDATION):
        return "liquidation"
    return "other"


def _extract_snippet(text: str, max_len: int = 480) -> str:
    if not text:
        return ""
    m = re.search(
        r"Item\s*1\.03(.{0,2000}?)(?:Item\s*\d|SIGNATURES|SIGNATURE\b|$)",
        text,
        re.I | re.S,
    )
    chunk = (m.group(0) if m else text)[: max_len * 3]
    chunk = re.sub(r"\s+", " ", chunk).strip()
    return chunk[:max_len]


def _filing_has_item_103(items: str) -> bool:
    if not items:
        return False
    parts = re.split(r"[\s,;]+", str(items).strip())
    return any(p.strip() == "1.03" for p in parts)


def fetch_item_103_events_for_company(
    entry: dict,
    scan_years: int = 20,
    *,
    sleep_between_docs: float = 0.05,
) -> list[dict[str, Any]]:
    """
    Pull 8-K Item 1.03 events for one universe entry.
    Downloads document text only for matching items (rate-friendly).
    """
    _ensure_identity()
    company = _resolve_company(entry)
    cik = _norm_cik(getattr(company, "cik", None) or entry.get("cik"))
    ticker = (entry.get("ticker") or "").strip().upper()
    name = (
        entry.get("name")
        or getattr(company, "name", None)
        or getattr(company, "company_name", None)
        or ticker
    )

    cutoff = None
    if scan_years and scan_years > 0:
        cutoff = (datetime.now() - timedelta(days=365 * scan_years)).date()

    filings = company.get_filings(form="8-K")
    events: list[dict[str, Any]] = []

    for f in filings:
        try:
            items = str(getattr(f, "items", "") or "")
            if not _filing_has_item_103(items):
                continue
            fdate = getattr(f, "filing_date", None)
            if cutoff and fdate is not None:
                try:
                    d = fdate if hasattr(fdate, "year") else datetime.fromisoformat(str(fdate)[:10]).date()
                    if d < cutoff:
                        continue
                except Exception:
                    pass

            text = ""
            try:
                # Prefer plain text; fall back to markdown/html
                if hasattr(f, "text") and callable(f.text):
                    text = f.text() or ""
                if not text and hasattr(f, "markdown") and callable(f.markdown):
                    text = f.markdown() or ""
                if not text and hasattr(f, "html") and callable(f.html):
                    raw = f.html()
                    text = str(raw or "")
            except Exception as e:
                logger.debug("Item 1.03 text fetch failed %s: %s", getattr(f, "accession_number", ""), e)
                text = ""

            if sleep_between_docs:
                time.sleep(sleep_between_docs)

            event_class = classify_item_103_text(text)
            acc = getattr(f, "accession_number", None) or getattr(f, "accession_no", None) or ""
            url = getattr(f, "filing_url", None) or getattr(f, "homepage_url", None) or ""
            period = getattr(f, "period_of_report", None)

            events.append(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "accession": str(acc),
                    "filing_date": str(fdate)[:10] if fdate else None,
                    "period_of_report": str(period)[:10] if period else None,
                    "items": items,
                    "event_class": event_class,
                    "snippet": _extract_snippet(text),
                    "filing_url": str(url) if url else "",
                    "company_name": str(name),
                    "fetched_at": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            logger.warning("Skip filing for %s: %s", ticker, e)

    return events


def _store_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    n = 0
    for e in events:
        conn.execute(
            """
            INSERT INTO mining_item103_events (
                ticker, cik, accession, filing_date, period_of_report, items,
                event_class, snippet, filing_url, company_name, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cik, accession) DO UPDATE SET
                ticker=excluded.ticker,
                filing_date=excluded.filing_date,
                period_of_report=excluded.period_of_report,
                items=excluded.items,
                event_class=excluded.event_class,
                snippet=excluded.snippet,
                filing_url=excluded.filing_url,
                company_name=excluded.company_name,
                fetched_at=excluded.fetched_at
            """,
            (
                e.get("ticker"),
                e.get("cik"),
                e.get("accession"),
                e.get("filing_date"),
                e.get("period_of_report"),
                e.get("items"),
                e.get("event_class"),
                e.get("snippet"),
                e.get("filing_url"),
                e.get("company_name"),
                e.get("fetched_at"),
            ),
        )
        n += 1
    return n


def _summarize_events(
    events: list[dict],
    entry: dict,
    *,
    scan_error: str | None = None,
) -> dict[str, Any]:
    ticker = (entry.get("ticker") or "").strip().upper()
    classes = {e.get("event_class") for e in events}
    by_date = sorted(
        [e for e in events if e.get("filing_date")],
        key=lambda x: x["filing_date"],
    )
    first_filing = next(
        (e["filing_date"] for e in by_date if e.get("event_class") == "filing"),
        by_date[0]["filing_date"] if by_date else None,
    )
    conf_dates = [e["filing_date"] for e in by_date if e.get("event_class") == "confirmation"]
    emerge_dates = [e["filing_date"] for e in by_date if e.get("event_class") == "emergence"]
    confirmation_date = conf_dates[-1] if conf_dates else None
    # Cleanse proxy: latest of confirmation / emergence (not petition/RSA date)
    cleanse_candidates = [d for d in (conf_dates + emerge_dates) if d]
    emerge_date = max(cleanse_candidates) if cleanse_candidates else None
    last_event = by_date[-1]["filing_date"] if by_date else None

    manual = entry.get("manual_reorg") or {}
    manual_flag = bool(manual)
    manual_note = (manual.get("note") or "").strip() if manual else ""

    has_103 = bool(events)
    has_conf = "confirmation" in classes or "emergence" in classes
    # Survivor candidate: confirmed/emerged plan (or curated successor) without pure liquidation-only
    liquidation_only = classes == {"liquidation"} or (
        "liquidation" in classes and not has_conf and "filing" not in classes
    )
    survivor = False
    if has_conf and not liquidation_only:
        survivor = True
    elif manual_flag and manual.get("relationship") in (
        "successor_entity",
        "successor_assets",
        "same_cik_survived",
    ):
        survivor = True
    elif has_103 and "filing" in classes and not liquidation_only and has_conf:
        survivor = True

    return {
        "ticker": ticker,
        "cik": _norm_cik(entry.get("cik") or (events[0].get("cik") if events else "")),
        "company_name": entry.get("name") or (events[0].get("company_name") if events else ticker),
        "has_item_103": has_103,
        "has_filing": "filing" in classes,
        "has_confirmation": "confirmation" in classes,
        "has_emergence": "emergence" in classes,
        "has_liquidation": "liquidation" in classes,
        "first_filing_date": first_filing,
        "confirmation_date": confirmation_date,
        "emerge_date": emerge_date,
        "last_event_date": last_event,
        "event_count": len(events),
        "survivor_candidate": survivor,
        "manual_reorg": manual_flag,
        "manual_note": manual_note,
        "commodities": list(entry.get("commodities") or []),
        "events": [
            {
                "filing_date": e.get("filing_date"),
                "event_class": e.get("event_class"),
                "accession": e.get("accession"),
                "items": e.get("items"),
                "snippet": (e.get("snippet") or "")[:240],
                "filing_url": e.get("filing_url"),
            }
            for e in by_date
        ],
        "last_scanned": datetime.now().isoformat(),
        "scan_error": scan_error,
    }


def _store_summary(conn: sqlite3.Connection, summary: dict) -> None:
    conn.execute(
        """
        INSERT INTO mining_reorg_summary (
            ticker, cik, company_name, has_item_103, has_filing, has_confirmation,
            has_emergence, has_liquidation, first_filing_date, confirmation_date,
            emerge_date, last_event_date, event_count, survivor_candidate,
            manual_reorg, manual_note, commodities_json, events_json,
            last_scanned, scan_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik=excluded.cik,
            company_name=excluded.company_name,
            has_item_103=excluded.has_item_103,
            has_filing=excluded.has_filing,
            has_confirmation=excluded.has_confirmation,
            has_emergence=excluded.has_emergence,
            has_liquidation=excluded.has_liquidation,
            first_filing_date=excluded.first_filing_date,
            confirmation_date=excluded.confirmation_date,
            emerge_date=excluded.emerge_date,
            last_event_date=excluded.last_event_date,
            event_count=excluded.event_count,
            survivor_candidate=excluded.survivor_candidate,
            manual_reorg=excluded.manual_reorg,
            manual_note=excluded.manual_note,
            commodities_json=excluded.commodities_json,
            events_json=excluded.events_json,
            last_scanned=excluded.last_scanned,
            scan_error=excluded.scan_error
        """,
        (
            summary["ticker"],
            summary.get("cik"),
            summary.get("company_name"),
            1 if summary.get("has_item_103") else 0,
            1 if summary.get("has_filing") else 0,
            1 if summary.get("has_confirmation") else 0,
            1 if summary.get("has_emergence") else 0,
            1 if summary.get("has_liquidation") else 0,
            summary.get("first_filing_date"),
            summary.get("confirmation_date"),
            summary.get("emerge_date"),
            summary.get("last_event_date"),
            summary.get("event_count") or 0,
            1 if summary.get("survivor_candidate") else 0,
            1 if summary.get("manual_reorg") else 0,
            summary.get("manual_note"),
            json.dumps(summary.get("commodities") or []),
            json.dumps(summary.get("events") or []),
            summary.get("last_scanned"),
            summary.get("scan_error"),
        ),
    )


def scan_company(entry: dict, db_path: str = DEFAULT_DB, scan_years: int | None = None) -> dict:
    """Scan one universe entry; store events + summary. Returns summary dict."""
    cfg = load_mining_universe()
    years = scan_years if scan_years is not None else int(cfg.get("scan_years") or 20)
    ticker = (entry.get("ticker") or "").strip().upper()
    conn = _conn(db_path)
    try:
        events = fetch_item_103_events_for_company(entry, scan_years=years)
        # Persist CIK from events if entry lacked it
        if events and not entry.get("cik"):
            entry = dict(entry)
            entry["cik"] = events[0].get("cik")
        _store_events(conn, events)
        summary = _summarize_events(events, entry)
        _store_summary(conn, summary)
        conn.commit()
        return summary
    except Exception as e:
        logger.warning("scan_company failed for %s: %s", ticker, e)
        summary = _summarize_events([], entry, scan_error=str(e))
        # Preserve prior events if any
        try:
            prior = get_reorg_summary(ticker, db_path=db_path, max_age_days=0)
            if prior and prior.get("events"):
                summary["events"] = prior["events"]
                summary["has_item_103"] = prior.get("has_item_103")
                summary["has_filing"] = prior.get("has_filing")
                summary["has_confirmation"] = prior.get("has_confirmation")
                summary["has_emergence"] = prior.get("has_emergence")
                summary["confirmation_date"] = prior.get("confirmation_date")
                summary["emerge_date"] = prior.get("emerge_date")
                summary["first_filing_date"] = prior.get("first_filing_date")
                summary["event_count"] = prior.get("event_count")
                summary["survivor_candidate"] = prior.get("survivor_candidate") or summary.get(
                    "survivor_candidate"
                )
        except Exception:
            pass
        _store_summary(conn, summary)
        conn.commit()
        return summary
    finally:
        conn.close()


def scan_mining_universe(
    db_path: str = DEFAULT_DB,
    *,
    force: bool = False,
    tickers: list[str] | None = None,
    sleep_seconds: float | None = None,
) -> dict[str, Any]:
    """
    Scan full pilot universe (or subset). Skips fresh summaries unless force=True.
    Returns {scanned, skipped, errors, summaries: [...]}
    """
    cfg = load_mining_universe()
    sleep = (
        sleep_seconds
        if sleep_seconds is not None
        else float(cfg.get("fetch_sleep_seconds") or EDGAR_FETCH_SLEEP_DEFAULT)
    )
    want = {t.strip().upper() for t in tickers} if tickers else None
    companies = list(cfg.get("companies") or [])
    scanned = 0
    skipped = 0
    errors = 0
    summaries = []

    for i, entry in enumerate(companies):
        t = (entry.get("ticker") or "").strip().upper()
        if not t:
            continue
        if want is not None and t not in want:
            continue
        if not force:
            existing = get_reorg_summary(t, db_path=db_path, max_age_days=SUMMARY_CACHE_MAX_AGE_DAYS)
            if existing and not existing.get("scan_error"):
                skipped += 1
                summaries.append(existing)
                continue
        try:
            s = scan_company(entry, db_path=db_path)
            summaries.append(s)
            scanned += 1
            if s.get("scan_error"):
                errors += 1
        except Exception as e:
            errors += 1
            logger.exception("Universe scan failed for %s: %s", t, e)
        if sleep and i < len(companies) - 1:
            time.sleep(sleep)

    return {
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "summaries": summaries,
        "universe_size": len(companies),
        "finished_at": datetime.now().isoformat(),
    }


def _row_to_summary(row: tuple) -> dict[str, Any]:
    (
        ticker,
        cik,
        company_name,
        has_item_103,
        has_filing,
        has_confirmation,
        has_emergence,
        has_liquidation,
        first_filing_date,
        confirmation_date,
        emerge_date,
        last_event_date,
        event_count,
        survivor_candidate,
        manual_reorg,
        manual_note,
        commodities_json,
        events_json,
        last_scanned,
        scan_error,
    ) = row
    try:
        commodities = json.loads(commodities_json) if commodities_json else []
    except Exception:
        commodities = []
    try:
        events = json.loads(events_json) if events_json else []
    except Exception:
        events = []
    return {
        "ticker": ticker,
        "cik": cik,
        "company_name": company_name,
        "has_item_103": bool(has_item_103),
        "has_filing": bool(has_filing),
        "has_confirmation": bool(has_confirmation),
        "has_emergence": bool(has_emergence),
        "has_liquidation": bool(has_liquidation),
        "first_filing_date": first_filing_date,
        "confirmation_date": confirmation_date,
        "emerge_date": emerge_date,
        "last_event_date": last_event_date,
        "event_count": event_count or 0,
        "survivor_candidate": bool(survivor_candidate),
        "manual_reorg": bool(manual_reorg),
        "manual_note": manual_note or "",
        "commodities": commodities,
        "events": events,
        "last_scanned": last_scanned,
        "scan_error": scan_error,
    }


def get_reorg_summary(
    ticker: str,
    db_path: str = DEFAULT_DB,
    max_age_days: int = SUMMARY_CACHE_MAX_AGE_DAYS,
) -> dict | None:
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    try:
        conn = _conn(db_path)
        row = conn.execute(
            """
            SELECT ticker, cik, company_name, has_item_103, has_filing, has_confirmation,
                   has_emergence, has_liquidation, first_filing_date, confirmation_date,
                   emerge_date, last_event_date, event_count, survivor_candidate,
                   manual_reorg, manual_note, commodities_json, events_json,
                   last_scanned, scan_error
            FROM mining_reorg_summary WHERE ticker=?
            """,
            (sym,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        if max_age_days > 0 and row[18]:
            try:
                lu = datetime.fromisoformat(row[18])
                if datetime.now() - lu > timedelta(days=max_age_days):
                    return None
            except Exception:
                pass
        return _row_to_summary(row)
    except Exception as e:
        logger.warning("get_reorg_summary failed for %s: %s", sym, e)
        return None


def list_reorg_summaries(
    db_path: str = DEFAULT_DB,
    *,
    survivors_only: bool = False,
) -> list[dict]:
    conn = _conn(db_path)
    sql = """
        SELECT ticker, cik, company_name, has_item_103, has_filing, has_confirmation,
               has_emergence, has_liquidation, first_filing_date, confirmation_date,
               emerge_date, last_event_date, event_count, survivor_candidate,
               manual_reorg, manual_note, commodities_json, events_json,
               last_scanned, scan_error
        FROM mining_reorg_summary
    """
    if survivors_only:
        sql += " WHERE survivor_candidate=1"
    sql += " ORDER BY ticker"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [_row_to_summary(r) for r in rows]


def attach_reorg_to_stock(stock: dict, summary: dict | None = None, ticker: str | None = None) -> dict:
    """Attach reorg summary fields onto a stock dict (cache-only if summary None)."""
    sym = (ticker or stock.get("ticker") or "").strip().upper().replace(".AX", "")
    if summary is None:
        summary = get_reorg_summary(sym, max_age_days=0)
    if not summary:
        stock["reorg"] = {
            "scanned": False,
            "has_item_103": False,
            "survivor_candidate": False,
        }
        return stock

    stock["reorg"] = {
        "scanned": True,
        "has_item_103": bool(summary.get("has_item_103")),
        "has_filing": bool(summary.get("has_filing")),
        "has_confirmation": bool(summary.get("has_confirmation")),
        "has_emergence": bool(summary.get("has_emergence")),
        "first_filing_date": summary.get("first_filing_date"),
        "confirmation_date": summary.get("confirmation_date"),
        "emerge_date": summary.get("emerge_date"),
        "survivor_candidate": bool(summary.get("survivor_candidate")),
        "manual_reorg": bool(summary.get("manual_reorg")),
        "manual_note": summary.get("manual_note") or "",
        "event_count": summary.get("event_count") or 0,
        "events": summary.get("events") or [],
        "commodities": summary.get("commodities") or [],
        "cik": summary.get("cik"),
        "scan_error": summary.get("scan_error"),
    }
    # Flat fields for filters / tables
    stock["reorg_survivor_candidate"] = stock["reorg"]["survivor_candidate"]
    stock["reorg_has_item_103"] = stock["reorg"]["has_item_103"]
    stock["reorg_emerge_date"] = stock["reorg"].get("emerge_date")
    return stock


def enrich_stocks_reorg_cached(stocks: list) -> list:
    """Attach cached reorg summaries (no SEC calls)."""
    if not stocks:
        return stocks
    try:
        by_ticker = {
            (s.get("ticker") or "").strip().upper(): s
            for s in list_reorg_summaries()
            if s.get("ticker")
        }
    except Exception as e:
        logger.warning("enrich_stocks_reorg_cached batch load failed: %s", e)
        by_ticker = {}
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        if not sym:
            continue
        summary = by_ticker.get(sym.replace(".AX", ""))
        if summary:
            attach_reorg_to_stock(stock, summary=summary, ticker=sym)
        else:
            stock["reorg"] = {
                "scanned": False,
                "has_item_103": False,
                "survivor_candidate": False,
            }
    return stocks


def reorg_status_public(db_path: str = DEFAULT_DB) -> dict:
    """Dashboard / API status blob."""
    summaries = list_reorg_summaries(db_path)
    survivors = [s for s in summaries if s.get("survivor_candidate")]
    with_103 = [s for s in summaries if s.get("has_item_103")]
    cfg = load_mining_universe()
    return {
        "universe_size": len(cfg.get("companies") or []),
        "summaries_cached": len(summaries),
        "with_item_103": len(with_103),
        "survivor_candidates": len(survivors),
        "survivors": [
            {
                "ticker": s["ticker"],
                "name": s.get("company_name"),
                "emerge_date": s.get("emerge_date"),
                "confirmation_date": s.get("confirmation_date"),
                "has_item_103": s.get("has_item_103"),
                "manual_reorg": s.get("manual_reorg"),
                "commodities": s.get("commodities") or [],
            }
            for s in survivors
        ],
        "item_103_hits": [
            {
                "ticker": s["ticker"],
                "name": s.get("company_name"),
                "event_count": s.get("event_count"),
                "confirmation_date": s.get("confirmation_date"),
                "first_filing_date": s.get("first_filing_date"),
                "classes": [
                    e.get("event_class")
                    for e in (s.get("events") or [])
                ],
            }
            for s in with_103
        ],
        "description": cfg.get("description") or "",
    }
