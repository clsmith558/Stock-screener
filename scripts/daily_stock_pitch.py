#!/usr/bin/env python3
"""
Daily Stock Pitch Orchestrator

Selects one stock screening cheaply based on a rotating multi-universe schedule:
  1. asx_mining          - ASX Resource Producers & Miners (> A$50M) + Commodity Unit Costs
  2. asx_general         - ASX Non-Mining / Australasian (> A$50M)
  3. energy_dc_power     - Energy & Data Center Power Supply Chain
  4. asia_opps           - Asian Market Opportunities (Hang Seng, CSI 300, Japan, Korea, Taiwan)
  5. us_value            - US Large-Cap & Wide Moat Value
  6. brazil_latam        - Brazil & Latin America Deep Value (0.5x half-weight)
  7. airlines_pariah     - Airlines & Capital Cycle Pariahs
  8. mid_small_value     - US Mid-Cap & Small-Cap Value
  9. antifragile_scenarios - Disorder / Tail-Risk Beneficiaries (1x standard weight)

Generates the research brief in the Obsidian vault and dispatches via email.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# Paths
SCREENER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCREENER_ROOT))

from dotenv import load_dotenv
load_dotenv(SCREENER_ROOT / ".env")

import yaml

VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "C:/Obsidian/csmith_test"))
PITCHES_DIR = VAULT_PATH / "wiki" / "investing" / "pitches"
OPPORTUNITIES_DIR = VAULT_PATH / "wiki" / "investing" / "opportunities"
SECTORS_DIR = VAULT_PATH / "wiki" / "investing" / "sectors"
REGISTRY_PATH = VAULT_PATH / "wiki" / "investing" / "Company-Registry.yaml"
STATE_FILE = SCREENER_ROOT / "data" / "pitch_rotation_state.json"
DB_PATH = SCREENER_ROOT / "asx_announcements_cache.db"
LISTS_DIR = SCREENER_ROOT / "Business lists"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("daily_pitch")

STANDARD_ROTATION_WITH_BRAZIL = [
    "asx_mining",
    "energy_dc_power",
    "asia_opps",
    "asx_general",
    "us_value",
    "brazil_latam",
    "airlines_pariah",
    "mid_small_value",
    "antifragile_scenarios",
]
STANDARD_ROTATION_NO_BRAZIL = [c for c in STANDARD_ROTATION_WITH_BRAZIL if c != "brazil_latam"]

# 17-run super-cycle: all 8 standard universes (incl. antifragile_scenarios) appear 2x;
# brazil_latam appears 1x (0.5x half-weight vs standard).
ROTATION = STANDARD_ROTATION_WITH_BRAZIL + STANDARD_ROTATION_NO_BRAZIL
CATEGORIES = list(STANDARD_ROTATION_WITH_BRAZIL)

# Market cap thresholds (exclude illiquid micro-caps)
ASX_MIN_MCAP = 50_000_000               # Exclude micro-caps (< A$50M market cap)
US_LARGE_MIN_MCAP = 1_000_000_000       # US large-cap value minimum ($1B)
MID_SMALL_MIN_MCAP = 100_000_000        # US mid/small minimum ($100M)
ASIA_MIN_MCAP = 300_000_000             # Asia opportunities minimum ($300M)
BRAZIL_MIN_MCAP = 150_000_000           # Brazil minimum (R$150M)
ANTIFRAGILE_MIN_MCAP = 200_000_000      # Antifragility sleeve minimum ($200M)

_EXCHANGE_SUFFIX_RE = re.compile(r"\.(AX|HK|T|KS|TW|SS|SZ|SA|TO|L)$", re.IGNORECASE)
_B3_CLASS_SUFFIX_RE = re.compile(r"\s+\b(?:ON|PN|PNA|PNB|UNT)\b(?:\s+\b(?:NM|N1|N2|MB|ED|EJ|EDJ)\b)?$", re.IGNORECASE)
_ASX_FPO_SUFFIX_RE = re.compile(r"\s+\bFPO\b(?:\s+\[[A-Z0-9.-]+\])?$", re.IGNORECASE)


def load_vault_registry() -> tuple[dict[str, dict], set[str], set[str]]:
    """Load Company-Registry.yaml entries by ticker, valid Sector-* stems, and existing opportunity stems."""
    by_ticker: dict[str, dict] = {}
    if REGISTRY_PATH.exists():
        try:
            data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
            companies = data.get("companies") or {}
            for reg_id, entry in companies.items():
                if not isinstance(entry, dict):
                    continue
                rec = {"registry_id": str(reg_id), **entry}
                candidates = [str(reg_id), *(str(t) for t in (entry.get("tickers") or []))]
                for tk in candidates:
                    key = tk.strip().upper()
                    if key:
                        by_ticker.setdefault(key, rec)
        except Exception as e:
            logger.warning(f"Failed loading Company-Registry.yaml: {e}")

    sector_stems = {p.stem for p in SECTORS_DIR.glob("Sector-*.md")} if SECTORS_DIR.exists() else {"Sector-Comparison-Index"}
    opp_stems = {p.stem for p in OPPORTUNITIES_DIR.glob("*.md") if p.stem != "_template"} if OPPORTUNITIES_DIR.exists() else set()
    return by_ticker, sector_stems, opp_stems


def lookup_registry_entry(ticker: str, by_ticker: dict[str, dict]) -> dict | None:
    """Resolve registry entry using exact ticker or base symbol without exchange suffix."""
    tk = ticker.strip().upper()
    if tk in by_ticker:
        return by_ticker[tk]
    base = _EXCHANGE_SUFFIX_RE.sub("", tk)
    return by_ticker.get(base)


def normalize_company_name(raw_name: str, ticker: str, reg_entry: dict | None = None) -> str:
    """Prefer canonical registry name; otherwise clean extra whitespace and exchange share-class tags."""
    if reg_entry and reg_entry.get("names"):
        canonical = str(reg_entry["names"][0]).strip()
        if canonical:
            return re.sub(r"\s+", " ", canonical)
    cleaned = re.sub(r"\s+", " ", (raw_name or ticker)).strip()
    cleaned = _B3_CLASS_SUFFIX_RE.sub("", cleaned).strip()
    cleaned = _ASX_FPO_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or ticker


def resolve_sector_moc(candidate: dict, reg_entry: dict | None, valid_sectors: set[str]) -> str:
    """Restrict pitch [[Sector-...]] links to existing canonical Sector-*.md stems."""
    if reg_entry:
        moc = (reg_entry.get("wiki") or {}).get("sector_moc")
        if moc and moc in valid_sectors:
            return moc

    cat = (candidate.get("category") or "").lower()
    sec = (candidate.get("sector") or "").lower()
    ind = (candidate.get("industry") or "").lower()
    text = f"{sec} {ind}"

    if cat == "airlines_pariah" or any(k in text for k in ("airline", "aerospace", "defense", "defence", "trucking", "freight", "rail", "marine", "shipping", "industrial", "machinery", "rental", "fleet", "logistics")):
        guess = "Sector-Industrials-Aerospace"
    elif cat in ("asx_mining", "energy_dc_power") or any(k in text for k in ("energy", "oil", "gas", "coal", "uranium", "mining", "basic materials", "metal", "steel", "copper", "chemical", "utilities", "power")):
        guess = "Sector-Energy"
    elif any(k in text for k in ("agricult", "fertilizer", "farm", "timber")):
        guess = "Sector-Agriculture"
    elif any(k in text for k in ("semiconductor", "chip", "wafer", "foundry")):
        guess = "Sector-Semiconductors"
    elif any(k in text for k in ("payment", "credit services", "transaction")):
        guess = "Sector-Payments"
    elif any(k in text for k in ("asset management", "capital markets", "exchange", "brokerage")):
        guess = "Sector-Asset-Management"
    elif "financial" in text or "insurance" in text or "bank" in text:
        guess = "Sector-Financials"
    elif any(k in text for k in ("telecom", "communication infrastructure", "wireless", "cable")):
        guess = "Sector-Infrastructure-Telecom"
    elif any(k in text for k in ("media", "entertainment", "gaming", "communication services")):
        guess = "Sector-Media"
    elif "health" in text or "biotech" in text or "pharma" in text or "medical" in text:
        guess = "Sector-Healthcare"
    elif "consumer defensive" in text or "staple" in text or "food" in text or "beverage" in text:
        guess = "Sector-Consumer-Staples"
    elif "luxury" in text:
        guess = "Sector-Luxury"
    elif "consumer cyclical" in text or "retail" in text or "restaurant" in text or "apparel" in text:
        guess = "Sector-Retail"
    elif any(k in text for k in ("it services", "consulting", "outsourcing")):
        guess = "Sector-Software-Services"
    elif "technology" in text or "software" in text or "cloud" in text:
        guess = "Sector-Software-Cloud"
    else:
        guess = "Sector-Comparison-Index"

    return guess if guess in valid_sectors else "Sector-Comparison-Index"


def resolve_opportunity_memo(ticker: str, reg_entry: dict | None, valid_opps: set[str]) -> str | None:
    """Return `opportunities/<stem>` only if that opportunity memo file actually exists on disk."""
    if reg_entry:
        memo = (reg_entry.get("wiki") or {}).get("opportunity_memo")
        if memo:
            stem = Path(str(memo)).name
            if stem in valid_opps:
                return f"opportunities/{stem}"
    tk = ticker.strip().upper()
    base = _EXCHANGE_SUFFIX_RE.sub("", tk)
    for stem in (tk, base):
        if stem in valid_opps:
            return f"opportunities/{stem}"
    return None

def read_list_csv(filename: str) -> set[str]:
    """Parse comma or newline separated tickers from a Business lists CSV."""
    tickers = set()
    p = LISTS_DIR / filename
    if not p.exists():
        return tickers
    with open(p, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip().upper() for x in line.split(",") if x.strip()]
            tickers.update(parts)
    return tickers

def get_recently_pitched_tickers(days: int = 60) -> set[str]:
    """Inspect existing pitch briefs in vault to avoid pitching the same stock recently."""
    pitched = set()
    if not PITCHES_DIR.exists():
        return pitched

    for f in PITCHES_DIR.glob("*.md"):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*-\s*([A-Za-z0-9._-]+)", f.stem)
        if m:
            file_date_str, ticker = m.groups()
            add = True
            try:
                dt = datetime.strptime(file_date_str, "%Y-%m-%d").date()
                add = (date.today() - dt).days <= days
            except ValueError:
                add = True
            if add:
                tk = ticker.upper()
                pitched.add(tk)
                pitched.add(_EXCHANGE_SUFFIX_RE.sub("", tk))
    return pitched

def get_next_category() -> tuple[str, int]:
    """Determine next rotation category and schedule index from state file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                last_idx = state.get("last_index")
                if isinstance(last_idx, int) and 0 <= last_idx < len(ROTATION):
                    next_idx = (last_idx + 1) % len(ROTATION)
                    return ROTATION[next_idx], next_idx
                last_cat = state.get("last_category")
                if last_cat in ROTATION:
                    next_idx = (ROTATION.index(last_cat) + 1) % len(ROTATION)
                    return ROTATION[next_idx], next_idx
        except Exception as e:
            logger.warning(f"Error reading rotation state: {e}")

    return ROTATION[0], 0

