"""
Latest company announcements with outbound source links.

ASX  → Markit/ASX announcement PDFs (cache + company endpoint)
US   → recent SEC filings (10-Q / 10-K / 8-K) via edgartools
Other → Yahoo Finance quote news page (fallback link only)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _norm(ticker: str) -> str:
    return (ticker or "").strip().upper()


def _is_asx(ticker: str) -> bool:
    return _norm(ticker).endswith(".AX")


def _is_us_like(ticker: str) -> bool:
    t = _norm(ticker)
    if not t or "." in t:
        # Allow HESAY-style ADRs without suffix; reject .AX/.HK etc.
        if any(t.endswith(s) for s in (
            ".AX", ".HK", ".T", ".L", ".TO", ".PA", ".DE", ".SW", ".SI",
            ".NS", ".BO", ".KS", ".KQ", ".NZ", ".SA", ".MX",
        )):
            return False
    try:
        from data.edgar_metrics import is_us_edgar_eligible
        return is_us_edgar_eligible(t)
    except Exception:
        return "." not in t


def _asx_announcements(ticker: str, limit: int = 15) -> list[dict[str, Any]]:
    t = _norm(ticker).replace(".AX", "")
    items: list[dict] = []

    # 1) Market-wide announcements cache
    try:
        from asx_announcements import load_cached_announcements

        cached = load_cached_announcements([t], days=365) or []
        for a in cached:
            items.append(
                {
                    "date": (a.get("date") or "")[:10],
                    "headline": a.get("headline") or "",
                    "url": a.get("url") or "",
                    "source": "ASX",
                    "type": a.get("classification") or "announcement",
                    "price_sensitive": bool(a.get("is_price_sensitive")),
                }
            )
    except Exception as e:
        logger.debug("ASX cache anns %s: %s", t, e)

    # 2) Company endpoint (fresh, shallow)
    if len(items) < limit:
        try:
            from data.asx_mining import fetch_company_announcements

            live = fetch_company_announcements(t) or []
            seen = {i.get("url") for i in items if i.get("url")}
            for a in live:
                url = a.get("url") or ""
                if url and url in seen:
                    continue
                seen.add(url)
                items.append(
                    {
                        "date": (a.get("date") or "")[:10],
                        "headline": a.get("headline") or "",
                        "url": url,
                        "source": "ASX",
                        "type": a.get("announcement_type") or "announcement",
                        "price_sensitive": bool(a.get("is_price_sensitive")),
                    }
                )
        except Exception as e:
            logger.debug("ASX live anns %s: %s", t, e)

    items = [i for i in items if i.get("headline") or i.get("url")]
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    return items[:limit]


def _us_sec_filings(ticker: str, limit: int = 12) -> list[dict[str, Any]]:
    t = _norm(ticker).replace(".AX", "")
    try:
        from config.identity import sec_edgar_identity, sec_user_agent
        from edgar import Company, set_identity

        identity = sec_edgar_identity() or sec_user_agent()
        if identity:
            set_identity(identity)
    except Exception as e:
        logger.debug("SEC identity: %s", e)
        return []

    out: list[dict] = []
    try:
        company = Company(t)
    except Exception as e:
        logger.debug("SEC company %s: %s", t, e)
        return []

    # Always include EDGAR company search as a reliable outbound link
    out.append(
        {
            "date": datetime.now().date().isoformat(),
            "headline": f"SEC EDGAR filings for {t}",
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={quote(t)}&owner=exclude&count=40",
            "source": "SEC EDGAR",
            "type": "company_page",
            "price_sensitive": False,
        }
    )

    per_form = max(2, (limit - 1) // 3)
    for form in ("8-K", "10-Q", "10-K"):
        try:
            filings = company.get_filings(form=form)
            n = 0
            for f in filings:
                if n >= per_form:
                    break
                fdate = getattr(f, "filing_date", None)
                date_s = str(fdate)[:10] if fdate else ""
                url = (
                    getattr(f, "filing_url", None)
                    or getattr(f, "homepage_url", None)
                    or getattr(f, "url", None)
                    or ""
                )
                if not url:
                    acc = getattr(f, "accession_number", None) or getattr(f, "accession_no", None)
                    if acc:
                        acc_nodash = str(acc).replace("-", "")
                        url = f"https://www.sec.gov/Archives/edgar/data/{quote(str(getattr(company, 'cik', '') or ''))}/{acc_nodash}/"
                headline = f"{form}"
                items = str(getattr(f, "items", "") or "")
                if items:
                    headline = f"{form} — items {items[:80]}"
                else:
                    por = getattr(f, "period_of_report", None)
                    if por:
                        headline = f"{form} — period {str(por)[:10]}"
                out.append(
                    {
                        "date": date_s or datetime.now().date().isoformat(),
                        "headline": headline,
                        "url": str(url) if url else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={quote(t)}&type={quote(form)}&owner=exclude&count=10",
                        "source": "SEC EDGAR",
                        "type": form,
                        "price_sensitive": form == "8-K",
                    }
                )
                n += 1
        except Exception as e:
            logger.debug("SEC filings %s %s: %s", t, form, e)

    # Prefer dated filings first, keep company page
    dated = [x for x in out if x.get("type") != "company_page"]
    page = [x for x in out if x.get("type") == "company_page"]
    dated.sort(key=lambda x: x.get("date") or "", reverse=True)
    return (dated + page)[:limit]


def _yahoo_fallback(ticker: str) -> list[dict[str, Any]]:
    """Single outbound link to Yahoo Finance quote (news tab lives on that site)."""
    t = _norm(ticker)
    if not t:
        return []
    return [
        {
            "date": datetime.now().date().isoformat(),
            "headline": f"Open Yahoo Finance news / filings for {t}",
            "url": f"https://finance.yahoo.com/quote/{quote(t)}/news",
            "source": "Yahoo Finance",
            "type": "news_page",
            "price_sensitive": False,
        }
    ]


def get_announcements(
    ticker: str,
    *,
    market: str = "",
    limit: int = 15,
) -> dict[str, Any]:
    """
    Return { ticker, market, announcements: [...], source_note }.
    Each announcement: date, headline, url, source, type, price_sensitive.
    """
    t = _norm(ticker)
    m = (market or "").strip().lower()
    if not m:
        if _is_asx(t):
            m = "asx"
        elif _is_us_like(t):
            m = "us"
        else:
            m = "other"

    anns: list[dict] = []
    note = ""

    if m == "asx" or _is_asx(t):
        anns = _asx_announcements(t, limit=limit)
        note = "ASX company announcements (PDF links). Free feed is recent-only."
    elif m in ("us", "buybacks") or _is_us_like(t):
        anns = _us_sec_filings(t, limit=limit)
        note = "SEC EDGAR filings (8-K / 10-Q / 10-K). Not retail news wires."
        if not anns:
            anns = _yahoo_fallback(t)
            note = "No SEC filings resolved — Yahoo Finance news page."
    else:
        # Business list tickers may still be ASX or US
        if t.endswith(".AX"):
            anns = _asx_announcements(t, limit=limit)
            note = "ASX company announcements (PDF links)."
        elif _is_us_like(t):
            anns = _us_sec_filings(t, limit=limit)
            note = "SEC EDGAR filings."
        if not anns:
            anns = _yahoo_fallback(t)
            note = "Exchange-specific feed not wired — open Yahoo Finance."

    # Drop entries with no usable link when we have alternatives
    linked = [a for a in anns if a.get("url")]
    if linked:
        anns = linked

    return {
        "ticker": t,
        "market": m,
        "announcements": anns[:limit],
        "source_note": note,
        "count": len(anns[:limit]),
    }
