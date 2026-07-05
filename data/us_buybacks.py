"""
US buyback announcements via SEC EDGAR (Russell 1000 + Nasdaq-100 + S&P 500).

Scans recent 8-K and 10-Q filings for share-repurchase keywords, caches matches in
SQLite (shared universe DB). 10-Q filings parse the Issuer Purchases table for
shares and average price per period.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from config.identity import sec_user_agent
from data.asx import DEFAULT_UNIVERSE_DB
from data.edgar_metrics import EDGAR_FETCH_SLEEP_SECONDS, _ensure_identity

logger = logging.getLogger(__name__)
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SCAN_FORMS = ("8-K", "10-Q")
BUYBACK_KEYWORDS_8K = (
    "share repurchase",
    "stock repurchase",
    "repurchase program",
    "repurchase authorization",
    "repurchase plan",
    "stock buyback",
    "share buyback",
    "buy-back",
    "buyback",
)
BUYBACK_KEYWORDS_10Q = (
    "issuer purchases of equity",
    "purchases of equity securities",
    "share repurchases",
    "stock repurchases",
    "repurchase program",
    "shares repurchased",
    "stock repurchased",
)

_PERIOD_RANGE_RE = re.compile(
    r"(?P<start>[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*(?:[-–—]|to)\s*"
    r"(?P<end>[A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"\$?\s*([\d,]+\.\d{2,4})")
_SHARES_RE = re.compile(r"^[\d,]+$")
_SKIP_ROW_LABELS = ("total", "as of", "period", "periods", "fiscal year")

# Plausibility caps for monthly 10-Q repurchase rows (guards misparsed $/scale columns).
_MAX_SHARES_PER_PERIOD = 150_000_000
_MAX_DOLLARS_PER_PERIOD = 30_000_000_000.0
_MAX_FRAC_OUTSTANDING_PERIOD = 0.25

_cik_map_cache: dict[str, dict] | None = None


def normalize_repurchase_shares(
    raw_shares,
    *,
    price: float | None = None,
    shares_outstanding: float | None = None,
    table_multiplier: int = 1,
) -> int | None:
    """
    Normalize share counts from 10-Q tables. Footnotes like "in millions" usually
    refer to dollar columns; applying ×1M to a share cell produces trillion-share bugs.
    """
    try:
        raw = int(float(raw_shares))
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None

    scaled = raw * max(1, int(table_multiplier))
    candidates: list[int] = []
    seen: set[int] = set()
    for val in (scaled, raw, scaled // 1000, scaled // 1_000_000, scaled // 1_000_000_000):
        if val is None:
            continue
        try:
            iv = int(val)
        except (TypeError, ValueError):
            continue
        if iv > 0 and iv not in seen:
            seen.add(iv)
            candidates.append(iv)

    def _plausible(shares: int) -> bool:
        if shares < 1 or shares > _MAX_SHARES_PER_PERIOD:
            return False
        if shares_outstanding and shares_outstanding > 0:
            if shares > shares_outstanding * _MAX_FRAC_OUTSTANDING_PERIOD:
                return False
        if price and price > 0:
            if shares * price > _MAX_DOLLARS_PER_PERIOD:
                return False
        return True

    good = [c for c in candidates if _plausible(c)]
    if not good:
        logger.debug(
            "Rejected implausible repurchase shares raw=%s mult=%s price=%s",
            raw,
            table_multiplier,
            price,
        )
        return None
    return max(good)


def _conn(db_path: str = DEFAULT_UNIVERSE_DB):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_buyback_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            yf_symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            form TEXT NOT NULL,
            accession TEXT NOT NULL UNIQUE,
            headline TEXT,
            url TEXT,
            source TEXT NOT NULL DEFAULT 'SEC',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_buyback_repurchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            yf_symbol TEXT NOT NULL,
            period_end TEXT NOT NULL,
            period_label TEXT,
            shares INTEGER,
            price REAL,
            form TEXT NOT NULL,
            accession TEXT NOT NULL,
            filing_date TEXT NOT NULL,
            headline TEXT,
            url TEXT,
            source TEXT NOT NULL DEFAULT 'SEC',
            created_at TEXT NOT NULL,
            UNIQUE(accession, period_label)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_buyback_scan_log (
            accession TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            matched INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_buybacks_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_bb_ticker ON us_buyback_events(yf_symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_bb_date ON us_buyback_events(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_bb_rep_ticker ON us_buyback_repurchases(yf_symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_bb_rep_date ON us_buyback_repurchases(period_end)")
    conn.commit()
    return conn


def _meta_get(key: str, db_path: str = DEFAULT_UNIVERSE_DB) -> str:
    conn = _conn(db_path)
    row = conn.execute("SELECT value FROM us_buybacks_meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else ""


def _meta_set(pairs: dict, db_path: str = DEFAULT_UNIVERSE_DB):
    conn = _conn(db_path)
    for k, v in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO us_buybacks_meta (key, value) VALUES (?,?)",
            (k, str(v)),
        )
    conn.commit()
    conn.close()


def get_us_buybacks_meta(db_path: str = DEFAULT_UNIVERSE_DB) -> dict:
    return {
        "last_fetched": _meta_get("last_fetched", db_path),
        "last_scan_tickers": int(_meta_get("last_scan_tickers", db_path) or 0),
        "event_count": count_us_buyback_events(db_path),
        "repurchase_rows": count_us_buyback_repurchases(db_path),
    }


def count_us_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = _conn(db_path)
        n = conn.execute("SELECT COUNT(*) FROM us_buyback_events").fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def count_us_buyback_repurchases(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    try:
        conn = _conn(db_path)
        n = conn.execute("SELECT COUNT(*) FROM us_buyback_repurchases").fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def load_sec_ticker_cik_map(force: bool = False) -> dict[str, dict]:
    """ticker -> {cik, title} from SEC company_tickers.json (cached in-memory)."""
    global _cik_map_cache
    if _cik_map_cache and not force:
        return _cik_map_cache

    headers = {"User-Agent": sec_user_agent()}
    try:
        resp = requests.get(COMPANY_TICKERS_URL, headers=headers, timeout=45)
        resp.raise_for_status()
        raw = resp.json()
        out = {}
        for entry in raw.values():
            ticker = str(entry.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            out[ticker] = {
                "cik": int(entry["cik_str"]),
                "title": str(entry.get("title") or ticker),
            }
        _cik_map_cache = out
        return out
    except Exception as e:
        logger.warning("SEC company_tickers.json load failed: %s", e)
        return _cik_map_cache or {}


def _yf_symbol(ticker: str) -> str:
    return (ticker or "").strip().upper().replace(".", "-")


def _sec_headers() -> dict:
    return {"User-Agent": sec_user_agent(), "Accept": "application/json"}


def _fetch_submissions(cik: int) -> dict:
    url = SUBMISSIONS_URL.format(cik=cik)
    resp = requests.get(url, headers=_sec_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _recent_filings(submissions: dict, forms: tuple, cutoff: datetime.date) -> list[dict]:
    recent = submissions.get("filings", {}).get("recent") or {}
    names = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []
    primary = recent.get("primaryDocument") or []
    out = []
    for i, form in enumerate(names):
        if form not in forms:
            continue
        fdate = dates[i] if i < len(dates) else ""
        if not fdate or fdate < cutoff.isoformat():
            continue
        acc = accessions[i] if i < len(accessions) else ""
        if not acc:
            continue
        out.append({
            "form": form,
            "filing_date": fdate,
            "accession": acc,
            "primary_document": primary[i] if i < len(primary) else "",
        })
    return out


def _already_scanned(accession: str, db_path: str) -> bool:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT 1 FROM us_buyback_scan_log WHERE accession=?", (accession,)
    ).fetchone()
    conn.close()
    return bool(row)


def _repurchases_for_accession(accession: str, db_path: str) -> int:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT COUNT(*) FROM us_buyback_repurchases WHERE accession=?", (accession,)
    ).fetchone()
    conn.close()
    return int(row[0] or 0)


def _log_scan(accession: str, ticker: str, matched: bool, db_path: str):
    conn = _conn(db_path)
    conn.execute(
        """INSERT OR REPLACE INTO us_buyback_scan_log (accession, ticker, scanned_at, matched)
           VALUES (?,?,?,?)""",
        (accession, ticker, datetime.now().isoformat(), 1 if matched else 0),
    )
    conn.commit()
    conn.close()


def _parse_us_date(label: str) -> str | None:
    label = (label or "").strip().rstrip(":")
    m = _PERIOD_RANGE_RE.search(label)
    if not m:
        return None
    try:
        end = datetime.strptime(m.group("end").strip(), "%B %d, %Y")
        return end.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_shares_cell(val: str) -> int | None:
    raw = (val or "").strip()
    if not raw or raw in ("—", "-", "–", "N/A", "(1)"):
        return None
    if not _SHARES_RE.match(raw.replace(" ", "")):
        return None
    try:
        n = int(raw.replace(",", ""))
        return n if n > 0 else None
    except ValueError:
        return None


def _parse_price_from_cells(cells: list[str]) -> float | None:
    for cell in cells:
        cell = (cell or "").strip()
        if not cell or cell in ("(1)", "—", "-"):
            continue
        m = _PRICE_RE.search(cell)
        if m:
            try:
                p = float(m.group(1).replace(",", ""))
                if 0.01 < p < 1_000_000:
                    return round(p, 4)
            except ValueError:
                continue
    return None


def _clean_cells(row) -> list[str]:
    return [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]


def _is_repurchase_table(table) -> bool:
    txt = table.get_text(" ", strip=True).lower()
    if "issuer purchases" in txt and "average price" in txt:
        return True
    if "total number of shares purchased" in txt and "average price paid" in txt:
        return True
    if "purchases of equity securities" in txt and "average price" in txt:
        return True
    return False


def _shares_multiplier(table, section_text: str, filing_text: str = "") -> int:
    """
    Scale share counts when the filing states share figures are in thousands.
    Do NOT apply 'in millions' to share columns — that footnote almost always
    refers to dollar amounts (e.g. TMUS), which caused trillion-share errors.
    """
    ctx = " ".join([
        (section_text or "").lower(),
        (filing_text or "")[:8000].lower(),
    ])
    near = table.get_text(" ", strip=True).lower()
    share_ctx = "share" in near or "shares purchased" in near or "shares" in ctx
    if share_ctx and (
        "in thousands" in ctx
        or "reflected in thousands" in ctx
        or "which are in thousands" in ctx
        or "in thousands" in near
    ):
        return 1000
    if (
        "in thousands" in near
        and "total number of shares" in near
    ):
        return 1000
    return 1


def _should_skip_period_label(label: str) -> bool:
    low = (label or "").strip().lower().rstrip(":")
    if not low:
        return True
    if any(low.startswith(s) for s in _SKIP_ROW_LABELS):
        return True
    if low.startswith("total amount"):
        return True
    return False


def _parse_repurchase_table(
    table,
    section_text: str = "",
    filing_text: str = "",
    shares_outstanding: float | None = None,
) -> list[dict]:
    """Parse SEC Item 2 repurchase table rows from HTML."""
    rows_out = []
    multiplier = _shares_multiplier(table, section_text, filing_text)
    trs = table.find_all("tr")
    if not trs:
        return rows_out

    current_period_label = None
    current_period_end = None

    for tr in trs:
        cells = _clean_cells(tr)
        cells = [c for c in cells if c is not None]
        if not cells or all(not c for c in cells):
            continue

        joined = " ".join(cells).lower()
        if "total number" in joined and "average price" in joined and "period" in joined:
            continue

        label = cells[0].strip()
        period_match = _PERIOD_RANGE_RE.search(label)

        if period_match:
            current_period_label = label.rstrip(":")
            current_period_end = _parse_us_date(current_period_label)
            shares = None
            for cell in cells[1:6]:
                shares = _parse_shares_cell(cell)
                if shares is not None:
                    break
            price = _parse_price_from_cells(cells[1:])
            norm = normalize_repurchase_shares(
                shares,
                price=price,
                shares_outstanding=shares_outstanding,
                table_multiplier=multiplier,
            )
            if norm is not None and current_period_end:
                rows_out.append({
                    "period_label": current_period_label,
                    "period_end": current_period_end,
                    "shares": norm,
                    "price": price,
                })
            continue

        if _should_skip_period_label(label):
            continue

        if label.lower().startswith("open market") or "privately negotiated" in label.lower():
            if not current_period_end:
                continue
            shares = None
            for cell in cells[1:8]:
                shares = _parse_shares_cell(cell)
                if shares is not None:
                    break
            price = _parse_price_from_cells(cells[1:])
            norm = normalize_repurchase_shares(
                shares,
                price=price,
                shares_outstanding=shares_outstanding,
                table_multiplier=multiplier,
            )
            if norm is not None:
                rows_out.append({
                    "period_label": current_period_label or label,
                    "period_end": current_period_end,
                    "shares": norm,
                    "price": price,
                })
            continue

        if period_match is None and _PERIOD_RANGE_RE.search(label):
            pass

    return rows_out


def _section_context(html: str) -> str:
    low = (html or "").lower()
    for kw in (
        "issuer purchases of equity securities",
        "purchases of equity securities by the issuer",
        "share repurchase activity",
    ):
        idx = low.find(kw)
        if idx >= 0:
            return html[max(0, idx - 200): idx + 1200]
    return html[:2000] if html else ""


def parse_10q_repurchases(
    html: str,
    text: str = "",
    shares_outstanding: float | None = None,
) -> list[dict]:
    """Extract monthly/period repurchase rows from a 10-Q HTML filing."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    section = _section_context(html)
    all_rows = []
    seen = set()

    for table in soup.find_all("table"):
        if not _is_repurchase_table(table):
            continue
        for row in _parse_repurchase_table(table, section, text, shares_outstanding):
            key = (row.get("period_label"), row.get("period_end"), row.get("shares"))
            if key in seen:
                continue
            seen.add(key)
            if row.get("shares"):
                all_rows.append(row)

    if not all_rows and text:
        all_rows = _parse_repurchases_from_text(text, shares_outstanding=shares_outstanding)

    return all_rows