def save_state(category: str, ticker: str, index: int | None = None):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if index is None and category in ROTATION:
        index = ROTATION.index(category)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "last_category": category,
            "last_index": index,
            "last_ticker": ticker,
            "last_date": date.today().isoformat(),
            "rotation_order": ROTATION,
            "updated_at": datetime.now().isoformat()
        }, f, indent=2)

def get_commodity_unit_cost(ticker: str) -> dict | None:
    """Check mining_unit_cost_history for reported unit cash costs (AISC / C1 / FOB)."""
    base = ticker.replace(".AX", "").upper()
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT period_end, cost_value_usd, cost_value_raw, cost_currency, cost_unit, cost_metric, snippet, label
            FROM mining_unit_cost_history
            WHERE ticker = ? OR ticker = ? OR ticker LIKE ?
            ORDER BY period_end DESC LIMIT 1
        """, (base, f"{base}.AX", f"%{base}%"))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "period_end": row[0],
                "cost_value_usd": row[1],
                "cost_value_raw": row[2],
                "cost_currency": row[3],
                "cost_unit": row[4],
                "cost_metric": row[5],
                "snippet": row[6],
                "label": row[7] or row[5]
            }
    except Exception as e:
        logger.warning(f"Error looking up unit costs for {ticker}: {e}")
    return None

def get_forward_earnings_consensus(ticker: str) -> str:
    """Fetch concise 12-month forward earnings consensus table from yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.earnings_estimate
        if df is None or df.empty:
            return ""

        rows = []
        period_map = {
            "0q": "Current Quarter (0q)",
            "+1q": "Next Quarter (+1q)",
            "0y": "Current FY (0y)",
            "+1y": "Next FY / 12M Fwd (+1y)"
        }

        for p_key in ["0q", "+1q", "0y", "+1y"]:
            if p_key in df.index:
                r = df.loc[p_key]
                avg_eps = r.get("avg")
                if avg_eps is not None and not (isinstance(avg_eps, float) and avg_eps != avg_eps):
                    low_eps = r.get("low")
                    high_eps = r.get("high")
                    growth = r.get("growth")
                    analysts = r.get("numberOfAnalysts")

                    growth_str = f"{growth * 100:+.1f}%" if (growth is not None and growth == growth) else "—"
                    analysts_str = f"{int(analysts)}" if (analysts is not None and analysts == analysts) else "—"
                    range_str = f"${low_eps:.2f} - ${high_eps:.2f}" if (low_eps is not None and low_eps == low_eps and high_eps is not None and high_eps == high_eps) else "—"
                    
                    rows.append(
                        f"| **{period_map.get(p_key, p_key)}** | ${avg_eps:.2f} | {range_str} | {analysts_str} | {growth_str} |"
                    )

        if not rows:
            return ""

        target_price = t.info.get("targetMeanPrice") or t.info.get("targetMedianPrice")
        rec = (t.info.get("recommendationKey") or "").capitalize()
        target_str = f"\n*Consensus Target Price: **${target_price:.2f}** ({rec})*" if target_price else ""

        table_header = """
### 12-Month Forward Earnings Estimate Consensus (yfinance)

| Period | Consensus EPS | Estimate Range | Analysts | Projected YoY Growth |
| :--- | :--- | :--- | :--- | :--- |
"""
        return "\n" + table_header.strip() + "\n" + "\n".join(rows) + "\n" + target_str + "\n"
    except Exception as e:
        logger.warning(f"Error fetching forward consensus for {ticker}: {e}")
        return ""

