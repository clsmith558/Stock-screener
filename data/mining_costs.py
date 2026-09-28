"""
Phase-3: Reported unit-cost rank within mining pilot peers (not WoodMac).

Parses latest Form 10-K / 6-K narrative for non-GAAP / unit cost metrics:
  coal  → cash cost of sales / costs per ton ($/ton or $/metric ton)
  gold  → AISC / all-in sustaining cost ($/oz)
  copper→ unit net cash cost ($/lb)
  iron  → C1 cash cost ($/t FOB, typically ex-third-party purchases)

Ranks only within the same commodity+metric cohort in the pilot universe.
Does NOT claim industry cost-curve percentile or replacement-cost proof.
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

# Rank cohorts need this many peers with the same metric to be meaningful
MIN_PEER_FOR_RANK = 2

_identity_set = False

# Metric keys used for peer ranking
METRIC_COAL_CASH_COST = "coal_cash_cost_per_ton"
METRIC_GOLD_AISC = "gold_aisc_per_oz"
METRIC_COPPER_UNIT_CASH = "copper_unit_net_cash_per_lb"
METRIC_IRON_C1 = "iron_c1_cash_cost_per_t"
METRIC_GENERIC_COST_PER_TON = "cost_per_ton"

# Patterns: (regex, metric_key, commodity, unit_label)
# Capture group 1 = numeric cost value
_COST_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    # --- Gold AISC ---
    (
        re.compile(
            r"(?:all[- ]in\s+sustaining\s+costs?|AISC)\s*"
            r"(?:\(1\)|\(5\)|\*)?\s*"
            r"(?:of|were|was|:|=)?\s*"
            r"\$?\s*([\d,.]+)\s*(?:per\s+ounce|/oz|per\s+oz)",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    (
        re.compile(
            r"\$\s*([\d,.]+)\s*(?:per\s+ounce|/oz)\s*"
            r"(?:[^\n.]{0,40})?(?:AISC|all[- ]in\s+sustaining)",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    (
        re.compile(
            r"AISC\s*(?:per\s+ounce)?\s*(?:of|:|=)?\s*\$?\s*([\d,.]+)",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    # AEM: all-in sustaining costs ("AISC") per ounce 2 of $1,459
    (
        re.compile(
            r"all[- ]in sustaining costs?\s*(?:\([^)]{0,24}\))?\s*"
            r"per ounce[^\n$]{0,24}\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    (
        re.compile(
            r"AISC per ounce[^\n$]{0,24}\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    (
        re.compile(
            r"All-in sustaining cost\s*\(AISC\)\s+for Q[1-4]\s+20\d{2}\s+"
            r"is expected to be US\$\s*([\d,.]+)\s*/\s*oz",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    (
        re.compile(
            r"AISC[^\n]{0,40}?US\$\s*([\d,.]+)\s*/\s*oz",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    # Table-ish: "$1,729 $2,507 All-in sustaining costs" — take first $ amount before label
    (
        re.compile(
            r"\$\s*([\d,.]+)\s+(?:\$\s*[\d,.]+\s+)?All-in\s+sustaining\s+costs?",
            re.I,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    # "Gold (per ounce) ... AISC ... $1,xxx" loose
    (
        re.compile(
            r"Gold\s*\(per\s+ounce\)[^\n]{0,120}?"
            r"(?:AISC|all[- ]in\s+sustaining)[^\n]{0,40}?\$\s*([\d,.]+)",
            re.I | re.S,
        ),
        METRIC_GOLD_AISC,
        "gold",
        "$/oz",
    ),
    # --- Copper unit net cash costs ---
    (
        re.compile(
            r"Unit\s+net\s+cash\s+costs\s+per\s+pound\s*a?\s*\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COPPER_UNIT_CASH,
        "copper",
        "$/lb",
    ),
    (
        re.compile(
            r"(?:unit\s+)?net\s+cash\s+costs?\s*per\s*pound\s*"
            r"(?:of\s+copper\s*)?(?:a\s*)?\$?\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COPPER_UNIT_CASH,
        "copper",
        "$/lb",
    ),
    # "average unit net cash costs ... of $3.05 per pound of copper in 2025"
    (
        re.compile(
            r"average\s+unit\s+net\s+cash\s+costs?\s*"
            r"(?:\(net of by-product credits\)\s*)?"
            r"(?:for[^\n]{0,80}?)?\s*of\s+\$\s*([\d,.]+)\s*per\s+pound\s+of\s+copper\s+in\s+20\d{2}",
            re.I,
        ),
        METRIC_COPPER_UNIT_CASH,
        "copper",
        "$/lb",
    ),
    (
        re.compile(
            r"\$\s*([\d,.]+)\s*per\s*pound\s+of\s+copper\s+in\s+20\d{2}",
            re.I,
        ),
        METRIC_COPPER_UNIT_CASH,
        "copper",
        "$/lb",
    ),
    # --- Iron ore C1 (Vale fines FOB ex-third-party; similar majors) ---
    (
        re.compile(
            r"C1 cash cost,?\s+excluding third-party purchases?,?\s+"
            r"reached\s+US\$\s*([\d,.]+)\s*/\s*t",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    (
        re.compile(
            r"C1 cash cost,?\s+ex-third-party purchase costs,?\s+"
            r"reached\s+US\$\s*([\d,.]+)\s*/\s*t",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    (
        re.compile(
            r"(?:The\s+)?C1 cash cost,?\s+ex(?:cluding|-)?\s*third-party purchase(?:s| costs)?,?\s*"
            r"(?:reached|was|of|:)\s*(?:US)?\$?\s*([\d,.]+)\s*(?:/\s*t|per\s+t(?:onne)?)",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    (
        re.compile(
            r"C1 cash cost,?\s+ex-third-party purchase costs\s*(?:\(C/F\))?\s*"
            r"(?:\(US\$/t\))?\s*([\d,.]+)",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    (
        re.compile(
            r"C1 cash cost of iron ore[^\n]{0,40}?(?:US)?\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    (
        re.compile(
            r"Iron ore C1 cash cost(?:s)?\s+"
            r"(?:totaled|reached|was|of)\s+US\$\s*([\d,.]+)\s*/\s*t",
            re.I,
        ),
        METRIC_IRON_C1,
        "iron",
        "$/t",
    ),
    # --- Coal cash cost of sales (HCC-style, metric ton) ---
    (
        re.compile(
            r"Cash\s+cost\s+of\s+sales\s+per\s+metric\s+ton\s*\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COAL_CASH_COST,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"cash\s+cost\s+of\s+sales\s+per\s+(?:metric\s+)?ton\s*"
            r"(?:was\s+|of\s+|:\s*)?\$?\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COAL_CASH_COST,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"cash\s+cost\s+of\s+sales[^\n.]{0,40}\$?\s*([\d,.]+)\s*"
            r"(?:per\s+(?:metric\s+)?ton)",
            re.I,
        ),
        METRIC_COAL_CASH_COST,
        "coal",
        "$/t",
    ),
    # AMR: cost of coal sales per ton $XX (level, not YoY delta)
    (
        re.compile(
            r"(?:average\s+)?(?:non-GAAP\s+)?cost\s+of\s+coal\s+sales\s+per\s+"
            r"(?:metric\s+)?ton\s*(?:was\s+|of\s+|:\s*)\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COAL_CASH_COST,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"cost\s+of\s+coal\s+sales\s+per\s+(?:metric\s+)?ton\s*\$\s*([\d,.]+)",
            re.I,
        ),
        METRIC_COAL_CASH_COST,
        "coal",
        "$/t",
    ),
    # --- Generic costs per ton (BTU / ARLP style) — lower priority ---
    # BTU table: "Costs per Ton 44.55 114.31" — first number is a segment cost
    (
        re.compile(
            r"Costs\s+per\s+Ton\s+([\d,.]+)",
            re.I,
        ),
        METRIC_GENERIC_COST_PER_TON,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"(?:average\s+)?(?:costs?|cost\s+of\s+sales)\s+per\s+"
            r"(?:short\s+|metric\s+)?ton\s*(?:of\s+\$\s*|\$\s*|was\s+|of\s+|:\s*)?"
            r"([\d,.]+)",
            re.I,
        ),
        METRIC_GENERIC_COST_PER_TON,
        "coal",
        "$/t",
    ),
    # ARLP: Segment Adjusted EBITDA Expense per ton sold ... $41.29 per ton sold
    (
        re.compile(
            r"Segment\s+Adjusted\s+EBITDA\s+Expense\s+per\s+ton\s+sold[^\n]{0,80}"
            r"\$\s*([\d,.]+)\s*per\s+ton",
            re.I,
        ),
        METRIC_GENERIC_COST_PER_TON,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"\$\s*([\d,.]+)\s*per\s+ton\s+sold\s+in\s+20\d{2}",
            re.I,
        ),
        METRIC_GENERIC_COST_PER_TON,
        "coal",
        "$/t",
    ),
    (
        re.compile(
            r"\$\s*([\d,.]+)\s*per\s+(?:short\s+|metric\s+)?ton\s*"
            r"(?:[^\n.]{0,25})?(?:cost\s+of\s+sales|cash\s+cost|operating\s+cost)",
            re.I,
        ),
        METRIC_GENERIC_COST_PER_TON,
        "coal",
        "$/t",
    ),
]


def _ensure_identity() -> None:
    global _identity_set
    if _identity_set:
        return
    try:
        from edgar import set_identity

        identity = sec_edgar_identity()
        if not identity:
            logger.warning(
                "SEC_EDGAR_IDENTITY / SCREENER_CONTACT_EMAIL not set — EDGAR may fail"
            )
            return
        set_identity(identity)
        _identity_set = True
    except Exception as e:
        logger.warning("SEC identity setup failed: %s", e)


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mining_costs_summary (
            ticker TEXT PRIMARY KEY,
            cik TEXT,
            company_name TEXT,
            filing_date TEXT,
            accession TEXT,
            form TEXT,
            cost_metric TEXT,
            cost_value REAL,
            cost_unit TEXT,
            cost_commodity TEXT,
            cost_raw TEXT,
            cost_snippet TEXT,
            cost_year TEXT,
            peer_cohort TEXT,
            peer_count INTEGER,
            cost_rank INTEGER,
            cost_rank_pct REAL,
            low_cost_quartile INTEGER,
            commodities_json TEXT,
            all_hits_json TEXT,
            parse_notes_json TEXT,
            last_scanned TEXT,
            scan_error TEXT
        )
        """
    )
    conn.commit()
    return conn


