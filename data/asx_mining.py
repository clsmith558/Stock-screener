"""
ASX mining pilot (phases 1–3) for a small coal + dual-list gold set.

Sources:
  - Markit/ASX company announcements API (/companies/{xid}/announcements)
  - PDF text (quarterly activities, annual reports)
  - Optional IR seed PDF URLs in config/asx_mining_universe.yaml
  - Dual-list cross-fill from US SEC mining caches (e.g. NEM.AX ← NEM)

Writes the same stock-facing shapes as the US pilot:
  reorg / reserves / unit_costs  (via attach_asx_mining_to_stock)

Limitations:
  Free ASX company endpoint returns only a short recent window (~5 docs).
  Seed annual-report PDFs supply reserve depth; quarterly PDFs supply costs/prod.
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

import requests
import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = _PROJECT_ROOT / "config" / "asx_mining_universe.yaml"
DEFAULT_DB = str(_PROJECT_ROOT / "asx_announcements_cache.db")

SUMMARY_CACHE_MAX_AGE_DAYS = 30
FETCH_SLEEP_DEFAULT = 0.5

# Reuse ASX PDF plumbing
from asx_announcements import (  # noqa: E402
    BASE_URL,
    PDF_BASE,
    _download_and_extract_pdf_text,
    _get_session,
    _retry_request,
    get_company_xid,
)

# --- classification / parse patterns ---

_REORG_RULES: list[tuple[str, re.Pattern]] = [
    (
        "administration",
        re.compile(
            r"\b(voluntary\s+administration|administrators?\s+appointed|"
            r"appointment of administrators?|enters?\s+administration)\b",
            re.I,
        ),
    ),
    (
        "doca",
        re.compile(
            r"\b(deed of company arrangement|DOCA|effectuation of (?:the\s+)?DOCA)\b",
            re.I,
        ),
    ),
    (
        "scheme",
        re.compile(
            r"\b(scheme of arrangement|scheme becomes effective|implementation of (?:the\s+)?scheme)\b",
            re.I,
        ),
    ),
    (
        "receivership",
        re.compile(
            r"\b(receivers?\s+(?:and\s+managers?\s+)?appointed|in receivership)\b",
            re.I,
        ),
    ),
    (
        "liquidation",
        re.compile(r"\b(liquidators?\s+appointed|in liquidation|winding[- ]up)\b", re.I),
    ),
]

_RESERVE_HEADLINE = re.compile(
    r"\b(ore reserve|mineral resource|JORC|reserves?\s+(?:and\s+)?resources?|"
    r"annual report|appendix 4e|full year|year end|resources and reserves)\b",
    re.I,
)
_COST_HEADLINE = re.compile(
    r"\b(quarterly|production report|activities report|operations report|"
    r"AISC|cash cost|unit cost|C1|hematite|"
    r"december quarter|march quarter|june quarter|september quarter)\b",
    re.I,
)
_REORG_HEADLINE = re.compile(
    r"\b(administration|administrator|DOCA|receivership|liquidat|scheme of arrangement|"
    r"creditors|restructuring|restructure)\b",
    re.I,
)


def load_asx_mining_universe() -> dict:
    defaults = {
        "companies": [],
        "fetch_sleep_seconds": FETCH_SLEEP_DEFAULT,
        "description": "",
    }
    if not UNIVERSE_PATH.exists():
        return defaults
    try:
        with open(UNIVERSE_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k, v in defaults.items():
            if k not in raw:
                raw[k] = v
        return raw
    except Exception as e:
        logger.warning("Failed to load ASX mining universe: %s", e)
        return defaults


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asx_mining_announcements (
            document_key TEXT PRIMARY KEY,
            ticker TEXT,
            date TEXT,
            headline TEXT,
            announcement_type TEXT,
            is_price_sensitive INTEGER,
            url TEXT,
            fetched_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asx_mining_summary (
            ticker TEXT PRIMARY KEY,
            yf_symbol TEXT,
            company_name TEXT,
            commodities_json TEXT,
            -- phase 1
            has_reorg_signal INTEGER,
            reorg_survivor_candidate INTEGER,
            reorg_event_class TEXT,
            reorg_date TEXT,
            reorg_headline TEXT,
            reorg_manual INTEGER,
            reorg_note TEXT,
            -- phase 2
            reserve_life_years REAL,
            life_confidence TEXT,
            reserve_display TEXT,
            production_display TEXT,
            reserve_snippet TEXT,
            production_snippet TEXT,
            has_jorc_language INTEGER,
            -- phase 3
            cost_metric TEXT,
            cost_value REAL,
            cost_unit TEXT,
            cost_display TEXT,
            cost_snippet TEXT,
            cost_commodity TEXT,
            peer_count INTEGER,
            cost_rank INTEGER,
            cost_rank_pct REAL,
            low_cost_quartile INTEGER,
            -- meta
            us_crossfill TEXT,
            sources_json TEXT,
            parse_notes_json TEXT,
            last_scanned TEXT,
            scan_error TEXT
        )
        """
    )
    conn.commit()
    return conn


def _norm_ticker(t: str) -> str:
    return (t or "").strip().upper().replace(".AX", "")


def classify_reorg_text(text: str) -> str:
    if not text:
        return "other"
    for label, pat in _REORG_RULES:
        if pat.search(text):
            return label
    return "other"


def fetch_company_announcements(ticker: str, session: requests.Session | None = None) -> list[dict]:
    """Recent company announcements via Markit /companies/{xid}/announcements."""
    session = session or _get_session()
    t = _norm_ticker(ticker)
    xid = get_company_xid(session, t)
    if not xid:
        logger.warning("No xid for %s", t)
        return []
    try:
        resp = _retry_request(
            session,
            f"{BASE_URL}/companies/{xid}/announcements",
            params={"page": 0, "itemsPerPage": 100},
        )
        data = resp.json().get("data") or {}
        items = data.get("items") or []
    except Exception as e:
        logger.warning("Company announcements failed for %s: %s", t, e)
        return []

    out = []
    for it in items:
        doc = it.get("documentKey") or ""
        date_str = it.get("date") or ""
        try:
            d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            d = date_str[:10] if date_str else None
        out.append(
            {
                "ticker": t,
                "date": d,
                "headline": it.get("headline") or "",
                "announcement_type": it.get("announcementType") or "",
                "is_price_sensitive": bool(it.get("isPriceSensitive")),
                "document_key": doc,
                "url": f"{PDF_BASE}{doc}" if doc else "",
            }
        )
    return out


def _store_announcements(conn: sqlite3.Connection, anns: list[dict]) -> None:
    now = datetime.now().isoformat()
    for a in anns:
        if not a.get("document_key"):
            continue
        conn.execute(
            """
            INSERT INTO asx_mining_announcements (
                document_key, ticker, date, headline, announcement_type,
                is_price_sensitive, url, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(document_key) DO UPDATE SET
                ticker=excluded.ticker,
                date=excluded.date,
                headline=excluded.headline,
                announcement_type=excluded.announcement_type,
                is_price_sensitive=excluded.is_price_sensitive,
                url=excluded.url,
                fetched_at=excluded.fetched_at
            """,
            (
                a["document_key"],
                a.get("ticker"),
                a.get("date"),
                a.get("headline"),
                a.get("announcement_type"),
                1 if a.get("is_price_sensitive") else 0,
                a.get("url"),
                now,
            ),
        )