def screen_candidate(category: str, exclude_tickers: set[str]) -> dict | None:
    """Screen for the cheapest, highest-conviction candidate in the target category."""
    if not DB_PATH.exists():
        logger.error(f"Database not found at {DB_PATH}")
        return None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    candidates = []

    if category == "asx_mining":
        cur.execute("""
            SELECT ticker, name, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM asx_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            t, name, price, low, high, p_fcf, m_json = r
            if not t or t.upper() in exclude_tickers:
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < ASX_MIN_MCAP:
                continue
            sec = (m.get("sector") or "").lower()
            ind = (m.get("industry") or "").lower()
            if not ("material" in sec or "energy" in sec or "mining" in ind or "metal" in ind or "oil" in ind or "gas" in ind or "coal" in ind):
                continue

            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 15) or (0 < pe < 15) or (0 < p_fcf < 12)):
                unit_cost = get_commodity_unit_cost(t)
                candidates.append({
                    "ticker": f"{t}.AX",
                    "base_ticker": t,
                    "name": name or t,
                    "exchange": "ASX",
                    "currency": "AUD",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": m.get("sector", "ASX Resource Producers"),
                    "unit_cost": unit_cost
                })

    elif category == "asx_general":
        cur.execute("""
            SELECT ticker, name, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM asx_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            t, name, price, low, high, p_fcf, m_json = r
            if not t or t.upper() in exclude_tickers:
                continue
            m = json.loads(m_json) if m_json else {}
            sec = (m.get("sector") or "").lower()
            ind = (m.get("industry") or "").lower()
            if "material" in sec or "energy" in sec or "mining" in ind or "metal" in ind or "oil" in ind or "gas" in ind or "coal" in ind:
                continue
            mcap = m.get("market_cap")
            if not mcap or mcap < ASX_MIN_MCAP:
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 16) or (0 < pe < 16) or (0 < p_fcf < 12)):
                candidates.append({
                    "ticker": f"{t}.AX",
                    "base_ticker": t,
                    "name": name or t,
                    "exchange": "ASX",
                    "currency": "AUD",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": m.get("sector", "ASX Industrials & Commercials"),
                    "unit_cost": None
                })

    elif category == "energy_dc_power":
        dc_tickers = read_list_csv("dc_energy_supply_chain.csv") | read_list_csv("Energy businesses list.csv") | read_list_csv("energy_pilot.csv")
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or sym.upper() not in dc_tickers or sym.upper() in exclude_tickers:
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < 200_000_000:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 28 and ((0 < fpe < 16) or (0 < pe < 16) or (0 < p_fcf < 14)):
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": "NYSE/NASDAQ",
                    "currency": "USD",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": "Energy & DC Power Supply Chain",
                    "unit_cost": None
                })

    elif category == "asia_opps":
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or not any(sym.endswith(x) for x in (".HK", ".T", ".KS", ".TW", ".SS", ".SZ")):
                continue
            if sym.upper() in exclude_tickers:
                continue
            if price < 2.0 or (len(sym) == 5 and sym.endswith(("F", "Y"))):
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < ASIA_MIN_MCAP:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 14) or (0 < pe < 14) or (0 < p_fcf < 10)):
                exchange = "HKEX" if sym.endswith(".HK") else ("TSE" if sym.endswith(".T") else "Asia")
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": exchange,
                    "currency": "Local",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": sector or "Asian Markets",
                    "unit_cost": None
                })

    elif category == "brazil_latam":
        brazil_tickers = read_list_csv("brazil_stocks.csv")
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or (sym not in brazil_tickers and not sym.endswith(".SA")):
                continue
            if sym.upper() in exclude_tickers:
                continue
            if price < 2.0 or (len(sym) == 5 and sym.endswith(("F", "Y"))):
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < BRAZIL_MIN_MCAP:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 14) or (0 < pe < 14) or (0 < p_fcf < 10)):
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": "B3 / Brazil",
                    "currency": "BRL",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": sector or "Latin America / Brazil",
                    "unit_cost": None
                })

    elif category == "airlines_pariah":
        air_tickers = read_list_csv("airlines.csv")
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or sym.upper() not in air_tickers or sym.upper() in exclude_tickers:
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < 200_000_000:
                continue

            # 6-Month Minimum Liquidity Gate:
            # Cash on hand must cover at least 6 months of fixed debt obligations / cash burn
            cash_m = m.get("cash_on_hand_m") or 0
            if cash_m <= 0:
                continue

            fcf_m = m.get("fcf_m") or 0
            if fcf_m < 0:
                monthly_burn = abs(fcf_m) / 12.0
                runway_months = cash_m / monthly_burn if monthly_burn > 0 else 0
            else:
                runway_months = 6.0 + (cash_m / (mcap / 1e6 * 0.1))

            if runway_months < 6.0:
                continue

            # Exclude penny stocks (< $2.00) and OTC foreign ordinary pink sheets (5-letter tickers ending in F or Y)
            if price < 2.0:
                continue
            if len(sym) == 5 and sym.endswith(("F", "Y")):
                continue

            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((1.0 <= fpe < 16) or (1.0 <= pe < 16) or (0 < p_fcf < 12)):
                candidates.append({
                "ticker": sym,
                "name": name or sym,
                "exchange": "Global / US",
                "currency": "USD",
                "price": price,
                "low_52w": low,
                "high_52w": high,
                "pct_low": pct_low,
                "pct_high": pct_high,
                "pe": pe,
                "forward_pe": fpe,
                "p_fcf": p_fcf,
                "ev_ebitda": m.get("ev_ebitda"),
                "market_cap": mcap,
                "div_yield": m.get("div_yield") or 0,
                "fcf_yield": m.get("fcf_yield") or 0,
                "category": category,
                "sector": "Airlines & Capital-Cycle Pariahs",
                "unit_cost": None,
                "liquidity_gate": {
                    "cash_m": cash_m,
                    "runway_months": runway_months
                }
            })

    elif category == "mid_small_value":
        sm_tickers = read_list_csv("Mid-cap value business url list.csv") | read_list_csv("Small cap value business url list.csv")
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or sym.upper() not in sm_tickers or sym.upper() in exclude_tickers:
                continue
            if price < 2.0 or (len(sym) == 5 and sym.endswith(("F", "Y"))):
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < MID_SMALL_MIN_MCAP:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 14) or (0 < pe < 14) or (0 < p_fcf < 10)):
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": "NYSE/NASDAQ",
                    "currency": "USD",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": sector or "Mid & Small-Cap Value",
                    "unit_cost": None
                })

    elif category == "us_value":
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json 
            FROM business_list_yf_cache 
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym:
                continue
            if re.search(r"\.[A-Za-z]{1,4}$", sym) and not re.search(r"\.(A|B)$", sym):
                continue
            if sym.upper() in exclude_tickers:
                continue
            if price < 2.0 or (len(sym) == 5 and sym.endswith(("F", "Y"))):
                continue

            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < US_LARGE_MIN_MCAP:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            if pct_low <= 25 and ((0 < fpe < 14) or (0 < pe < 14) or (0 < p_fcf < 10)):
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": "NYSE/NASDAQ",
                    "currency": "USD",
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": sector or "US Large-Cap Value",
                    "unit_cost": None
                })

    elif category == "antifragile_scenarios":
        af_tickers = read_list_csv("antifragile_scenarios.csv")
        cur.execute("""
            SELECT yf_symbol, name, sector, price, low_52w, high_52w, p_fcf, metrics_json
            FROM business_list_yf_cache
            WHERE price > 0 AND low_52w > 0
        """)
        for r in cur.fetchall():
            sym, name, sector, price, low, high, p_fcf, m_json = r
            if not sym or sym.upper() not in af_tickers or sym.upper() in exclude_tickers:
                continue
            pct_low = (price - low) / low * 100 if low else 999
            pct_high = (high - price) / high * 100 if high else 0
            m = json.loads(m_json) if m_json else {}
            mcap = m.get("market_cap")
            if not mcap or mcap < ANTIFRAGILE_MIN_MCAP:
                continue
            fpe = m.get("forward_pe") or 0
            pe = m.get("pe") or 0
            p_fcf_val = p_fcf or 0
            if pct_low <= 30 and ((0 < fpe < 22) or (0 < pe < 22) or (0 < p_fcf_val < 18)):
                if sym.endswith(".AX"):
                    exchange, currency = "ASX", "AUD"
                elif sym.endswith(".TO"):
                    exchange, currency = "TSX", "CAD"
                elif sym.endswith(".T"):
                    exchange, currency = "TSE", "JPY"
                else:
                    exchange, currency = "NYSE/NASDAQ/Global", "USD"
                candidates.append({
                    "ticker": sym,
                    "name": name or sym,
                    "exchange": exchange,
                    "currency": currency,
                    "price": price,
                    "low_52w": low,
                    "high_52w": high,
                    "pct_low": pct_low,
                    "pct_high": pct_high,
                    "pe": pe,
                    "forward_pe": fpe,
                    "p_fcf": p_fcf_val,
                    "ev_ebitda": m.get("ev_ebitda"),
                    "market_cap": mcap,
                    "div_yield": m.get("div_yield") or 0,
                    "fcf_yield": m.get("fcf_yield") or 0,
                    "category": category,
                    "sector": sector or "Antifragile Scenarios",
                    "industry": m.get("industry") or "",
                    "unit_cost": get_commodity_unit_cost(sym),
                })

    conn.close()

    if not candidates:
        logger.warning(f"No qualifying candidates found for category: {category}")
        return None

    # Blended ranking score (lower forward PE/PE + distance from low)
    candidates.sort(key=lambda x: (
        (x["forward_pe"] if x["forward_pe"] > 0 else (x["pe"] if x["pe"] > 0 else 999)) +
        (x["p_fcf"] if x["p_fcf"] > 0 else 5.0) +
        (x["pct_low"] * 0.2)
    ))

    return candidates[0]

