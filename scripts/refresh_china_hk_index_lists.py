#!/usr/bin/env python3
"""
Refresh China (CSI 300), Hong Kong (Hang Seng), and Taiwan 50 Business lists.

China   = CSI 300 constituents (Yahoo .SS / .SZ) ∪ China A-share buyback names
Hong Kong = Hang Seng constituents (Yahoo .HK) ∪ HKEX buyback names
Taiwan  = FTSE TWSE Taiwan 50 (Yahoo .TW) ∪ .TW buybacks if any

Sources:
  Hang Seng: Wikipedia constituent table, fallback yfiua/index-constituents.
  CSI 300: yfiua/index-constituents.
  Taiwan 50: Wikipedia (zh) constituent table + later review changes.
  Buybacks: buyback_registry + HKEX CSV + CNINFO 回购股份 announcements
            (unified SSE/SZSE disclosure; neither exchange publishes an
            HKEX-style daily repurchase blotter).

Usage:
  python scripts/refresh_china_hk_index_lists.py
  python scripts/refresh_china_hk_index_lists.py --json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config.identity import wiki_user_agent
from data.asx import DEFAULT_UNIVERSE_DB

logger = logging.getLogger("refresh_asia_index")

CHINA_OUT = ROOT / "Business lists" / "China CSI 300.csv"
CHINA_META = ROOT / "Business lists" / "China CSI 300.meta.csv"
LEGACY_CHINA_OUT = ROOT / "Business lists" / "China SSE Composite.csv"
LEGACY_CHINA_META = ROOT / "Business lists" / "China SSE Composite.meta.csv"
HK_OUT = ROOT / "Business lists" / "Hong Kong Hang Seng.csv"
HK_META = ROOT / "Business lists" / "Hong Kong Hang Seng.meta.csv"
TW_OUT = ROOT / "Business lists" / "Taiwan 50.csv"
TW_META = ROOT / "Business lists" / "Taiwan 50.meta.csv"
LEGACY_TW_OUT = ROOT / "Business lists" / "Taiwan Weighted.csv"
LEGACY_TW_META = ROOT / "Business lists" / "Taiwan Weighted.meta.csv"
CNINFO_CSV = ROOT / "cn_stock_repurchases" / "a_share_repurchases.csv"

HSI_WIKI = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
YFIUA_HSI = "https://yfiua.github.io/index-constituents/constituents-hsi.csv"
YFIUA_CSI300 = "https://yfiua.github.io/index-constituents/constituents-csi300.csv"
YFIUA_HSI_RAW = (
    "https://raw.githubusercontent.com/yfiua/index-constituents/gh-pages/"
    "constituents-hsi.csv"
)
YFIUA_CSI300_RAW = (
    "https://raw.githubusercontent.com/yfiua/index-constituents/gh-pages/"
    "constituents-csi300.csv"
)
TW50_WIKI = "https://zh.wikipedia.org/wiki/%E8%87%BA%E7%81%A350%E6%8C%87%E6%95%B8"
CNINFO_SEARCH = "https://www.cninfo.com.cn/new/fulltextSearch/full"
CNINFO_SEARCH_HTTP = "http://www.cninfo.com.cn/new/fulltextSearch/full"

HSI_SAMPLES = ("0005.HK", "0700.HK", "9988.HK", "0941.HK", "1299.HK")
CSI_SAMPLES = ("600519.SS", "300750.SZ", "000333.SZ", "601318.SS", "002594.SZ")
TW_SAMPLES = ("2330.TW", "2317.TW", "2454.TW", "2412.TW", "2308.TW")

# Post-June 2026 review snapshot if Wikipedia parse fails (50 names).
_TW50_FALLBACK = (
    "1216", "1303", "2059", "2301", "2303", "2308", "2317", "2327", "2330",
    "2344", "2345", "2357", "2360", "2368", "2382", "2383", "2395", "2408",
    "2412", "2449", "2454", "2603", "2880", "2881", "2882", "2883", "2884",
    "2885", "2886", "2887", "2890", "2891", "2892", "3008", "3017", "3037",
    "3045", "3231", "3443", "3653", "3661", "3665", "3711", "4904", "4958",
    "5880", "6505", "6669", "7769", "8046",
)

_HK_CODE_RE = re.compile(r"(\d{1,5})")
_A_SHARE_RE = re.compile(r"^(\d{6})(?:\.(SS|SZ))?$", re.I)
_TW_CODE_RE = re.compile(r"^(\d{4})(?:\.TW)?$", re.I)

# Shanghai A/B / STAR / CDR prefixes
_SSE_PREFIXES = ("600", "601", "603", "605", "688", "689", "900")
# Shenzhen main / SME / ChiNext
_SZSE_PREFIXES = ("000", "001", "002", "003", "300", "301", "302")


def _session_headers(kind: str = "wiki") -> dict:
    if kind == "wiki":
        return {"User-Agent": wiki_user_agent(), "Accept": "text/html,*/*"}
    return {
        "User-Agent": wiki_user_agent(),
        "Accept": "text/csv,application/json,text/plain,*/*",
    }


def _fetch_bytes(url: str, *, kind: str = "wiki", timeout: int = 60) -> bytes:
    """HTTP GET. TWSE often fails Windows/urllib SSL; retry via requests, then unverified."""
    headers = _session_headers(kind)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as first:
        try:
            import requests

            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception:
            try:
                import requests
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                r = requests.get(url, headers=headers, timeout=timeout, verify=False)
                r.raise_for_status()
                logger.warning("Fetched %s with SSL verify disabled (%s)", url, first)
                return r.content
            except Exception:
                raise first


def _read_wikipedia_tables(url: str) -> list:
    import pandas as pd

    headers = _session_headers("wiki")
    try:
        try:
            return pd.read_html(url, storage_options=headers)
        except TypeError:
            html = _fetch_bytes(url, kind="wiki", timeout=45)
            return pd.read_html(io.BytesIO(html))
    except Exception as e:
        logger.warning("Wikipedia table load failed for %s: %s", url, e)
        return []


def hk_code_to_yf(raw: str) -> str:
    """SEHK 5 / 0005 / 0005.HK / SEHK: 9988 → 0005.HK / 9988.HK."""
    s = str(raw or "").strip().upper().replace(".HK", "")
    s = s.replace("SEHK", " ").replace(":", " ")
    m = _HK_CODE_RE.search(s)
    if not m:
        return ""
    code = m.group(1)
    if len(code) <= 4:
        code = code.zfill(4)
    if not re.fullmatch(r"\d{4,5}", code):
        return ""
    return f"{code}.HK"


def a_share_to_yf(raw: str) -> str:
    """Map a 6-digit A-share code (or Yahoo symbol) to 600519.SS / 300750.SZ."""
    s = str(raw or "").strip().upper()
    m = _A_SHARE_RE.fullmatch(s)
    if m:
        code, sfx = m.group(1), (m.group(2) or "").upper()
        if sfx in ("SS", "SZ"):
            return f"{code}.{sfx}"
    digits = re.sub(r"\D", "", s)
    if not re.fullmatch(r"\d{6}", digits):
        return ""
    if digits.startswith(_SSE_PREFIXES):
        return f"{digits}.SS"
    if digits.startswith(_SZSE_PREFIXES):
        return f"{digits}.SZ"
    return ""


def tw_code_to_yf(raw: str) -> str:
    """2330 / 2330.TW → 2330.TW. Drop ETFs (00xx) and TDRs (91xx)."""
    s = str(raw or "").strip().upper().replace(".TWO", "").replace(".TW", "")
    m = _TW_CODE_RE.fullmatch(s)
    if not m:
        return ""
    code = m.group(1)
    if code.startswith("00") or code.startswith("91"):
        return ""
    return f"{code}.TW"


def _dedupe(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _dedupe_named(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for sym, name in rows:
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append((sym, name))
    return out


def _clean_name(val) -> str:
    name = str(val or "").strip()
    if name.lower() in ("nan", "none"):
        return ""
    return name


def load_hang_seng_wikipedia() -> list[tuple[str, str]]:
    """Return [(yf_symbol, name), ...] from the Hang Seng Index page."""
    tables = _read_wikipedia_tables(HSI_WIKI)
    best: list[tuple[str, str]] = []
    for df in tables:
        if df is None or df.empty or len(df) < 40:
            continue
        cols = {str(c).strip().lower(): c for c in df.columns}
        tick_col = None
        for want in ("ticker", "symbol", "stock code", "code"):
            if want in cols:
                tick_col = cols[want]
                break
        if tick_col is None:
            for c in df.columns:
                cl = str(c).strip().lower()
                if "ticker" in cl or "symbol" in cl or cl == "code":
                    tick_col = c
                    break
        if tick_col is None:
            continue
        name_col = None
        for want in ("name", "company", "constituent"):
            if want in cols:
                name_col = cols[want]
                break
        if name_col is None:
            for c in df.columns:
                if "name" in str(c).strip().lower():
                    name_col = c
                    break
        rows: list[tuple[str, str]] = []
        for _, row in df.iterrows():
            sym = hk_code_to_yf(row[tick_col])
            if not sym:
                continue
            name = _clean_name(row[name_col]) if name_col is not None else ""
            rows.append((sym, name))
        if len(rows) > len(best):
            best = rows
    return _dedupe_named(best)


def _symbol_mapper(market: str):
    if market == "hk":
        return hk_code_to_yf
    if market in ("csi", "csi300", "sse"):
        return a_share_to_yf
    if market in ("tw", "taiwan"):
        return tw_code_to_yf
    raise ValueError(f"unknown market {market}")


def _load_yfiua_csv(urls: list[str], market: str) -> list[tuple[str, str]]:
    import pandas as pd

    mapper = _symbol_mapper(market)
    last_err = None
    for url in urls:
        try:
            raw = _fetch_bytes(url, kind="csv", timeout=60)
            df = pd.read_csv(io.BytesIO(raw))
        except Exception as e:
            last_err = e
            logger.warning("yfiua fetch failed %s: %s", url, e)
            continue
        if df is None or df.empty:
            continue
        cols = {str(c).strip().lower(): c for c in df.columns}
        sym_col = cols.get("symbol") or cols.get("ticker") or cols.get("code")
        if sym_col is None:
            sym_col = df.columns[0]
        name_col = cols.get("name") or cols.get("company")
        rows: list[tuple[str, str]] = []
        for _, row in df.iterrows():
            sym = mapper(str(row[sym_col] or ""))
            if not sym:
                continue
            name = _clean_name(row[name_col]) if name_col is not None else ""
            rows.append((sym, name))
        if rows:
            print(f"  yfiua {market}: {len(rows)} from {url}")
            return _dedupe_named(rows)
    if last_err:
        logger.warning("All yfiua URLs failed for %s: %s", market, last_err)
    return []


def load_hang_seng_constituents() -> tuple[list[tuple[str, str]], str]:
    wiki = load_hang_seng_wikipedia()
    yfiua = _load_yfiua_csv([YFIUA_HSI, YFIUA_HSI_RAW], "hk")
    by_sym: dict[str, str] = {}
    sources = []
    if wiki:
        sources.append(f"wikipedia:{len(wiki)}")
        for sym, name in wiki:
            by_sym[sym] = name
    if yfiua:
        sources.append(f"yfiua:{len(yfiua)}")
        for sym, name in yfiua:
            if sym not in by_sym:
                by_sym[sym] = name
            elif not by_sym[sym] and name:
                by_sym[sym] = name
    rows = [(s, by_sym[s]) for s in sorted(by_sym)]
    return rows, "+".join(sources) or "none"


def load_csi300_constituents() -> tuple[list[tuple[str, str]], str]:
    rows = _load_yfiua_csv([YFIUA_CSI300, YFIUA_CSI300_RAW], "csi")
    return rows, ("yfiua" if rows else "none")


def _tw_codes_from_text(text: str) -> list[str]:
    """TWSE 4-digit codes from a Wikipedia cell (臺證所：2330 / 2330 台積電)."""
    s = str(text or "")
    found = re.findall(r"(?:臺證所|TWSE|TSE)[^\d]{0,12}(\d{4})", s, flags=re.I)
    if not found:
        found = re.findall(r"\b(\d{4})\b", s)
    out = []
    for code in found:
        if code.startswith("00") or code.startswith("91"):
            continue
        out.append(code)
    return out


def _parse_tw_year_month(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", str(text or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def load_taiwan_50_wikipedia() -> tuple[list[tuple[str, str]], str]:
    """FTSE TWSE Taiwan 50 from zh.wikipedia, applying later review changes."""
    import pandas as pd

    html = _fetch_bytes(TW50_WIKI, kind="wiki", timeout=45).decode("utf-8", errors="replace")
    tables = pd.read_html(io.StringIO(html))
    as_of = _parse_tw_year_month(
        next(iter(re.findall(r"成份股[^\n]{0,40}?(\d{4}年\d{1,2}月)", html)), "")
    ) or (2025, 9)

    by_sym: dict[str, str] = {}
    for df in tables:
        if df is None or df.empty or len(df) < 8 or len(df) > 80:
            continue
        cols = [str(c) for c in df.columns]
        joined = " ".join(cols)
        if "股票代號" not in joined and "代號" not in joined:
            continue
        # Constituent table is two side-by-side groups: code, name, weight × 2
        for _, row in df.iterrows():
            cells = [str(v) for v in row.tolist()]
            i = 0
            while i < len(cells):
                codes = _tw_codes_from_text(cells[i])
                if len(codes) == 1:
                    name = ""
                    if i + 1 < len(cells) and not _tw_codes_from_text(cells[i + 1]):
                        name = _clean_name(cells[i + 1])
                    by_sym[tw_code_to_yf(codes[0])] = name
                i += 1
        if len(by_sym) >= 40:
            break

    # Apply quarterly 納入 / 剔除 rows dated after the published table.
    for df in tables:
        if df is None or df.empty:
            continue
        cols = [str(c) for c in df.columns]
        joined = " ".join(cols)
        if "納入" not in joined or "剔除" not in joined:
            continue
        date_col = in_col = out_col = None
        for c in df.columns:
            cl = str(c)
            if date_col is None and ("日期" in cl or "date" in cl.lower()):
                date_col = c
            if in_col is None and "納入" in cl:
                in_col = c
            if out_col is None and "剔除" in cl:
                out_col = c
        if not (date_col and in_col and out_col):
            continue
        changes = []
        for _, row in df.iterrows():
            ym = _parse_tw_year_month(str(row[date_col]))
            if not ym:
                continue
            adds = [tw_code_to_yf(c) for c in _tw_codes_from_text(str(row[in_col]))]
            drops = [tw_code_to_yf(c) for c in _tw_codes_from_text(str(row[out_col]))]
            changes.append((ym, [a for a in adds if a], [d for d in drops if d]))
        changes.sort(key=lambda x: x[0])
        applied = 0
        for ym, adds, drops in changes:
            if ym <= as_of:
                continue
            for d in drops:
                by_sym.pop(d, None)
            for a in adds:
                by_sym.setdefault(a, "")
            applied += 1
        if applied:
            print(f"  Taiwan 50: applied {applied} reviews after {as_of[0]}-{as_of[1]:02d}")

    rows = [(s, by_sym[s]) for s in sorted(by_sym) if s]
    src = f"wikipedia:{len(rows)} as_of={as_of[0]}-{as_of[1]:02d}"
    return rows, src


def load_taiwan_50_constituents() -> tuple[list[tuple[str, str]], str]:
    try:
        rows, src = load_taiwan_50_wikipedia()
    except Exception as e:
        logger.warning("Taiwan 50 Wikipedia load failed: %s", e)
        rows, src = [], "none"
    if 45 <= len(rows) <= 55 and "2330.TW" in {s for s, _ in rows}:
        return rows, src
    fallback = [(tw_code_to_yf(c), "") for c in _TW50_FALLBACK]
    fallback = [(s, n) for s, n in fallback if s]
    print(f"  Taiwan 50: using June 2026 fallback ({len(fallback)}); wiki had {len(rows)}")
    return fallback, "fallback_2026q2"


def _hk_buyback_from_csv() -> list[str]:
    try:
        from data.hkex_repurchases import DEFAULT_CSV_PATH
    except Exception:
        return []
    path = Path(DEFAULT_CSV_PATH)
    if not path.exists():
        return []
    try:
        import pandas as pd

        df = pd.read_csv(path, encoding="utf-8")
    except Exception as e:
        logger.warning("HK buyback CSV read failed: %s", e)
        return []
    col = None
    for c in df.columns:
        if str(c).strip().lower() in ("stock code", "stockcode", "code"):
            col = c
            break
    if col is None:
        return []
    out = []
    for raw in df[col].tolist():
        sym = hk_code_to_yf(str(raw))
        if sym:
            out.append(sym)
    return _dedupe(out)


_CNINFO_KEEP = ("回购股份", "股份回购", "回购报告书", "回购进展", "回购实施")
_CNINFO_DROP = ("质押回购", "债券回购", "约定购回", "股票质押", "质押式回购", "国债回购")


def _cninfo_title_ok(title: str) -> bool:
    t = title or ""
    if any(k in t for k in _CNINFO_DROP):
        return False
    return any(k in t for k in _CNINFO_KEEP)


EASTMONEY_GPHG = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _write_cn_buyback_csv(found: dict[str, str]) -> list[str]:
    if not found:
        return []
    CNINFO_CSV.parent.mkdir(parents=True, exist_ok=True)
    with CNINFO_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "name"])
        w.writeheader()
        for sym in sorted(found):
            w.writerow({"ticker": sym, "name": found[sym]})
    return sorted(found)


def fetch_eastmoney_buyback_tickers(*, max_pages: int = 12) -> list[str]:
    """Daily A-share repurchase tape via East Money (aggregates SSE/SZSE filings)."""
    try:
        import requests
    except Exception:
        return []

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": wiki_user_agent(),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://data.eastmoney.com/gphg/",
        }
    )
    found: dict[str, str] = {}
    pages = 1
    for page in range(1, max_pages + 1):
        params = {
            "reportName": "RPTA_WEB_GPHG",
            "columns": "ALL",
            "pageNumber": str(page),
            "pageSize": "500",
            "source": "WEB",
            "client": "WEB",
        }
        try:
            r = session.get(EASTMONEY_GPHG, params=params, timeout=30)
            r.raise_for_status()
            js = r.json()
        except Exception as e:
            logger.warning("East Money GPHG page %s failed: %s", page, e)
            break
        if not js.get("success"):
            logger.warning("East Money GPHG: %s", js.get("message"))
            break
        result = js.get("result") or {}
        rows = result.get("data") or []
        pages = int(result.get("pages") or pages)
        if not rows:
            break
        for row in rows:
            sym = a_share_to_yf(str(row.get("SCODE") or ""))
            if not sym:
                continue
            found.setdefault(sym, str(row.get("SNAME") or "").strip())
        if page >= pages:
            break
    if found:
        print(f"  East Money A-share buybacks: {len(found)} tickers")
    return _write_cn_buyback_csv(found) if found else []


def fetch_cninfo_buyback_tickers(*, max_pages: int = 12) -> list[str]:
    """Unique A-share tickers from CNINFO 回购股份 announcements (SSE+SZSE)."""
    import datetime as dt

    try:
        import requests
    except Exception:
        return []

    end = dt.date.today()
    start = end.replace(year=end.year - 1)
    found: dict[str, str] = {}
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": wiki_user_agent(),
            "Accept": "application/json,text/plain,*/*",
            "Referer": "http://www.cninfo.com.cn/",
        }
    )
    search_urls = (CNINFO_SEARCH_HTTP, CNINFO_SEARCH)
    active_url = None
    for page in range(1, max_pages + 1):
        params = {
            "searchkey": "回购股份",
            "sdate": start.isoformat(),
            "edate": end.isoformat(),
            "isfulltext": "false",
            "sortName": "nothing",
            "sortType": "desc",
            "pageNum": str(page),
        }
        js = None
        last_err = None
        for url in ((active_url,) if active_url else search_urls):
            if not url:
                continue
            try:
                r = session.get(url, params=params, timeout=30)
                r.raise_for_status()
                js = r.json()
                active_url = url
                break
            except Exception as e:
                last_err = e
                continue
        if js is None:
            logger.warning("CNINFO buyback page %s failed: %s", page, last_err)
            break
        anns = js.get("announcements") or []
        if not anns:
            break
        for a in anns:
            if not _cninfo_title_ok(str(a.get("announcementTitle") or "")):
                continue
            sym = a_share_to_yf(str(a.get("secCode") or ""))
            if not sym:
                continue
            found.setdefault(sym, str(a.get("secName") or "").strip())
        total = int(js.get("totalAnnouncement") or 0)
        if page * 10 >= total:
            break

    if found:
        print(f"  CNINFO A-share buybacks: {len(found)} tickers (last 12 months)")
    return _write_cn_buyback_csv(found) if found else []


def harvest_china_buyback_tickers() -> list[str]:
    """Prefer East Money daily tape; fall back to CNINFO announcement search."""
    tickers = fetch_eastmoney_buyback_tickers()
    if tickers:
        return tickers
    return fetch_cninfo_buyback_tickers()


def _cninfo_buyback_from_csv() -> list[str]:
    if not CNINFO_CSV.exists():
        return []
    try:
        import pandas as pd

        df = pd.read_csv(CNINFO_CSV, encoding="utf-8-sig")
    except Exception:
        return []
    col = None
    for c in df.columns:
        if str(c).strip().lower() in ("ticker", "yf_symbol", "symbol", "code"):
            col = c
            break
    if col is None:
        return []
    out = []
    for raw in df[col].tolist():
        sym = a_share_to_yf(str(raw))
        if sym:
            out.append(sym)
    return _dedupe(out)


def buyback_symbols(*suffixes: str) -> list[str]:
    """Yahoo symbols in the buyback registry (and HK/CNINFO CSVs) for these suffixes."""
    found: set[str] = set()
    db = Path(DEFAULT_UNIVERSE_DB)
    if not db.is_absolute():
        db = ROOT / db
    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            try:
                for (sym,) in conn.execute("SELECT yf_symbol FROM buyback_registry"):
                    s = (sym or "").strip().upper()
                    if s.endswith(suffixes):
                        found.add(s)
            except sqlite3.OperationalError:
                pass
            try:
                for (sym,) in conn.execute(
                    "SELECT yf_symbol FROM business_list_entries WHERE list_id=?",
                    ("__builtin__:buybacks",),
                ):
                    s = (sym or "").strip().upper()
                    if s.endswith(suffixes):
                        found.add(s)
            except sqlite3.OperationalError:
                pass
            conn.close()
        except Exception as e:
            logger.warning("buyback registry query failed: %s", e)
    if ".HK" in suffixes:
        found.update(_hk_buyback_from_csv())
    if ".SS" in suffixes or ".SZ" in suffixes:
        found.update(_cninfo_buyback_from_csv())
    return sorted(found)


def union_with_buybacks(
    index_rows: list[tuple[str, str]],
    extras: list[str],
) -> list[tuple[str, str, str]]:
    """(ticker, name, source) with index first, then buyback-only extras."""
    in_index = {s for s, _ in index_rows}
    out: list[tuple[str, str, str]] = [(s, n, "index") for s, n in index_rows]
    for sym in extras:
        if sym and sym not in in_index:
            out.append((sym, "", "buyback"))
            in_index.add(sym)
    return out


def write_list(path: Path, symbols: list[str], comments: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [c if c.startswith("#") else f"# {c}" for c in comments]
    lines.extend(symbols)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_meta(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "name", "source"])
        w.writeheader()
        for ticker, name, source in rows:
            w.writerow({"ticker": ticker, "name": name or "", "source": source})


def _check_samples(symbols: list[str], samples: tuple[str, ...]) -> list[str]:
    sset = set(symbols)
    return [t for t in samples if t not in sset]


def _unlink_legacy_lists() -> None:
    for path in (LEGACY_CHINA_OUT, LEGACY_CHINA_META, LEGACY_TW_OUT, LEGACY_TW_META):
        if path.exists():
            path.unlink()
            print(f"Removed legacy {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh China CSI 300, Hong Kong Hang Seng, and Taiwan 50 lists"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--skip-buybacks",
        action="store_true",
        help="Do not union buyback-registry extras",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    hsi_rows, hsi_src = load_hang_seng_constituents()
    csi_rows, csi_src = load_csi300_constituents()
    tw_rows, tw_src = load_taiwan_50_constituents()
    print(f"Hang Seng constituents: {len(hsi_rows)} ({hsi_src})")
    print(f"CSI 300 constituents: {len(csi_rows)} ({csi_src})")
    print(f"Taiwan 50 constituents: {len(tw_rows)} ({tw_src})")

    if not args.skip_buybacks:
        try:
            harvest_china_buyback_tickers()
        except Exception as e:
            logger.warning("A-share buyback harvest failed: %s", e)

    hk_bb = [] if args.skip_buybacks else buyback_symbols(".HK")
    cn_bb = [] if args.skip_buybacks else buyback_symbols(".SS", ".SZ")
    tw_bb = [] if args.skip_buybacks else buyback_symbols(".TW")
    print(
        f"Buyback extras available: HK={len(hk_bb)} China(.SS/.SZ)={len(cn_bb)} "
        f"TW={len(tw_bb)}"
    )

    hk_all = union_with_buybacks(hsi_rows, hk_bb)
    cn_all = union_with_buybacks(csi_rows, cn_bb)
    tw_all = union_with_buybacks(tw_rows, tw_bb)
    hk_syms = [t for t, _, _ in hk_all]
    cn_syms = [t for t, _, _ in cn_all]
    tw_syms = [t for t, _, _ in tw_all]
    hk_extra = sum(1 for *_, src in hk_all if src == "buyback")
    cn_extra = sum(1 for *_, src in cn_all if src == "buyback")
    tw_extra = sum(1 for *_, src in tw_all if src == "buyback")

    result = {
        "hang_seng": {
            "index_source": hsi_src,
            "index_n": len(hsi_rows),
            "buyback_extras": hk_extra,
            "total": len(hk_syms),
            "missing_samples": _check_samples(hk_syms, HSI_SAMPLES),
        },
        "csi300": {
            "index_source": csi_src,
            "index_n": len(csi_rows),
            "buyback_extras": cn_extra,
            "total": len(cn_syms),
            "missing_samples": _check_samples(cn_syms, CSI_SAMPLES),
        },
        "taiwan_50": {
            "index_source": tw_src,
            "index_n": len(tw_rows),
            "buyback_extras": tw_extra,
            "total": len(tw_syms),
            "missing_samples": _check_samples(tw_syms, TW_SAMPLES),
        },
    }

    errors = []
    if len(hsi_rows) < 50:
        errors.append("Hang Seng constituent count too low")
    if not (250 <= len(csi_rows) <= 350):
        errors.append(f"CSI 300 count unexpected ({len(csi_rows)})")
    if not (45 <= len(tw_rows) <= 55):
        errors.append(f"Taiwan 50 count unexpected ({len(tw_rows)})")
    for key, label in (
        ("hang_seng", "Hang Seng"),
        ("csi300", "CSI 300"),
        ("taiwan_50", "Taiwan 50"),
    ):
        missing = result[key]["missing_samples"]
        if missing:
            errors.append(f"{label} missing {missing}")
    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        if args.json:
            print(json.dumps(result, indent=2))
        return 2

    write_list(
        HK_OUT,
        hk_syms,
        [
            "Hong Kong Hang Seng constituents (Yahoo .HK) plus HKEX buyback extras.",
            "Refresh: python scripts/refresh_china_hk_index_lists.py",
            f"Source: {hsi_src}; index={len(hsi_rows)} buyback_extras={hk_extra}",
        ],
    )
    write_meta(HK_META, hk_all)
    write_list(
        CHINA_OUT,
        cn_syms,
        [
            "China CSI 300 constituents (Yahoo .SS / .SZ) plus China-market buyback extras.",
            "Refresh: python scripts/refresh_china_hk_index_lists.py",
            f"Source: {csi_src}; index={len(csi_rows)} buyback_extras={cn_extra}",
        ],
    )
    write_meta(CHINA_META, cn_all)
    write_list(
        TW_OUT,
        tw_syms,
        [
            "FTSE TWSE Taiwan 50 constituents (Yahoo .TW) plus Taiwan-market buyback extras.",
            "Refresh: python scripts/refresh_china_hk_index_lists.py",
            f"Source: {tw_src}; index={len(tw_rows)} buyback_extras={tw_extra}",
        ],
    )
    write_meta(TW_META, tw_all)
    _unlink_legacy_lists()

    print(f"Wrote {HK_OUT} ({len(hk_syms)} tickers, {hk_extra} buyback extras)")
    print(f"Wrote {CHINA_OUT} ({len(cn_syms)} tickers, {cn_extra} buyback extras)")
    print(f"Wrote {TW_OUT} ({len(tw_syms)} tickers, {tw_extra} buyback extras)")
    for label, samples, syms in (
        ("HSI", HSI_SAMPLES, hk_syms),
        ("CSI", CSI_SAMPLES, cn_syms),
        ("TW", TW_SAMPLES, tw_syms),
    ):
        sset = set(syms)
        for t in samples:
            print(f"  {label} {t}: {'yes' if t in sset else 'NO'}")

    if args.json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
