"""
Multi-period unit cost history for commodity producers.

Sources:
  - ASX: IR/ASX quarterly PDF URLs (seed list + company announcements)
  - US: recent 10-Q / 10-K / 8-K Item 2.02 text via edgartools

Stores points in SQLite `mining_unit_cost_history` and feeds the Commodities chart.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml

from data.yf_helpers import get_fx_rate, safe_float

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(_PROJECT_ROOT / "asx_announcements_cache.db")
CONFIG_PATH = _PROJECT_ROOT / "config" / "commodity_producers.yaml"
ASX_UNIVERSE = _PROJECT_ROOT / "config" / "asx_mining_universe.yaml"

# Seed quarterly / results PDFs (expand over time)
# period_end: ISO date for the reporting period end (used as series x-axis)
DEFAULT_SEED_REPORTS: dict[str, list[dict]] = {
    "NHC.AX": [
        {
            "period_end": "2026-04-30",
            "url": "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/2924-03090876-2A1672403",
            "label": "Q3 FY26 Quarterly Activities",
        },
        {
            "period_end": "2026-01-31",
            "url": "https://newhopegroup.com.au/wp-content/uploads/2026/02/Quarterly-Activities-Report-31-January-2026.pdf",
            "label": "Q2 FY26 Quarterly Activities",
        },
        {
            "period_end": "2025-10-31",
            "url": "https://newhopegroup.com.au/wp-content/uploads/2025/11/Quarterly-Activities-Report-31-October-2025.pdf",
            "label": "Q1 FY26 Quarterly Activities",
        },
        {
            "period_end": "2025-07-31",
            "url": "https://newhopegroup.com.au/wp-content/uploads/2025/08/Quarterly-Activities-Report-31-July-2025.pdf",
            "label": "Q4 FY25 Quarterly Activities",
        },
        {
            "period_end": "2025-01-31",
            "url": "https://newhopegroup.com.au/wp-content/uploads/2025/02/NHC_Quarterly_Activities_Report-31-January-2025.pdf",
            "label": "Q2 FY25 Quarterly Activities",
        },
    ],
    "WHC.AX": [
        {
            "period_end": "2026-03-31",
            "url": "https://whitehavencoal.com.au/wp-content/uploads/2026/04/WHC_March_2026_Quarterly_Report.pdf",
            "label": "Mar 2026 Quarterly",
        },
        {
            "period_end": "2025-12-31",
            "url": "https://whitehavencoal.com.au/wp-content/uploads/2026/01/WHC_December_2025_Quarterly_Report.pdf",
            "label": "Dec 2025 Quarterly",
        },
        {
            "period_end": "2025-09-30",
            "url": "https://whitehavencoal.com.au/wp-content/uploads/2025/10/WHC_September_2025_Quarterly_Report.pdf",
            "label": "Sep 2025 Quarterly",
        },
        {
            "period_end": "2025-06-30",
            "url": "https://whitehavencoal.com.au/wp-content/uploads/2025/07/WHC_June_2025_Quarterly_Report.pdf",
            "label": "Jun 2025 Quarterly",
        },
    ],
    "YAL.AX": [
        {
            "period_end": "2025-12-31",
            "url": "https://ea-cdn.eurolandir.com/press-releases-attachments/3994243/FY_2025_Financial_Results_Announcement_2502202619431.pdf",
            "label": "FY 2025 Financial Results",
        },
        {
            "period_end": "2025-06-30",
            "url": "https://announcements.asx.com.au/asxpdf/20250819/pdf/06n1rzrf62fb14.pdf",
            "label": "1H 2025 Financial and Production Results",
        },
        {
            "period_end": "2024-12-31",
            "url": "https://www.yancoal.com.au/wp-content/uploads/2025/02/Yancoal-2024-Full-Year-Results-webcast-notes.pdf",
            "label": "FY 2024 Results webcast notes",
        },
    ],
    "FMG.AX": [
        {
            "period_end": "2026-06-30",
            "url": "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/2924-03116249-6A1336475",
            "label": "Q4 FY26 Quarterly Production Report",
        },
        {
            "period_end": "2026-03-31",
            "url": "https://announcements.asx.com.au/asxpdf/20260424/pdf/06ytclmh4bwzlr.pdf",
            "label": "Q3 FY26 Quarterly Production Report",
        },
        {
            "period_end": "2025-12-31",
            "url": "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/2924-03048082-6A1307990",
            "label": "Q2 FY26 Quarterly Production Report",
        },
    ],
    "CIA.AX": [
        {
            "period_end": "2026-06-30",
            "url": "https://www.marketindex.com.au/data-api/api/v1/announcements/XASX:CIA:2A1686563/pdf/inline/quarterly-activities-report-q1-fy27-results",
            "label": "Q1 FY27 Quarterly Activities",
        },
        {
            "period_end": "2026-03-31",
            "url": "https://company-announcements.afr.com/asx/cia/5413eacc-5a2d-11f1-a667-32528fbcc2f6.pdf",
            "label": "FY2026 Q4 results",
        },
    ],
    "MIN.AX": [
        {
            "period_end": "2026-06-30",
            "url": "https://www.mineralresources.com.au/news/company-updates/q4-fy26-quarterly-activity-report/",
            "label": "Q4 FY26 Quarterly Activity Report",
        },
        {
            "period_end": "2026-03-31",
            "url": "https://www.mineralresources.com.au/news/company-updates/q3-fy26-quarterly-activity-report/",
            "label": "Q3 FY26 Quarterly Activity Report",
        },
        {
            "period_end": "2025-12-31",
            "url": "https://www.mineralresources.com.au/news/company-updates/q2-fy26-quarterly-activity-report/",
            "label": "Q2 FY26 Quarterly Activity Report",
        },
        {
            "period_end": "2025-09-30",
            "url": "https://www.mineralresources.com.au/news/stories/q1-fy26-quarterly-activity-report/",
            "label": "Q1 FY26 Quarterly Activity Report",
        },
    ],
    "PLS.AX": [
        {
            "period_end": "2026-06-30",
            "url": "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file/2924-03115560-6A1336130",
            "label": "June 2026 Quarterly Activities Report",
        },
    ],
    "AEM": [
        {
            "period_end": "2026-06-30",
            "url": "https://www.prnewswire.com/news-releases/agnico-eagle-reports-second-quarter-2026-results--record-quarterly-free-cash-flow-reflects-solid-operational-performance-record-quarterly-shareholder-returns-302837897.html",
            "label": "AEM Q2 2026 results PR",
        },
    ],
    "GFI": [
        {
            "period_end": "2026-06-30",
            "url": "https://www.sec.gov/Archives/edgar/data/1172724/000162828026055525/h12026tradingstatement.htm",
            "label": "GFI Q2 2026 trading statement",
        },
    ],
    "VALE": [
        {
            "period_end": "2026-03-31",
            "url": "https://www.sec.gov/Archives/edgar/data/917851/000129281426002648/vale20260428_6k.htm",
            "label": "1Q26 6-K earnings",
        },
        {
            "period_end": "2025-12-31",
            "url": "https://www.sec.gov/Archives/edgar/data/917851/000129281426000608/valepr4q25_6ka1.htm",
            "label": "4Q25 6-K earnings",
        },
    ],
}


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mining_unit_cost_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TEXT NOT NULL,
            cost_value_usd REAL,
            cost_value_raw REAL,
            cost_currency TEXT,
            cost_unit TEXT,
            cost_metric TEXT,
            source TEXT,
            source_url TEXT,
            snippet TEXT,
            label TEXT,
            fetched_at TEXT,
            UNIQUE(ticker, period_end, cost_metric, source)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cost_hist_ticker ON mining_unit_cost_history(ticker)"
    )
    conn.commit()
    return conn


def _session() -> requests.Session:
    s = requests.Session()
    try:
        from config.identity import sec_user_agent

        ua = sec_user_agent() or "Mozilla/5.0 (compatible; SignalScreener/1.0; research)"
    except Exception:
        ua = "Mozilla/5.0 (compatible; SignalScreener/1.0; research)"
    s.headers.update(
        {
            "User-Agent": ua,
            "Accept": "*/*",
        }
    )
    return s


def _pdf_text_from_url(url: str, session: requests.Session | None = None, max_pages: int = 40) -> str:
    session = session or _session()
    try:
        # Prefer ASX helper when available (handles markit PDFs)
        if "markitdigital" in url or "asx" in url.lower():
            try:
                from asx_announcements import _download_and_extract_pdf_text

                return _download_and_extract_pdf_text(url, session, max_pages=max_pages, lowercase=False) or ""
            except Exception:
                pass
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" in ctype or url.lower().endswith((".htm", ".html")):
            return resp.text or ""
        from io import BytesIO

        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        reader = PdfReader(BytesIO(resp.content))
        text = ""
        for page in reader.pages[:max_pages]:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        logger.warning("PDF text failed %s: %s", url[:80], e)
        return ""


def _to_usd(value: float, currency: str) -> tuple[float | None, float]:
    cur = (currency or "USD").upper()
    if cur in ("USD", "USDT", ""):
        return value, 1.0
    rate = get_fx_rate(cur, "USD")
    if rate and rate > 0:
        return value * rate, rate
    return None, 0.0


def _parse_period_from_text(text: str, fallback: str | None = None) -> str | None:
    """Try to find quarter/period end date in report text."""
    if not text:
        return fallback
    # "Quarter ended 30 April 2026" / "31 March 2026"
    m = re.search(
        r"(?:quarter|period|year)\s+ended\s+(\d{1,2}\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
        text[:3000],
        re.I,
    )
    if m:
        try:
            return datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
        except Exception:
            try:
                return datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
            except Exception:
                pass
    m2 = re.search(
        r"\b(30|31|28|29)\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
        text[:2000],
        re.I,
    )
    if m2:
        try:
            return datetime.strptime(
                f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%d %B %Y"
            ).date().isoformat()
        except Exception:
            pass
    # Fortescue-style "Q4 FY26" (June year-end): Q1=30 Sep prior, Q4=30 Jun of FY year
    m3 = re.search(r"\bQ([1-4])\s+FY\s*(20)?(\d{2})\b", text[:2500], re.I)
    if m3:
        q = int(m3.group(1))
        yy = int(m3.group(3))
        year = 2000 + yy if yy < 100 else yy
        q_end = {1: (year - 1, 9, 30), 2: (year - 1, 12, 31), 3: (year, 3, 31), 4: (year, 6, 30)}
        y, mo, d = q_end[q]
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # Vale-style "2Q26" / "4Q25" calendar quarters
    m4 = re.search(r"\b([1-4])Q\s*(20)?(\d{2})\b", text[:2500], re.I)
    if m4:
        q = int(m4.group(1))
        yy = int(m4.group(3))
        year = 2000 + yy if yy < 100 else yy
        q_end = {1: (year, 3, 31), 2: (year, 6, 30), 3: (year, 9, 30), 4: (year, 12, 31)}
        y, mo, d = q_end[q]
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return fallback


def extract_cost_points_from_text(
    text: str,
    *,
    ticker: str,
    period_end: str | None,
    source: str,
    source_url: str = "",
    label: str = "",
    prefer_commodity: str = "coal",
) -> list[dict]:
    """Parse one or more unit-cost points from a report body."""
    points = []
    if not text:
        return points
    pe = period_end or _parse_period_from_text(text) or datetime.now().date().isoformat()

    # Prefer ASX parser for coal / iron producers
    try:
        from data.asx_mining import parse_asx_unit_costs

        r = parse_asx_unit_costs(text, [prefer_commodity])
        cost = r.get("cost")
        if cost and cost.get("value") is not None:
            ustr = cost.get("unit") or ""
            raw = cost.get("raw") or ""
            snip = cost.get("snippet") or ""
            if "US$" in snip or "US$" in raw or ustr.upper().startswith("US"):
                raw_ccy = "USD"
            elif "CAD" in ustr.upper() or "C$" in snip:
                raw_ccy = "CAD"
            elif "A$" in ustr or "A$" in snip or "A$" in raw:
                raw_ccy = "AUD"
            else:
                raw_ccy = "USD"
            usd, fx = _to_usd(float(cost["value"]), raw_ccy)
            # If unit already USD from display
            if cost.get("unit") in ("$/t", "$/oz", "$/lb") and "A$" not in (cost.get("raw") or ""):
                usd, fx = float(cost["value"]), 1.0
                raw_ccy = "USD"
            unit = "t"
            ustr = (cost.get("unit") or "").lower()
            if "oz" in ustr:
                unit = "oz"
            elif "lb" in ustr:
                unit = "lb"
            elif "wmt" in ustr:
                unit = "wmt"
            if usd is not None:
                points.append(
                    {
                        "ticker": ticker.upper().replace(".AX", "") + (
                            ".AX" if ticker.upper().endswith(".AX") or source.startswith("asx") else ""
                        ),
                        "period_end": pe,
                        "cost_value_usd": round(usd, 4),
                        "cost_value_raw": float(cost["value"]),
                        "cost_currency": raw_ccy,
                        "cost_unit": unit,
                        "cost_metric": cost.get("metric") or "reported",
                        "source": source,
                        "source_url": source_url,
                        "snippet": (cost.get("snippet") or "")[:280],
                        "label": label or cost.get("metric") or source,
                    }
                )
    except Exception as e:
        logger.debug("asx parse in history: %s", e)

    # US / generic (AISC, copper unit net cash, Vale iron C1) if ASX missed or those commodities
    if not points or prefer_commodity in ("gold", "copper"):
        try:
            from data.mining_costs import parse_unit_costs_from_10k_text

            r = parse_unit_costs_from_10k_text(text, [prefer_commodity])
            cost = r.get("cost")
            if cost and cost.get("value") is not None:
                unit = "oz" if "oz" in (cost.get("unit") or "") else (
                    "lb" if "lb" in (cost.get("unit") or "") else "t"
                )
                points.append(
                    {
                        "ticker": ticker.upper().replace(".AX", ""),
                        "period_end": pe,
                        "cost_value_usd": round(float(cost["value"]), 4),
                        "cost_value_raw": float(cost["value"]),
                        "cost_currency": "USD",
                        "cost_unit": unit,
                        "cost_metric": cost.get("metric") or "us_reported",
                        "source": source,
                        "source_url": source_url,
                        "snippet": (cost.get("snippet") or "")[:280],
                        "label": label or cost.get("metric") or source,
                    }
                )
        except Exception as e:
            logger.debug("us parse in history: %s", e)

    # Normalize ticker: keep .AX suffix if in seed key
    for p in points:
        if ".AX" in ticker.upper() and not p["ticker"].endswith(".AX"):
            p["ticker"] = ticker.upper() if ticker.upper().endswith(".AX") else ticker.upper() + ".AX"
        else:
            p["ticker"] = ticker.upper().replace(".AX", "") if not ticker.upper().endswith(".AX") else ticker.upper()
    return points


def _store_points(conn: sqlite3.Connection, points: list[dict]) -> int:
    n = 0
    now = datetime.now().isoformat()
    for p in points:
        if p.get("cost_value_usd") is None or not p.get("period_end"):
            continue
        conn.execute(
            """
            INSERT INTO mining_unit_cost_history (
                ticker, period_end, cost_value_usd, cost_value_raw, cost_currency,
                cost_unit, cost_metric, source, source_url, snippet, label, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, period_end, cost_metric, source) DO UPDATE SET
                cost_value_usd=excluded.cost_value_usd,
                cost_value_raw=excluded.cost_value_raw,
                cost_currency=excluded.cost_currency,
                cost_unit=excluded.cost_unit,
                source_url=excluded.source_url,
                snippet=excluded.snippet,
                label=excluded.label,
                fetched_at=excluded.fetched_at
            """,
            (
                p["ticker"].upper(),
                p["period_end"][:10],
                p.get("cost_value_usd"),
                p.get("cost_value_raw"),
                p.get("cost_currency"),
                p.get("cost_unit"),
                p.get("cost_metric") or "reported",
                p.get("source") or "",
                p.get("source_url") or "",
                p.get("snippet") or "",
                p.get("label") or "",
                now,
            ),
        )
        n += 1
    return n


def _prefer_commodity_for_ticker(ticker: str) -> str:
    """Map producer YAML primary (or name) to parse commodity."""
    t = (ticker or "").upper().strip()
    t_ax = t if t.endswith(".AX") else (t + ".AX" if len(t) <= 4 else t)
    t_bare = t.replace(".AX", "")
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for p in cfg.get("producers") or []:
                pt = (p.get("ticker") or "").upper()
                if pt in (t, t_ax, t_bare):
                    prim = (p.get("primary") or "").lower()
                    if prim in ("iron_ore", "iron"):
                        return "iron"
                    if prim in ("gold", "silver"):
                        return "gold"
                    if prim == "copper":
                        return "copper"
                    if prim in ("lithium", "lithium_carbonate"):
                        return "lithium"
                    if "coal" in prim:
                        return "coal"
                    if prim:
                        return prim.split("_")[0]
    except Exception:
        pass
    if t_bare in ("FMG", "VALE", "RIO", "BHP", "MIN", "CIA"):
        return "iron"
    if t_bare in ("PLS", "LTR", "SGML"):
        return "lithium"
    if "NEM" in t or t_bare in ("GOLD", "AEM", "KGC", "GFI", "AU"):
        return "gold"
    if t_bare in ("FCX", "SCCO"):
        return "copper"
    return "coal"


def collect_asx_cost_history(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Download seed + recent ASX PDFs and extract unit costs per period."""
    session = session or _session()
    conn = _conn(db_path)
    # None = all seed tickers; empty list = collect none
    if tickers is None:
        want = None
    else:
        want = set()
        for t in tickers:
            t = t.upper().strip()
            if not t.endswith(".AX") and len(t) <= 4 and "." not in t:
                want.add(t + ".AX")
            else:
                want.add(t)

    # merge seeds from DEFAULT + config yaml cost_history_urls
    seeds = {k: list(v) for k, v in DEFAULT_SEED_REPORTS.items()}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for p in cfg.get("producers") or []:
                t = (p.get("ticker") or "").upper()
                for doc in p.get("cost_history_pdfs") or []:
                    seeds.setdefault(t, []).append(doc)
    except Exception as e:
        logger.debug("config seed merge: %s", e)

    # also recent company announcements (may include latest quarterly)
    try:
        from data.asx_mining import fetch_company_announcements
        from asx_announcements import PDF_BASE

        for t in list(seeds.keys()):
            if want is not None and t not in want:
                continue
            code = t.replace(".AX", "")
            anns = fetch_company_announcements(code, session)
            for a in anns:
                h = (a.get("headline") or "").lower()
                if not any(
                    k in h
                    for k in (
                        "quarter",
                        "production report",
                        "activities report",
                        "operations report",
                    )
                ):
                    continue
                if any(k in h for k in ("full year results", "annual report", "appendix 4e")):
                    continue
                if not a.get("document_key"):
                    continue
                seeds.setdefault(t, []).append(
                    {
                        "period_end": "",
                        "url": a.get("url") or f"{PDF_BASE}{a['document_key']}",
                        "label": a.get("headline") or "ASX announcement",
                    }
                )
    except Exception as e:
        logger.debug("asx ann seed: %s", e)

    stored = 0
    errors = []
    for ticker, docs in seeds.items():
        if not ticker.upper().endswith(".AX"):
            continue
        if want is not None and ticker not in want:
            continue
        prefer = _prefer_commodity_for_ticker(ticker)
        seen_urls = set()
        for doc in docs:
            url = doc.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            text = _pdf_text_from_url(url, session)
            if not text:
                errors.append(f"{ticker}: empty {url[:60]}")
                continue
            seed_pe = (doc.get("period_end") or "").strip()[:10]
            pe = seed_pe if len(seed_pe) == 10 else _parse_period_from_text(text)
            pts = extract_cost_points_from_text(
                text,
                ticker=ticker,
                period_end=pe,
                source="asx_pdf",
                source_url=url,
                label=doc.get("label") or "",
                prefer_commodity=prefer,
            )
            stored += _store_points(conn, pts)
            time.sleep(0.2)
    conn.commit()
    conn.close()
    return {"stored": stored, "errors": errors, "finished_at": datetime.now().isoformat()}