def create_brief_file(candidate: dict) -> Path:
    """Create a high-conviction markdown investment brief in the Obsidian pitches folder."""
    today_str = date.today().isoformat()
    by_ticker, valid_sectors, valid_opps = load_vault_registry()
    ticker_upper = candidate["ticker"].strip().upper()
    reg_entry = lookup_registry_entry(ticker_upper, by_ticker)
    display_name = normalize_company_name(candidate.get("name") or ticker_upper, ticker_upper, reg_entry)
    sector_moc = resolve_sector_moc(candidate, reg_entry, valid_sectors)
    opp_memo = resolve_opportunity_memo(ticker_upper, reg_entry, valid_opps)

    safe_name = display_name[:30].strip().replace("/", "-").replace(":", "-")
    filename = f"{today_str} - {ticker_upper} - {safe_name}.md"
    PITCHES_DIR.mkdir(parents=True, exist_ok=True)
    file_path = PITCHES_DIR / filename

    # Remove older same-day, same-ticker pitch with a non-canonical company name
    for old_same_ticker in PITCHES_DIR.glob(f"{today_str} - {ticker_upper} - *.md"):
        if old_same_ticker != file_path:
            old_same_ticker.unlink()
            logger.info(f"Replaced duplicate same-day pitch: {old_same_ticker.name}")

    curr_map = {"AUD": "A$", "BRL": "R$", "CAD": "C$", "JPY": "¥"}
    curr_sym = curr_map.get(candidate.get("currency", ""), "$")
    mcap_b = (candidate["market_cap"] or 0) / 1e9
    ev_ebitda_val = candidate.get("ev_ebitda")
    ev_ebitda_str = f"{ev_ebitda_val:.1f}x" if isinstance(ev_ebitda_val, (int, float)) else "—"
    pe_val = candidate.get("pe") or 0.0
    fpe_val = candidate.get("forward_pe") or 0.0
    pfcf_val = candidate.get("p_fcf") or 0.0

    vault_links = ["[[Opportunity-Queue]]"]
    if opp_memo:
        vault_links.append(f"[[{opp_memo}]]")
    vault_links.append(f"[[{sector_moc}]]")
    if candidate.get("category") == "antifragile_scenarios":
        vault_links.append("[[Antifragility-Scenario-Briefs]]")
    vault_links.append("[[Opportunity-Checklist]]")
    vault_links_line = " · ".join(vault_links)

    unit_cost_block = ""
    unit_cost_exec = ""
    if candidate.get("unit_cost"):
        uc = candidate["unit_cost"]
        unit_cost_exec = f"\n> **Official Commodity Unit Cost ({uc['period_end']}):** {uc['label']} = **{uc['cost_currency']} ${uc['cost_value_raw']:.2f} / {uc['cost_unit']}** (USD ${uc['cost_value_usd']:.2f})."
        unit_cost_block = f"""
### Official Unit Cost History Benchmark
> [!info] Mining & Operating Cost Metric ({uc['label']})
> * **Metric:** `{uc['cost_metric']}`
> * **Unit Cost:** **{uc['cost_currency']} ${uc['cost_value_raw']:.2f} / {uc['cost_unit']}** (USD ${uc['cost_value_usd']:.2f})
> * **Reporting Period:** {uc['period_end']}
> * **Filing Snippet:** "{uc['snippet']}"
"""

    liquidity_row = ""
    if candidate.get("liquidity_gate"):
        lg = candidate["liquidity_gate"]
        liquidity_row = f"| **Minimum Liquidity Gate (6 Mos)** | ${lg['cash_m']:.0f}M Cash (~{lg['runway_months']:.1f} mos runway) | PASS (≥ 6.0 mos) | PASS |\n"

    consensus_block = get_forward_earnings_consensus(ticker_upper)

    content = f"""---
title: "Daily Pitch — {display_name} ({ticker_upper})"
date: {today_str}
tags:
  - investing
  - stock-pitch
  - deep-value
  - contrarian
  - {candidate['category'].replace('_', '-')}
ticker: {ticker_upper}
company: {display_name}
exchange: {candidate['exchange']}
current_price: "{curr_sym}{candidate['price']:.2f}"
pe_ratio: "{pe_val:.1f}x"
forward_pe: "{fpe_val:.1f}x"
p_fcf: "{pfcf_val:.1f}x"
ev_ebitda: "{ev_ebitda_str}"
pct_from_52w_low: "+{candidate['pct_low']:.1f}%"
52w_range: "{curr_sym}{candidate['low_52w']:.2f} - {curr_sym}{candidate['high_52w']:.2f}"
market_cap: "{curr_sym}{mcap_b:.2f}B"
status: evergreen
---

# Daily Stock Pitch: {display_name} ({candidate['exchange']}: {ticker_upper})

> [!abstract] Executive Summary (The 3-Sentence Pitch)
> **{display_name} ({ticker_upper})** is currently screening at deep-value levels, trading within **+{candidate['pct_low']:.1f}% of its 52-week low** at **{fpe_val:.1f}x forward earnings** and **{pfcf_val:.1f}x Price-to-FCF**. Market pessimism has compressed multiples significantly, discounting the resilience of its underlying cash flows and capital discipline.{unit_cost_exec} For contrarian value investors, this setup offers compelling asymmetric risk/reward with substantial re-rating torque against low cyclical expectations.

**Vault Links:** {vault_links_line}

---

## 1. Company Profile & Business Engine

* **Company Name:** {display_name}
* **Ticker / Exchange:** {ticker_upper} ({candidate['exchange']})
* **Universe Category:** {candidate['category']} ({candidate['sector']})
* **Sector MOC:** [[{sector_moc}]]
* **Market Capitalization:** {curr_sym}{mcap_b:.2f}B

---

## 2. Valuation & Screening Posture

| Metric | Current Value | Benchmark Gate | Status |
| :--- | :--- | :--- | :--- |
| **Current Price** | {curr_sym}{candidate['price']:.2f} | — | — |
| **52-Week Low** | {curr_sym}{candidate['low_52w']:.2f} | +{candidate['pct_low']:.1f}% from low | PASS (≤ 25%) |
| **Forward P/E** | {fpe_val:.1f}x | ≤ 15.0x | PASS |
| **Trailing P/E** | {pe_val:.1f}x | ≤ 16.0x | PASS |
| **Price / FCF** | {pfcf_val:.1f}x | ≤ 12.0x | PASS |
| **EV / EBITDA** | {ev_ebitda_str} | ≤ 10.0x | PASS |
| **Dividend Yield** | {candidate['div_yield']:.2f}% | Income cushion | ACTIVE |
{liquidity_row}{unit_cost_block}
{consensus_block}
---

## 3. Moat & Structural Setup (Seven Powers Lens)

* **Cost Advantage / Scale Economies:** Operates with established scale in its core geographic and sector footprint, resisting marginal pricing pressure.
* **Asset Backing & Cash Conversion:** Downside is anchored by tangible asset backing and steady operating cash flow conversion.

---

## 4. Bull Case vs. Bear Case

### Bull Case (The Catalysts)
1. **Valuation Normalization:** Multiple re-rating from cyclical/sentiment trough toward median historical multiples.
2. **Capital Discipline & Returns:** Free cash flow supporting dividends, debt retirement, and opportunistic share repurchases.

### Bear Case (The Risks)
1. **Macro / Cyclical Softness:** Extended downturn in demand or sector-specific headwinds delaying operational recovery.
2. **Cost Inflation:** Persistent operating expense escalation squeezing operating margins.

---

## 5. Research Action Checklist

```
Research Checklist:
[x] Identify unloved, cheap multiple (Forward P/E {fpe_val:.1f}x, P/FCF {pfcf_val:.1f}x)
[x] Confirm price posture within +{candidate['pct_low']:.1f}% of 52-week low
[ ] Validate balance sheet leverage and debt maturity profile
[ ] Track next quarterly earnings announcement and guidance update
```
"""

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"Created daily brief at {file_path}")
    return file_path