def load_mining_universe() -> dict:
    defaults: dict[str, Any] = {
        "companies": [],
        "fetch_sleep_seconds": EDGAR_FETCH_SLEEP_DEFAULT,
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
        logger.warning("Failed to load mining universe: %s", e)
        return defaults


def _norm_cik(cik: Any) -> str:
    if cik is None:
        return ""
    s = re.sub(r"\D", "", str(cik))
    return s.lstrip("0") or "0"


def _resolve_company(entry: dict):
    from edgar import Company

    cik = entry.get("cik")
    ticker = (entry.get("ticker") or "").strip().upper()
    last_err = None
    if cik is not None and str(cik).strip():
        try:
            return Company(int(str(cik).strip()))
        except Exception as e:
            last_err = e
    if ticker:
        try:
            return Company(ticker)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f"Cannot resolve company for {entry}")


def _preferred_commodities(entry: dict) -> list[str]:
    out = []
    for c in entry.get("commodities") or []:
        s = str(c).strip().lower()
        if s in ("met", "metallurgical", "coke"):
            s = "coal"
        if s:
            out.append(s)
    return out


def _parse_number(raw: str) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("\u00a0", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _plausible_cost(metric: str, value: float) -> bool:
    if value is None or value <= 0:
        return False
    if metric == METRIC_GOLD_AISC:
        return 200 <= value <= 5000  # $/oz
    if metric == METRIC_COPPER_UNIT_CASH:
        return 0.3 <= value <= 8.0  # $/lb
    if metric in (METRIC_COAL_CASH_COST, METRIC_GENERIC_COST_PER_TON):
        return 5 <= value <= 500  # $/ton
    if metric == METRIC_IRON_C1:
        return 8 <= value <= 100  # Pilbara ~18; Champion loaded-vessel ~60; skip AISC ~110
    return 0.1 <= value <= 10000


def _score_cost_hit(
    metric: str,
    value: float,
    snip: str,
    preferred: list[str],
    commodity: str,
) -> float:
    score = 1.0
    snip_l = snip.lower()
    if preferred and commodity in preferred:
        score += 4.0
    # Prefer company consolidated / average language
    if re.search(r"\b(average|consolidated|company|our|we)\b", snip_l):
        score += 1.0
    if re.search(r"\b(first quartile|lowest|industry|peer|benchmark|spot price)\b", snip_l):
        score -= 1.5
    # Prefer cash cost of sales / AISC / unit net cash over generic
    if metric == METRIC_COAL_CASH_COST:
        score += 2.0
    if metric == METRIC_GOLD_AISC:
        score += 2.0
        if re.search(r"\bconsolidated\b|\bgroup\b", snip_l):
            score += 2.0
        if re.search(r"for q[1-4]\s+20\d{2}", snip_l):
            score += 2.0
        if re.search(r"\band\s+(?:us)?\$\s*[\d,.]+", snip_l):
            score -= 2.5  # comparative pair, not the headline AISC
    if metric == METRIC_COPPER_UNIT_CASH:
        score += 2.0
        if re.search(r"average unit net cash costs.{0,80}of \$?[\d.]+ per pound of copper in 20\d{2}", snip_l):
            score += 2.0  # prefer segment average with year over bare table cell
    if metric == METRIC_IRON_C1:
        score += 2.5
        if re.search(r"\breached\b|\btotaled\b", snip_l):
            score += 2.0
        if re.search(r"\bex-third-party\b|\bexcluding third-party\b", snip_l):
            score += 1.5
        if re.search(r"\b[1-4]q\d{2}\b|\bin\s+q[1-4]\b", snip_l):
            score += 2.0
        if re.search(r"\ball[- ]in\b", snip_l):
            score -= 4.0
        if re.search(r"\bincluding third-party\b", snip_l):
            score -= 5.0
    if metric == METRIC_GENERIC_COST_PER_TON:
        score += 0.5
        if re.search(r"segment adjusted ebitda expense per ton", snip_l):
            score += 1.5
    # Skip royalty / tax per ton noise
    if re.search(r"\b(royalt|severance|black lung|reclamation fee|tax)\b", snip_l):
        score -= 3.0
    # Prefer mid-range plausible values slightly (avoid table headers)
    if _plausible_cost(metric, value):
        score += 1.0
    else:
        score -= 5.0
    # Prefer recent year mentions
    if re.search(r"\b202[4-6]\b", snip):
        score += 0.5
    return score


def parse_unit_costs_from_10k_text(
    text: str,
    preferred_commodities: list[str] | None = None,
) -> dict[str, Any]:
    """Extract best unit-cost hit from 10-K plain text."""
    preferred = []
    for c in preferred_commodities or []:
        cl = (c or "").lower()
        preferred.append(cl)
        if cl in ("iron_ore", "iron"):
            preferred.extend(["iron", "iron_ore"])
    notes: list[str] = []
    hits: list[dict[str, Any]] = []
    if not text:
        return {
            "cost": None,
            "hits": [],
            "parse_notes": ["empty 10-K text"],
        }

    for pat, metric, commodity, unit in _COST_PATTERNS:
        for m in pat.finditer(text):
            val = _parse_number(m.group(1) if m.lastindex else None)
            if val is None:
                continue
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 40) : m.end() + 80]).strip()
            # Drop copper spot prices masquerading as costs
            if metric == METRIC_COPPER_UNIT_CASH and re.search(
                r"\b(spot|COMEX|LME|closed at|averaged|ranging from)\b", snip, re.I
            ):
                continue
            # Drop gold spot prices
            if metric == METRIC_GOLD_AISC and re.search(
                r"\b(spot|LBMA|averaged|closed at|realized price)\b", snip, re.I
            ) and not re.search(r"\b(AISC|sustaining)\b", snip, re.I):
                continue
            # Drop tiny royalty per ton / excise tax
            if metric in (METRIC_COAL_CASH_COST, METRIC_GENERIC_COST_PER_TON) and val < 5:
                continue
            if not _plausible_cost(metric, val):
                continue
            # Skip guidance / forward year targets for copper
            if re.search(r"\b(expected to approximate|for the year 2026|would change by)\b", snip, re.I):
                continue
            if metric == METRIC_IRON_C1:
                if re.search(r"\b(guidance|all[- ]in|including third-party)\b", snip, re.I):
                    continue
                if re.search(r"\b\d{1,2}\.\d\s*[–\-]\s*\d{1,2}\.\d\b", snip):
                    continue
                # US$ million C1 totals, not unit cost
                if re.search(r"US\$\s*million|\(\s*US\$\s*million\s*\)", snip, re.I) and val > 45:
                    continue
            # Skip comparative pairs ("AISC was $1,289 and $1,235")
            if metric == METRIC_GOLD_AISC and re.search(
                r"and\s+(?:us)?\$\s*[\d,.]+", snip, re.I
            ):
                continue
            # Skip YoY change amounts: "decreased $9.78" (not a level)
            if re.search(
                r"\b(decreased|increased|down|up|change[ds]?)\s+(?:by\s+)?\$?\s*"
                + re.escape(str(m.group(1)).replace(",", ""))
                + r"\b",
                snip,
                re.I,
            ):
                continue
            if re.search(r"\b(decreased|increased)\s+\$\s*[\d,.]+\s*,?\s*or\s+[\d.]+%", snip, re.I):
                continue
            # Skip index prices (met coal indices)
            if re.search(r"\b(index|API2|volatile index|LBMA|COMEX|LME)\b", snip, re.I):
                continue
            score = _score_cost_hit(metric, val, snip, preferred, commodity)
            if score < 0:
                continue
            year_m = re.search(r"\b(20\d{2})\b", snip)
            hits.append(
                {
                    "value": val,
                    "metric": metric,
                    "commodity": commodity,
                    "unit": unit,
                    "snippet": snip[:280],
                    "raw": m.group(0)[:160],
                    "score": score,
                    "year": year_m.group(1) if year_m else None,
                    "start": m.start(),
                }
            )

    hits.sort(key=lambda h: (-h["score"], h["value"]))
    # Prefer preferred commodity metric
    best = None
    for h in hits:
        if preferred and h["commodity"] in preferred and _plausible_cost(h["metric"], h["value"]):
            best = h
            break
    if best is None:
        for h in hits:
            if _plausible_cost(h["metric"], h["value"]):
                best = h
                break
    if best is None and hits:
        notes.append("no plausible unit cost in band")
    if not hits:
        notes.append("no unit cost pattern matched")

    return {"cost": best, "hits": hits[:12], "parse_notes": notes}