def collect_us_cost_history(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
    max_10q: int = 8,
    max_8k: int = 12,
    max_10k: int = 3,
) -> dict[str, Any]:
    """Parse recent US 10-Q / 10-K / earnings 8-K for unit cost time series."""
    from config.identity import sec_edgar_identity

    try:
        from edgar import Company, set_identity

        identity = sec_edgar_identity()
        if identity:
            set_identity(identity)
    except Exception as e:
        return {"stored": 0, "errors": [str(e)]}

    cfg_tickers = []
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        for p in cfg.get("producers") or []:
            t = (p.get("ticker") or "").upper()
            if t.endswith(".AX"):
                continue
            if (p.get("cost_source") or "").startswith("us") or p.get("cost_source") in ("us_mining", "yaml", None):
                cfg_tickers.append(t)
    except Exception:
        cfg_tickers = ["NEM", "BTU", "HCC", "AMR", "FCX", "ARLP"]

    if tickers is not None:
        cfg_tickers = [
            t.upper().replace(".AX", "")
            for t in tickers
            if t and not t.upper().endswith(".AX")
        ]

    conn = _conn(db_path)
    stored = 0
    errors = []
    session = _session()
    seeds = {k.upper(): list(v) for k, v in DEFAULT_SEED_REPORTS.items()}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for p in cfg.get("producers") or []:
                t = (p.get("ticker") or "").upper()
                for doc in p.get("cost_history_pdfs") or []:
                    seeds.setdefault(t, []).append(doc)
    except Exception:
        pass

    for ticker in cfg_tickers:
        prefer = _prefer_commodity_for_ticker(ticker)
        for doc in seeds.get(ticker, []):
            url = doc.get("url") or ""
            if not url:
                continue
            text = _pdf_text_from_url(url, session)
            if not text:
                errors.append(f"{ticker}: empty seed {url[:60]}")
                continue
            seed_pe = (doc.get("period_end") or "").strip()[:10]
            pe = seed_pe if len(seed_pe) == 10 else _parse_period_from_text(text)
            pts = extract_cost_points_from_text(
                text,
                ticker=ticker,
                period_end=pe,
                source="sec_seed",
                source_url=url,
                label=doc.get("label") or "",
                prefer_commodity=prefer,
            )
            for p in pts:
                p["ticker"] = ticker
            stored += _store_points(conn, pts)
            time.sleep(0.15)

        try:
            company = Company(ticker)
        except Exception as e:
            errors.append(f"{ticker}: {e}")
            continue

        filings_to_read = []
        try:
            # Foreign issuers file 6-K / 20-F / 40-F, not 10-Q
            _fpi = {
                "VALE", "AEM", "GFI", "GOLD", "AU", "HBM", "TECK", "SGML",
            }
            if ticker in _fpi or prefer == "iron":
                form_limits = (("6-K", 24), ("20-F", 3), ("40-F", 3))
            else:
                form_limits = (("10-Q", max_10q), ("10-K", max_10k))
            for form, lim in form_limits:
                fl = company.get_filings(form=form)
                n = 0
                for f in fl:
                    if n >= lim:
                        break
                    filings_to_read.append(f)
                    n += 1
            if prefer != "iron":
                fl = company.get_filings(form="8-K")
                n = 0
                for f in fl:
                    if n >= max_8k:
                        break
                    items = str(getattr(f, "items", "") or "")
                    if "2.02" in items:
                        filings_to_read.append(f)
                        n += 1
        except Exception as e:
            errors.append(f"{ticker} list filings: {e}")
            continue

        for f in filings_to_read:
            try:
                fdate = getattr(f, "filing_date", None)
                pe = str(fdate)[:10] if fdate else None
                # better period from period_of_report
                por = getattr(f, "period_of_report", None)
                if por:
                    pe = str(por)[:10]
                text = ""
                if hasattr(f, "text") and callable(f.text):
                    text = f.text() or ""
                if not text and hasattr(f, "markdown"):
                    text = f.markdown() or ""
                if not text:
                    continue
                acc = getattr(f, "accession_number", None) or ""
                form = str(getattr(f, "form", "") or "")
                if form.startswith("6-K"):
                    blob = text.lower()
                    if prefer == "iron" and "c1" not in blob and "cash cost" not in blob:
                        continue
                    if prefer == "gold" and "aisc" not in blob and "all-in sustaining" not in blob:
                        continue
                    if prefer == "lithium" and "unit cost" not in blob and "cash cost" not in blob:
                        continue
                    pe = (str(por)[:10] if por else pe)
                    parsed = _parse_period_from_text(text, fallback=pe)
                    if parsed:
                        pe = parsed
                pts = extract_cost_points_from_text(
                    text,
                    ticker=ticker,
                    period_end=pe,
                    source=f"sec_{form}",
                    source_url=str(getattr(f, "filing_url", "") or acc),
                    label=f"{form} {pe}",
                    prefer_commodity=prefer,
                )
                # force ticker without .AX
                for p in pts:
                    p["ticker"] = ticker
                stored += _store_points(conn, pts)
                time.sleep(0.15)
            except Exception as e:
                errors.append(f"{ticker} filing: {e}")
        time.sleep(0.25)

    conn.commit()
    conn.close()
    return {"stored": stored, "errors": errors[:20], "finished_at": datetime.now().isoformat()}


