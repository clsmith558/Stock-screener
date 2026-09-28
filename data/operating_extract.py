"""
Phase 2: quarterly operating extracts from 10-Q / 10-K / 8-K 2.02 and ASX PDFs.

Fields: production, realized_price, hedge_pct, capex.
Seed list in config/operating_extract.yaml. Heuristic — keep snippet and verify.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _PROJECT_ROOT / "config" / "operating_extract.yaml"
DEFAULT_DB = str(_PROJECT_ROOT / "asx_announcements_cache.db")

_config_cache: dict | None = None


def load_operating_config(force: bool = False) -> dict:
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache
    if not CONFIG_PATH.exists():
        _config_cache = {"seed": []}
        return _config_cache
    with open(CONFIG_PATH, encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f) or {}
    _config_cache.setdefault("seed", [])
    return _config_cache


def seed_entries(tickers: list[str] | None = None) -> list[dict]:
    want = {t.strip().upper() for t in (tickers or []) if t.strip()}
    out = []
    for e in load_operating_config().get("seed") or []:
        t = (e.get("ticker") or "").strip().upper()
        if not t:
            continue
        if want and t not in want and t.replace(".AX", "") not in want:
            continue
        out.append(e)
    return out


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operating_extract (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TEXT NOT NULL,
            field TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT,
            currency TEXT,
            commodity TEXT,
            source TEXT,
            source_url TEXT,
            snippet TEXT,
            label TEXT,
            fetched_at TEXT,
            UNIQUE(ticker, period_end, field, commodity, source)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_op_extract_ticker ON operating_extract(ticker)"
    )
    conn.commit()
    return conn


def _num(raw: str) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v != v:
        return None
    return v


def _snip(text: str, start: int, end: int, width: int = 140) -> str:
    a = max(0, start - 40)
    b = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[a:b]).strip()[:280]


def _plausible(field: str, value: float, unit: str) -> bool:
    u = (unit or "").lower()
    if field == "hedge_pct":
        return 0 <= value <= 100
    if field == "realized_price":
        if "bbl" in u or "boe" in u:
            return 15 <= value <= 250
        if "mcf" in u or "mmbtu" in u:
            return 0.4 <= value <= 30
        if "/t" in u or "ton" in u:
            # thermal/met coal or alumina; primary Al is usually >$1,000
            return 20 <= value <= 800
        if "mwh" in u:
            return 5 <= value <= 500
        return 0.1 < value < 10000
    if field == "production":
        if "mmboe" in u:
            return 10 <= value <= 800
        if "mboe" in u:
            return 0.1 <= value <= 8000
        if "bcfe" in u or "bcf" in u:
            return 0.05 <= value <= 50
        if u in ("mt", "mtpa") or "ton" in u:
            return 0.05 <= value <= 80
        if "koz" in u:
            return 20 <= value <= 4000  # quarterly; 10-K FY totals are larger
        if "mlb" in u:
            return 10 <= value <= 8000
        if "kmt" in u:
            return 80 <= value <= 4000
        if "twh" in u:
            return 0.5 <= value <= 400
        if "mwh" in u:
            return 100 <= value <= 5e7
        return 0 < value < 1e7
    if field == "capex":
        return 1e5 <= value <= 8e10  # stored in USD
    return True


# (field, unit, commodity, regex) — group 1 is the number; optional group 2 is scale
# Table-first: 10-Q sales-volume / realized-price grids (OXY-style).
_GENERIC: list[tuple[str, str, str, re.Pattern]] = [
    (
        "production",
        "Mt",
        "coal",
        re.compile(
            r"Total tons sold from reportable segments\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "kt",
        "coal",
        re.compile(
            r"(?:^|\n)\s*Tons sold\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "koz",
        "gold",
        re.compile(
            r"Consolidated gold ounces\s*\(thousands\):\s*Produced\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "koz",
        "gold",
        re.compile(
            r"Produced\s+([\d,.]+)\s+[\d,.]+\s+[\d,.]+\s+Sold",
            re.I,
        ),
    ),
    (
        "production",
        "Mlb",
        "copper",
        re.compile(
            r"Copper sales\s*\(millions of recoverable pounds\)\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "Mlb",
        "copper",
        re.compile(
            r"Total mined copper\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "kmt",
        "aluminum",
        re.compile(
            r"aluminum shipments\s*\(kmt\)\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "mboe/d",
        "oil",
        re.compile(
            r"Total\s+Sales\s+Volumes\s*\(Mboe\)\s*(?:\(a\))?\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/bbl",
        "oil",
        re.compile(
            r"Average\s+Realized\s+Prices.{0,400}?Oil\s*\(\$/Bbl\).{0,800}?"
            r"Total\s+Worldwide\s+\$\s*([\d,.]+)",
            re.I | re.S,
        ),
    ),
    (
        "capex",
        "USD",
        "",
        re.compile(
            r"Capital expenditures\s*\(\s*([\d,.]+)\s*\)",
            re.I,
        ),
    ),
    (
        "production",
        "kt",
        "coal",
        re.compile(
            r"Metric tons sold\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/t",
        "coal",
        re.compile(
            r"Average net selling price per metric ton\s+\$\s*([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "mboe/d",
        "oil",
        re.compile(
            r"(?:average\s+(?:daily\s+)?(?:total\s+)?production|total\s+average\s+daily\s+production|"
            r"production\s+averaged|average\s+production)\s*"
            r"(?:of|was|:)?\s*([\d,.]+)\s*(?:Mboe/d|MBOE/d|MBoe/d|Mboe\s+per\s+day)",
            re.I,
        ),
    ),
    (
        "production",
        "MMboe",
        "oil",
        re.compile(
            r"(?:total\s+)?production\s+(?:of|was|:)?\s*([\d,.]+)\s*(?:MMboe|mmboe|million\s+boe)",
            re.I,
        ),
    ),
    (
        "production",
        "Bcfe/d",
        "gas",
        re.compile(
            r"(?:sales\s+volume|production)\s+(?:of|was|averaged|:)?\s*([\d,.]+)\s*(?:Bcfe/d|Bcfe\s+per\s+day)",
            re.I,
        ),
    ),
    (
        "production",
        "Mt",
        "coal",
        re.compile(
            r"(?:saleable\s+(?:coal\s+)?production|met(?:allurgical)?\s+coal\s+production|"
            r"tons?\s+of\s+(?:met(?:allurgical)?\s+)?coal\s+(?:produced|sold))\s*"
            r"(?:of|was|:)?\s*([\d,.]+)\s*(?:million\s+(?:metric\s+)?tons?|Mt)",
            re.I,
        ),
    ),
    (
        "production",
        "kt",
        "coal",
        re.compile(
            r"(?:saleable\s+coal\s+production|coal\s+production)\s+[‘']000t\s+([\d,.]+)",
            re.I,
        ),
    ),
    (
        "production",
        "TWh",
        "power",
        re.compile(
            r"(?:owned\s+)?(?:electricity\s+)?generation\s+(?:of|was|:)?\s*([\d,.]+)\s*TWh",
            re.I,
        ),
    ),
    (
        "production",
        "koz",
        "gold",
        re.compile(
            r"(?:attributable\s+)?gold\s+production\s+(?:of|was|:)?\s*([\d,.]+)\s*"
            r"(?:koz|thousand\s+ounces|k\s*oz)",
            re.I,
        ),
    ),
    (
        "production",
        "Mlb",
        "copper",
        re.compile(
            r"(?:copper\s+)?(?:sales|production)\s+(?:of|was|:)?\s*([\d,.]+)\s*"
            r"(?:million\s+pounds|Mlbs?)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/bbl",
        "oil",
        re.compile(
            r"(?:average\s+)?realized\s+(?:oil\s+)?prices?\s*"
            r"(?:per\s+barrel)?\s*(?:of|was|:)?\s*\$?\s*([\d,.]+)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/boe",
        "oil",
        re.compile(
            r"(?:average\s+)?realis[ed]{2}\s+price\s*"
            r"(?:of|was|:)?\s*\$?\s*([\d,.]+)\s*(?:per\s+(?:barrel\s+of\s+oil\s+equivalent|boe)|/boe)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/Mcfe",
        "gas",
        re.compile(
            r"(?:average\s+)?realized\s+prices?\s*(?:of|was|:)?\s*\$?\s*([\d,.]+)\s*"
            r"(?:per\s+Mcfe|/Mcfe)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/t",
        "coal",
        re.compile(
            r"(?:average\s+)?(?:net\s+)?(?:selling|realis[ed]{2})\s+prices?\s*"
            r"(?:of|was|:)?\s*(?:US|A)?\$?\s*([\d,.]+)\s*(?:per\s+(?:metric\s+)?t(?:onne)?|/t)",
            re.I,
        ),
    ),
    (
        "realized_price",
        "USD/MWh",
        "power",
        re.compile(
            r"(?:average\s+)?realized\s+(?:energy\s+)?(?:price|revenue)\s*"
            r"(?:of|was|:)?\s*\$?\s*([\d,.]+)\s*(?:per\s+MWh|/MWh)",
            re.I,
        ),
    ),
    (
        "hedge_pct",
        "pct",
        "",
        re.compile(
            r"(?:approximately\s+|about\s+)?([\d,.]+)\s*%\s+of\s+"
            r"(?:expected\s+)?(?:20\d{2}\s+)?(?:forecast\s+)?(?:production|oil|gas|volumes?)\s+"
            r"(?:is\s+|are\s+|has\s+been\s+)?hedged",
            re.I,
        ),
    ),
    (
        "hedge_pct",
        "pct",
        "",
        re.compile(
            r"hedged\s+(?:approximately\s+|about\s+)?([\d,.]+)\s*%\s+of\s+"
            r"(?:expected\s+)?(?:20\d{2}\s+)?(?:production|volumes?)",
            re.I,
        ),
    ),
    (
        "capex",
        "USD",
        "",
        re.compile(
            r"capital\s+expenditures?\s*(?:of|were|was|:)?\s*\$\s*([\d,.]+)\s*(billion|million)",
            re.I,
        ),
    ),
]


def parse_operating_text(
    text: str,
    entry: dict,
    *,
    period_end: str | None,
    source: str,
    source_url: str = "",
    label: str = "",
) -> list[dict]:
    """Return candidate extract points from one document body."""
    if not text or len(text) < 80:
        return []
    ticker = (entry.get("ticker") or "").strip().upper()
    want = set(entry.get("fields") or ["production", "realized_price", "hedge_pct", "capex"])
    pe = period_end or datetime.now().date().isoformat()
    # Prefer the front half (current period tables sit before MD&A repeats)
    window = text[: min(len(text), 180_000)]
    hits: dict[tuple[str, str], dict] = {}

    for field, unit, commodity, pat in _GENERIC:
        if field not in want:
            continue
        pref = entry.get(
            {"production": "production", "realized_price": "realized", "hedge_pct": "hedge", "capex": "capex"}.get(field, field)
        ) or {}
        unit_out = pref.get("unit") or unit
        comm_out = pref.get("commodity") or commodity
        for m in pat.finditer(window):
            val = _num(m.group(1))
            if val is None:
                continue
            if field == "capex":
                scale = ""
                if m.lastindex and m.lastindex >= 2:
                    scale = (m.group(2) or "").lower()
                if scale.startswith("billion"):
                    val *= 1e9
                elif scale.startswith("million"):
                    val *= 1e6
                elif 80_000 < val < 20_000_000:
                    # cash-flow table in $ thousands: "Capital expenditures ( 1,662,112 )"
                    val *= 1_000
                elif 50 <= val <= 80_000:
                    # cash-flow table in $ millions: "Capital expenditures ( 3,143 )"
                    val *= 1e6
            if unit == "kt":
                val = val / 1000.0
                unit_out = "Mt"
            if not _plausible(field, val, unit_out):
                continue
            key = (field, comm_out or "")
            if key in hits:
                continue  # first plausible match
            hits[key] = {
                "ticker": ticker,
                "period_end": pe[:10],
                "field": field,
                "value": round(val, 4),
                "unit": unit_out,
                "currency": "USD" if field in ("realized_price", "capex") else "",
                "commodity": comm_out or "",
                "source": source,
                "source_url": source_url,
                "snippet": _snip(window, m.start(), m.end()),
                "label": label or f"{field} {pe}",
            }
    return list(hits.values())


def _store(conn: sqlite3.Connection, points: list[dict]) -> int:
    n = 0
    now = datetime.now().isoformat(timespec="seconds")
    for p in points:
        if p.get("value") is None or not p.get("period_end") or not p.get("field"):
            continue
        conn.execute(
            """
            INSERT INTO operating_extract (
                ticker, period_end, field, value, unit, currency, commodity,
                source, source_url, snippet, label, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, period_end, field, commodity, source) DO UPDATE SET
                value=excluded.value,
                unit=excluded.unit,
                currency=excluded.currency,
                source_url=excluded.source_url,
                snippet=excluded.snippet,
                label=excluded.label,
                fetched_at=excluded.fetched_at
            """,
            (
                p["ticker"].upper(),
                p["period_end"][:10],
                p["field"],
                float(p["value"]),
                p.get("unit") or "",
                p.get("currency") or "",
                p.get("commodity") or "",
                p.get("source") or "",
                p.get("source_url") or "",
                (p.get("snippet") or "")[:280],
                p.get("label") or "",
                now,
            ),
        )
        n += 1
    return n


def _set_identity() -> None:
    from config.identity import sec_edgar_identity
    from edgar import set_identity

    ident = sec_edgar_identity()
    if ident:
        set_identity(ident)


def _filing_text(filing) -> str:
    text = ""
    try:
        if hasattr(filing, "text") and callable(filing.text):
            text = filing.text() or ""
    except Exception:
        text = ""
    if not text:
        try:
            if hasattr(filing, "markdown") and callable(filing.markdown):
                text = filing.markdown() or ""
        except Exception:
            text = ""
    return text


def collect_us_operating(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    cfg = load_operating_config()
    max_10q = int(cfg.get("max_10q") or 6)
    max_10k = int(cfg.get("max_10k") or 2)
    max_8k = int(cfg.get("max_8k") or 8)
    try:
        from edgar import Company

        _set_identity()
    except Exception as e:
        return {"stored": 0, "errors": [str(e)], "tickers": []}

    stored = 0
    errors: list[str] = []
    done: list[str] = []
    conn = _conn(db_path)

    for entry in seed_entries(tickers):
        market = (entry.get("market") or "").lower()
        sec_t = (entry.get("sec_ticker") or "").strip().upper()
        t = (entry.get("ticker") or "").strip().upper()
        if market == "asx" and not sec_t:
            continue
        lookup = sec_t or t.replace(".AX", "")
        try:
            company = Company(lookup)
        except Exception as e:
            errors.append(f"{t}: {e}")
            continue
        filings = []
        try:
            for form, lim in (("10-Q", max_10q), ("10-K", max_10k), ("20-F", max_10k), ("6-K", max_8k)):
                try:
                    fl = company.get_filings(form=form)
                except Exception:
                    continue
                n = 0
                for f in fl:
                    if n >= lim:
                        break
                    if form == "6-K":
                        # keep recent only; 6-K volume is high
                        filings.append(f)
                    else:
                        filings.append(f)
                    n += 1
            try:
                fl = company.get_filings(form="8-K")
                n = 0
                for f in fl:
                    if n >= max_8k:
                        break
                    items = str(getattr(f, "items", "") or "")
                    if "2.02" in items:
                        filings.append(f)
                        n += 1
            except Exception:
                pass
        except Exception as e:
            errors.append(f"{t} list filings: {e}")
            continue

        n_this = 0
        for f in filings:
            try:
                por = getattr(f, "period_of_report", None) or getattr(f, "filing_date", None)
                pe = str(por)[:10] if por else None
                text = _filing_text(f)
                if not text:
                    continue
                form = str(getattr(f, "form", "") or "")
                acc = str(getattr(f, "accession_number", "") or "")
                url = str(getattr(f, "filing_url", "") or acc)
                pts = parse_operating_text(
                    text,
                    entry,
                    period_end=pe,
                    source=f"sec_{form}",
                    source_url=url,
                    label=f"{form} {pe or ''}".strip(),
                )
                for p in pts:
                    p["ticker"] = t
                n_this += _store(conn, pts)
                time.sleep(0.15)
            except Exception as e:
                errors.append(f"{t} filing: {e}")
        stored += n_this
        done.append(t)
        time.sleep(0.25)

    conn.commit()
    conn.close()
    return {
        "stored": stored,
        "errors": errors[:24],
        "tickers": done,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def collect_asx_operating(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    from data.unit_cost_history import DEFAULT_SEED_REPORTS, _parse_period_from_text, _pdf_text_from_url, _session

    stored = 0
    errors: list[str] = []
    done: list[str] = []
    session = _session()
    conn = _conn(db_path)

    for entry in seed_entries(tickers):
        if (entry.get("market") or "").lower() != "asx":
            continue
        t = (entry.get("ticker") or "").strip().upper()
        if not t.endswith(".AX"):
            t = t + ".AX"
        docs = list(DEFAULT_SEED_REPORTS.get(t) or [])
        try:
            from data.asx_mining import fetch_company_announcements

            anns = fetch_company_announcements(t.replace(".AX", ""), session)
            for a in anns:
                h = (a.get("headline") or "").lower()
                if not any(k in h for k in ("quarter", "production", "activities", "results", "report")):
                    continue
                if not a.get("url"):
                    continue
                docs.append(
                    {
                        "period_end": a.get("date") or "",
                        "url": a["url"],
                        "label": a.get("headline") or "ASX",
                    }
                )
        except Exception as e:
            logger.debug("asx anns %s: %s", t, e)

        seen = set()
        n_this = 0
        for doc in docs:
            url = doc.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            text = _pdf_text_from_url(url, session)
            if not text:
                errors.append(f"{t}: empty PDF {url[:70]}")
                continue
            pe = doc.get("period_end") or _parse_period_from_text(text)
            pts = parse_operating_text(
                text,
                {**entry, "ticker": t},
                period_end=pe,
                source="asx_pdf",
                source_url=url,
                label=doc.get("label") or "",
            )
            n_this += _store(conn, pts)
            time.sleep(0.2)
        stored += n_this
        done.append(t)

    conn.commit()
    conn.close()
    return {
        "stored": stored,
        "errors": errors[:24],
        "tickers": done,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def collect_all_operating(
    tickers: list[str] | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    us = collect_us_operating(tickers=tickers, db_path=db_path)
    asx = collect_asx_operating(tickers=tickers, db_path=db_path)
    return {
        "us": us,
        "asx": asx,
        "stored": (us.get("stored") or 0) + (asx.get("stored") or 0),
        "errors": (us.get("errors") or []) + (asx.get("errors") or []),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_operating_history(ticker: str, db_path: str = DEFAULT_DB) -> list[dict]:
    t = (ticker or "").strip().upper()
    variants = {t, t.replace(".AX", "")}
    if not t.endswith(".AX") and "." not in t and len(t) <= 4:
        variants.add(t + ".AX")
    conn = _conn(db_path)
    rows = []
    for v in variants:
        rows.extend(
            conn.execute(
                """
                SELECT period_end, field, value, unit, currency, commodity,
                       source, source_url, snippet, label, fetched_at
                FROM operating_extract
                WHERE ticker=?
                ORDER BY period_end, field
                """,
                (v,),
            ).fetchall()
        )
    conn.close()
    out = []
    seen = set()
    for r in rows:
        key = (r[0], r[1], r[5], r[6])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "period_end": (r[0] or "")[:10],
                "field": r[1],
                "value": r[2],
                "unit": r[3] or "",
                "currency": r[4] or "",
                "commodity": r[5] or "",
                "source": r[6] or "",
                "source_url": r[7] or "",
                "snippet": r[8] or "",
                "label": r[9] or "",
                "fetched_at": r[10] or "",
            }
        )
    out.sort(key=lambda x: (x["period_end"], x["field"]))
    return out


def latest_operating(ticker: str, db_path: str = DEFAULT_DB) -> dict[str, Any]:
    """Latest point per field (and realized vs strip when units line up)."""
    hist = get_operating_history(ticker, db_path)
    latest: dict[str, dict] = {}
    for p in hist:
        f = p["field"]
        prev = latest.get(f)
        if prev is None or p["period_end"] >= prev["period_end"]:
            latest[f] = p
    return latest


def operating_status(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    conn = _conn(db_path)
    rows = conn.execute(
        """
        SELECT ticker, field, COUNT(*), MIN(period_end), MAX(period_end)
        FROM operating_extract
        GROUP BY ticker, field
        """
    ).fetchall()
    conn.close()
    by_t: dict[str, dict] = {}
    for t, field, n, a, b in rows:
        rec = by_t.setdefault(t, {"ticker": t, "fields": {}, "n": 0})
        rec["fields"][field] = {"n": n, "from": a, "to": b}
        rec["n"] += n
    seed = [e.get("ticker") for e in seed_entries()]
    return {
        "seed": seed,
        "tickers": list(by_t.values()),
        "ticker_count": len(by_t),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