def _pdf_text(url: str, session: requests.Session, max_pages: int = 30) -> str:
    if not url:
        return ""
    return _download_and_extract_pdf_text(url, session, max_pages=max_pages, lowercase=False) or ""


def _parse_number(raw: str) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("\u00a0", "").replace("’", "")
    s = re.sub(r"^[A$US]+", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def parse_asx_reserves_production(text: str, commodities: list[str]) -> dict[str, Any]:
    """Heuristic JORC / coal reserve + production extract from report text."""
    notes: list[str] = []
    if not text:
        return {
            "reserve_life_years": None,
            "life_confidence": "none",
            "reserve_display": "",
            "production_display": "",
            "reserve_snippet": "",
            "production_snippet": "",
            "has_jorc_language": False,
            "parse_notes": ["empty text"],
        }

    has_jorc = bool(
        re.search(r"\bJORC\b|ore reserves?|mineral resources?|marketable reserves?|recoverable reserves?", text, re.I)
    )

    reserve_pats = [
        # coal marketable / recoverable reserves Mt
        re.compile(
            r"(?:marketable|recoverable|proved\s+and\s+probable|total)\s+"
            r"(?:coal\s+)?reserves?\s*(?:of|:)?\s*([\d,.]+)\s*(Mt|million\s+tonnes?|Mtpa)?",
            re.I,
        ),
        re.compile(
            r"([\d,.]+)\s*(Mt|million\s+tonnes?)\s+of\s+(?:marketable|recoverable|proved|probable)?"
            r"\s*(?:coal\s+)?reserves?",
            re.I,
        ),
        re.compile(
            r"(?:Ore\s+)?Reserves?\s*(?:\(Mt\)|total)?[^\n]{0,40}?([\d,.]+)\s*(Mt)?",
            re.I,
        ),
        # gold Moz
        re.compile(
            r"(?:gold\s+)?(?:proven\s+and\s+probable\s+)?reserves?\s+of\s+([\d,.]+)\s*"
            r"(million\s+ounces?|Moz)",
            re.I,
        ),
        re.compile(
            r"([\d,.]+)\s*(Moz|million\s+ounces?)\s+(?:of\s+)?(?:gold\s+)?reserves?",
            re.I,
        ),
    ]
    prod_pats = [
        # Prefer explicit Mt annual tables (WHC)
        re.compile(
            r"Saleable production\s*\(Mt\)\s+([\d,.]+)",
            re.I,
        ),
        re.compile(
            r"(?:saleable\s+coal\s+production|ROM\s+coal\s+production|coal\s+production|"
            r"saleable\s+production)\s*(?:‘000t|'000t|kt|Mt)?[^\n]{0,20}?"
            r"([\d,.]+)",
            re.I,
        ),
        re.compile(
            r"(?:saleable\s+coal\s+production|coal\s+sales)\s+(?:was\s+|of\s+|totalling\s+)?"
            r"([\d,.]+)\s*(Mt|million\s+tonnes?|kt)",
            re.I,
        ),
        re.compile(
            r"(?:Group\s+)?(?:coal\s+)?sales\s+of\s+([\d,.]+)\s*(Mt|kt)",
            re.I,
        ),
        re.compile(
            r"(?:gold\s+)?production\s+(?:of\s+|was\s+|approximately\s+)?"
            r"([\d,.]+)\s*(?:million\s+)?(?:ounces?|koz|Moz)",
            re.I,
        ),
        # table: Saleable coal production ‘000t 3,006 ...
        re.compile(
            r"Saleable coal production\s+[‘']000t\s+([\d,.]+)",
            re.I,
        ),
        re.compile(
            r"Coal sales\s+[‘']000t\s+([\d,.]+)",
            re.I,
        ),
    ]

    reserve_hits = []
    for pat in reserve_pats:
        for m in pat.finditer(text):
            val = _parse_number(m.group(1))
            if val is None or val <= 0:
                continue
            unit = (m.group(2) if m.lastindex and m.lastindex >= 2 else "") or ""
            unit_l = unit.lower()
            # normalize to Mt for coal
            scale = 1.0
            if "000" in m.group(0) or unit_l in ("kt",):
                scale = 0.001
            elif "million" in unit_l or unit_l == "mt":
                scale = 1.0
            elif "moz" in unit_l or "ounce" in unit_l:
                scale = 1.0  # Moz
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 30) : m.end() + 60])
            if re.search(r"\b(guidance|target|potential)\b", snip, re.I):
                continue
            reserve_hits.append(
                {
                    "value_mt_or_moz": val * scale,
                    "raw": m.group(0)[:120],
                    "snippet": snip[:240],
                    "is_gold": bool(re.search(r"ounce|moz|gold", m.group(0), re.I)),
                }
            )

    prod_hits = []
    for pat in prod_pats:
        for m in pat.finditer(text):
            val = _parse_number(m.group(1))
            if val is None or val <= 0:
                continue
            unit = ""
            if m.lastindex and m.lastindex >= 2:
                unit = m.group(2) or ""
            g0 = m.group(0)
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 30) : m.end() + 50])
            if re.search(r"\b(guidance|target)\b", snip, re.I):
                continue
            # ‘000t table → Mt
            if "000t" in g0.lower() or "‘000" in g0 or "'000" in g0:
                val_mt = val / 1000.0  # 3006 ‘000t = 3.006 Mt
            elif unit.lower() in ("kt",):
                val_mt = val / 1000.0
            elif "(mt)" in g0.lower() or unit.lower() in ("mt",) or "million" in unit.lower():
                val_mt = val  # already Mt (annual report table)
            else:
                val_mt = val if val < 100 else val / 1000.0

            # Annualize only clear quarterly single-period figures
            is_quarterly = bool(
                re.search(r"quarter ended|for the quarter|this quarter", snip, re.I)
            ) or (
                re.search(r"quarterly activities", text[:800], re.I)
                and "000t" in g0.lower()
            )
            # Prefer YTD column when present: "‘000t 3006 2766 8.7% 8464" — hard; use *4 only for qtr
            if is_quarterly and val_mt < 15:
                annual_mt = val_mt * 4
            else:
                annual_mt = val_mt
            prod_hits.append(
                {
                    "value_mt": val_mt,
                    "annual_mt": annual_mt,
                    "raw": m.group(0)[:120],
                    "snippet": snip[:240],
                    "is_annual_table": "(mt)" in g0.lower(),
                }
            )

    # Prefer larger reserve figures that look like company totals (tens–thousands Mt coal)
    reserve = None
    prefer_gold = "gold" in [c.lower() for c in commodities]
    coal_hits = [h for h in reserve_hits if not h["is_gold"]]
    gold_hits = [h for h in reserve_hits if h["is_gold"]]
    pool = gold_hits if prefer_gold and gold_hits else (coal_hits or reserve_hits)
    if pool:
        # pick max plausible coal reserve 10–50000 Mt or gold 0.5–500 Moz
        plausible = []
        for h in pool:
            v = h["value_mt_or_moz"]
            if h["is_gold"] and 0.1 <= v <= 500:
                plausible.append(h)
            elif not h["is_gold"] and 5 <= v <= 50000:
                plausible.append(h)
        if plausible:
            reserve = max(plausible, key=lambda x: x["value_mt_or_moz"])
        else:
            reserve = max(pool, key=lambda x: x["value_mt_or_moz"])

    production = None
    if prod_hits:
        # Prefer annual-report Mt tables, then plausible 1–100 Mtpa coal
        annual_tables = [h for h in prod_hits if h.get("is_annual_table") and 1 <= h["annual_mt"] <= 100]
        good = [h for h in prod_hits if 0.5 <= h["annual_mt"] <= 100]
        pool = annual_tables or good or prod_hits
        # Prefer higher annual_mt among annual tables (current year first column)
        production = max(pool, key=lambda x: (1 if x.get("is_annual_table") else 0, x["annual_mt"]))

    life = None
    conf = "none"
    if reserve and production and production["annual_mt"] > 0 and not reserve["is_gold"]:
        life = round(reserve["value_mt_or_moz"] / production["annual_mt"], 1)
        if 0.5 <= life <= 200:
            conf = "medium" if has_jorc else "low"
        else:
            notes.append(f"implausible life {life}y discarded")
            life = None
            conf = "none"
    elif reserve and production and reserve["is_gold"]:
        # gold life: Moz / (annual Moz) — annual_mt field misnamed for gold ounces
        notes.append("gold reserve life needs Moz production alignment")

    if not reserve:
        notes.append("no reserve figure matched")
    if not production:
        notes.append("no production figure matched")

    r_disp = ""
    if reserve:
        if reserve["is_gold"]:
            r_disp = f"{reserve['value_mt_or_moz']:.1f} Moz"
        else:
            r_disp = f"{reserve['value_mt_or_moz']:.1f} Mt"
    p_disp = ""
    if production:
        p_disp = f"{production['annual_mt']:.1f} Mtpa (ann.)"

    return {
        "reserve_life_years": life,
        "life_confidence": conf,
        "reserve_display": r_disp,
        "production_display": p_disp,
        "reserve_snippet": (reserve or {}).get("snippet", ""),
        "production_snippet": (production or {}).get("snippet", ""),
        "has_jorc_language": has_jorc,
        "parse_notes": notes,
        "reserve_value": (reserve or {}).get("value_mt_or_moz"),
        "production_annual_mt": (production or {}).get("annual_mt"),
    }