def collect_all_cost_history(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
    force: bool = False,
) -> dict[str, Any]:
    """Run ASX + US collectors."""
    asx_tickers = None
    us_tickers = None
    if tickers:
        _asx_shorts = {
            "NHC", "WHC", "YAL", "NEM", "FMG", "MIN", "NST", "EVN",
            "RMS", "PRU", "PDN", "CIA", "PLS", "LTR",
        }
        asx_tickers = []
        us_tickers = []
        for t in tickers:
            tu = t.upper().strip()
            if tu.endswith(".AX"):
                asx_tickers.append(tu)
            elif tu in _asx_shorts:
                asx_tickers.append(tu + ".AX")
            else:
                us_tickers.append(tu.replace(".AX", ""))
        if any(t.upper().startswith("NEM") for t in tickers):
            us_tickers = list(set((us_tickers or []) + ["NEM"]))
            asx_tickers = list(set((asx_tickers or []) + ["NEM.AX"]))

    asx = collect_asx_cost_history(tickers=asx_tickers, db_path=db_path)
    us = collect_us_cost_history(tickers=us_tickers, db_path=db_path)
    return {
        "asx": asx,
        "us": us,
        "stored": (asx.get("stored") or 0) + (us.get("stored") or 0),
        "finished_at": datetime.now().isoformat(),
    }