def fetch_costs_for_company(entry: dict) -> dict[str, Any]:
    """Download latest 10-K and parse unit costs."""
    _ensure_identity()
    company = _resolve_company(entry)
    ticker = (entry.get("ticker") or "").strip().upper()
    cik = _norm_cik(getattr(company, "cik", None) or entry.get("cik"))
    name = (
        entry.get("name")
        or getattr(company, "name", None)
        or getattr(company, "company_name", None)
        or ticker
    )
    preferred = _preferred_commodities(entry)

    filings = company.get_filings(form="10-K")
    if filings is None or len(filings) == 0:
        filings = company.get_filings(form="20-F")
    if filings is None or len(filings) == 0:
        raise RuntimeError("No 10-K/20-F filings found")

    filing = filings.latest()
    form = str(getattr(filing, "form", None) or "10-K")
    fdate = getattr(filing, "filing_date", None)
    acc = getattr(filing, "accession_number", None) or getattr(filing, "accession_no", None) or ""

    text = ""
    try:
        if hasattr(filing, "text") and callable(filing.text):
            text = filing.text() or ""
        if not text and hasattr(filing, "markdown") and callable(filing.markdown):
            text = filing.markdown() or ""
    except Exception as e:
        logger.warning("10-K text fetch failed for %s: %s", ticker, e)

    parsed = parse_unit_costs_from_10k_text(text, preferred)
    cost = parsed.get("cost") or {}

    return {
        "ticker": ticker,
        "cik": cik,
        "company_name": str(name),
        "filing_date": str(fdate)[:10] if fdate else None,
        "accession": str(acc),
        "form": form,
        "cost_metric": cost.get("metric") or "",
        "cost_value": cost.get("value"),
        "cost_unit": cost.get("unit") or "",
        "cost_commodity": cost.get("commodity") or "",
        "cost_raw": cost.get("raw") or "",
        "cost_snippet": cost.get("snippet") or "",
        "cost_year": cost.get("year"),
        "peer_cohort": cost.get("metric") or "",
        "peer_count": None,
        "cost_rank": None,
        "cost_rank_pct": None,
        "low_cost_quartile": False,
        "commodities": preferred,
        "all_hits": parsed.get("hits") or [],
        "parse_notes": parsed.get("parse_notes") or [],
        "last_scanned": datetime.now().isoformat(),
        "scan_error": None,
        "cost_display": _format_cost(cost.get("value"), cost.get("unit") or ""),
    }