def parse_asx_unit_costs(text: str, commodities: list[str]) -> dict[str, Any]:
    """Extract FOB cash cost / AISC / iron C1 style unit costs from ASX report text."""
    if not text:
        return {"cost": None, "parse_notes": ["empty text"]}

    hits = []
    wanted = []
    for c in commodities or []:
        cl = (c or "").lower()
        wanted.append(cl)
        if cl in ("iron", "iron_ore"):
            wanted.extend(["iron", "iron_ore"])
        if cl in ("lithium", "lithium_carbonate"):
            wanted.extend(["lithium", "lithium_carbonate"])
    # Parenthetical after cash cost often includes "and trade coal" (NHC)
    _excl_paren = r"(?:\([^)]{0,90}\)\s*)?"
    patterns = [
        # Fortescue: Hematite C1 unit cost of US$19.37/wet metric tonne (wmt) in Q4 FY26
        (
            re.compile(
                r"(?:Hematite\s+)?C1\s+unit\s+cost(?:s)?\s*"
                r"(?:of|was|:)?\s*(?:US)?\$?\s*([\d,.]+)\s*"
                r"(?:/\s*wmt|/wmt"
                r"|/\s*w(?:et)?\s*(?:metric\s+)?t(?:onne)?"
                r"|per\s+w(?:et\s+)?(?:metric\s+)?t(?:onne)?)",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "US$/wmt",
        ),
        # Table: Hematite C1 unit cost (US$/wmt)  19.37
        (
            re.compile(
                r"(?:Hematite\s+)?C1\s+unit\s+cost\s*\(\s*US\$\s*/\s*wmt\s*\)\s*([\d,.]+)",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "US$/wmt",
        ),
        # Generic iron C1 (BHP WAIO / others): C1 unit cost of US$17.66/t
        (
            re.compile(
                r"(?:WAIO\s+|Pilbara\s+|iron\s+ore\s+)?C1\s+(?:unit\s+)?costs?\s*"
                r"(?:of|was|:)?\s*(?:US)?\$?\s*([\d,.]+)\s*"
                r"(?:/\s*t|/t|per\s+(?:dry\s+|wet\s+)?(?:metric\s+)?t(?:onne)?)",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "US$/t",
        ),
        # Champion Iron: C1 cash cost ... $83.7/dmt (US$60.5/dmt) — capture USD
        (
            re.compile(
                r"C1 cash cost[^\n]{0,100}?\(US\$\s*([\d,.]+)\s*/\s*dmt",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "US$/dmt",
        ),
        (
            re.compile(
                r"C1 cash cost[^\n]{0,60}?US\$\s*([\d,.]+)\s*/\s*dmt",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "US$/dmt",
        ),
        (
            re.compile(
                r"C1 cash cost\s*(?:\(per dmt sold\))?\s*([\d,.]+)",
                re.I,
            ),
            "iron_c1_cash_cost_per_t",
            "iron",
            "CAD/dmt",
        ),
        # MinRes Onslow Iron FOB cost $50/wmt (AUD unless US$ stated)
        (
            re.compile(
                r"(?:Onslow Iron[^\n]{0,120}?)?FOB costs?\s+"
                r"(?:was|of|:)?\s*(?:US\$|A\$|\$)\s*([\d,.]+)\s*/\s*wmt",
                re.I,
            ),
            "iron_onslow_fob_per_wmt",
            "iron",
            "A$/wmt",
        ),
        # PLS / spodumene: Unit operating cost (FOB) ... (US$437/t)
        (
            re.compile(
                r"Unit operating cost\s*\(FOB\)[^\n]{0,90}?\(US\$\s*([\d,.]+)\s*(?:/\s*t|per\s+t)",
                re.I,
            ),
            "lithium_fob_sc6_per_t",
            "lithium",
            "US$/t",
        ),
        (
            re.compile(
                r"Unit operating cost\s*\(FOB\)[^\n]{0,50}?(?:US\$|A\$|\$)\s*([\d,.]+)\s*"
                r"(?:/\s*t|per\s+t(?:onne)?)",
                re.I,
            ),
            "lithium_fob_sc6_per_t",
            "lithium",
            "A$/t",
        ),
        # FOB cash cost (excluding royalties [and trade coal]) of $74.0 per sales tonne
        (
            re.compile(
                r"(?:FOB\d*\s+)?(?:Free\s+on\s+Board\s*\(FOB\)\s+)?"
                r"cash\s+cost(?:s)?\s*" + _excl_paren
                + r"(?:of|was|:)?\s*(?:A\$|US\$|\$)?\s*([\d,.]+)\s*"
                r"(?:per\s+(?:sales\s+)?t(?:onne)?|A?\$/sales\s*t|/t)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"cash\s+cost\s*\(ex(?:cl)?\.?\s*royalt(?:y|ies)\)[^\n]{0,40}?([\d,.]+)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"(?:unit\s+)?cash\s+costs?\s+(?:of\s+)?(?:US|A)?\$?\s*([\d,.]+)\s*/\s*t",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "$/t",
        ),
        # Prefer full-year figure when H2 and FY appear in one sentence
        (
            re.compile(
                r"(?:A\$|US\$|\$)\s*([\d,.]+)\s*per\s+t(?:onne)?\s+for\s+the\s+full\s+year",
                re.I,
            ),
            "coal_cash_operating_cost_per_t",
            "coal",
            "A$/t",
        ),
        # YAL: Cash operating costs of/were $92 per tonne
        (
            re.compile(
                r"(?:cash\s+operating\s+costs?|operating\s+cash\s+costs?)\s*"
                r"(?:of|were|was|:)?\s*(?:A\$|US\$|\$)\s*([\d,.]+)\s*"
                r"(?:per\s+(?:saleable\s+(?:product\s+)?)?t(?:onne)?|/\s*t|/t)",
                re.I,
            ),
            "coal_cash_operating_cost_per_t",
            "coal",
            "A$/t",
        ),
        # "full year cash cost down to $93 per tonne"
        (
            re.compile(
                r"(?:full[- ]year|FY)\s+(?:cash\s+)?(?:operating\s+)?costs?\s*"
                r"(?:down\s+to|of|were|was|:)?\s*(?:A\$|US\$|\$)\s*([\d,.]+)\s*"
                r"(?:per\s+t(?:onne)?|/\s*t|/t)",
                re.I,
            ),
            "coal_cash_operating_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"Cash\s+operating\s+costs\s*A?\$/t\s*([\d,.]+)",
                re.I,
            ),
            "coal_cash_operating_cost_per_t",
            "coal",
            "A$/t",
        ),
        # WHC: unit cost of coal of ~$139/t / Group unit cost of coal of ~A$135/t
        (
            re.compile(
                r"(?:Group\s+)?(?:H1\s+|H2\s+|FY\d{2}\s+)?"
                r"unit\s+cost\s+of\s+coal\s*"
                r"(?:\(excl(?:uding)?\.?\s*royalt(?:y|ies)\)\s*)?"
                r"(?:of|was|:)?\s*~?\s*(?:A\$|US\$|\$)\s*([\d,.]+)\s*"
                r"(?:/\s*t|/t|per\s+t(?:onne)?)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"(?:half[- ]year|H1|H2)\s+unit\s+cost\s+(?:of\s+)?(?:A\$|US\$|\$)\s*([\d,.]+)\s*/\s*t",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"unit\s+of\s+cost\s+of\s+coal\s+for\s+[^\n]{0,40}?"
                r"~?\s*(?:A\$|US\$|\$)\s*([\d,.]+)\s*(?:/\s*t|/t|per\s+t|tonne)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        # WHC: FOB Unit Cost ... $ 139
        (
            re.compile(
                r"FOB\s+(?:Unit\s+)?Cost[^\n$]{0,40}\$\s*([\d,.]+)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"FOB\s+costs?\s+of\s+\$\s*([\d,.]+)\s*/\s*t",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"Group\s+FOB\s+costs?\s+of\s+\$\s*([\d,.]+)",
                re.I,
            ),
            "coal_fob_cash_cost_per_t",
            "coal",
            "A$/t",
        ),
        (
            re.compile(
                r"(?:all[- ]in\s+sustaining\s+costs?|AISC)\s*(?:of|was|:)?\s*"
                r"(?:US)?\$?\s*([\d,.]+)\s*(?:per\s+ounce|/oz)",
                re.I,
            ),
            "gold_aisc_per_oz",
            "gold",
            "$/oz",
        ),
        (
            re.compile(
                r"AISC\s*(?:per\s+(?:GEO|ounce))?[^\n]{0,20}?\$\s*([\d,.]+)",
                re.I,
            ),
            "gold_aisc_per_oz",
            "gold",
            "$/oz",
        ),
    ]
    for pat, metric, commodity, unit in patterns:
        for m in pat.finditer(text):
            val = _parse_number(m.group(1))
            if val is None:
                continue
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 40) : m.end() + 50])
            # Skip pure guidance ranges unless actual result language nearby
            if re.search(
                r"\b(guidance|expected range|target|between\s+\$|tracking\s+to\s+be\s+around)\b",
                snip,
                re.I,
            ) and not re.search(
                r"\b(was|achieved|FOB Unit Cost|report(?:ed)?|of\s+~?\s*A?\$)\b",
                snip,
                re.I,
            ):
                continue
            # Skip midpoint of guidance ranges like 130–145 or 17.50-18.50
            if re.search(r"\b\d{2,3}(?:\.\d+)?\s*[–\-]\s*\d{2,3}(?:\.\d+)?\b", snip) and not re.search(
                r"~|was|achieved|report|in Q[1-4]", snip, re.I
            ):
                continue
            if metric.startswith("coal") and not (20 <= val <= 300):
                continue
            if metric.startswith("gold") and not (200 <= val <= 5000):
                continue
            if metric.startswith("iron"):
                # Pilbara C1 ~15–25; Onslow FOB ~50; Champion loaded-vessel C1 ~55–90 USD
                if not (8 <= val <= 100):
                    continue
                if re.search(r"\ball[- ]in\b|\bAISC\b", snip, re.I):
                    continue
            if metric.startswith("lithium") and not (150 <= val <= 1200):
                continue
            if wanted:
                iron_only = ("iron" in wanted or "iron_ore" in wanted) and "coal" not in wanted
                coal_only = "coal" in wanted and "iron" not in wanted and "iron_ore" not in wanted
                lithium_only = (
                    ("lithium" in wanted or "lithium_carbonate" in wanted)
                    and "iron" not in wanted
                    and "coal" not in wanted
                )
                if iron_only and (metric.startswith("coal") or metric.startswith("lithium")):
                    continue
                if coal_only and (metric.startswith("iron") or metric.startswith("lithium")):
                    continue
                if lithium_only and (metric.startswith("iron") or metric.startswith("coal")):
                    continue
            score = 2.0
            if "fob unit cost" in snip.lower():
                score += 3.0
            if re.search(r"\bhematite\s+c1\b", snip, re.I):
                score += 3.0
            if re.search(r"\bonslow iron\b", snip, re.I):
                score += 3.0
            if re.search(r"\bunit operating cost\s*\(fob\)", snip, re.I):
                score += 3.0
            if "US$" in snip and metric.startswith(("iron", "lithium")):
                score += 1.5
                if metric == "iron_onslow_fob_per_wmt":
                    unit = "US$/wmt"
                if metric == "lithium_fob_sc6_per_t":
                    unit = "US$/t"
            if re.search(r"\bin\s+Q[1-4]\b", snip, re.I):
                score += 3.5
            if re.search(r"\ball[- ]in\b", snip, re.I) and metric.startswith("iron"):
                score -= 4.0
            if re.search(r"\bnine months\b|\bYTD\b|\bin FY\d{2}\b", snip, re.I) and not re.search(
                r"\bin\s+Q[1-4]\b", snip, re.I
            ):
                score -= 1.5
            if "unit cost of coal" in snip.lower():
                score += 2.5
            if "for the full year" in snip.lower() or re.search(r"\bfull[- ]year\b", snip, re.I):
                score += 2.0
            if re.search(r"\bsecond half\b|\bH2\b", snip, re.I) and not re.search(
                r"\bfull year\b", snip, re.I
            ):
                score -= 2.0
            if "fob" in snip.lower() or "excl" in snip.lower():
                score += 1.0
            if re.search(r"\b(was|achieved|report)\b", snip, re.I):
                score += 1.5
            if re.search(r"\bguidance\b", snip, re.I):
                score -= 1.5
            if wanted and commodity in wanted:
                score += 2.0
            hits.append(
                {
                    "value": val,
                    "metric": metric,
                    "commodity": commodity,
                    "unit": unit,
                    "snippet": snip[:260],
                    "raw": m.group(0)[:140],
                    "score": score,
                }
            )
    hits.sort(key=lambda h: -h["score"])
    best = hits[0] if hits else None
    return {
        "cost": best,
        "hits": hits[:8],
        "parse_notes": [] if best else ["no unit cost matched"],
    }


def _us_crossfill(us_ticker: str) -> dict[str, Any]:
    """Pull phase-2/3 summaries from US SEC cache for dual-listed names."""
    out: dict[str, Any] = {}
    try:
        from data.mining_reserves import get_reserves_summary

        r = get_reserves_summary(us_ticker, max_age_days=0)
        if r and r.get("reserve_life_years") is not None:
            out["reserves"] = r
    except Exception as e:
        logger.debug("US reserves crossfill %s: %s", us_ticker, e)
    try:
        from data.mining_costs import get_costs_summary

        c = get_costs_summary(us_ticker, max_age_days=0)
        if c and c.get("cost_value") is not None:
            out["costs"] = c
    except Exception as e:
        logger.debug("US costs crossfill %s: %s", us_ticker, e)
    try:
        from data.bankruptcy_reorg import get_reorg_summary

        b = get_reorg_summary(us_ticker, max_age_days=0)
        if b and (b.get("survivor_candidate") or b.get("has_item_103")):
            out["reorg"] = b
    except Exception as e:
        logger.debug("US reorg crossfill %s: %s", us_ticker, e)
    return out


def scan_asx_company(entry: dict, db_path: str = DEFAULT_DB, session: requests.Session | None = None) -> dict:
    """Run phases 1–3 for one ASX mining universe entry."""
    session = session or _get_session()
    ticker = _norm_ticker(entry.get("ticker") or "")
    yf_sym = (entry.get("yf_symbol") or f"{ticker}.AX").upper()
    name = entry.get("name") or ticker
    commodities = list(entry.get("commodities") or [])
    notes: list[str] = []
    sources: list[str] = []

    conn = _conn(db_path)
    try:
        anns = fetch_company_announcements(ticker, session)
        _store_announcements(conn, anns)
        sources.append(f"asx_announcements:{len(anns)}")

        # Phase 1: reorg from headlines (+ optional body for reorg-like)
        reorg_events = []
        for a in anns:
            blob = f"{a.get('headline') or ''} {a.get('announcement_type') or ''}"
            if _REORG_HEADLINE.search(blob):
                cls = classify_reorg_text(blob)
                reorg_events.append({**a, "event_class": cls})
                # deepen with PDF if needed
                if cls == "other" and a.get("url"):
                    body = _pdf_text(a["url"], session, max_pages=5)
                    cls = classify_reorg_text(body)
                    reorg_events[-1]["event_class"] = cls

        has_reorg = bool(reorg_events)
        reorg_surv = any(
            e.get("event_class") in ("doca", "scheme", "administration") for e in reorg_events
        )
        # liquidation-only is not a survivor
        if reorg_events and all(e.get("event_class") == "liquidation" for e in reorg_events):
            reorg_surv = False
        reorg_best = sorted(reorg_events, key=lambda x: x.get("date") or "")[-1] if reorg_events else None
        manual = entry.get("manual_reorg") or {}
        if manual:
            reorg_surv = True
            has_reorg = True

        # Collect PDF texts: headline-selected announcements + seed IR PDFs
        texts: list[tuple[str, str, str]] = []  # (kind, url, text)
        for a in anns:
            h = a.get("headline") or ""
            url = a.get("url") or ""
            if not url:
                continue
            kind = None
            if _RESERVE_HEADLINE.search(h):
                kind = "reserve_candidate"
            elif _COST_HEADLINE.search(h):
                kind = "cost_candidate"
            elif a.get("is_price_sensitive") and re.search(r"quarterly|report|production", h, re.I):
                kind = "cost_candidate"
            if kind:
                txt = _pdf_text(url, session, max_pages=35)
                if txt:
                    texts.append((kind, url, txt))
                    sources.append(f"pdf:{a.get('document_key')}")
                time.sleep(0.15)

        for seed in entry.get("seed_pdfs") or []:
            url = seed.get("url") or ""
            if not url:
                continue
            try:
                # download seed via session.get through helper — absolute URL
                txt = _pdf_text(url, session, max_pages=60)
                if txt:
                    texts.append((seed.get("kind") or "seed", url, txt))
                    sources.append(f"seed:{url.split('/')[-1][:40]}")
                else:
                    notes.append(f"seed PDF empty: {url[:60]}")
            except Exception as e:
                notes.append(f"seed PDF failed: {e}")
            time.sleep(0.2)

        # Merge text for parsing (prefer longer/more specific)
        reserve_text = "\n".join(t for k, u, t in texts if k in ("reserve_candidate", "annual_report", "seed") or "annual" in k)
        cost_text = "\n".join(t for k, u, t in texts if k in ("cost_candidate", "quarterly", "annual_report", "seed") or True)
        if not reserve_text and texts:
            reserve_text = max((t for _, _, t in texts), key=len)
        if not cost_text and texts:
            cost_text = max((t for _, _, t in texts), key=len)

        res = parse_asx_reserves_production(reserve_text, commodities)
        costs = parse_asx_unit_costs(cost_text, commodities)
        notes.extend(res.get("parse_notes") or [])
        notes.extend(costs.get("parse_notes") or [])

        cost = costs.get("cost") or {}
        us_cf = entry.get("us_crossfill")
        cross = _us_crossfill(us_cf) if us_cf else {}
        if cross:
            sources.append(f"us_crossfill:{us_cf}")

        # Prefer ASX parse; fill gaps from US
        life = res.get("reserve_life_years")
        life_conf = res.get("life_confidence")
        r_disp = res.get("reserve_display") or ""
        p_disp = res.get("production_display") or ""
        r_snip = res.get("reserve_snippet") or ""
        p_snip = res.get("production_snippet") or ""
        has_jorc = bool(res.get("has_jorc_language"))
        if life is None and cross.get("reserves"):
            ur = cross["reserves"]
            life = ur.get("reserve_life_years")
            life_conf = (ur.get("life_confidence") or "low") + "+us"
            r_disp = ur.get("reserve_display") or r_disp
            p_disp = ur.get("production_display") or p_disp
            r_snip = ur.get("reserve_snippet") or r_snip
            notes.append("reserve life from US SEC cross-fill")

        if not cost and cross.get("costs"):
            uc = cross["costs"]
            cost = {
                "value": uc.get("cost_value"),
                "metric": uc.get("cost_metric") or "us_crossfill",
                "commodity": uc.get("cost_commodity") or "",
                "unit": uc.get("cost_unit") or "",
                "snippet": uc.get("cost_snippet") or "",
                "raw": uc.get("cost_raw") or "",
            }
            notes.append("unit cost from US SEC cross-fill")

        if not has_reorg and cross.get("reorg"):
            ub = cross["reorg"]
            has_reorg = True
            reorg_surv = bool(ub.get("survivor_candidate"))
            reorg_best = {
                "date": ub.get("emerge_date") or ub.get("confirmation_date"),
                "headline": "US Item 1.03 / reorg cache",
                "event_class": "confirmation" if ub.get("has_confirmation") else "filing",
            }
            notes.append("reorg from US SEC cross-fill")

        summary = {
            "ticker": ticker,
            "yf_symbol": yf_sym,
            "company_name": name,
            "commodities": commodities,
            "has_reorg_signal": has_reorg,
            "reorg_survivor_candidate": bool(reorg_surv or manual),
            "reorg_event_class": (reorg_best or {}).get("event_class")
            or (manual.get("relationship") if manual else ""),
            "reorg_date": (reorg_best or {}).get("date"),
            "reorg_headline": (reorg_best or {}).get("headline") or (manual.get("note") if manual else ""),
            "reorg_manual": bool(manual),
            "reorg_note": (manual.get("note") if manual else "") or "",
            "reserve_life_years": life,
            "life_confidence": life_conf or "none",
            "reserve_display": r_disp,
            "production_display": p_disp,
            "reserve_snippet": r_snip,
            "production_snippet": p_snip,
            "has_jorc_language": has_jorc,
            "cost_metric": cost.get("metric") or "",
            "cost_value": cost.get("value"),
            "cost_unit": cost.get("unit") or "",
            "cost_display": (
                f"{cost.get('unit', '').replace('A$/t', 'A$')}{cost.get('value'):.1f}/t"
                if cost.get("value") is not None and "t" in (cost.get("unit") or "")
                else (
                    f"${cost.get('value'):,.0f}/oz"
                    if cost.get("value") is not None and "oz" in (cost.get("unit") or "")
                    else ""
                )
            ),
            "cost_snippet": cost.get("snippet") or "",
            "cost_commodity": cost.get("commodity") or "",
            "peer_count": None,
            "cost_rank": None,
            "cost_rank_pct": None,
            "low_cost_quartile": False,
            "us_crossfill": us_cf or "",
            "sources": sources,
            "parse_notes": notes,
            "last_scanned": datetime.now().isoformat(),
            "scan_error": None,
        }
        # nicer cost_display
        if cost.get("value") is not None:
            u = cost.get("unit") or ""
            v = cost["value"]
            if "oz" in u:
                summary["cost_display"] = f"${v:,.0f}/oz"
            elif "A$" in u or "A$" in (cost.get("raw") or ""):
                summary["cost_display"] = f"A${v:.1f}/t"
            else:
                summary["cost_display"] = f"${v:.1f}/t"

        _store_summary(conn, summary)
        conn.commit()
        return summary
    except Exception as e:
        logger.exception("ASX mining scan failed for %s", ticker)
        summary = {
            "ticker": ticker,
            "yf_symbol": yf_sym,
            "company_name": name,
            "commodities": commodities,
            "has_reorg_signal": False,
            "reorg_survivor_candidate": False,
            "reorg_event_class": "",
            "reorg_date": None,
            "reorg_headline": "",
            "reorg_manual": False,
            "reorg_note": "",
            "reserve_life_years": None,
            "life_confidence": "none",
            "reserve_display": "",
            "production_display": "",
            "reserve_snippet": "",
            "production_snippet": "",
            "has_jorc_language": False,
            "cost_metric": "",
            "cost_value": None,
            "cost_unit": "",
            "cost_display": "",
            "cost_snippet": "",
            "cost_commodity": "",
            "peer_count": None,
            "cost_rank": None,
            "cost_rank_pct": None,
            "low_cost_quartile": False,
            "us_crossfill": entry.get("us_crossfill") or "",
            "sources": [],
            "parse_notes": [],
            "last_scanned": datetime.now().isoformat(),
            "scan_error": str(e),
        }
        _store_summary(conn, summary)
        conn.commit()
        return summary
    finally:
        conn.close()


def _store_summary(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        """
        INSERT INTO asx_mining_summary (
            ticker, yf_symbol, company_name, commodities_json,
            has_reorg_signal, reorg_survivor_candidate, reorg_event_class, reorg_date,
            reorg_headline, reorg_manual, reorg_note,
            reserve_life_years, life_confidence, reserve_display, production_display,
            reserve_snippet, production_snippet, has_jorc_language,
            cost_metric, cost_value, cost_unit, cost_display, cost_snippet, cost_commodity,
            peer_count, cost_rank, cost_rank_pct, low_cost_quartile,
            us_crossfill, sources_json, parse_notes_json, last_scanned, scan_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            yf_symbol=excluded.yf_symbol,
            company_name=excluded.company_name,
            commodities_json=excluded.commodities_json,
            has_reorg_signal=excluded.has_reorg_signal,
            reorg_survivor_candidate=excluded.reorg_survivor_candidate,
            reorg_event_class=excluded.reorg_event_class,
            reorg_date=excluded.reorg_date,
            reorg_headline=excluded.reorg_headline,
            reorg_manual=excluded.reorg_manual,
            reorg_note=excluded.reorg_note,
            reserve_life_years=excluded.reserve_life_years,
            life_confidence=excluded.life_confidence,
            reserve_display=excluded.reserve_display,
            production_display=excluded.production_display,
            reserve_snippet=excluded.reserve_snippet,
            production_snippet=excluded.production_snippet,
            has_jorc_language=excluded.has_jorc_language,
            cost_metric=excluded.cost_metric,
            cost_value=excluded.cost_value,
            cost_unit=excluded.cost_unit,
            cost_display=excluded.cost_display,
            cost_snippet=excluded.cost_snippet,
            cost_commodity=excluded.cost_commodity,
            peer_count=excluded.peer_count,
            cost_rank=excluded.cost_rank,
            cost_rank_pct=excluded.cost_rank_pct,
            low_cost_quartile=excluded.low_cost_quartile,
            us_crossfill=excluded.us_crossfill,
            sources_json=excluded.sources_json,
            parse_notes_json=excluded.parse_notes_json,
            last_scanned=excluded.last_scanned,
            scan_error=excluded.scan_error
        """,
        (
            s["ticker"],
            s.get("yf_symbol"),
            s.get("company_name"),
            json.dumps(s.get("commodities") or []),
            1 if s.get("has_reorg_signal") else 0,
            1 if s.get("reorg_survivor_candidate") else 0,
            s.get("reorg_event_class"),
            s.get("reorg_date"),
            s.get("reorg_headline"),
            1 if s.get("reorg_manual") else 0,
            s.get("reorg_note"),
            s.get("reserve_life_years"),
            s.get("life_confidence"),
            s.get("reserve_display"),
            s.get("production_display"),
            s.get("reserve_snippet"),
            s.get("production_snippet"),
            1 if s.get("has_jorc_language") else 0,
            s.get("cost_metric"),
            s.get("cost_value"),
            s.get("cost_unit"),
            s.get("cost_display"),
            s.get("cost_snippet"),
            s.get("cost_commodity"),
            s.get("peer_count"),
            s.get("cost_rank"),
            s.get("cost_rank_pct"),
            1 if s.get("low_cost_quartile") else 0,
            s.get("us_crossfill"),
            json.dumps(s.get("sources") or []),
            json.dumps(s.get("parse_notes") or []),
            s.get("last_scanned"),
            s.get("scan_error"),
        ),
    )


def _row_to_summary(row: tuple) -> dict:
    (
        ticker, yf_symbol, company_name, commodities_json,
        has_reorg_signal, reorg_survivor_candidate, reorg_event_class, reorg_date,
        reorg_headline, reorg_manual, reorg_note,
        reserve_life_years, life_confidence, reserve_display, production_display,
        reserve_snippet, production_snippet, has_jorc_language,
        cost_metric, cost_value, cost_unit, cost_display, cost_snippet, cost_commodity,
        peer_count, cost_rank, cost_rank_pct, low_cost_quartile,
        us_crossfill, sources_json, parse_notes_json, last_scanned, scan_error,
    ) = row
    try:
        commodities = json.loads(commodities_json) if commodities_json else []
    except Exception:
        commodities = []
    try:
        sources = json.loads(sources_json) if sources_json else []
    except Exception:
        sources = []
    try:
        notes = json.loads(parse_notes_json) if parse_notes_json else []
    except Exception:
        notes = []
    return {
        "ticker": ticker,
        "yf_symbol": yf_symbol,
        "company_name": company_name,
        "commodities": commodities,
        "has_reorg_signal": bool(has_reorg_signal),
        "reorg_survivor_candidate": bool(reorg_survivor_candidate),
        "reorg_event_class": reorg_event_class or "",
        "reorg_date": reorg_date,
        "reorg_headline": reorg_headline or "",
        "reorg_manual": bool(reorg_manual),
        "reorg_note": reorg_note or "",
        "reserve_life_years": reserve_life_years,
        "life_confidence": life_confidence or "none",
        "reserve_display": reserve_display or "",
        "production_display": production_display or "",
        "reserve_snippet": reserve_snippet or "",
        "production_snippet": production_snippet or "",
        "has_jorc_language": bool(has_jorc_language),
        "cost_metric": cost_metric or "",
        "cost_value": cost_value,
        "cost_unit": cost_unit or "",
        "cost_display": cost_display or "",
        "cost_snippet": cost_snippet or "",
        "cost_commodity": cost_commodity or "",
        "peer_count": peer_count,
        "cost_rank": cost_rank,
        "cost_rank_pct": cost_rank_pct,
        "low_cost_quartile": bool(low_cost_quartile),
        "us_crossfill": us_crossfill or "",
        "sources": sources,
        "parse_notes": notes,
        "last_scanned": last_scanned,
        "scan_error": scan_error,
    }


def recompute_asx_cost_ranks(db_path: str = DEFAULT_DB) -> dict:
    """Rank ASX pilot peers within the same cost_metric (lower cost = rank 1)."""
    summaries = list_asx_mining_summaries(db_path)
    by_metric: dict[str, list] = {}
    for s in summaries:
        m = (s.get("cost_metric") or "").strip()
        if m and s.get("cost_value") is not None:
            by_metric.setdefault(m, []).append(s)
    conn = _conn(db_path)
    updated = 0
    cohorts = {}
    for metric, peers in by_metric.items():
        peers_sorted = sorted(peers, key=lambda x: (x["cost_value"], x["ticker"]))
        n = len(peers_sorted)
        cohorts[metric] = [p["ticker"] for p in peers_sorted]
        for i, p in enumerate(peers_sorted):
            rank = i + 1
            rank_pct = (rank - 1) / (n - 1) if n > 1 else 0.0
            low_q = rank_pct <= 0.25 if n >= 2 else True
            conn.execute(
                """
                UPDATE asx_mining_summary
                SET peer_count=?, cost_rank=?, cost_rank_pct=?, low_cost_quartile=?
                WHERE ticker=?
                """,
                (n, rank, round(rank_pct, 4), 1 if low_q else 0, p["ticker"]),
            )
            updated += 1
    conn.execute(
        """
        UPDATE asx_mining_summary
        SET peer_count=NULL, cost_rank=NULL, cost_rank_pct=NULL, low_cost_quartile=0
        WHERE cost_value IS NULL
        """
    )
    conn.commit()
    conn.close()
    return {"updated": updated, "cohorts": cohorts}


def list_asx_mining_summaries(db_path: str = DEFAULT_DB) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute("SELECT * FROM asx_mining_summary ORDER BY ticker").fetchall()
    conn.close()
    return [_row_to_summary(r) for r in rows]


def get_asx_mining_summary(
    ticker: str,
    db_path: str = DEFAULT_DB,
    max_age_days: int = SUMMARY_CACHE_MAX_AGE_DAYS,
) -> dict | None:
    """Lookup by ASX code or yf_symbol (YAL or YAL.AX)."""
    raw = (ticker or "").strip().upper()
    if not raw:
        return None
    t = _norm_ticker(raw)
    yf = raw if raw.endswith(".AX") else f"{t}.AX"
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT * FROM asx_mining_summary WHERE ticker=? OR yf_symbol=? OR yf_symbol=?",
            (t, yf, t),
        ).fetchone()
        conn.close()
        if not row:
            return None
        if max_age_days > 0 and row[-2]:
            try:
                lu = datetime.fromisoformat(row[-2])
                if datetime.now() - lu > timedelta(days=max_age_days):
                    return None
            except Exception:
                pass
        return _row_to_summary(row)
    except Exception as e:
        logger.warning("get_asx_mining_summary failed for %s: %s", ticker, e)
        return None


def scan_asx_mining_universe(
    db_path: str = DEFAULT_DB,
    *,
    force: bool = False,
    tickers: list[str] | None = None,
    sleep_seconds: float | None = None,
) -> dict[str, Any]:
    cfg = load_asx_mining_universe()
    sleep = (
        sleep_seconds
        if sleep_seconds is not None
        else float(cfg.get("fetch_sleep_seconds") or FETCH_SLEEP_DEFAULT)
    )
    want = {_norm_ticker(t) for t in tickers} if tickers else None
    companies = list(cfg.get("companies") or [])
    scanned = skipped = errors = 0
    summaries = []
    session = _get_session()

    for i, entry in enumerate(companies):
        t = _norm_ticker(entry.get("ticker") or "")
        if not t:
            continue
        if want is not None and t not in want:
            continue
        if not force:
            existing = get_asx_mining_summary(t, db_path=db_path, max_age_days=SUMMARY_CACHE_MAX_AGE_DAYS)
            if existing and not existing.get("scan_error"):
                skipped += 1
                summaries.append(existing)
                continue
        s = scan_asx_company(entry, db_path=db_path, session=session)
        summaries.append(s)
        scanned += 1
        if s.get("scan_error"):
            errors += 1
        if sleep and i < len(companies) - 1:
            time.sleep(sleep)

    ranks = recompute_asx_cost_ranks(db_path)
    refreshed = [get_asx_mining_summary(s["ticker"], db_path=db_path, max_age_days=0) or s for s in summaries]
    return {
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "summaries": refreshed,
        "ranks": ranks,
        "universe_size": len(companies),
        "finished_at": datetime.now().isoformat(),
    }


def attach_asx_mining_to_stock(stock: dict, summary: dict | None = None, ticker: str | None = None) -> dict:
    """Merge ASX mining pilot fields onto a stock (same keys as US pilot)."""
    sym = ticker or stock.get("ticker") or ""
    if summary is None:
        summary = get_asx_mining_summary(sym, max_age_days=0)
    if not summary:
        return stock

    # Phase 1 — reorg
    stock["reorg"] = {
        "scanned": True,
        "has_item_103": False,
        "has_filing": summary.get("reorg_event_class") == "administration",
        "has_confirmation": summary.get("reorg_event_class") in ("doca", "scheme"),
        "has_emergence": summary.get("reorg_event_class") in ("doca", "scheme"),
        "first_filing_date": summary.get("reorg_date"),
        "confirmation_date": summary.get("reorg_date")
        if summary.get("reorg_event_class") in ("doca", "scheme")
        else None,
        "emerge_date": summary.get("reorg_date")
        if summary.get("reorg_survivor_candidate")
        else None,
        "survivor_candidate": bool(summary.get("reorg_survivor_candidate")),
        "manual_reorg": bool(summary.get("reorg_manual")),
        "manual_note": summary.get("reorg_note") or summary.get("reorg_headline") or "",
        "event_count": 1 if summary.get("has_reorg_signal") else 0,
        "events": [],
        "source": "asx",
        "us_crossfill": summary.get("us_crossfill") or "",
    }
    stock["reorg_survivor_candidate"] = stock["reorg"]["survivor_candidate"]
    stock["reorg_has_item_103"] = False
    stock["reorg_emerge_date"] = stock["reorg"].get("emerge_date")

    # Phase 2 — reserves
    stock["reserves"] = {
        "scanned": True,
        "filing_date": summary.get("last_scanned"),
        "has_sk1300_language": False,
        "has_exhibit_96": False,
        "has_jorc_language": bool(summary.get("has_jorc_language")),
        "reserve_life_years": summary.get("reserve_life_years"),
        "life_confidence": summary.get("life_confidence"),
        "reserve_display": summary.get("reserve_display") or "",
        "production_display": summary.get("production_display") or "",
        "reserve_commodity": (summary.get("commodities") or [""])[0] if summary.get("commodities") else "",
        "reserve_snippet": summary.get("reserve_snippet") or "",
        "production_snippet": summary.get("production_snippet") or "",
        "source": "asx",
        "us_crossfill": summary.get("us_crossfill") or "",
    }
    stock["reserve_life_years"] = summary.get("reserve_life_years")
    stock["has_exhibit_96"] = False

    # Phase 3 — unit costs
    stock["unit_costs"] = {
        "scanned": True,
        "cost_metric": summary.get("cost_metric") or "",
        "cost_value": summary.get("cost_value"),
        "cost_unit": summary.get("cost_unit") or "",
        "cost_commodity": summary.get("cost_commodity") or "",
        "cost_display": summary.get("cost_display") or "",
        "cost_snippet": summary.get("cost_snippet") or "",
        "peer_cohort": summary.get("cost_metric") or "",
        "peer_count": summary.get("peer_count"),
        "cost_rank": summary.get("cost_rank"),
        "cost_rank_pct": summary.get("cost_rank_pct"),
        "low_cost_quartile": bool(summary.get("low_cost_quartile")),
        "source": "asx",
        "us_crossfill": summary.get("us_crossfill") or "",
    }
    stock["unit_cost_value"] = summary.get("cost_value")
    stock["unit_cost_rank"] = summary.get("cost_rank")
    stock["unit_cost_low_quartile"] = bool(summary.get("low_cost_quartile"))
    stock["asx_mining"] = {
        "sources": summary.get("sources") or [],
        "parse_notes": summary.get("parse_notes") or [],
        "us_crossfill": summary.get("us_crossfill") or "",
    }
    return stock


def enrich_stocks_asx_mining_cached(stocks: list) -> list:
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        if not sym:
            continue
        # Only attach when we have a summary (ASX pilot or .AX)
        if get_asx_mining_summary(sym, max_age_days=0):
            attach_asx_mining_to_stock(stock, ticker=sym)
    return stocks


def asx_mining_status_public(db_path: str = DEFAULT_DB) -> dict:
    summaries = list_asx_mining_summaries(db_path)
    cfg = load_asx_mining_universe()
    with_cost = [s for s in summaries if s.get("cost_value") is not None]
    with_life = [s for s in summaries if s.get("reserve_life_years") is not None]
    reorgs = [s for s in summaries if s.get("has_reorg_signal") or s.get("reorg_survivor_candidate")]
    return {
        "universe_size": len(cfg.get("companies") or []),
        "summaries_cached": len(summaries),
        "with_cost": len(with_cost),
        "with_reserve_life": len(with_life),
        "with_reorg": len(reorgs),
        "companies": [
            {
                "ticker": s["ticker"],
                "yf_symbol": s.get("yf_symbol"),
                "name": s.get("company_name"),
                "reorg_survivor": s.get("reorg_survivor_candidate"),
                "reserve_life_years": s.get("reserve_life_years"),
                "reserve_display": s.get("reserve_display"),
                "production_display": s.get("production_display"),
                "cost_display": s.get("cost_display"),
                "cost_rank": s.get("cost_rank"),
                "peer_count": s.get("peer_count"),
                "low_cost_quartile": s.get("low_cost_quartile"),
                "us_crossfill": s.get("us_crossfill"),
                "sources": s.get("sources") or [],
                "parse_notes": s.get("parse_notes") or [],
                "scan_error": s.get("scan_error"),
            }
            for s in summaries
        ],
        "description": cfg.get("description") or "",
        "disclaimer": (
            "ASX free company API returns a short recent window; annual-report seed PDFs "
            "and US dual-list cross-fill (NEM) fill gaps. Heuristic PDF parse — verify."
        ),
    }