def _parse_repurchases_from_text(text: str, shares_outstanding: float | None = None) -> list[dict]:
    """Fallback text-line parser for markdown-style 10-Q tables."""
    rows = []
    if not text:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("─"):
            continue
        m = _PERIOD_RANGE_RE.search(line)
        if not m:
            continue
        label = m.group(0)
        if any(label.lower().startswith(s) for s in _SKIP_ROW_LABELS):
            continue
        period_end = _parse_us_date(label)
        if not period_end:
            continue
        nums = re.findall(r"[\d,]+(?:\.\d{2})?", line[m.end():])
        shares = None
        price = None
        for n in nums:
            if "." in n:
                p = float(n.replace(",", ""))
                if 0.5 < p < 500_000 and price is None:
                    price = p
            else:
                s = int(n.replace(",", ""))
                if s > 0 and shares is None:
                    shares = s
        norm = normalize_repurchase_shares(shares, price=price, shares_outstanding=shares_outstanding)
        if norm:
            rows.append({
                "period_label": label,
                "period_end": period_end,
                "shares": norm,
                "price": price,
            })
    return rows


def _matches_buyback(text: str, form: str) -> bool:
    low = (text or "").lower()
    if not low:
        return False
    if form == "8-K":
        return any(k in low for k in BUYBACK_KEYWORDS_8K)
    return any(k in low for k in BUYBACK_KEYWORDS_10Q)


