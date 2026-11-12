"""
Automated "Superinvestor" SEC 13F-HR & ASX Substantial Holder Tracker (`data/superinvestor_13f.py`).

Implements Step 5 ("Shameless Cloner" institutional conviction):
1. Curated registry of 16 deep-value / contrarian / capital-cycle Superinvestors filing SEC Form 13F-HR:
   - Scion Asset Management (Michael Burry)
   - Dalal Street LLC (Mohnish Pabrai)
   - Baupost Group (Seth Klarman)
   - Greenlight Capital (David Einhorn)
   - Kopernik Global Investors (Dave Iben)
   - Horizon Kinetics (Murray Stahl)
   - Fairfax Financial Holdings (Prem Watsa)
   - Appaloosa Management (David Tepper)
   - Himalaya Capital (Li Lu)
   - Oakcliff Capital (Bryan Lawrence)
   - Tweedy, Browne Company
   - Third Avenue Management (Marty Whitman legacy)
   - Goehring & Rozencwajg (Resource Deep Value)
   - Contrarius Investment Management
   - Hosking Partners (Capital Cycle)
   - Chou Associates (Francis Chou)
2. Downloads and parses each manager's latest two 13F-HR XML information tables from SEC EDGAR
   to detect:
   - `cloner_holder_count`: number of tracked superinvestors holding the stock
   - `cloner_new_position_flag`: 1 if any superinvestor initiated a NEW position or added >=15% QoQ
   - `cloner_max_weight_pct`: highest portfolio weight % allocated to this stock by any tracked manager
   - `cloner_holders_summary`: human-readable summary of holders, weights, and QoQ activity
3. Scans local ASX `announcements` table for Form 603 ("Becoming a substantial holder") and
   Form 604 ("Change in substantial holding") notices over the trailing 180 days so ASX stocks
   also receive institutional cloner / substantial-holder signals.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from config.identity import sec_user_agent
from data.db import connect_db

logger = logging.getLogger(__name__)

DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "asx_announcements_cache.db")

SUPERINVESTORS: list[dict[str, str]] = [
    {"id": "scion", "name": "Scion (Michael Burry)", "cik": "0001649339", "style": "Deep Value / Contrarian"},
    {"id": "pabrai", "name": "Dalal Street (Mohnish Pabrai)", "cik": "0001549575", "style": "Clone / Cigar Butt"},
    {"id": "baupost", "name": "Baupost (Seth Klarman)", "cik": "0001061768", "style": "Distressed / Deep Value"},
    {"id": "greenlight", "name": "Greenlight (David Einhorn)", "cik": "0001489933", "style": "Classic Value"},
    {"id": "kopernik", "name": "Kopernik Global (Dave Iben)", "cik": "0001599814", "style": "Global Real Asset Value"},
    {"id": "horizon", "name": "Horizon Kinetics (Murray Stahl)", "cik": "0001056823", "style": "Hard Assets / Owner-Operator"},
    {"id": "fairfax", "name": "Fairfax Financial (Prem Watsa)", "cik": "0000915191", "style": "Deep Value Insurance"},
    {"id": "appaloosa", "name": "Appaloosa (David Tepper)", "cik": "0001656456", "style": "Distressed / Macro Value"},
    {"id": "himalaya", "name": "Himalaya Capital (Li Lu)", "cik": "0001709323", "style": "Concentrated Value"},
    {"id": "oakcliff", "name": "Oakcliff Capital (Bryan Lawrence)", "cik": "0001657335", "style": "Value Compounders"},
    {"id": "tweedy", "name": "Tweedy, Browne", "cik": "0000732905", "style": "Ben Graham Deep Value"},
    {"id": "third_ave", "name": "Third Avenue (Whitman Legacy)", "cik": "0001099281", "style": "Safe & Cheap Asset Value"},
    {"id": "goehring", "name": "Goehring & Rozencwajg", "cik": "0001863154", "style": "Commodity Capital Cycle"},
    {"id": "contrarius", "name": "Contrarius Investment Mgmt", "cik": "0002054801", "style": "Contrarian Deep Value"},
    {"id": "hosking", "name": "Hosking Partners", "cik": "0001650135", "style": "Capital Cycle"},
    {"id": "chou", "name": "Chou Associates (Francis Chou)", "cik": "0001389403", "style": "Deep Value"},
    {"id": "oaktree", "name": "Oaktree Capital (Howard Marks)", "cik": "0000949509", "style": "Distressed / Credit & Value"},
    {"id": "southeastern", "name": "Southeastern Asset (Mason Hawkins)", "cik": "0000807985", "style": "Longleaf Intrinsic Value"},
]

SUPERINVESTOR_MANAGERS = SUPERINVESTORS

_CLONER_DDL = (
    """
    CREATE TABLE IF NOT EXISTS superinvestor_13f_holdings (
        manager_id TEXT,
        manager_name TEXT,
        cik TEXT,
        report_date TEXT,
        filing_date TEXT,
        cusip TEXT,
        issuer_name TEXT,
        ticker TEXT,
        value_usd REAL,
        shares REAL,
        portfolio_weight_pct REAL,
        qoq_action TEXT,
        qoq_shares_change_pct REAL,
        updated_at TEXT,
        PRIMARY KEY (manager_id, report_date, cusip)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS superinvestor_stock_summary (
        ticker TEXT PRIMARY KEY,
        cloner_holder_count INTEGER,
        cloner_new_position_flag INTEGER,
        cloner_max_weight_pct REAL,
        cloner_holders_summary TEXT,
        cloner_details_json TEXT,
        asx_subst_notices_180d INTEGER DEFAULT 0,
        updated_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_si13f_ticker ON superinvestor_13f_holdings(ticker)",
)

# Common 13F issuer-name / CUSIP prefix overrides for fast high-accuracy resolution
_KNOWN_ISSUER_TO_TICKER: dict[str, str] = {
    "ALIBABA GROUP": "BABA",
    "JD.COM": "JD",
    "JD COM": "JD",
    "BAIDU": "BIDU",
    "PINDUODUO": "PDD",
    "PDD HOLDINGS": "PDD",
    "CHARTER COMMUNICATIONS": "CHTR",
    "LIBERTY BROADBAND": "LBRDK",
    "PAYPAL HOLDINGS": "PYPL",
    "BRIGHTHOUSE FINL": "BHF",
    "BRIGHTHOUSE FINANCIAL": "BHF",
    "FIDELITY NATL INFORMATION": "FIS",
    "FIDELITY NATIONAL INFO": "FIS",
    "SKYWEST": "SKYW",
    "WARRIOR MET COAL": "HCC",
    "ALPHA METALLURGICAL": "AMR",
    "CONSOL ENERGY": "CEIX",
    "CORE NATURAL RESOURCES": "CNR",
    "ARCH RESOURCES": "ARCH",
    "PEABODY ENERGY": "BTU",
    "CNX RESOURCES": "CNX",
    "ALLIANCE RESOURCE": "ARLP",
    "BERKSHIRE HATHAWAY": "BRK-B",
    "ALPHABET": "GOOG",
    "META PLATFORMS": "META",
    "AMAZON COM": "AMZN",
    "TEXAS PACIFIC LAND": "TPL",
    "GREEN BRICK PARTNERS": "GRBK",
    "CONSOL": "CNR",
    "VALARIS": "VAL",
    "NOBLE CORP": "NE",
    "TIDEWATER": "TDW",
    "BARRICK GOLD": "GOLD",
    "NEWMONT": "NEM",
    "AGNICO EAGLE": "AEM",
    "CAMECO": "CCJ",
    "TECK RESOURCES": "TECK",
    "VALE S A": "VALE",
    "PETROLEO BRASILEIRO": "PBR",
    "KASPI KZ": "KSPI",
    "DOLLAR TREE": "DLTR",
    "DOLLAR GENERAL": "DG",
    "CVS HEALTH": "CVS",
    "HUMANA": "HUM",
    "UNITEDHEALTH": "UNH",
    "CENTENE": "CNC",
    "MOLINA HEALTHCARE": "MOH",
    "CIGNA": "CI",
    "ESTEE LAUDER": "EL",
    "NIKE": "NKE",
    "STARBUCKS": "SBUX",
    "BOEING": "BA",
    "INTEL": "INTC",
    "MICRON TECHNOLOGY": "MU",
    "WESTERN DIGITAL": "WDC",
    "HP INC": "HPQ",
    "DELL TECHNOLOGIES": "DELL",
    "PELOTON": "PTON",
    "WARNER BROS DISCOVERY": "WBD",
    "PARAMOUNT GLOBAL": "PARA",
    "WALT DISNEY": "DIS",
    "COMCAST": "CMCSA",
    "SIRIUS XM": "SIRI",
    "HERTZ GLOBAL": "HTZ",
    "AVIS BUDGET": "CAR",
    "AUTONATION": "AN",
    "AUTOZONE": "AZO",
    "OCCIDENTAL PETE": "OXY",
    "CHEVRON": "CVX",
    "EXXON MOBIL": "XOM",
    "DEVON ENERGY": "DVN",
    "DIAMONDBACK ENERGY": "FANG",
    "APACHE": "APA",
    "APA CORP": "APA",
    "WEATHERFORD": "WFRD",
    "TRANSOCEAN": "RIG",
    "NABORS INDUSTRIES": "NBR",
    "FIRST QUANTUM": "FM.TO",
    "IVANHOE MINES": "IVN.TO",
    "WHEATON PRECIOUS": "WPM",
    "ROYAL GOLD": "RGLD",
    "FRANCO NEVADA": "FNV",
    "SPROTT": "SII",
    "URANIUM ENERGY": "UEC",
    "NEXGEN ENERGY": "NXE",
    "DENISON MINES": "DNN",
    "PALADIN ENERGY": "PDN.AX",
    "WHITEHAVEN COAL": "WHC.AX",
    "NEW HOPE": "NHC.AX",
    "YANCOAL": "YAL.AX",
}


def _cloner_conn(db_path: str = DEFAULT_DB):
    return connect_db(db_path, schema_key="superinvestor_13f", ddl=_CLONER_DDL)


def _norm_company_name(name: str) -> str:
    s = (name or "").upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    s = re.sub(
        r"\b(INC|CORP|CORPORATION|LTD|LIMITED|PLC|HOLDINGS|HOLDING|GROUP|CO|COMPANY|NEW|DEL|CL A|CL B|CL C|CLASS A|CLASS B|COM|SHS|ADR|SPONSORED|ORD|SA|NV|AG|LP|LLC|THE)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def _build_issuer_lookup(db_path: str = DEFAULT_DB) -> dict[str, str]:
    """Build normalized company name -> ticker lookup from local SQLite + SEC tickers."""
    lookup: dict[str, str] = {}
    for k, v in _KNOWN_ISSUER_TO_TICKER.items():
        lookup[_norm_company_name(k)] = v

    conn = _cloner_conn(db_path)
    try:
        for sym, name in conn.execute(
            "SELECT yf_symbol, name FROM business_list_yf_cache WHERE name IS NOT NULL"
        ).fetchall():
            if not sym or not name:
                continue
            t = str(sym).strip().upper()
            # Prefer US primary listings over foreign suffixes for 13F matching
            if "." in t and not t.endswith(".TO"):
                continue
            nk = _norm_company_name(str(name))
            if len(nk) >= 4 and nk not in lookup:
                lookup[nk] = t
    except Exception:
        pass
    finally:
        conn.close()

    # Also try SEC company_tickers.json if reachable
    try:
        headers = {"User-Agent": sec_user_agent(), "Accept": "application/json"}
        resp = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=12)
        if resp.status_code == 200:
            data = resp.json() or {}
            for item in data.values():
                t = str(item.get("ticker") or "").strip().upper()
                title = str(item.get("title") or "").strip()
                if t and title:
                    nk = _norm_company_name(title)
                    if len(nk) >= 3 and nk not in lookup:
                        lookup[nk] = t
    except Exception as ex:
        logger.debug("SEC company_tickers.json fetch skipped: %s", ex)

    return lookup


def _resolve_issuer_to_ticker(issuer_name: str, lookup: dict[str, str]) -> str:
    raw_up = (issuer_name or "").upper()
    for prefix, tkr in _KNOWN_ISSUER_TO_TICKER.items():
        if prefix in raw_up:
            return tkr
    nk = _norm_company_name(issuer_name)
    if not nk:
        return ""
    if nk in lookup:
        return lookup[nk]
    # Prefix match on first 2 words if sufficiently distinctive
    words = nk.split()
    if len(words) >= 2:
        two = f"{words[0]} {words[1]}"
        if len(two) >= 7 and two in lookup:
            return lookup[two]
    return ""


def _parse_13f_xml(xml_bytes: bytes, lookup: dict[str, str]) -> dict[str, dict[str, Any]]:
    """
    Parse SEC 13F-HR `informationTable` XML bytes and aggregate holdings by CUSIP/ticker.
    """
    root = ET.fromstring(xml_bytes)
    agg: dict[str, dict[str, Any]] = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag.lower() != "infotable":
            continue
        issuer = ""
        cusip = ""
        val_raw = 0.0
        shrs_raw = 0.0
        put_call = ""
        for child in elem:
            ctag = (child.tag.split("}")[-1] if "}" in child.tag else child.tag).lower()
            if ctag == "nameofissuer":
                issuer = (child.text or "").strip()
            elif ctag == "cusip":
                cusip = (child.text or "").strip().upper()
            elif ctag == "value":
                try:
                    val_raw = float((child.text or "0").strip())
                except ValueError:
                    val_raw = 0.0
            elif ctag == "putcall":
                put_call = (child.text or "").strip().upper()
            elif ctag in ("shrsorprnamt", "shrsornamt"):
                for sub in child:
                    stag = (sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag).lower()
                    if stag == "sshprnamt":
                        try:
                            shrs_raw = float((sub.text or "0").strip())
                        except ValueError:
                            shrs_raw = 0.0
        # Skip pure PUT options so puts aren't misread as long stock conviction
        if put_call == "PUT":
            continue
        if not cusip or val_raw <= 0:
            continue
        tkr = _resolve_issuer_to_ticker(issuer, lookup)
        key = tkr or cusip
        if key not in agg:
            agg[key] = {
                "cusip": cusip,
                "issuer_name": issuer,
                "ticker": tkr,
                "value_usd": 0.0,
                "shares": 0.0,
            }
        agg[key]["value_usd"] += val_raw
        agg[key]["shares"] += shrs_raw

    return agg


def _fetch_manager_13f_filings(
    mgr: dict[str, str],
    lookup: dict[str, str],
    *,
    sleep_s: float = 0.25,
) -> list[dict[str, Any]]:
    """
    Fetch the latest 2 13F-HR XML information tables for one manager from SEC EDGAR
    and compute portfolio weights + QoQ share changes.
    """
    cik_pad = str(mgr["cik"]).zfill(10)
    cik_int = str(int(cik_pad))
    headers = {"User-Agent": sec_user_agent(), "Accept": "application/json"}

    sub_url = f"https://data.sec.gov/submissions/CIK{cik_pad}.json"
    resp = requests.get(sub_url, headers=headers, timeout=15)
    time.sleep(sleep_s)
    if resp.status_code != 200:
        return []

    sub = resp.json() or {}
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    acc_nums = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []

    candidates: list[tuple[str, str, str]] = []
    for idx, form in enumerate(forms):
        if form == "13F-HR":
            acc = acc_nums[idx] if idx < len(acc_nums) else ""
            fdate = filing_dates[idx] if idx < len(filing_dates) else ""
            rdate = report_dates[idx] if idx < len(report_dates) else fdate
            if acc:
                candidates.append((acc, rdate or fdate, fdate))
            if len(candidates) >= 2:
                break

    if not candidates:
        return []

    parsed_quarters: list[tuple[str, str, dict[str, dict[str, Any]]]] = []
    for acc, rdate, fdate in candidates:
        acc_nodash = acc.replace("-", "")
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
        r_idx = requests.get(idx_url, headers=headers, timeout=15)
        time.sleep(sleep_s)
        if r_idx.status_code != 200:
            continue
        items = ((r_idx.json() or {}).get("directory") or {}).get("item") or []
        xml_files = [
            it.get("name")
            for it in items
            if str(it.get("name") or "").lower().endswith(".xml")
            and "primary_doc" not in str(it.get("name") or "").lower()
        ]
        if not xml_files:
            continue
        xml_name = xml_files[0]
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{xml_name}"
        r_xml = requests.get(
            xml_url,
            headers={"User-Agent": sec_user_agent(), "Accept": "application/xml,text/xml,*/*"},
            timeout=20,
        )
        time.sleep(sleep_s)
        if r_xml.status_code != 200:
            continue
        try:
            q_holdings = _parse_13f_xml(r_xml.content, lookup)
            if q_holdings:
                parsed_quarters.append((rdate, fdate, q_holdings))
        except Exception as ex:
            logger.debug("13F XML parse error for %s (%s): %s", mgr["name"], acc, ex)

    if not parsed_quarters:
        return []

    latest_rdate, latest_fdate, latest_map = parsed_quarters[0]
    prev_map = parsed_quarters[1][2] if len(parsed_quarters) >= 2 else {}

    total_val = sum(h["value_usd"] for h in latest_map.values())
    if total_val <= 0:
        return []
    prev_total_val = sum(h["value_usd"] for h in prev_map.values()) or total_val

    now_iso = datetime.now().isoformat(timespec="seconds")
    out_rows: list[dict[str, Any]] = []
    for key, h in latest_map.items():
        w_pct = round((h["value_usd"] / total_val) * 100.0, 2)
        prev_h = prev_map.get(key)
        qoq_action = "UNCHANGED"
        qoq_chg_pct: float | None = 0.0
        if not prev_h or prev_h.get("shares", 0) <= 0:
            qoq_action = "NEW"
            qoq_chg_pct = 100.0
        else:
            s0 = prev_h["shares"]
            s1 = h["shares"]
            chg = ((s1 - s0) / s0) * 100.0 if s0 > 0 else 0.0
            qoq_chg_pct = round(chg, 1)
            if chg >= 2.0:
                qoq_action = "ADDED"
            elif chg <= -2.0:
                qoq_action = "REDUCED"

        out_rows.append(
            {
                "manager_id": mgr["id"],
                "manager_name": mgr["name"],
                "cik": mgr["cik"],
                "report_date": latest_rdate,
                "filing_date": latest_fdate,
                "cusip": h["cusip"],
                "issuer_name": h["issuer_name"],
                "ticker": h["ticker"],
                "value_usd": h["value_usd"],
                "shares": h["shares"],
                "portfolio_weight_pct": w_pct,
                "qoq_action": qoq_action,
                "qoq_shares_change_pct": qoq_chg_pct,
                "updated_at": now_iso,
            }
        )

    # Also record positions from prior quarter that were 100% sold out (EXITED)
    for key, prev_h in prev_map.items():
        if key in latest_map:
            continue
        if prev_h.get("value_usd", 0) <= 0:
            continue
        prev_w_pct = round((prev_h["value_usd"] / prev_total_val) * 100.0, 2)
        out_rows.append(
            {
                "manager_id": mgr["id"],
                "manager_name": mgr["name"],
                "cik": mgr["cik"],
                "report_date": latest_rdate,
                "filing_date": latest_fdate,
                "cusip": prev_h["cusip"],
                "issuer_name": prev_h["issuer_name"],
                "ticker": prev_h["ticker"],
                "value_usd": prev_h["value_usd"],
                "shares": prev_h["shares"],
                "portfolio_weight_pct": prev_w_pct,
                "qoq_action": "EXITED",
                "qoq_shares_change_pct": -100.0,
                "updated_at": now_iso,
            }
        )
    return out_rows


def collect_and_rebuild_superinvestor_13f(
    *,
    fetch_sec: bool = True,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    """
    Collect latest 13F-HR holdings from SEC EDGAR for all 16 Superinvestors,
    scan local ASX Substantial Holder notices (Form 603/604), and rebuild
    `superinvestor_stock_summary`.
    """
    conn = _cloner_conn(db_path)
    managers_ok = 0
    total_holdings = 0

    if fetch_sec:
        lookup = _build_issuer_lookup(db_path=db_path)
        for mgr in SUPERINVESTORS:
            try:
                rows = _fetch_manager_13f_filings(mgr, lookup)
                if rows:
                    managers_ok += 1
                    total_holdings += len(rows)
                    with conn:
                        conn.execute(
                            "DELETE FROM superinvestor_13f_holdings WHERE manager_id = ?",
                            (mgr["id"],),
                        )
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO superinvestor_13f_holdings (
                                manager_id, manager_name, cik, report_date, filing_date,
                                cusip, issuer_name, ticker, value_usd, shares,
                                portfolio_weight_pct, qoq_action, qoq_shares_change_pct, updated_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            [
                                (
                                    r["manager_id"],
                                    r["manager_name"],
                                    r["cik"],
                                    r["report_date"],
                                    r["filing_date"],
                                    r["cusip"],
                                    r["issuer_name"],
                                    r["ticker"],
                                    r["value_usd"],
                                    r["shares"],
                                    r["portfolio_weight_pct"],
                                    r["qoq_action"],
                                    r["qoq_shares_change_pct"],
                                    r["updated_at"],
                                )
                                for r in rows
                            ],
                        )
            except Exception as ex:
                logger.warning("13F fetch failed for %s: %s", mgr["name"], ex)

    # Rebuild per-ticker summary combining active 13F holdings + ASX Substantial Holder notices
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        """
        SELECT ticker, manager_name, report_date, portfolio_weight_pct, qoq_action, qoq_shares_change_pct
        FROM superinvestor_13f_holdings
        WHERE ticker IS NOT NULL AND ticker != '' AND COALESCE(qoq_action, '') != 'EXITED'
        ORDER BY portfolio_weight_pct DESC
        """
    ).fetchall():
        tkr = str(r[0]).strip().upper()
        by_ticker.setdefault(tkr, []).append(
            {
                "manager_name": r[1],
                "report_date": r[2],
                "weight_pct": float(r[3] or 0.0),
                "qoq_action": r[4] or "UNCHANGED",
                "qoq_chg_pct": r[5],
            }
        )

    # Also scan ASX Substantial Holder notices (Form 603 / 604) in trailing 180 days
    asx_subst_counts: dict[str, int] = {}
    try:
        cutoff_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
        for atkr, headline in conn.execute(
            """
            SELECT ticker, headline FROM announcements
            WHERE date >= ? AND lower(headline) LIKE '%substantial%'
            """,
            (cutoff_date,),
        ).fetchall():
            if not atkr:
                continue
            hl = str(headline or "").lower()
            if "ceasing" in hl:
                continue
            sym_ax = str(atkr).strip().upper()
            if not sym_ax.endswith(".AX"):
                sym_ax = f"{sym_ax}.AX"
            asx_subst_counts[sym_ax] = asx_subst_counts.get(sym_ax, 0) + 1
    except Exception as ex:
        logger.debug("ASX substantial holder scan skipped: %s", ex)

    all_syms = set(by_ticker.keys()) | set(asx_subst_counts.keys())
    now_iso = datetime.now().isoformat(timespec="seconds")
    summary_rows: list[tuple[Any, ...]] = []

    for sym in sorted(all_syms):
        holders = by_ticker.get(sym) or []
        asx_cnt = asx_subst_counts.get(sym, 0)
        holder_cnt = len(holders) + (1 if asx_cnt > 0 and not holders else 0)
        new_flag = 1 if any(h["qoq_action"] in ("NEW", "ADDED") for h in holders) or asx_cnt > 0 else 0
        max_w = max((h["weight_pct"] for h in holders), default=0.0)

        parts = []
        for h in holders[:4]:
            short_mgr = h["manager_name"].split("(")[0].strip()
            tag = f" [{h['qoq_action']}]" if h["qoq_action"] in ("NEW", "ADDED") else ""
            parts.append(f"{short_mgr} ({h['weight_pct']:.1f}%{tag})")
        if asx_cnt > 0:
            parts.append(f"ASX Subst. Holder ×{asx_cnt} (180d)")

        summary_rows.append(
            (
                sym,
                holder_cnt,
                new_flag,
                round(max_w, 2),
                ", ".join(parts),
                json.dumps(holders),
                asx_cnt,
                now_iso,
            )
        )

    with conn:
        conn.execute("DELETE FROM superinvestor_stock_summary")
        conn.executemany(
            """
            INSERT INTO superinvestor_stock_summary (
                ticker, cloner_holder_count, cloner_new_position_flag,
                cloner_max_weight_pct, cloner_holders_summary,
                cloner_details_json, asx_subst_notices_180d, updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            summary_rows,
        )
    conn.close()
    return {
        "managers_fetched": managers_ok,
        "total_13f_positions": total_holdings,
        "stocks_with_cloner_signal": len(summary_rows),
        "updated_at": now_iso,
    }


def load_superinvestor_summary_map(db_path: str = DEFAULT_DB) -> dict[str, dict[str, Any]]:
    """Fast O(1) lookup map of `ticker -> superinvestor summary` from SQLite."""
    conn = _cloner_conn(db_path)
    try:
        out: dict[str, dict[str, Any]] = {}
        for (
            tkr,
            cnt,
            new_flag,
            max_w,
            summary,
            asx_cnt,
        ) in conn.execute(
            """
            SELECT ticker, cloner_holder_count, cloner_new_position_flag,
                   cloner_max_weight_pct, cloner_holders_summary, asx_subst_notices_180d
            FROM superinvestor_stock_summary
            """
        ).fetchall():
            sym = str(tkr).strip().upper()
            rec = {
                "ticker": sym,
                "cloner_holder_count": int(cnt or 0),
                "cloner_new_position_flag": int(new_flag or 0),
                "cloner_max_weight_pct": float(max_w or 0.0),
                "cloner_holders_summary": summary or "",
                "asx_subst_notices_180d": int(asx_cnt or 0),
            }
            out[sym] = rec
            if sym.endswith(".AX"):
                out[sym[:-3]] = rec
        return out
    except Exception:
        return {}
    finally:
        conn.close()


load_all_superinvestor_summaries = load_superinvestor_summary_map


def get_superinvestor_summary_for_ticker(
    ticker: str,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    sym = (ticker or "").strip().upper()
    if not sym:
        return {}
    m = load_superinvestor_summary_map(db_path=db_path)
    return m.get(sym) or m.get(f"{sym}.AX") or m.get(sym.replace(".AX", "")) or {}


def get_superinvestor_dashboard_data(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    """
    Build the complete 13F Superinvestor & ASX Substantial Holder dashboard payload:
      - `kpi`: aggregate statistics across all tracked managers
      - `managers`: full directory of investors with style, CIK, report date, AUM, and QoQ activity
      - `holdings_by_manager`: full reported holdings list per manager (active + prior-quarter exits)
      - `common_holdings`: stocks held by >=2 superinvestors (or high-weight >=5% single-manager)
      - `recent_buys`: newly initiated (`NEW`) and increased (`ADDED`) positions
      - `recent_sells`: trimmed (`REDUCED`) and sold-out (`EXITED`) positions
      - `asx_substantial_notices`: trailing 180d ASX Form 603/604 substantial holder filings
    """
    from scoring.unsupervised_ml import load_full_global_stock_universe, load_persisted_global_ml

    universe = load_full_global_stock_universe(db_path=db_path)
    stock_map: dict[str, dict[str, Any]] = {}
    for s in universe:
        t = str(s.get("ticker") or "").strip().upper()
        if t:
            stock_map[t] = s
            if t.endswith(".AX"):
                stock_map[t[:-3]] = s

    ml_payload = load_persisted_global_ml(db_path=db_path) or {}
    ml_profiles = ml_payload.get("profiles") or {}

    def _enrich_ticker(sym: str, fallback_name: str = "") -> dict[str, Any]:
        clean_sym = (sym or "").strip().upper()
        st = stock_map.get(clean_sym) or stock_map.get(f"{clean_sym}.AX") or {}
        m = st.get("metrics") or {}
        mp = ml_profiles.get(clean_sym) or ml_profiles.get(clean_sym.replace(".AX", "")) or {}
        return {
            "name": st.get("name") or fallback_name or clean_sym,
            "sector": st.get("sector") or "Unknown",
            "currency": st.get("currency") or m.get("currency") or "USD",
            "current_price": st.get("current_price") or None,
            "pct_from_52w_low": st.get("pct_from_52w_low"),
            "pe": st.get("pe") or m.get("pe") or None,
            "pb": st.get("pb") or m.get("pb") or None,
            "p_fcf": st.get("p_fcf") or m.get("p_fcf") or None,
            "vq_score": mp.get("vq_score"),
            "in_screener": bool(st and (st.get("current_price") or 0) > 0),
        }

    conn = _cloner_conn(db_path)
    try:
        raw_rows = conn.execute(
            """
            SELECT manager_id, manager_name, cik, report_date, filing_date,
                   cusip, issuer_name, ticker, value_usd, shares,
                   portfolio_weight_pct, qoq_action, qoq_shares_change_pct, updated_at
            FROM superinvestor_13f_holdings
            ORDER BY CASE WHEN qoq_action = 'EXITED' THEN 1 ELSE 0 END ASC,
                     portfolio_weight_pct DESC,
                     value_usd DESC
            """
        ).fetchall()

        holdings_by_manager: dict[str, list[dict[str, Any]]] = {m["id"]: [] for m in SUPERINVESTORS}
        by_ticker_active: dict[str, list[dict[str, Any]]] = {}
        recent_buys: list[dict[str, Any]] = []
        recent_sells: list[dict[str, Any]] = []

        total_active_positions = 0
        combined_aum_usd = 0.0

        for (
            mid,
            mname,
            cik,
            rdate,
            fdate,
            cusip,
            issuer,
            tkr,
            val_usd,
            shrs,
            w_pct,
            action,
            chg_pct,
            upd_at,
        ) in raw_rows:
            sym = str(tkr or "").strip().upper()
            enr = _enrich_ticker(sym, fallback_name=issuer or sym or cusip)
            act = str(action or "UNCHANGED").upper()
            val_f = float(val_usd or 0.0)
            shrs_f = float(shrs or 0.0)
            w_f = float(w_pct or 0.0)

            rec = {
                "manager_id": mid,
                "manager_name": mname,
                "cik": cik,
                "report_date": rdate,
                "filing_date": fdate,
                "cusip": cusip,
                "issuer_name": issuer,
                "ticker": sym,
                "display_name": enr["name"] if sym else (issuer or cusip),
                "sector": enr["sector"],
                "value_usd": val_f,
                "shares": shrs_f,
                "portfolio_weight_pct": w_f,
                "qoq_action": act,
                "qoq_shares_change_pct": chg_pct,
                "current_price": enr["current_price"],
                "currency": enr["currency"],
                "pct_from_52w_low": enr["pct_from_52w_low"],
                "pe": enr["pe"],
                "pb": enr["pb"],
                "p_fcf": enr["p_fcf"],
                "vq_score": enr["vq_score"],
                "in_screener": enr["in_screener"],
            }
            holdings_by_manager.setdefault(mid, []).append(rec)

            if act != "EXITED":
                total_active_positions += 1
                combined_aum_usd += val_f
                if sym:
                    by_ticker_active.setdefault(sym, []).append(rec)

            if act in ("NEW", "ADDED"):
                recent_buys.append(rec)
            elif act in ("EXITED", "REDUCED"):
                recent_sells.append(rec)

        # Sort recent buys and recent sells by portfolio weight % descending
        recent_buys.sort(
            key=lambda x: (1 if x["qoq_action"] == "NEW" else 0, x["portfolio_weight_pct"], x["value_usd"]),
            reverse=True,
        )
        recent_sells.sort(
            key=lambda x: (1 if x["qoq_action"] == "EXITED" else 0, x["portfolio_weight_pct"], x["value_usd"]),
            reverse=True,
        )

        # Build full investor directory
        managers_out: list[dict[str, Any]] = []
        managers_with_filings = 0
        for m in SUPERINVESTORS:
            mid = m["id"]
            m_rows = holdings_by_manager.get(mid) or []
            active_rows = [r for r in m_rows if r["qoq_action"] != "EXITED"]
            exited_rows = [r for r in m_rows if r["qoq_action"] == "EXITED"]
            if active_rows:
                managers_with_filings += 1
            rdate = active_rows[0]["report_date"] if active_rows else (m_rows[0]["report_date"] if m_rows else None)
            fdate = active_rows[0]["filing_date"] if active_rows else (m_rows[0]["filing_date"] if m_rows else None)
            tot_val = sum(r["value_usd"] for r in active_rows)
            qoq_counts = {
                "new": sum(1 for r in active_rows if r["qoq_action"] == "NEW"),
                "added": sum(1 for r in active_rows if r["qoq_action"] == "ADDED"),
                "reduced": sum(1 for r in active_rows if r["qoq_action"] == "REDUCED"),
                "unchanged": sum(1 for r in active_rows if r["qoq_action"] == "UNCHANGED"),
                "exited": len(exited_rows),
            }
            top_5 = [
                {
                    "ticker": r["ticker"] or r["issuer_name"][:12],
                    "issuer_name": r["issuer_name"],
                    "weight_pct": r["portfolio_weight_pct"],
                    "qoq_action": r["qoq_action"],
                }
                for r in active_rows[:5]
            ]
            cik_pad = str(m["cik"]).zfill(10)
            managers_out.append(
                {
                    "id": mid,
                    "name": m["name"],
                    "cik": m["cik"],
                    "style": m["style"],
                    "edgar_url": f"https://www.sec.gov/edgar/browse/?CIK={cik_pad}",
                    "report_date": rdate,
                    "filing_date": fdate,
                    "active_positions_count": len(active_rows),
                    "exited_positions_count": len(exited_rows),
                    "total_value_usd": round(tot_val, 0),
                    "top_holdings": top_5,
                    "qoq_counts": qoq_counts,
                }
            )

        managers_out.sort(
            key=lambda m: (
                str(m.get("filing_date") or ""),
                str(m.get("report_date") or ""),
                float(m.get("total_value_usd") or 0.0),
            ),
            reverse=True,
        )

        # Build Common / Consensus Holdings list
        common_holdings: list[dict[str, Any]] = []
        consensus_count = 0
        for sym, h_list in by_ticker_active.items():
            h_cnt = len(h_list)
            if h_cnt >= 2:
                consensus_count += 1
            max_w = max((r["portfolio_weight_pct"] for r in h_list), default=0.0)
            comb_w = round(sum(r["portfolio_weight_pct"] for r in h_list), 2)
            tot_val_tkr = round(sum(r["value_usd"] for r in h_list), 0)
            new_add_cnt = sum(1 for r in h_list if r["qoq_action"] in ("NEW", "ADDED"))
            enr = _enrich_ticker(sym, fallback_name=h_list[0]["issuer_name"])

            holder_badges = []
            for r in h_list:
                short_m = r["manager_name"].split("(")[0].strip()
                act_tag = f" [{r['qoq_action']}]" if r["qoq_action"] in ("NEW", "ADDED", "REDUCED") else ""
                holder_badges.append(f"{short_m} ({r['portfolio_weight_pct']:.1f}%{act_tag})")

            common_holdings.append(
                {
                    "ticker": sym,
                    "name": enr["name"],
                    "sector": enr["sector"],
                    "holder_count": h_cnt,
                    "new_or_added_count": new_add_cnt,
                    "combined_weight_pct": comb_w,
                    "max_weight_pct": round(max_w, 2),
                    "total_13f_value_usd": tot_val_tkr,
                    "holders_summary": ", ".join(holder_badges),
                    "holders": [
                        {
                            "manager_id": r["manager_id"],
                            "manager_name": r["manager_name"],
                            "weight_pct": r["portfolio_weight_pct"],
                            "qoq_action": r["qoq_action"],
                            "qoq_chg_pct": r["qoq_shares_change_pct"],
                            "value_usd": r["value_usd"],
                            "shares": r["shares"],
                            "report_date": r["report_date"],
                        }
                        for r in h_list
                    ],
                    "current_price": enr["current_price"],
                    "currency": enr["currency"],
                    "pct_from_52w_low": enr["pct_from_52w_low"],
                    "pe": enr["pe"],
                    "pb": enr["pb"],
                    "p_fcf": enr["p_fcf"],
                    "vq_score": enr["vq_score"],
                    "in_screener": enr["in_screener"],
                }
            )

        common_holdings.sort(
            key=lambda r: (r["holder_count"], r["new_or_added_count"], r["combined_weight_pct"]),
            reverse=True,
        )

        # ASX Substantial Holder Notices (trailing 180 days)
        asx_notices: list[dict[str, Any]] = []
        try:
            cutoff_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
            for atkr, dt_s, headline, pdf_url in conn.execute(
                """
                SELECT ticker, date, headline, url
                FROM announcements
                WHERE date >= ? AND lower(headline) LIKE '%substantial%'
                ORDER BY date DESC
                LIMIT 80
                """,
                (cutoff_date,),
            ).fetchall():
                if not atkr:
                    continue
                hl = str(headline or "")
                sym_ax = str(atkr).strip().upper()
                if not sym_ax.endswith(".AX"):
                    sym_ax = f"{sym_ax}.AX"
                enr = _enrich_ticker(sym_ax)
                notice_type = (
                    "Ceasing Substantial Holder"
                    if "ceasing" in hl.lower()
                    else ("New Substantial Holder (603)" if "becoming" in hl.lower() else "Change in Substantial Holding (604)")
                )
                asx_notices.append(
                    {
                        "ticker": sym_ax,
                        "name": enr["name"],
                        "sector": enr["sector"],
                        "date": str(dt_s or "")[:10],
                        "headline": hl,
                        "notice_type": notice_type,
                        "url": pdf_url or "",
                        "current_price": enr["current_price"],
                        "pct_from_52w_low": enr["pct_from_52w_low"],
                        "pe": enr["pe"],
                        "pb": enr["pb"],
                    }
                )
        except Exception as ex:
            logger.debug("ASX notices dashboard query skipped: %s", ex)

        return {
            "kpi": {
                "managers_tracked": len(SUPERINVESTORS),
                "managers_with_filings": managers_with_filings,
                "total_active_positions": total_active_positions,
                "total_unique_tickers": len(by_ticker_active),
                "consensus_stocks_count": consensus_count,
                "recent_buys_count": len(recent_buys),
                "recent_sells_count": len(recent_sells),
                "combined_13f_aum_usd": round(combined_aum_usd, 0),
                "asx_notices_180d_count": len(asx_notices),
            },
            "managers": managers_out,
            "holdings_by_manager": holdings_by_manager,
            "common_holdings": common_holdings[:200],
            "recent_buys": recent_buys[:150],
            "recent_sells": recent_sells[:150],
            "asx_substantial_notices": asx_notices,
        }
    finally:
        conn.close()
