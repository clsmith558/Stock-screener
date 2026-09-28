"""
Phase-2: Low-cost curve / reserves proxy via S-K 1300 narrative in 10-K.

Parses latest Form 10-K text for proven+probable reserves and annual production,
computes reserve_life_years ≈ reserves / production (same unit), and flags
Exhibit 96 technical report summaries.

Cost ranking is phase 3 (`data/mining_costs.py`). Numbers here are heuristic
regex extracts — always verify in the 10-K.
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

_identity_set = False

# --- quantity helpers ---

_SCALE = {
    "billion": 1e9,
    "million": 1e6,
    "thousand": 1e3,
    "bn": 1e9,
    "mm": 1e6,
    "m": 1e6,
    "": 1.0,
}

_UNIT_CANON = {
    "ton": "tons",
    "tons": "tons",
    "tonne": "tonnes",
    "tonnes": "tonnes",
    "metric ton": "tonnes",
    "metric tons": "tonnes",
    "ounce": "ounces",
    "ounces": "ounces",
    "oz": "ounces",
    "pound": "pounds",
    "pounds": "pounds",
    "lb": "pounds",
    "lbs": "pounds",
}


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


def _to_base(value: float, scale_word: str | None) -> float:
    key = (scale_word or "").strip().lower()
    return value * _SCALE.get(key, 1.0)


def _canon_unit(unit: str | None) -> str:
    if not unit:
        return ""
    u = unit.strip().lower()
    u = re.sub(r"\s+", " ", u)
    if u.startswith("metric "):
        u = u.replace("metric ", "", 1).strip()
        # map metric ton(s) → tonnes
        if u in ("ton", "tons"):
            return "tonnes"
    return _UNIT_CANON.get(u, u)


# Mass unit token: short tons, metric tons, tonnes
_U_MASS = r"(?:metric\s+)?(?:tons?|tonnes?)"

# Reserve narrative patterns: groups = value, scale?, unit?
_RESERVE_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (
        re.compile(
            r"(?:controlled|had|have|with|estimated)?\s*"
            r"(?:approximately|an estimated|estimated)?\s*"
            r"([\d,.]+)\s*(billion|million)?\s*"
            rf"({_U_MASS})\s+of\s+proven\s+and\s+probable\s+"
            r"(?:coal\s+|bituminous\s+coal\s+|marketable\s+)?"
            r"reserves?",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"reserve\s+base\s+of\s+([\d,.]+)\s*(billion|million)?\s*"
            rf"({_U_MASS})\s+of\s+proven\s+and\s+probable",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            rf"([\d,.]+)\s*(billion|million)?\s*({_U_MASS})\s+of\s+proven\s+and\s+probable\s+"
            r"(?:metallurgical|thermal|steelmaking|coal\s+|bituminous\s+coal\s+|marketable\s+)?"
            r"reserves?",
            re.I,
        ),
        "coal",
    ),
    # HCC / S-K style: "estimated reserves totaling 186.2 million metric tons"
    (
        re.compile(
            r"(?:estimated\s+)?(?:recoverable\s+)?reserves?\s+"
            r"(?:totaling|totalled|totaled|of|were|was)\s+"
            r"([\d,.]+)\s*(billion|million)\s*"
            rf"({_U_MASS})",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:approximately|an estimated|estimated)?\s*"
            rf"([\d,.]+)\s*(billion|million)\s*({_U_MASS})\s+of\s+"
            r"(?:recoverable\s+)?(?:coal\s+)?reserves?",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:owned or controlled|controlled)\s+approximately\s+"
            rf"([\d,.]+)\s*(billion|million)?\s*({_U_MASS})\s+of\s+"
            r"(?:marketable\s+)?proven\s+and\s+probable",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:attributable\s+)?proven\s+and\s+probable\s+gold\s+reserves?\s+of\s+"
            r"([\d,.]+)\s*(million|billion)?\s*(ounces?|oz)",
            re.I,
        ),
        "gold",
    ),
    (
        re.compile(
            r"(?:attributable\s+)?proven\s+and\s+probable\s+silver\s+reserves?\s+of\s+"
            r"([\d,.]+)\s*(million|billion)?\s*(ounces?|oz)",
            re.I,
        ),
        "silver",
    ),
    (
        re.compile(
            r"(?:consolidated\s+)?recoverable\s+(?:proven\s+and\s+probable\s+)?"
            r"copper\s+reserves?\s+(?:include|of|total(?:ed|ing)?|were|was)?\s*"
            r"([\d,.]+)\s*(billion|million)\s*(pounds?|lbs?)",
            re.I,
        ),
        "copper",
    ),
    (
        re.compile(
            r"([\d,.]+)\s*(billion|million)\s*(pounds?|lbs?)\s+of\s+(?:recoverable\s+)?"
            r"(?:proven\s+and\s+probable\s+)?copper(?:\s+reserves?)?",
            re.I,
        ),
        "copper",
    ),
    (
        re.compile(
            r"proven\s+and\s+probable\s+(?:mineral\s+)?reserves?\s+of\s+"
            rf"([\d,.]+)\s*(million|billion)?\s*(ounces?|oz|{_U_MASS}|pounds?)",
            re.I,
        ),
        None,
    ),
]

_PRODUCTION_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    # Prefer explicit "production volume of X" (BTU)
    (
        re.compile(
            r"production\s+volume\s+of\s+([\d,.]+)\s*"
            rf"(billion|million)?\s*({_U_MASS})",
            re.I,
        ),
        "coal",
    ),
    # HCC: "steelmaking coal production totaled 9.3 million metric tons"
    (
        re.compile(
            r"(?:steelmaking\s+|metallurgical\s+|met\s+)?coal\s+production\s+"
            r"(?:totaled|totalled|was|of)\s+([\d,.]+)\s*"
            rf"(billion|million)?\s*({_U_MASS})",
            re.I,
        ),
        "coal",
    ),
    # AMR: "We produced approximately 13.7 million tons of met coal"
    (
        re.compile(
            r"produced\s+(?:approximately\s+|a\s+record\s+high\s+)?"
            rf"([\d,.]+)\s*(billion|million)?\s*({_U_MASS})\s+of\s+"
            r"(?:steelmaking\s+|metallurgical\s+|met\s+|thermal\s+)?coal",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:production\s+volume|produced|production)\s+of\s+([\d,.]+)\s*"
            rf"(billion|million)?\s*({_U_MASS})\s+(?:of\s+coal)?",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:in\s+20\d{2},?\s+)?(?:\w+\s+)?sold\s+([\d,.]+)\s*"
            rf"(billion|million)?\s*({_U_MASS})\s+of\s+coal",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:coal\s+)?(?:production|sales)\s+(?:volume\s+)?(?:of\s+|totaled\s+|totalled\s+|was\s+)?"
            rf"([\d,.]+)\s*(billion|million)?\s*({_U_MASS})",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:produced|shipped|sold)\s+([\d,.]+)\s*(million|billion)?\s*"
            r"(?:metric\s+)?(tons?|tonnes?)\s+of\s+(?:steelmaking\s+|met(?:allurgical)?\s+)?"
            r"coal",
            re.I,
        ),
        "coal",
    ),
    (
        re.compile(
            r"(?:attributable\s+)?gold\s+production[:\s]+(?:produced\s+)?"
            r"(?:approximately\s+)?([\d,.]+)\s*(million|billion)?\s*(ounces?|oz)",
            re.I,
        ),
        "gold",
    ),
    (
        re.compile(
            r"produced\s+approximately\s+([\d,.]+)\s*(million|billion)?\s*"
            r"(ounces?|oz)\s+of\s+gold",
            re.I,
        ),
        "gold",
    ),
    (
        re.compile(
            r"([\d,.]+)\s*(million|billion)?\s*(ounces?|oz)\s+of\s+gold\s+"
            r"(?:produced|sold|in\s+20\d{2})",
            re.I,
        ),
        "gold",
    ),
    (
        re.compile(
            r"(?:sales\s+volumes?\s+of|produced|production\s+totaled|totaled)\s+"
            r"([\d,.]+)\s*(billion|million)\s*(pounds?|lbs?)\s+of\s+copper",
            re.I,
        ),
        "copper",
    ),
    (
        re.compile(
            r"([\d,.]+)\s*(billion|million)\s*(pounds?|lbs?)\s+of\s+copper\s+"
            r"(?:in\s+20\d{2}|sold|produced)",
            re.I,
        ),
        "copper",
    ),
]

_SK1300_HINT = re.compile(
    r"(?:subpart\s*1300|regulation\s*s-k\s*1300|item\s*130[0-4]|"
    r"technical\s+report\s+summary|mineral\s+reserves?\s+and\s+mineral\s+resources|"
    r"proven\s+and\s+probable)",
    re.I,
)


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
        CREATE TABLE IF NOT EXISTS mining_reserves_summary (
            ticker TEXT PRIMARY KEY,
            cik TEXT,
            company_name TEXT,
            filing_date TEXT,
            accession TEXT,
            form TEXT,
            has_sk1300_language INTEGER,
            has_exhibit_96 INTEGER,
            exhibit_96_docs_json TEXT,
            reserve_value REAL,
            reserve_unit TEXT,
            reserve_commodity TEXT,
            reserve_raw TEXT,
            reserve_snippet TEXT,
            production_value REAL,
            production_unit TEXT,
            production_commodity TEXT,
            production_raw TEXT,
            production_snippet TEXT,
            reserve_life_years REAL,
            life_confidence TEXT,
            commodities_json TEXT,
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
    raw = entry.get("commodities") or []
    out = []
    for c in raw:
        s = str(c).strip().lower()
        if not s:
            continue
        # normalize tags
        if s in ("met", "metallurgical", "coke"):
            s = "coal"
        if s in ("rare_earth", "potash", "phosphate", "fertilizer", "materials", "minerals", "royalty", "steel", "iron", "aluminum", "uranium"):
            # keep but deprioritize vs primary metals in scoring
            pass
        out.append(s)
    return out


def _score_match(
    commodity_hint: str | None,
    preferred: list[str],
    value: float,
    unit: str,
    kind: str,
) -> float:
    """Higher is better. Prefer preferred commodities and plausible magnitudes."""
    score = 1.0
    hint = (commodity_hint or "").lower()
    if hint and preferred:
        if hint in preferred:
            score += 5.0
        elif hint == "coal" and any(p in ("coal", "met") for p in preferred):
            score += 5.0
        else:
            score -= 1.0
    # Prefer larger company-scale figures (avoid mine-level noise)
    if kind == "reserve":
        if unit in ("tons", "tonnes") and value >= 1e6:
            score += 2.0
        if unit == "ounces" and value >= 1e6:
            score += 2.0
        if unit == "pounds" and value >= 1e9:
            score += 2.0
        # Penalize tiny
        if unit in ("tons", "tonnes") and value < 1e5:
            score -= 2.0
    if kind == "production":
        if unit in ("tons", "tonnes") and 1e5 <= value <= 5e9:
            score += 2.0
        if unit == "ounces" and 1e5 <= value <= 5e7:
            score += 2.0
        if unit == "pounds" and 1e7 <= value <= 5e10:
            score += 2.0
        # Target production / guidance often lower priority if "targeting" — handled via snippet filter
    return score


def _extract_quantity_hits(
    text: str,
    patterns: list[tuple[re.Pattern, str | None]],
    preferred: list[str],
    kind: str,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    if not text:
        return hits
    for pat, commodity_hint in patterns:
        for m in pat.finditer(text):
            g = m.groups()
            # Expected: value, scale?, unit?
            val = _parse_number(g[0] if len(g) > 0 else None)
            if val is None or val <= 0:
                continue
            scale = g[1] if len(g) > 1 else None
            unit_raw = g[2] if len(g) > 2 else None
            # Some patterns may have scale empty and unit in group 1
            scale_l = (scale or "").lower().strip() if scale else ""
            unit_like = (
                "ton", "tons", "tonne", "tonnes", "ounces", "ounce", "oz",
                "pounds", "lbs", "lb", "pound", "metric ton", "metric tons",
                "metric tonne", "metric tonnes",
            )
            if scale_l in unit_like or scale_l.startswith("metric "):
                unit_raw = scale
                scale = None
            unit = _canon_unit(unit_raw)
            if not unit and kind == "reserve":
                # try to infer from pattern commodity
                unit = "tons" if commodity_hint == "coal" else ("ounces" if commodity_hint in ("gold", "silver") else "pounds" if commodity_hint == "copper" else "")
            base = _to_base(val, scale if isinstance(scale, str) else None)
            snip = re.sub(r"\s+", " ", text[max(0, m.start() - 40) : m.end() + 80]).strip()
            raw_l = (m.group(0) or "").lower()
            snip_l = snip.lower()
            # Prefer company-level language; skip noisy production hits carefully
            if kind == "production":
                company_voice = bool(
                    re.search(r"\b(we|our|the company|company'?s)\b", snip, re.I)
                    or re.search(r"\bproduction\s+volume\b", raw_l)
                )
                if re.search(r"\b(targeting|target|guidance|expected to)\b", snip, re.I):
                    # keep only if not pure guidance sentence
                    if re.search(r"\btargeting annual production\b", snip, re.I):
                        continue
                # Contract backlog adjacent to sales, not "production volume of"
                if re.search(r"\bbacklog\b", snip, re.I) and "production volume" not in raw_l:
                    if re.search(r"\b(contract|committed)\b", snip, re.I) or re.search(
                        r"backlog.{0,60}\btons\b", snip, re.I
                    ):
                        continue
                # Macro / peer country aggregates (e.g. China production)
                if not company_voice and re.search(
                    r"\b(countries|year-over-year|world\s+production|global\s+production)\b",
                    snip,
                    re.I,
                ):
                    continue
                if re.search(
                    r"\b(countries.{0,40}production of|industry data|U\.S\.\s+in\s+20\d{2}.{0,30}we produced)\b",
                    snip,
                    re.I,
                ):
                    # keep "we produced X" even near industry sentences unless it's the industry total
                    if not re.search(r"\bwe produced\b", snip, re.I):
                        continue
                if re.search(
                    r"\bof the approximately\s+[\d,.]+\s+million\s+tons\s+(?:of\s+\w+\s+)?produced in the U\.?S",
                    snip,
                    re.I,
                ):
                    continue

            # Prefer company production volume wording over "sold"
            score_boost = 0.0
            if kind == "production" and re.search(r"\bproduction\s+volume\b", raw_l):
                score_boost += 2.0
            elif kind == "production" and re.search(r"\bwe produced\b", snip, re.I):
                score_boost += 1.5
            elif kind == "production" and re.search(r"\bsold\b", raw_l):
                score_boost -= 0.5
            # Skip mine-level reserve fragments when total language exists (score later)
            if kind == "reserve" and re.search(
                r"\b(additional reserves|lease tract|Mine No\.|Blue Creek Mine reserve tonnage)\b",
                snip,
                re.I,
            ):
                score_boost -= 2.0
            if kind == "reserve" and re.search(
                r"\b(totaling|totalled|totaled|reserve base|controlled approximately)\b",
                snip,
                re.I,
            ):
                score_boost += 1.5
            score = _score_match(commodity_hint, preferred, base, unit, kind) + score_boost
            hits.append(
                {
                    "value": base,
                    "display_value": val,
                    "scale": (scale or "").lower() if scale else "",
                    "unit": unit,
                    "commodity": commodity_hint,
                    "snippet": snip[:280],
                    "raw": m.group(0)[:160],
                    "score": score,
                    "start": m.start(),
                }
            )
    hits.sort(key=lambda h: (-h["score"], -h["value"]))
    return hits


def _pick_best(hits: list[dict], preferred: list[str]) -> dict | None:
    if not hits:
        return None
    # Prefer hits matching preferred commodity unit family
    for h in hits:
        c = (h.get("commodity") or "").lower()
        if preferred and c and c in preferred:
            return h
    return hits[0]


def _compatible_units(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # tons ~ tonnes (short tons vs metric — imperfect but usable for life order-of-magnitude)
    if {a, b} <= {"tons", "tonnes"}:
        return True
    return False


def _format_qty(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    if value >= 1e9:
        return f"{value / 1e9:.2f} billion {unit}".strip()
    if value >= 1e6:
        return f"{value / 1e6:.1f} million {unit}".strip()
    if value >= 1e3:
        return f"{value / 1e3:.1f} thousand {unit}".strip()
    return f"{value:.0f} {unit}".strip()


def list_exhibit_96(filing) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        atts = getattr(filing, "attachments", None)
        if atts is None:
            return out
        for a in atts:
            doc = str(getattr(a, "document", "") or "")
            desc = str(getattr(a, "description", "") or "")
            doc_l = doc.lower()
            desc_l = desc.lower()
            is_96 = (
                re.search(r"ex-?96", doc_l)
                or re.search(r"ex-?96", desc_l)
                or "technical report" in desc_l
                or re.search(r"xex96", doc_l)
                or re.search(r"ex96", doc_l)
            )
            # Exclude XBRL idea R96.htm noise unless also technical
            if doc_l.startswith("r96") and "technical" not in desc_l:
                continue
            if is_96:
                out.append({"document": doc, "description": desc})
    except Exception as e:
        logger.debug("list_exhibit_96 failed: %s", e)
    return out


def parse_reserves_from_10k_text(
    text: str,
    preferred_commodities: list[str] | None = None,
) -> dict[str, Any]:
    """
    Extract best reserve + production estimates and reserve life from 10-K plain text.
    """
    preferred = list(preferred_commodities or [])
    notes: list[str] = []
    has_sk = bool(_SK1300_HINT.search(text or ""))

    reserve_hits = _extract_quantity_hits(text, _RESERVE_PATTERNS, preferred, "reserve")
    prod_hits = _extract_quantity_hits(text, _PRODUCTION_PATTERNS, preferred, "production")

    reserve = _pick_best(reserve_hits, preferred)
    production = None

    if reserve:
        # Prefer production with matching unit + commodity
        r_unit = reserve.get("unit") or ""
        r_comm = (reserve.get("commodity") or "").lower()
        candidates = []
        for p in prod_hits:
            score = p["score"]
            if _compatible_units(r_unit, p.get("unit") or ""):
                score += 4
            if r_comm and p.get("commodity") == r_comm:
                score += 3
            candidates.append((score, p))
        candidates.sort(key=lambda x: -x[0])
        production = candidates[0][1] if candidates else None
    else:
        production = _pick_best(prod_hits, preferred)

    life = None
    life_conf = "none"
    if reserve and production and _compatible_units(reserve["unit"], production["unit"]):
        if production["value"] > 0:
            raw_life = round(reserve["value"] / production["value"], 1)
            # Sanity band for usable screen filter (reject unit-mismatch garbage)
            if 0.5 <= raw_life <= 200:
                life = raw_life
                life_conf = "low"
                if reserve.get("commodity") and production.get("commodity"):
                    life_conf = "medium"
                if reserve.get("commodity") and reserve.get("commodity") == production.get("commodity"):
                    life_conf = "medium"
            else:
                notes.append(f"implausible life {raw_life}y — ratio discarded")
                life = None
                life_conf = "none"
    elif reserve and production:
        notes.append(
            f"unit mismatch reserve={reserve.get('unit')} vs production={production.get('unit')}"
        )
    if not reserve:
        notes.append("no proven/probable reserve figure matched")
    if not production:
        notes.append("no production figure matched")

    return {
        "has_sk1300_language": has_sk,
        "reserve": reserve,
        "production": production,
        "reserve_life_years": life,
        "life_confidence": life_conf,
        "parse_notes": notes,
        "reserve_hits_n": len(reserve_hits),
        "production_hits_n": len(prod_hits),
    }


def fetch_reserves_for_company(entry: dict) -> dict[str, Any]:
    """Download latest 10-K and parse reserves/production."""
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
        # try 20-F for foreign private issuers
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
        text = ""

    parsed = parse_reserves_from_10k_text(text, preferred)
    ex96 = list_exhibit_96(filing)

    reserve = parsed.get("reserve") or {}
    production = parsed.get("production") or {}

    return {
        "ticker": ticker,
        "cik": cik,
        "company_name": str(name),
        "filing_date": str(fdate)[:10] if fdate else None,
        "accession": str(acc),
        "form": form,
        "has_sk1300_language": bool(parsed.get("has_sk1300_language")),
        "has_exhibit_96": bool(ex96),
        "exhibit_96_docs": ex96,
        "reserve_value": reserve.get("value"),
        "reserve_unit": reserve.get("unit") or "",
        "reserve_commodity": reserve.get("commodity") or "",
        "reserve_raw": reserve.get("raw") or "",
        "reserve_snippet": reserve.get("snippet") or "",
        "production_value": production.get("value"),
        "production_unit": production.get("unit") or "",
        "production_commodity": production.get("commodity") or "",
        "production_raw": production.get("raw") or "",
        "production_snippet": production.get("snippet") or "",
        "reserve_life_years": parsed.get("reserve_life_years"),
        "life_confidence": parsed.get("life_confidence") or "none",
        "commodities": preferred,
        "parse_notes": parsed.get("parse_notes") or [],
        "last_scanned": datetime.now().isoformat(),
        "scan_error": None,
        "reserve_display": _format_qty(reserve.get("value"), reserve.get("unit") or ""),
        "production_display": _format_qty(production.get("value"), production.get("unit") or ""),
    }


def _store_summary(conn: sqlite3.Connection, summary: dict) -> None:
    conn.execute(
        """
        INSERT INTO mining_reserves_summary (
            ticker, cik, company_name, filing_date, accession, form,
            has_sk1300_language, has_exhibit_96, exhibit_96_docs_json,
            reserve_value, reserve_unit, reserve_commodity, reserve_raw, reserve_snippet,
            production_value, production_unit, production_commodity, production_raw, production_snippet,
            reserve_life_years, life_confidence, commodities_json, parse_notes_json,
            last_scanned, scan_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            cik=excluded.cik,
            company_name=excluded.company_name,
            filing_date=excluded.filing_date,
            accession=excluded.accession,
            form=excluded.form,
            has_sk1300_language=excluded.has_sk1300_language,
            has_exhibit_96=excluded.has_exhibit_96,
            exhibit_96_docs_json=excluded.exhibit_96_docs_json,
            reserve_value=excluded.reserve_value,
            reserve_unit=excluded.reserve_unit,
            reserve_commodity=excluded.reserve_commodity,
            reserve_raw=excluded.reserve_raw,
            reserve_snippet=excluded.reserve_snippet,
            production_value=excluded.production_value,
            production_unit=excluded.production_unit,
            production_commodity=excluded.production_commodity,
            production_raw=excluded.production_raw,
            production_snippet=excluded.production_snippet,
            reserve_life_years=excluded.reserve_life_years,
            life_confidence=excluded.life_confidence,
            commodities_json=excluded.commodities_json,
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
            1 if summary.get("has_sk1300_language") else 0,
            1 if summary.get("has_exhibit_96") else 0,
            json.dumps(summary.get("exhibit_96_docs") or []),
            summary.get("reserve_value"),
            summary.get("reserve_unit"),
            summary.get("reserve_commodity"),
            summary.get("reserve_raw"),
            summary.get("reserve_snippet"),
            summary.get("production_value"),
            summary.get("production_unit"),
            summary.get("production_commodity"),
            summary.get("production_raw"),
            summary.get("production_snippet"),
            summary.get("reserve_life_years"),
            summary.get("life_confidence"),
            json.dumps(summary.get("commodities") or []),
            json.dumps(summary.get("parse_notes") or []),
            summary.get("last_scanned"),
            summary.get("scan_error"),
        ),
    )