def _headline_from_text(text: str, form: str, company: str) -> str:
    low = (text or "").lower()
    keys = BUYBACK_KEYWORDS_8K if form == "8-K" else BUYBACK_KEYWORDS_10Q
    for k in keys:
        i = low.find(k)
        if i >= 0:
            snippet = re.sub(r"\s+", " ", text[max(0, i - 40): i + 140]).strip()
            if snippet:
                return f"SEC {form}: {snippet[:200]}"
    return f"SEC {form}: {company} share repurchase disclosure"


def _repurchase_headline(period_label: str, shares: int, price: float | None) -> str:
    parts = [f"SEC 10-Q: {period_label}"]
    if shares:
        parts.append(f"{shares:,} shares")
    if price:
        parts.append(f"@ ${price:.2f}")
    return " — ".join(parts)


def _filing_url(cik: int, accession: str, primary_doc: str = "") -> str:
    acc_nodash = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}"
    if primary_doc:
        return f"{base}/{primary_doc}"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={accession}&dateb=&owner=include&count=40"


def _upsert_event(
    ticker: str,
    yf_symbol: str,
    filing_date: str,
    form: str,
    accession: str,
    headline: str,
    url: str,
    db_path: str,
):
    conn = _conn(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO us_buyback_events
           (ticker, yf_symbol, date, form, accession, headline, url, source, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            ticker,
            yf_symbol,
            filing_date,
            form,
            accession,
            headline,
            url,
            "SEC",
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _replace_repurchases(
    ticker: str,
    yf_symbol: str,
    filing_date: str,
    accession: str,
    url: str,
    rows: list[dict],
    db_path: str,
) -> int:
    conn = _conn(db_path)
    conn.execute("DELETE FROM us_buyback_repurchases WHERE accession=?", (accession,))
    inserted = 0
    now = datetime.now().isoformat()
    for row in rows:
        period_end = row.get("period_end")
        shares = row.get("shares")
        if not period_end or not shares:
            continue
        period_label = row.get("period_label") or period_end
        price = row.get("price")
        headline = _repurchase_headline(period_label, int(shares), price)
        cur = conn.execute(
            """INSERT OR REPLACE INTO us_buyback_repurchases
               (ticker, yf_symbol, period_end, period_label, shares, price,
                form, accession, filing_date, headline, url, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ticker,
                yf_symbol,
                period_end,
                period_label,
                int(shares),
                price,
                "10-Q",
                accession,
                filing_date,
                headline,
                url,
                "SEC",
                now,
            ),
        )
        if cur.rowcount:
            inserted += 1
    conn.commit()
    conn.close()
    return inserted


def _fetch_filing_content(cik: int, company: str, form: str, filing_date: str, accession: str) -> tuple[str, str]:
    from edgar import Filing

    filing = Filing(
        cik=cik,
        company=company,
        form=form,
        filing_date=filing_date,
        accession_no=accession,
    )
    text = filing.text() or ""
    html = filing.html() or ""
    return text, html


def _shares_outstanding_from_yf_cached(yf_symbol: str, yf_cache: dict | None = None) -> float:
    try:
        from data.business_lists import load_yf_cached

        yd = (yf_cache or {}).get(yf_symbol) or load_yf_cached([yf_symbol]).get(yf_symbol, {})
        metrics = yd.get("metrics") or {}
        mcap = float(metrics.get("market_cap") or 0)
        price = float(yd.get("price") or 0)
        if mcap > 0 and price > 0:
            return mcap / price
    except Exception:
        pass
    return 0.0


def load_us_buyback_repurchases(db_path: str = DEFAULT_UNIVERSE_DB) -> list[dict]:
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            """
            SELECT ticker, yf_symbol, period_end, period_label, shares, price,
                   form, accession, filing_date, headline, url, source
            FROM us_buyback_repurchases
            ORDER BY period_end DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("US buyback repurchases load failed: %s", e)
        return []

    symbols = sorted({r[1] for r in rows})
    yf_cache = {}
    if symbols:
        try:
            from data.business_lists import load_yf_cached
            yf_cache = load_yf_cached(symbols)
        except Exception:
            pass

    out = []
    for ticker, yf, period_end, period_label, shares, price, form, accession, filing_date, headline, url, source in rows:
        pr = float(price) if price is not None else None
        so = _shares_outstanding_from_yf_cached(yf, yf_cache)
        norm = normalize_repurchase_shares(
            shares,
            price=pr,
            shares_outstanding=so if so > 0 else None,
            table_multiplier=1,
        )
        if not norm:
            continue
        out.append({
            "yf_symbol": yf,
            "ticker": ticker,
            "date": period_end,
            "period_end": period_end,
            "period_label": period_label,
            "form": form,
            "headline": headline or "",
            "url": url or "",
            "source": source or "SEC",
            "accession": accession,
            "filing_date": filing_date,
            "shares_purchased": norm,
            "shares": norm,
            "price": pr,
        })
    return out


def load_us_buyback_events(db_path: str = DEFAULT_UNIVERSE_DB) -> list[dict]:
    """Filing-level 8-K events plus parsed 10-Q repurchase rows (for registry + charts)."""
    events = []

    try:
        conn = _conn(db_path)
        filing_rows = conn.execute(
            """
            SELECT ticker, yf_symbol, date, form, accession, headline, url, source
            FROM us_buyback_events
            ORDER BY date DESC
            """
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("US buyback load failed: %s", e)
        filing_rows = []

    for ticker, yf, date, form, accession, headline, url, source in filing_rows:
        events.append({
            "yf_symbol": yf,
            "ticker": ticker,
            "date": date,
            "form": form,
            "headline": headline or "",
            "url": url or "",
            "source": source or "SEC",
            "accession": accession,
            "shares_purchased": None,
            "shares": None,
            "price": None,
        })

    for row in load_us_buyback_repurchases(db_path):
        events.append({
            "yf_symbol": row["yf_symbol"],
            "ticker": row["ticker"],
            "date": row["date"],
            "form": row.get("form") or "10-Q",
            "headline": row.get("headline") or "",
            "url": row.get("url") or "",
            "source": row.get("source") or "SEC",
            "accession": row.get("accession"),
            "shares_purchased": row.get("shares_purchased"),
            "shares": row.get("shares"),
            "price": row.get("price"),
        })

    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    return events


def clear_10q_scan_log(db_path: str = DEFAULT_UNIVERSE_DB) -> int:
    """Allow re-parsing 10-Q filings (e.g. after parser upgrade)."""
    conn = _conn(db_path)
    cur = conn.execute(
        """
        DELETE FROM us_buyback_scan_log
        WHERE accession IN (SELECT accession FROM us_buyback_events WHERE form='10-Q')
        OR accession IN (SELECT accession FROM us_buyback_repurchases)
        """
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def refresh_us_buybacks(
    tickers: list[str] | None = None,
    days_back: int = 365,
    sleep_seconds: float | None = None,
    db_path: str = DEFAULT_UNIVERSE_DB,
    max_tickers: int | None = None,
    reparse_10q: bool = False,
    on_progress=None,
) -> dict:
    """
    Scan US large-cap universe SEC filings for buyback-related disclosures.
    """
    from data.us_universe import load_us_scan_universe

    _ensure_identity()
    if reparse_10q:
        clear_10q_scan_log(db_path)

    sleep = sleep_seconds if sleep_seconds is not None else EDGAR_FETCH_SLEEP_SECONDS
    universe = tickers or load_us_scan_universe()
    if max_tickers:
        universe = universe[: max(1, int(max_tickers))]

    cik_map = load_sec_ticker_cik_map()
    cutoff = (datetime.now() - timedelta(days=max(1, days_back))).date()
    yf_batch = {}
    try:
        from data.business_lists import load_yf_cached
        yf_batch = load_yf_cached(universe)
    except Exception:
        pass

    scanned_filings = 0
    new_events = 0
    new_repurchase_rows = 0
    tickers_scanned = 0
    tickers_missing = 0
    errors = 0

    def _report():
        if on_progress:
            on_progress(
                phase="buybacks",
                tickers_done=tickers_scanned,
                tickers_total=len(universe),
                current_ticker=sym if tickers_scanned else "",
                filings_scanned=scanned_filings,
                new_events=new_events,
                new_repurchase_rows=new_repurchase_rows,
                errors=errors,
            )

    for ticker in universe:
        sym = _yf_symbol(ticker)
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
            candidates = _recent_filings(submissions, SCAN_FORMS, cutoff)
        except Exception as e:
            logger.warning("US buybacks submissions failed for %s: %s", sym, e)
            errors += 1
            time.sleep(sleep)
            continue

        for cand in candidates:
            acc = cand["accession"]
            form = cand["form"]
            if _already_scanned(acc, db_path):
                if form != "10-Q" or _repurchases_for_accession(acc, db_path) > 0:
                    continue

            scanned_filings += 1
            matched = False
            try:
                text, html = _fetch_filing_content(
                    cik,
                    company,
                    form,
                    cand["filing_date"],
                    acc,
                )
                url = _filing_url(cik, acc, cand.get("primary_document") or "")

                if form == "10-Q":
                    shares_out = _shares_outstanding_from_yf_cached(sym, yf_batch)
                    rep_rows = parse_10q_repurchases(
                        html,
                        text,
                        shares_outstanding=shares_out if shares_out > 0 else None,
                    )
                    if rep_rows:
                        matched = True
                        n = _replace_repurchases(
                            sym, sym, cand["filing_date"], acc, url, rep_rows, db_path
                        )
                        new_repurchase_rows += n
                        summary = _headline_from_text(text, form, company)
                        _upsert_event(sym, sym, cand["filing_date"], form, acc, summary, url, db_path)
                        new_events += 1
                    elif _matches_buyback(text, form):
                        matched = True
                        headline = _headline_from_text(text, form, company)
                        _upsert_event(sym, sym, cand["filing_date"], form, acc, headline, url, db_path)
                        new_events += 1
                elif _matches_buyback(text, form):
                    matched = True
                    headline = _headline_from_text(text, form, company)
                    _upsert_event(sym, sym, cand["filing_date"], form, acc, headline, url, db_path)
                    new_events += 1
            except Exception as e:
                logger.debug("US buyback filing scan failed %s %s: %s", sym, acc, e)
                errors += 1
            finally:
                _log_scan(acc, sym, matched, db_path)
            time.sleep(sleep)

        time.sleep(sleep * 0.5)

    now = datetime.now().isoformat()
    _meta_set({
        "last_fetched": now,
        "last_scan_tickers": tickers_scanned,
        "event_count": count_us_buyback_events(db_path),
        "repurchase_rows": count_us_buyback_repurchases(db_path),
    }, db_path)

    result = {
        "tickers_requested": len(universe),
        "tickers_scanned": tickers_scanned,
        "tickers_missing_cik": tickers_missing,
        "filings_scanned": scanned_filings,
        "new_events": new_events,
        "new_repurchase_rows": new_repurchase_rows,
        "total_events": count_us_buyback_events(db_path),
        "total_repurchase_rows": count_us_buyback_repurchases(db_path),
        "errors": errors,
        "last_fetched": now,
    }
    logger.info("US buybacks refresh: %s", result)
    return result