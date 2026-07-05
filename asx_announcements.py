#!/usr/bin/env python3
"""
ASX Announcements Scraper - Enhanced Version

Features implemented:
1. PDF text extraction (pypdf) to distinguish actual purchases vs sales in Appendix 3Y
2. Broader market-wide searching (not just per-ticker)
3. SQLite caching + persistence layer (reduces hammering the API)
4. Improved error handling + optional proxy support

DISCLAIMER:
- Uses unofficial endpoints (Markit Digital / ASX). These can change or break at any time.
- This is for prototype / personal educational use only.
- Heavy usage may violate ASX Terms of Service.
- For production reliability, subscribe to ASX ComNews / MIA paid feed.

Usage examples at the bottom of the file.
"""

import os
import time
import json
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from urllib.parse import urljoin
import random

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("asx_scraper")

# ====================== CONFIG ======================
BASE_URL = "https://asx.api.markitdigital.com/asx-research/1.0"
PDF_BASE = "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/"

DEFAULT_DB_PATH = "asx_announcements_cache.db"
RATE_LIMIT_SECONDS = 1.0          # Base delay between requests
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

# Classification keywords (headline level)
DIRECTOR_INSIDER_KEYWORDS = [
    "appendix 3y", "change of director", "director's interest",
    "director interest", "3y", "director share",
    "initial director", "final director",
]
SUBSTANTIAL_HOLDER_KEYWORDS = [
    "substantial holder",
]
INSIDER_KEYWORDS = DIRECTOR_INSIDER_KEYWORDS + SUBSTANTIAL_HOLDER_KEYWORDS
BUYBACK_KEYWORDS = [
    "buy-back", "buyback", "on-market buy", "share repurchase",
    "notification of buy-back", "buy back", "on market buy-back",
]
BUYBACK_ANNOUNCEMENT_TYPES = (
    "daily share buy-back notice",
    "notification of buy-back",
    "buy-back",
    "on-market buy-back",
)
MAX_MARKET_PAGES = 160
INCREMENTAL_MAX_PAGES = 30

# Strong signals inside PDF text for actual *purchase*
PURCHASE_TEXT_SIGNS = [
    "acquired", "purchase", "bought", "taken up", "exercise of options",
    "on-market purchase", "director acquired"
]
SALE_TEXT_SIGNS = [
    "sold", "disposed", "sale", "on-market sale", "transferred"
]
# ====================================================


class ASXScraperError(Exception):
    """Base exception for scraper issues."""
    pass


class RateLimitError(ASXScraperError):
    pass


def _get_session(proxies: Optional[Dict] = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://www.asx.com.au",
        "Referer": "https://www.asx.com.au/markets/trade-our-cash-market/announcements",
    })
    if proxies:
        session.proxies.update(proxies)
    return session


def _retry_request(session: requests.Session, url: str, params: dict = None,
                   timeout: int = 20, proxies: dict = None) -> requests.Response:
    """Simple retry with exponential backoff."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                raise RateLimitError("Rate limited by ASX")
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            sleep_time = RETRY_BACKOFF ** attempt + random.uniform(0, 0.5)
            logger.warning(f"Request failed (attempt {attempt}/{MAX_RETRIES}): {e}. "
                           f"Retrying in {sleep_time:.1f}s...")
            time.sleep(sleep_time)
    raise ASXScraperError(f"Failed after {MAX_RETRIES} attempts: {last_exc}")


# ====================== SQLITE CACHE LAYER ======================

def init_db(db_path: str = DEFAULT_DB_PATH):
    """Initialize SQLite cache tables."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            document_key TEXT PRIMARY KEY,
            ticker TEXT,
            date TEXT,
            headline TEXT,
            url TEXT,
            is_price_sensitive INTEGER,
            classification TEXT,           -- 'insider_purchase', 'insider_sale', 'buyback', 'other'
            raw_json TEXT,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticker_date ON announcements(ticker, date);
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_classification ON announcements(classification);
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asx_scrape_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_scrape_meta(key: str, db_path: str = DEFAULT_DB_PATH) -> str:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT value FROM asx_scrape_meta WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def set_scrape_meta(pairs: dict, db_path: str = DEFAULT_DB_PATH):
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    for k, v in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO asx_scrape_meta (key, value) VALUES (?,?)",
            (k, str(v)),
        )
    conn.commit()
    conn.close()