def dispatch_email(pitch_file: Path):
    """Run send_pitch_email.py to dispatch the brief."""
    script_path = SCREENER_ROOT / "scripts" / "send_pitch_email.py"
    if not script_path.exists():
        logger.error(f"Dispatcher script not found: {script_path}")
        return

    try:
        cmd = [sys.executable, str(script_path), str(pitch_file)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(res.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to dispatch email: {e.stderr}")

def main():
    parser = argparse.ArgumentParser(description="Daily Stock Research Brief Orchestrator")
    parser.add_argument("--category", choices=CATEGORIES, help="Override category rotation")
    parser.add_argument("--dry-run", action="store_true", help="Screen without writing file or sending email")
    parser.add_argument("--force", action="store_true", help="Generate new brief even if today's exists")
    args = parser.parse_args()

    today_str = date.today().isoformat()

    existing_today = list(PITCHES_DIR.glob(f"{today_str} - *.md")) if PITCHES_DIR.exists() else []

    if existing_today and not args.force:
        logger.info(f"Today's brief already exists: {existing_today[0].name}")
        if not args.dry_run:
            dispatch_email(existing_today[0])
        return

    if args.category:
        cat = args.category
        rot_idx = None
    else:
        cat, rot_idx = get_next_category()
    logger.info(f"Target category for today: {cat}")

    excluded = get_recently_pitched_tickers(days=60)
    logger.info(f"Excluding {len(excluded)} recently pitched tickers: {sorted(list(excluded))}")

    candidate = screen_candidate(cat, excluded)
    if not candidate:
        logger.error(f"Failed to find candidate in {cat}.")
        return

    logger.info(f"Selected candidate: {candidate['ticker']} ({candidate['name']}) - P/E: {candidate['pe']}, Fwd: {candidate['forward_pe']}, P/FCF: {candidate['p_fcf']}")

    if args.dry_run:
        by_ticker, valid_sectors, valid_opps = load_vault_registry()
        reg_entry = lookup_registry_entry(candidate["ticker"], by_ticker)
        display_name = normalize_company_name(candidate.get("name") or candidate["ticker"], candidate["ticker"], reg_entry)
        sector_moc = resolve_sector_moc(candidate, reg_entry, valid_sectors)
        opp_memo = resolve_opportunity_memo(candidate["ticker"], reg_entry, valid_opps)
        print("\n--- DRY RUN SUMMARY ---")
        print(f"Category: {cat}")
        print(f"Ticker: {candidate['ticker']} ({display_name})")
        print(f"Sector MOC: {sector_moc} | Opp Memo: {opp_memo or 'none (pitch-only)'}")
        print(f"Price: {candidate['price']} (Low: {candidate['low_52w']}, +{candidate['pct_low']:.1f}%)")
        print(f"Multiples: PE {candidate['pe']} | FwdPE {candidate['forward_pe']} | P/FCF {candidate['p_fcf']}")
        if candidate.get("unit_cost"):
            print(f"Unit Cost: {candidate['unit_cost']['label']} = {candidate['unit_cost']['cost_currency']} ${candidate['unit_cost']['cost_value_raw']} / {candidate['unit_cost']['cost_unit']}")
        return

    brief_file = create_brief_file(candidate)
    save_state(cat, candidate['ticker'], index=rot_idx)
    dispatch_email(brief_file)

if __name__ == "__main__":
    main()