def _format_cost(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    if value >= 100:
        return f"${value:,.0f}{unit.replace('$', '')}" if unit.startswith("$") else f"{value:,.0f} {unit}"
    if value >= 10:
        return f"${value:,.1f}{unit.replace('$', '')}" if unit.startswith("$") else f"{value:,.1f} {unit}"
    return f"${value:,.2f}{unit.replace('$', '')}" if unit.startswith("$") else f"{value:,.2f} {unit}"


def _store_summary(conn: sqlite3.Connection, summary: dict) -> None:
    conn.execute(
        """
        INSERT INTO mining_costs_summary (
            ticker, cik, company_name, filing_date, accession, form,
            cost_metric, cost_value, cost_unit, cost_commodity, cost_raw, cost_snippet,
            cost_year, peer_cohort, peer_count, cost_rank, cost_rank_pct, low_cost_quartile,
            commodities_json, all_hits_json, parse_notes_json, last_scanned, scan_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik=excluded.cik,
            company_name=excluded.company_name,
            filing_date=excluded.filing_date,
            accession=excluded.accession,
            form=excluded.form,
            cost_metric=excluded.cost_metric,
            cost_value=excluded.cost_value,
            cost_unit=excluded.cost_unit,
            cost_commodity=excluded.cost_commodity,
            cost_raw=excluded.cost_raw,
            cost_snippet=excluded.cost_snippet,
            cost_year=excluded.cost_year,
            peer_cohort=excluded.peer_cohort,
            peer_count=excluded.peer_count,
            cost_rank=excluded.cost_rank,
            cost_rank_pct=excluded.cost_rank_pct,
            low_cost_quartile=excluded.low_cost_quartile,
            commodities_json=excluded.commodities_json,
            all_hits_json=excluded.all_hits_json,
            parse_notes_json=excluded.parse_notes_json,
            last_scanned=excluded.last_scanned,
            scan_error=excluded.scan_error
        """,
        (
            summary["ticker"],
            summary.get("cik"),
            summary.get("company_name"),
            summary.get("filing_date"),
            summary.get("accession"),
            summary.get("form"),
            summary.get("cost_metric"),
            summary.get("cost_value"),
            summary.get("cost_unit"),
            summary.get("cost_commodity"),
            summary.get("cost_raw"),
            summary.get("cost_snippet"),
            summary.get("cost_year"),
            summary.get("peer_cohort"),
            summary.get("peer_count"),
            summary.get("cost_rank"),
            summary.get("cost_rank_pct"),
            1 if summary.get("low_cost_quartile") else 0,
            json.dumps(summary.get("commodities") or []),
            json.dumps(summary.get("all_hits") or []),
            json.dumps(summary.get("parse_notes") or []),
            summary.get("last_scanned"),
            summary.get("scan_error"),
        ),
    )


def _row_to_summary(row: tuple) -> dict[str, Any]:
    (
        ticker, cik, company_name, filing_date, accession, form,
        cost_metric, cost_value, cost_unit, cost_commodity, cost_raw, cost_snippet,
        cost_year, peer_cohort, peer_count, cost_rank, cost_rank_pct, low_cost_quartile,
        commodities_json, all_hits_json, parse_notes_json, last_scanned, scan_error,
    ) = row
    try:
        commodities = json.loads(commodities_json) if commodities_json else []
    except Exception:
        commodities = []
    try:
        all_hits = json.loads(all_hits_json) if all_hits_json else []
    except Exception:
        all_hits = []
    try:
        notes = json.loads(parse_notes_json) if parse_notes_json else []
    except Exception:
        notes = []
    return {
        "ticker": ticker,
        "cik": cik,
        "company_name": company_name,
        "filing_date": filing_date,
        "accession": accession,
        "form": form,
        "cost_metric": cost_metric or "",
        "cost_value": cost_value,
        "cost_unit": cost_unit or "",
        "cost_commodity": cost_commodity or "",
        "cost_raw": cost_raw or "",
        "cost_snippet": cost_snippet or "",
        "cost_year": cost_year,
        "peer_cohort": peer_cohort or cost_metric or "",
        "peer_count": peer_count,
        "cost_rank": cost_rank,
        "cost_rank_pct": cost_rank_pct,
        "low_cost_quartile": bool(low_cost_quartile),
        "commodities": commodities,
        "all_hits": all_hits,
        "parse_notes": notes,
        "last_scanned": last_scanned,
        "scan_error": scan_error,
        "cost_display": _format_cost(cost_value, cost_unit or ""),
    }


def get_costs_summary(
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
            "SELECT * FROM mining_costs_summary WHERE ticker=?",
            (sym,),
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
        logger.warning("get_costs_summary failed for %s: %s", sym, e)
        return None


def list_costs_summaries(
    db_path: str = DEFAULT_DB,
    *,
    with_cost_only: bool = False,
) -> list[dict]:
    conn = _conn(db_path)
    sql = "SELECT * FROM mining_costs_summary"
    if with_cost_only:
        sql += " WHERE cost_value IS NOT NULL"
    sql += " ORDER BY ticker"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [_row_to_summary(r) for r in rows]


def recompute_peer_ranks(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    """
    Rank tickers within the same cost_metric cohort (lower cost = better rank 1).
    Updates peer_count, cost_rank, cost_rank_pct, low_cost_quartile in SQLite.
    """
    summaries = list_costs_summaries(db_path)
    by_metric: dict[str, list[dict]] = {}
    for s in summaries:
        m = (s.get("cost_metric") or "").strip()
        v = s.get("cost_value")
        if not m or v is None:
            continue
        by_metric.setdefault(m, []).append(s)

    conn = _conn(db_path)
    updated = 0
    cohorts = {}
    for metric, peers in by_metric.items():
        peers_sorted = sorted(peers, key=lambda x: (x["cost_value"], x["ticker"]))
        n = len(peers_sorted)
        cohorts[metric] = {
            "count": n,
            "tickers": [p["ticker"] for p in peers_sorted],
            "values": [p["cost_value"] for p in peers_sorted],
        }
        for i, p in enumerate(peers_sorted):
            rank = i + 1  # 1 = lowest cost
            # percentile of peers at or more expensive (higher = cheaper)
            # rank_pct: 0 = cheapest, 1 = most expensive
            rank_pct = (rank - 1) / (n - 1) if n > 1 else 0.0
            low_q = rank_pct <= 0.25 if n >= MIN_PEER_FOR_RANK else (rank == 1 and n >= 1)
            conn.execute(
                """
                UPDATE mining_costs_summary
                SET peer_cohort=?, peer_count=?, cost_rank=?, cost_rank_pct=?, low_cost_quartile=?
                WHERE ticker=?
                """,
                (metric, n, rank, round(rank_pct, 4), 1 if low_q else 0, p["ticker"]),
            )
            updated += 1
        # Clear rank for names not in this metric already handled
    # Clear ranks for tickers without cost
    conn.execute(
        """
        UPDATE mining_costs_summary
        SET peer_count=NULL, cost_rank=NULL, cost_rank_pct=NULL, low_cost_quartile=0
        WHERE cost_value IS NULL
        """
    )
    conn.commit()
    conn.close()
    return {"updated": updated, "cohorts": cohorts, "finished_at": datetime.now().isoformat()}


def scan_company(entry: dict, db_path: str = DEFAULT_DB) -> dict:
    ticker = (entry.get("ticker") or "").strip().upper()
    conn = _conn(db_path)
    try:
        summary = fetch_costs_for_company(entry)
        _store_summary(conn, summary)
        conn.commit()
        return summary
    except Exception as e:
        logger.warning("costs scan failed for %s: %s", ticker, e)
        summary = {
            "ticker": ticker,
            "cik": _norm_cik(entry.get("cik")),
            "company_name": entry.get("name") or ticker,
            "filing_date": None,
            "accession": "",
            "form": "",
            "cost_metric": "",
            "cost_value": None,
            "cost_unit": "",
            "cost_commodity": "",
            "cost_raw": "",
            "cost_snippet": "",
            "cost_year": None,
            "peer_cohort": "",
            "peer_count": None,
            "cost_rank": None,
            "cost_rank_pct": None,
            "low_cost_quartile": False,
            "commodities": _preferred_commodities(entry),
            "all_hits": [],
            "parse_notes": [],
            "last_scanned": datetime.now().isoformat(),
            "scan_error": str(e),
            "cost_display": "",
        }
        _store_summary(conn, summary)
        conn.commit()
        return summary
    finally:
        conn.close()


def scan_mining_costs(
    db_path: str = DEFAULT_DB,
    *,
    force: bool = False,
    tickers: list[str] | None = None,
    sleep_seconds: float | None = None,
) -> dict[str, Any]:
    cfg = load_mining_universe()
    sleep = (
        sleep_seconds
        if sleep_seconds is not None
        else float(cfg.get("fetch_sleep_seconds") or EDGAR_FETCH_SLEEP_DEFAULT)
    )
    want = {t.strip().upper() for t in tickers} if tickers else None
    companies = list(cfg.get("companies") or [])
    scanned = skipped = errors = 0
    summaries = []

    for i, entry in enumerate(companies):
        t = (entry.get("ticker") or "").strip().upper()
        if not t:
            continue
        if want is not None and t not in want:
            continue
        if not force:
            existing = get_costs_summary(t, db_path=db_path, max_age_days=SUMMARY_CACHE_MAX_AGE_DAYS)
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
            logger.exception("costs universe scan failed for %s: %s", t, e)
        if sleep and i < len(companies) - 1:
            time.sleep(sleep)

    ranks = recompute_peer_ranks(db_path)
    # Refresh summaries with ranks
    refreshed = []
    for s in summaries:
        full = get_costs_summary(s["ticker"], db_path=db_path, max_age_days=0)
        refreshed.append(full or s)

    return {
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "summaries": refreshed,
        "ranks": ranks,
        "universe_size": len(companies),
        "finished_at": datetime.now().isoformat(),
    }


def attach_costs_to_stock(stock: dict, summary: dict | None = None, ticker: str | None = None) -> dict:
    sym = (ticker or stock.get("ticker") or "").strip().upper().replace(".AX", "")
    if summary is None:
        summary = get_costs_summary(sym, max_age_days=0)
    if not summary:
        stock["unit_costs"] = {
            "scanned": False,
            "cost_value": None,
            "low_cost_quartile": False,
        }
        return stock

    stock["unit_costs"] = {
        "scanned": True,
        "filing_date": summary.get("filing_date"),
        "cost_metric": summary.get("cost_metric") or "",
        "cost_value": summary.get("cost_value"),
        "cost_unit": summary.get("cost_unit") or "",
        "cost_commodity": summary.get("cost_commodity") or "",
        "cost_display": summary.get("cost_display") or "",
        "cost_snippet": summary.get("cost_snippet") or "",
        "cost_year": summary.get("cost_year"),
        "peer_cohort": summary.get("peer_cohort") or "",
        "peer_count": summary.get("peer_count"),
        "cost_rank": summary.get("cost_rank"),
        "cost_rank_pct": summary.get("cost_rank_pct"),
        "low_cost_quartile": bool(summary.get("low_cost_quartile")),
        "parse_notes": summary.get("parse_notes") or [],
        "scan_error": summary.get("scan_error"),
    }
    stock["unit_cost_value"] = summary.get("cost_value")
    stock["unit_cost_rank"] = summary.get("cost_rank")
    stock["unit_cost_low_quartile"] = bool(summary.get("low_cost_quartile"))
    return stock


def enrich_stocks_costs_cached(stocks: list) -> list:
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        if not sym:
            continue
        attach_costs_to_stock(stock, ticker=sym)
    return stocks


def costs_status_public(db_path: str = DEFAULT_DB) -> dict:
    summaries = list_costs_summaries(db_path)
    with_cost = [s for s in summaries if s.get("cost_value") is not None]
    low_q = [s for s in with_cost if s.get("low_cost_quartile")]
    cfg = load_mining_universe()

    by_metric: dict[str, list] = {}
    for s in with_cost:
        m = s.get("cost_metric") or "unknown"
        by_metric.setdefault(m, []).append(s)

    cohorts = []
    for metric, peers in sorted(by_metric.items()):
        peers_sorted = sorted(peers, key=lambda x: (x.get("cost_value") or 9999, x["ticker"]))
        cohorts.append(
            {
                "metric": metric,
                "count": len(peers_sorted),
                "peers": [
                    {
                        "ticker": p["ticker"],
                        "name": p.get("company_name"),
                        "cost_value": p.get("cost_value"),
                        "cost_display": p.get("cost_display"),
                        "cost_rank": p.get("cost_rank"),
                        "cost_rank_pct": p.get("cost_rank_pct"),
                        "low_cost_quartile": p.get("low_cost_quartile"),
                        "commodity": p.get("cost_commodity"),
                        "filing_date": p.get("filing_date"),
                    }
                    for p in peers_sorted
                ],
            }
        )

    return {
        "universe_size": len(cfg.get("companies") or []),
        "summaries_cached": len(summaries),
        "with_cost": len(with_cost),
        "low_cost_quartile": len(low_q),
        "cohorts": cohorts,
        "description": (
            "Phase-3: reported unit costs from 10-K narrative, ranked only within "
            "pilot peers sharing the same metric (not WoodMac/industry curve)."
        ),
        "disclaimer": (
            "Metrics are non-GAAP and definitions differ (AISC vs cash cost vs unit net cash). "
            "Ranks are within this pilot cache only."
        ),
    }