def _row_to_summary(row: tuple) -> dict[str, Any]:
    (
        ticker, cik, company_name, filing_date, accession, form,
        has_sk1300_language, has_exhibit_96, exhibit_96_docs_json,
        reserve_value, reserve_unit, reserve_commodity, reserve_raw, reserve_snippet,
        production_value, production_unit, production_commodity, production_raw, production_snippet,
        reserve_life_years, life_confidence, commodities_json, parse_notes_json,
        last_scanned, scan_error,
    ) = row
    try:
        ex96 = json.loads(exhibit_96_docs_json) if exhibit_96_docs_json else []
    except Exception:
        ex96 = []
    try:
        commodities = json.loads(commodities_json) if commodities_json else []
    except Exception:
        commodities = []
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
        "has_sk1300_language": bool(has_sk1300_language),
        "has_exhibit_96": bool(has_exhibit_96),
        "exhibit_96_docs": ex96,
        "reserve_value": reserve_value,
        "reserve_unit": reserve_unit or "",
        "reserve_commodity": reserve_commodity or "",
        "reserve_raw": reserve_raw or "",
        "reserve_snippet": reserve_snippet or "",
        "production_value": production_value,
        "production_unit": production_unit or "",
        "production_commodity": production_commodity or "",
        "production_raw": production_raw or "",
        "production_snippet": production_snippet or "",
        "reserve_life_years": reserve_life_years,
        "life_confidence": life_confidence or "none",
        "commodities": commodities,
        "parse_notes": notes,
        "last_scanned": last_scanned,
        "scan_error": scan_error,
        "reserve_display": _format_qty(reserve_value, reserve_unit or ""),
        "production_display": _format_qty(production_value, production_unit or ""),
    }