def _known_document_keys(db_path: str = DEFAULT_DB_PATH) -> set:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT document_key FROM announcements").fetchall()
    conn.close()
    return {r[0] for r in rows if r[0]}


def save_announcements_to_db(announcements: List[Dict], db_path: str = DEFAULT_DB_PATH):
    """Upsert announcements into SQLite."""
    if not announcements:
        return
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    for ann in announcements:
        cur.execute("""
            INSERT OR REPLACE INTO announcements
            (document_key, ticker, date, headline, url, is_price_sensitive, classification, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ann.get("document_key"),
            ann.get("ticker"),
            ann.get("date"),
            ann.get("headline"),
            ann.get("url"),
            int(ann.get("is_price_sensitive", False)),
            ann.get("classification", "other"),
            json.dumps(ann.get("raw", {}))
        ))
    conn.commit()
    conn.close()


def load_cached_announcements(tickers: List[str] = None, days: int = 90,
                              db_path: str = DEFAULT_DB_PATH) -> List[Dict]:
    """Load recent announcements from cache."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).date().isoformat()

    query = """
        SELECT document_key, ticker, date, headline, url, is_price_sensitive, classification, raw_json
        FROM announcements
        WHERE date >= ?
    """
    params = [since]

    if tickers:
        placeholders = ",".join("?" * len(tickers))
        query += f" AND ticker IN ({placeholders})"
        params.extend([t.upper().replace(".AX", "") for t in tickers])

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for row in rows:
        results.append({
            "document_key": row[0],
            "ticker": row[1],
            "date": row[2],
            "headline": row[3],
            "url": row[4],
            "is_price_sensitive": bool(row[5]),
            "classification": row[6],
            "raw": json.loads(row[7]) if row[7] else {}
        })
    return results


# ====================== CORE FETCHING ======================

def get_company_xid(session: requests.Session, ticker: str) -> Optional[str]:
    """Resolve ticker to Markit Digital xid."""
    try:
        url = f"{BASE_URL}/search/predictive"
        resp = _retry_request(session, url, params={"searchText": ticker.upper()})
        data = resp.json()
        for item in data.get("data", {}).get("items", []):
            if item.get("symbol", "").upper() == ticker.upper():
                return item.get("xid")
        return None
    except Exception as e:
        logger.warning(f"xid lookup failed for {ticker}: {e}")
        return None


def fetch_announcements_for_tickers(
    tickers: List[str],
    days: int = 90,
    max_per_ticker: int = 150,
    use_cache: bool = True,
    proxies: Optional[Dict] = None,
    db_path: str = DEFAULT_DB_PATH
) -> List[Dict]:
    """Fetch announcements for specific tickers (with caching)."""
    session = _get_session(proxies)
    tickers = [t.upper().replace(".AX", "") for t in tickers]

    # Try cache first
    cached = []
    if use_cache:
        cached = load_cached_announcements(tickers, days, db_path)
        cached_tickers = {a["ticker"] for a in cached}
        tickers = [t for t in tickers if t not in cached_tickers]
        if not tickers:
            logger.info("All requested tickers served from cache.")
            return cached

    new_anns = []
    since = (datetime.now() - timedelta(days=days)).date()

    for ticker in tickers:
        xid = get_company_xid(session, ticker)
        if not xid:
            logger.warning(f"Could not resolve xid for {ticker}")
            time.sleep(RATE_LIMIT_SECONDS)
            continue

        try:
            url = f"{BASE_URL}/markets/announcements"
            params = {
                "page": 0,
                "itemsPerPage": max_per_ticker,
                "entityXids[]": xid,
            }
            resp = _retry_request(session, url, params=params)
            data = resp.json()
            items = data.get("data", {}).get("items", [])

            for item in items:
                try:
                    date_str = item.get("date", "")
                    ann_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
                    if ann_date < since:
                        continue

                    new_anns.append({
                        "ticker": ticker,
                        "date": ann_date.isoformat(),
                        "headline": item.get("headline", ""),
                        "is_price_sensitive": item.get("isPriceSensitive", False),
                        "document_key": item.get("documentKey"),
                        "url": f"{PDF_BASE}{item.get('documentKey')}",
                        "raw": item
                    })
                except Exception as e:
                    logger.debug(f"Skipping bad announcement: {e}")

            time.sleep(RATE_LIMIT_SECONDS)

        except Exception as e:
            logger.error(f"Failed fetching {ticker}: {e}")

    # Classify (headline level first)
    for ann in new_anns:
        ann["classification"] = _headline_classify(ann["headline"])

    # Save to cache
    if new_anns and use_cache:
        save_announcements_to_db(new_anns, db_path)

    return cached + new_anns


def _headline_refine_insider(headline: str) -> str:
    """Headline-only fallback when PDF text is unavailable."""
    h = (headline or "").lower()
    if any(kw in h for kw in SUBSTANTIAL_HOLDER_KEYWORDS):
        return "other"
    if any(kw in h for kw in ("disposal", "sold", "sale of", "on-market sale")):
        return "insider_sale"
    if any(kw in h for kw in DIRECTOR_INSIDER_KEYWORDS):
        return "insider_purchase"
    return "other"


def _headline_classify(headline: str) -> str:
    """Fast headline-only classification."""
    h = headline.lower()
    if any(kw in h for kw in BUYBACK_KEYWORDS):
        return "buyback"
    if any(kw in h for kw in SUBSTANTIAL_HOLDER_KEYWORDS):
        return "other"
    if any(kw in h for kw in DIRECTOR_INSIDER_KEYWORDS):
        return "insider"
    return "other"


def _announcement_from_db_row(row) -> Dict:
    return {
        "document_key": row[0],
        "ticker": row[1],
        "date": row[2],
        "headline": row[3],
        "url": row[4],
        "is_price_sensitive": bool(row[5]),
        "classification": row[6],
        "raw": json.loads(row[7]) if row[7] else {},
    }


def _classify_item(item: dict) -> str:
    """Classify using API announcementTypes first, then headline."""
    types = " ".join(t.lower() for t in (item.get("announcementTypes") or []))
    if any(t in types for t in BUYBACK_ANNOUNCEMENT_TYPES):
        return "buyback"
    if any(
        t in types
        for t in (
            "change of director's interest notice",
            "initial director's interest notice",
            "final director's interest notice",
            "appendix 3y",
        )
    ):
        return "insider"
    return _headline_classify(item.get("headline", ""))


def _item_to_announcement(item: dict, since: datetime.date) -> Optional[Dict]:
    try:
        date_str = item.get("date", "")
        ann_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        if ann_date < since:
            return None

        classification = _classify_item(item)
        if classification == "other":
            return None

        ticker = item.get("symbol") or (item.get("companies") or [{}])[0].get("symbol", "UNKNOWN")
        doc_key = item.get("documentKey")
        return {
            "ticker": ticker,
            "date": ann_date.isoformat(),
            "headline": item.get("headline", ""),
            "is_price_sensitive": item.get("isPriceSensitive", False),
            "document_key": doc_key,
            "url": f"{PDF_BASE}{doc_key}" if doc_key else "",
            "raw": item,
            "classification": classification,
        }
    except Exception:
        return None


def fetch_market_announcements_paginated(
    days: int = 30,
    items_per_page: int = 100,
    max_pages: int = MAX_MARKET_PAGES,
    incremental: bool = False,
    proxies: Optional[Dict] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    """
    Paginated market-wide ingest. Upserts insider/buyback candidates into SQLite.
    incremental=True stops after ~30 pages or when a full page is already cached.
    """
    session = _get_session(proxies)
    since = (datetime.now() - timedelta(days=days)).date()
    known_keys = _known_document_keys(db_path)
    watermark_key = get_scrape_meta("newest_document_key", db_path) if incremental else ""

    relevant_rows = []
    pages_fetched = 0
    oldest_date = None
    newest_key = None
    stopped_reason = "max_pages"

    page_limit = INCREMENTAL_MAX_PAGES if incremental else max_pages

    for page in range(page_limit):
        try:
            url = f"{BASE_URL}/markets/announcements"
            params = {"page": page, "itemsPerPage": items_per_page}
            resp = _retry_request(session, url, params=params)
            items = resp.json().get("data", {}).get("items", [])
        except Exception as e:
            logger.error("Market fetch page %d failed: %s", page, e)
            stopped_reason = "error"
            break

        if not items:
            stopped_reason = "empty_page"
            break

        pages_fetched += 1
        page_new = 0
        hit_watermark = False
        page_dates = []

        for item in items:
            doc_key = item.get("documentKey")
            if doc_key and doc_key == watermark_key:
                hit_watermark = True

            if item.get("date"):
                try:
                    d = datetime.fromisoformat(
                        item["date"].replace("Z", "+00:00")
                    ).date()
                    page_dates.append(d)
                except Exception:
                    pass

            ann = _item_to_announcement(item, since)
            if not ann:
                continue

            if doc_key and doc_key not in known_keys:
                page_new += 1
                known_keys.add(doc_key)

            if not newest_key and ann.get("document_key"):
                newest_key = ann["document_key"]
            relevant_rows.append(ann)
            try:
                d = datetime.fromisoformat(ann["date"]).date()
                page_dates.append(d)
                if oldest_date is None or d < oldest_date:
                    oldest_date = d
            except Exception:
                pass

        if hit_watermark:
            stopped_reason = "watermark"
            break

        if incremental and page > 0 and page_new == 0:
            stopped_reason = "incremental_caught_up"
            break

        if page_dates and min(page_dates) < since:
            stopped_reason = "since_date"
            break

        time.sleep(RATE_LIMIT_SECONDS * 0.35)

    if relevant_rows:
        save_announcements_to_db(relevant_rows, db_path)

    now = datetime.now().isoformat()
    set_scrape_meta({
        "last_market_fetch_at": now,
        "last_oldest_date_seen": oldest_date.isoformat() if oldest_date else "",
        "last_pages_fetched": pages_fetched,
        "last_rows_upserted": len(relevant_rows),
        "newest_document_key": newest_key or get_scrape_meta("newest_document_key", db_path),
        "last_stopped_reason": stopped_reason,
    }, db_path)

    return {
        "ok": True,
        "pages_fetched": pages_fetched,
        "relevant_rows": len(relevant_rows),
        "oldest_date": oldest_date.isoformat() if oldest_date else None,
        "stopped_reason": stopped_reason,
        "incremental": incremental,
    }


def load_relevant_announcements_from_db(
    days: int = 365,
    db_path: str = DEFAULT_DB_PATH,
) -> List[Dict]:
    """Load insider/buyback announcements already classified in SQLite."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        """
        SELECT document_key, ticker, date, headline, url, is_price_sensitive, classification, raw_json
        FROM announcements
        WHERE date >= ?
          AND classification IN ('insider', 'insider_purchase', 'insider_sale', 'buyback')
        ORDER BY date DESC
        """,
        (since,),
    ).fetchall()
    conn.close()
    return [_announcement_from_db_row(r) for r in rows]


def fetch_market_announcements(
    days: int = 30,
    items_per_page: int = 200,
    proxies: Optional[Dict] = None,
    use_cache: bool = True,
    force_fetch: bool = False,
    incremental: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> List[Dict]:
    """
    Market-wide announcements. Read-only from SQLite when use_cache=True and not force_fetch.
    Otherwise paginated API ingest then reload from DB.
    """
    if use_cache and not force_fetch:
        return load_relevant_announcements_from_db(days=days, db_path=db_path)

    fetch_market_announcements_paginated(
        days=days,
        items_per_page=items_per_page,
        incremental=incremental,
        proxies=proxies,
        db_path=db_path,
    )
    return load_relevant_announcements_from_db(days=days, db_path=db_path)


# ====================== PDF TEXT EXTRACTION (Feature 1) ======================

def _download_and_extract_pdf_text(url: str, session: requests.Session, max_pages: int = 3) -> str:
    """Download PDF and extract text using pypdf."""
    if PdfReader is None:
        logger.warning("pypdf not installed. Skipping PDF text extraction.")
        return ""

    try:
        resp = session.get(url, timeout=25)
        resp.raise_for_status()
        from io import BytesIO
        reader = PdfReader(BytesIO(resp.content))
        text = ""
        for i, page in enumerate(reader.pages[:max_pages]):
            text += page.extract_text() or ""
        return text.lower()
    except Exception as e:
        logger.debug(f"PDF extraction failed: {e}")
        return ""


def _refine_with_pdf(ann: Dict, session: requests.Session) -> str:
    """
    Download the PDF and try to determine if it's a real purchase.
    Returns refined classification: 'insider_purchase', 'insider_sale', 'buyback', 'other'
    """
    if not ann.get("url"):
        return ann.get("classification", "other")

    headline = ann.get("headline", "")
    headline_l = headline.lower()
    if any(kw in headline_l for kw in SUBSTANTIAL_HOLDER_KEYWORDS):
        return "other"
    is_insider = (
        "insider" in ann.get("classification", "")
        or any(k in headline_l for k in DIRECTOR_INSIDER_KEYWORDS)
    )

    text = _download_and_extract_pdf_text(ann["url"], session)

    if not text:
        if is_insider:
            return _headline_refine_insider(headline)
        return ann.get("classification", "other")

    # Insider purchase vs sale
    if is_insider:
        purchase_score = sum(1 for w in PURCHASE_TEXT_SIGNS if w in text)
        sale_score = sum(1 for w in SALE_TEXT_SIGNS if w in text)
        if purchase_score > sale_score:
            return "insider_purchase"
        elif sale_score > purchase_score:
            return "insider_sale"
        return _headline_refine_insider(headline)

    if "buyback" in ann.get("classification", ""):
        return "buyback"

    return ann.get("classification", "other")


# ====================== HIGH LEVEL API ======================

def get_relevant_signals(
    tickers: List[str],
    days: int = 90,
    use_pdf: bool = True,
    use_cache: bool = True,
    proxies: Optional[Dict] = None,
    db_path: str = DEFAULT_DB_PATH
) -> Dict[str, List[Dict]]:
    """
    Main high-level function.
    Returns {ticker: [ {date, type, headline, url, classification}, ... ]}
    """
    session = _get_session(proxies)
    raw_anns = fetch_announcements_for_tickers(
        tickers, days=days, use_cache=use_cache, proxies=proxies, db_path=db_path
    )

    signals = {}
    for ann in raw_anns:
        classification = ann.get("classification", "other")

        # Optional deep PDF classification
        if use_pdf and classification in ("insider", "buyback"):
            refined = _refine_with_pdf(ann, session)
            ann["classification"] = refined
            classification = refined

        if classification in ("insider_purchase", "buyback"):
            t = ann["ticker"]
            if t not in signals:
                signals[t] = []
            signals[t].append({
                "date": ann["date"],
                "type": "insider" if "insider" in classification else "buyback",
                "headline": ann["headline"],
                "url": ann["url"],
                "classification": classification
            })

    # Sort newest first
    for t in signals:
        signals[t].sort(key=lambda x: x["date"], reverse=True)

    return signals


def get_relevant_market_signals(
    days: int = 180,
    use_pdf: bool = True,
    use_cache: bool = True,
    proxies: Optional[Dict] = None,
    db_path: str = DEFAULT_DB_PATH
) -> Dict[str, List[Dict]]:
    """
    Market-wide discovery version of get_relevant_signals (for dynamic/full ASX list).
    Uses fetch_market_announcements (single endpoint) + optional PDF refinement.
    Returns {base_ticker: [ {date, type:'insider'|'buyback', headline, url, classification}, ... ]}
    Only includes tickers that have at least one qualifying (confirmed purchase or buyback) signal.
    """
    raw_anns = fetch_market_announcements(
        days=days,
        items_per_page=100,
        use_cache=use_cache,
        proxies=proxies,
        db_path=db_path,
    )

    signals: Dict[str, List[Dict]] = {}
    for ann in raw_anns:
        t = (ann.get("ticker") or "").upper().replace(".AX", "").strip()
        if not t or t == "UNKNOWN":
            continue
        classification = ann.get("classification", "other")
        if classification == "insider_purchase":
            sig_type = "insider"
        elif classification == "buyback":
            sig_type = "buyback"
        else:
            continue
        if t not in signals:
            signals[t] = []
        signals[t].append({
            "date": ann["date"],
            "type": sig_type,
            "headline": ann["headline"],
            "url": ann.get("url"),
            "classification": classification,
        })

    # Sort newest first per ticker
    for t in signals:
        signals[t].sort(key=lambda x: x["date"], reverse=True)

    return signals


def load_insider_announcements_from_db(
    days: int = 365,
    classifications: tuple = ("insider", "insider_purchase", "insider_sale"),
    db_path: str = DEFAULT_DB_PATH,
) -> List[Dict]:
    """Load insider-related announcements from SQLite cache."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    placeholders = ",".join("?" * len(classifications))
    rows = conn.execute(
        f"""
        SELECT document_key, ticker, date, headline, url, is_price_sensitive, classification, raw_json
        FROM announcements
        WHERE date >= ? AND classification IN ({placeholders})
        ORDER BY date DESC
        """,
        [since, *classifications],
    ).fetchall()
    conn.close()
    return [_announcement_from_db_row(r) for r in rows]


def count_insider_purchases_in_db(
    days: int = 365,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM announcements WHERE date >= ? AND classification = 'insider_purchase'",
        (since,),
    ).fetchone()[0]
    conn.close()
    return int(n)


def count_buybacks_in_db(
    days: int = 365,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM announcements WHERE date >= ? AND classification = 'buyback'",
        (since,),
    ).fetchone()[0]
    conn.close()
    return int(n)


def _refine_unclassified_insider_rows(
    days: int = 365,
    use_pdf: bool = True,
    max_pdf_refine: int = 500,
    db_path: str = DEFAULT_DB_PATH,
    session: requests.Session = None,
) -> Dict[str, int]:
    """PDF-refine headline-only 'insider' rows in SQLite."""
    session = session or _get_session()
    candidates = load_insider_announcements_from_db(days=days, db_path=db_path)
    to_refine = [a for a in candidates if a.get("classification") == "insider"]
    if use_pdf and PdfReader is None:
        logger.warning("pypdf not installed — insider headlines will not be PDF-refined")

    refined_purchase = refined_sale = refined_other = pdf_checked = 0
    for ann in to_refine[:max_pdf_refine]:
        new_cls = (
            _refine_with_pdf(ann, session)
            if use_pdf
            else _headline_refine_insider(ann.get("headline", ""))
        )
        pdf_checked += 1
        if new_cls != ann.get("classification"):
            ann["classification"] = new_cls
            save_announcements_to_db([ann], db_path)
        if new_cls == "insider_purchase":
            refined_purchase += 1
        elif new_cls == "insider_sale":
            refined_sale += 1
        else:
            refined_other += 1
        if use_pdf:
            time.sleep(RATE_LIMIT_SECONDS * 0.5)

    return {
        "pdf_checked": pdf_checked,
        "refined_purchase": refined_purchase,
        "refined_sale": refined_sale,
        "refined_other": refined_other,
        "candidates": len(candidates),
    }


def refresh_asx_announcements(
    days: int = 365,
    use_pdf: bool = True,
    incremental: bool = False,
    backfill: bool = False,
    max_pdf_refine: int = 500,
    db_path: str = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    """
    Unified ASX ingest: paginated market fetch, PDF-refine insider notices, persist to SQLite.
    incremental=True for daily runs; backfill=True paginates full API window (~2-3 weeks).
    """
    init_db(db_path)
    try:
        ingest = fetch_market_announcements_paginated(
            days=days,
            items_per_page=100,
            max_pages=MAX_MARKET_PAGES if backfill else INCREMENTAL_MAX_PAGES,
            incremental=incremental and not backfill,
            db_path=db_path,
        )
    except Exception as e:
        logger.error("ASX market ingest failed: %s", e)
        return {"ok": False, "error": str(e)}

    refine = _refine_unclassified_insider_rows(
        days=days,
        use_pdf=use_pdf,
        max_pdf_refine=max_pdf_refine,
        db_path=db_path,
    )

    return {
        "ok": True,
        "days": days,
        "incremental": incremental and not backfill,
        "backfill": backfill,
        "ingest": ingest,
        "refine": refine,
        "total_purchases": count_insider_purchases_in_db(days=days, db_path=db_path),
        "total_buybacks": count_buybacks_in_db(days=days, db_path=db_path),
        "last_market_fetch_at": get_scrape_meta("last_market_fetch_at", db_path),
        "db_path": db_path,
    }


def refresh_insider_purchases(
    days: int = 365,
    items_per_page: int = 500,
    use_pdf: bool = True,
    force_fetch: bool = True,
    max_pdf_refine: int = 150,
    db_path: str = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    """Backward-compatible alias — use refresh_asx_announcements instead."""
    return refresh_asx_announcements(
        days=days,
        use_pdf=use_pdf,
        incremental=not force_fetch,
        backfill=force_fetch,
        max_pdf_refine=max_pdf_refine,
        db_path=db_path,
    )


def enrich_asx_dashboard_data(
    existing_stocks: List[Dict],
    days: int = 90,
    use_pdf: bool = True,
    use_cache: bool = True,
    proxies: Optional[Dict] = None,
    db_path: str = DEFAULT_DB_PATH
) -> List[Dict]:
    """
    Enriches your existing ASX_STOCKS list with real data from the scraper.
    Compatible with the dashboard's data model.
    """
    tickers = [s["ticker"].replace(".AX", "") for s in existing_stocks]
    signals = get_relevant_signals(
        tickers, days=days, use_pdf=use_pdf, use_cache=use_cache,
        proxies=proxies, db_path=db_path
    )

    for stock in existing_stocks:
        t = stock["ticker"].replace(".AX", "").upper()
        real_signals = signals.get(t, [])

        insider_purchases = [s for s in real_signals if s["type"] == "insider"]
        buybacks = [s for s in real_signals if s["type"] == "buyback"]

        if real_signals:
            stock["insider_buys_2026"] = len(insider_purchases)
            stock["buyback_announced"] = len(buybacks) > 0
            stock["signals"] = real_signals[:8]
            if real_signals:
                stock["last_activity"] = max(s["date"] for s in real_signals)

    return existing_stocks


# ====================== CLI / EXAMPLE ======================

if __name__ == "__main__":
    print("=== ASX Scraper Demo (with all 4 features) ===\n")

    test_tickers = ["NAB", "BHP", "FMG"]

    # Feature 3 + 4 demo (cache + proxies placeholder)
    print("Fetching with caching + proxy support (proxies=None)...")
    signals = get_relevant_signals(
        test_tickers,
        days=120,
        use_pdf=True,           # Feature 1
        use_cache=True,
        proxies=None            # Feature 4
    )

    for ticker, items in signals.items():
        print(f"\n{ticker}: {len(items)} relevant signals")
        for s in items[:3]:
            print(f"  {s['date']} | {s['type']:8} | {s['classification']:18} | {s['headline'][:70]}")

    # Feature 2 demo
    print("\n\n--- Broader market search (last 14 days) ---")
    market = fetch_market_announcements(days=14, items_per_page=50, use_cache=True)
    print(f"Found {len(market)} relevant market-wide announcements")
    for m in market[:5]:
        print(f"  {m['ticker']:6} {m['date']} | {m['classification']:18} | {m['headline'][:65]}")

    print("\nDemo complete. Database:", DEFAULT_DB_PATH)