def get_cost_history(
    ticker: str,
    db_path: str = DEFAULT_DB,
    *,
    prefer_metric: str | None = None,
) -> list[dict]:
    """
    Return sorted cost history points for charting.
    One point per period_end (best metric for that period).
    """
    t = (ticker or "").strip().upper()
    variants = {t, t.replace(".AX", ""), t if t.endswith(".AX") else t + ".AX"}
    conn = _conn(db_path)
    rows = []
    for v in variants:
        rows.extend(
            conn.execute(
                """
                SELECT period_end, cost_value_usd, cost_value_raw, cost_currency, cost_unit,
                       cost_metric, source, source_url, snippet, label, fetched_at
                FROM mining_unit_cost_history
                WHERE ticker=?
                ORDER BY period_end
                """,
                (v,),
            ).fetchall()
        )
    conn.close()
    # pick best per period_end
    by_pe: dict[str, dict] = {}
    metric_pref = [
        "iron_c1_cash_cost_per_t",
        "iron_onslow_fob_per_wmt",
        "lithium_fob_sc6_per_t",
        "coal_fob_cash_cost_per_t",
        "coal_cash_operating_cost_per_t",
        "coal_cash_cost_per_ton",
        "gold_aisc_per_oz",
        "copper_unit_net_cash_per_lb",
        "cost_per_ton",
    ]
    if prefer_metric:
        metric_pref.insert(0, prefer_metric)

    def score(metric: str, source: str) -> int:
        s = 0
        for i, m in enumerate(metric_pref):
            if metric == m:
                s += 100 - i
        if "10-Q" in source or "asx_pdf" in source:
            s += 5
        if "10-K" in source:
            s += 3
        if "8-K" in source:
            s += 4
        return s

    for r in rows:
        pe, usd, raw, ccy, unit, metric, source, url, snip, label, fetched = r
        if usd is None:
            continue
        cand = {
            "period_end": pe[:10],
            "cost_value_usd": usd,
            "cost_value_raw": raw,
            "cost_currency": ccy,
            "cost_unit": unit,
            "cost_metric": metric,
            "source": source,
            "source_url": url,
            "snippet": snip,
            "label": label,
            "fetched_at": fetched,
            "_score": score(metric or "", source or ""),
        }
        prev = by_pe.get(pe[:10])
        if not prev or cand["_score"] >= prev["_score"]:
            by_pe[pe[:10]] = cand

    out = [by_pe[k] for k in sorted(by_pe.keys())]
    for p in out:
        p.pop("_score", None)
    return out


def cost_history_status(db_path: str = DEFAULT_DB) -> dict:
    conn = _conn(db_path)
    rows = conn.execute(
        """
        SELECT ticker, COUNT(*), MIN(period_end), MAX(period_end)
        FROM mining_unit_cost_history
        GROUP BY ticker
        ORDER BY ticker
        """
    ).fetchall()
    conn.close()
    return {
        "tickers": [
            {"ticker": r[0], "n_points": r[1], "from": r[2], "to": r[3]} for r in rows
        ],
        "ticker_count": len(rows),
    }