def get_reserves_summary(
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
            "SELECT * FROM mining_reserves_summary WHERE ticker=?",
            (sym,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        # last_scanned is near end of row
        if max_age_days > 0 and row[-2]:
            try:
                lu = datetime.fromisoformat(row[-2])
                if datetime.now() - lu > timedelta(days=max_age_days):
                    return None
            except Exception:
                pass
        return _row_to_summary(row)
    except Exception as e:
        logger.warning("get_reserves_summary failed for %s: %s", sym, e)
        return None


def list_reserves_summaries(
    db_path: str = DEFAULT_DB,
    *,
    with_life_only: bool = False,
) -> list[dict]:
    conn = _conn(db_path)
    sql = "SELECT * FROM mining_reserves_summary"
    if with_life_only:
        sql += " WHERE reserve_life_years IS NOT NULL"
    sql += " ORDER BY ticker"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [_row_to_summary(r) for r in rows]


def scan_company(entry: dict, db_path: str = DEFAULT_DB) -> dict:
    ticker = (entry.get("ticker") or "").strip().upper()
    conn = _conn(db_path)
    try:
        summary = fetch_reserves_for_company(entry)
        _store_summary(conn, summary)
        conn.commit()
        return summary
    except Exception as e:
        logger.warning("reserves scan failed for %s: %s", ticker, e)
        summary = {
            "ticker": ticker,
            "cik": _norm_cik(entry.get("cik")),
            "company_name": entry.get("name") or ticker,
            "filing_date": None,
            "accession": "",
            "form": "",
            "has_sk1300_language": False,
            "has_exhibit_96": False,
            "exhibit_96_docs": [],
            "reserve_value": None,
            "reserve_unit": "",
            "reserve_commodity": "",
            "reserve_raw": "",
            "reserve_snippet": "",
            "production_value": None,
            "production_unit": "",
            "production_commodity": "",
            "production_raw": "",
            "production_snippet": "",
            "reserve_life_years": None,
            "life_confidence": "none",
            "commodities": _preferred_commodities(entry),
            "parse_notes": [],
            "last_scanned": datetime.now().isoformat(),
            "scan_error": str(e),
            "reserve_display": "",
            "production_display": "",
        }
        _store_summary(conn, summary)
        conn.commit()
        return summary
    finally:
        conn.close()


def scan_mining_reserves(
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
            existing = get_reserves_summary(t, db_path=db_path, max_age_days=SUMMARY_CACHE_MAX_AGE_DAYS)
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
            logger.exception("reserves universe scan failed for %s: %s", t, e)
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


def attach_reserves_to_stock(stock: dict, summary: dict | None = None, ticker: str | None = None) -> dict:
    sym = (ticker or stock.get("ticker") or "").strip().upper().replace(".AX", "")
    if summary is None:
        summary = get_reserves_summary(sym, max_age_days=0)
    if not summary:
        stock["reserves"] = {
            "scanned": False,
            "reserve_life_years": None,
            "has_exhibit_96": False,
        }
        return stock

    life = summary.get("reserve_life_years")
    stock["reserves"] = {
        "scanned": True,
        "filing_date": summary.get("filing_date"),
        "has_sk1300_language": bool(summary.get("has_sk1300_language")),
        "has_exhibit_96": bool(summary.get("has_exhibit_96")),
        "reserve_life_years": life,
        "life_confidence": summary.get("life_confidence"),
        "reserve_display": summary.get("reserve_display") or "",
        "production_display": summary.get("production_display") or "",
        "reserve_commodity": summary.get("reserve_commodity") or "",
        "reserve_snippet": summary.get("reserve_snippet") or "",
        "production_snippet": summary.get("production_snippet") or "",
        "exhibit_96_docs": summary.get("exhibit_96_docs") or [],
        "parse_notes": summary.get("parse_notes") or [],
        "scan_error": summary.get("scan_error"),
    }
    stock["reserve_life_years"] = life
    stock["has_exhibit_96"] = bool(summary.get("has_exhibit_96"))
    return stock


def enrich_stocks_reserves_cached(stocks: list) -> list:
    for stock in stocks:
        sym = (stock.get("ticker") or "").strip().upper()
        if not sym:
            continue
        attach_reserves_to_stock(stock, ticker=sym)
    return stocks


def reserves_status_public(db_path: str = DEFAULT_DB) -> dict:
    summaries = list_reserves_summaries(db_path)
    with_life = [s for s in summaries if s.get("reserve_life_years") is not None]
    with_96 = [s for s in summaries if s.get("has_exhibit_96")]
    with_res = [s for s in summaries if s.get("reserve_value")]
    cfg = load_mining_universe()
    return {
        "universe_size": len(cfg.get("companies") or []),
        "summaries_cached": len(summaries),
        "with_reserves": len(with_res),
        "with_reserve_life": len(with_life),
        "with_exhibit_96": len(with_96),
        "lives": [
            {
                "ticker": s["ticker"],
                "name": s.get("company_name"),
                "reserve_life_years": s.get("reserve_life_years"),
                "life_confidence": s.get("life_confidence"),
                "reserve_display": s.get("reserve_display"),
                "production_display": s.get("production_display"),
                "commodity": s.get("reserve_commodity"),
                "has_exhibit_96": s.get("has_exhibit_96"),
                "filing_date": s.get("filing_date"),
            }
            for s in sorted(
                with_life,
                key=lambda x: -(x.get("reserve_life_years") or 0),
            )
        ],
        "description": "Phase-2: S-K 1300 / 10-K proven+probable reserves and reserve life (heuristic parse).",
    }
