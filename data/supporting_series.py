"""
Official supporting time-series: EIA v2 + FRED v1 + Pink Sheet + AEMO → SQLite.

Catalog: config/supporting_series.yaml
Store:   data/supporting_series.db  (table supporting_series)

Phase 1: eia/fred daily+weekly, plus derived daily (crack_321).
Phase 3: eia/fred monthly, World Bank Pink Sheet monthly, AEMO NEM daily means.
Barchart daily CSV and TBD/ember/worldsteel rows are not fetched here.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

from config.identity import wiki_user_agent

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _PROJECT_ROOT / "config" / "supporting_series.yaml"
DEFAULT_DB = str(_PROJECT_ROOT / "data" / "supporting_series.db")

EIA_SERIES_URL = "https://api.eia.gov/v2/seriesid/{series_id}"
EIA_ROUTE_DATA_URL = "https://api.eia.gov/v2/{route}/data/"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
AEMO_CSV_URL = (
    "https://aemo.com.au/aemo/data/nem/priceanddemand/"
    "PRICE_AND_DEMAND_{yyyymm}_{region}.csv"
)
WORLDBANK_PINK_URLS = (
    "https://thedocs.worldbank.org/en/doc/"
    "74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/"
    "CMO-Historical-Data-Monthly.xlsx",
    "https://thedocs.worldbank.org/en/doc/"
    "18675f1d1639c7a34d463f59263ba0a2-0050012025/related/"
    "CMO-Historical-Data-Monthly.xlsx",
)
WORLDBANK_COMMODITY_PAGE = (
    "https://www.worldbank.org/en/research/commodity-markets"
)
_SECRET_QS = re.compile(r"(api_key=)[^&]+", re.I)

PHASE1_PROVIDERS = frozenset({"eia", "fred", "yfinance"})
PHASE1_FREQ = frozenset({"daily", "weekly"})
PHASE3_FILE_PROVIDERS = frozenset({"worldbank", "aemo"})
EIA_PAGE = 5000
REQUEST_SLEEP = 0.35
FETCHABLE_PROVIDERS = frozenset(
    {"eia", "fred", "worldbank", "aemo", "yfinance", "bakerhughes"}
)
AOGR_RIG_URL = "https://www.aogr.com/web-exclusives/us-rig-count"
AEMO_FULL_START = date(2018, 1, 1)
AEMO_DEFAULT_MONTHS = 36
# Pink Sheet "Monthly Prices" header aliases → catalog native id
WORLDBANK_HEADER_ALIASES = {
    "COAL_AUS": ("coal, australian", "coal, australia", "coal (australia)"),
    "LNG_JPN": (
        "liquefied natural gas, japan",
        "lng, japan",
        "natural gas, japan",
        "lng japan",
    ),
    "ALUMINA": ("alumina", "aluminum, alumina", "aluminium, alumina"),
    "LITHIUM_CARB": (
        "lithium",
        "lithium carbonate",
        "lithium, carbonate",
        "lithium carbonate, battery",
        "lithium (carbonate)",
    ),
    "IRON_ORE": (
        "iron ore, cfr spot",
        "iron ore",
        "iron ore, cfr",
        "iron ore cfr",
    ),
}

_config_cache: dict | None = None
_pink_cache: dict[str, list[dict]] | None = None


def load_supporting_config(force: bool = False) -> dict:
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache
    if not CONFIG_PATH.exists():
        _config_cache = {"series": {}, "clusters": {}, "exposures": []}
        return _config_cache
    with open(CONFIG_PATH, encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f) or {}
    _config_cache.setdefault("series", {})
    _config_cache.setdefault("clusters", {})
    _config_cache.setdefault("exposures", [])
    return _config_cache


def _src(meta: dict, key: str = "source") -> dict:
    raw = meta.get(key) or {}
    return raw if isinstance(raw, dict) else {}


def _native_id(src: dict) -> str:
    return str(src.get("id") or "").strip()


def _is_tbd(src: dict) -> bool:
    nid = _native_id(src).upper()
    return not nid or nid.startswith("TBD")


def is_phase1_series(meta: dict) -> bool:
    """Daily/weekly official fetch, or derived from those."""
    freq = (meta.get("freq") or "").lower()
    if freq not in PHASE1_FREQ:
        return False
    src = _src(meta)
    if src.get("provider") == "derived":
        return True
    if src.get("provider") in PHASE1_PROVIDERS and not _is_tbd(src):
        return True
    fb = _src(meta, "fallback")
    return fb.get("provider") in PHASE1_PROVIDERS and not _is_tbd(fb)


def phase1_series_ids(*, include_derived: bool = True) -> list[str]:
    cfg = load_supporting_config()
    out = []
    for sid, meta in (cfg.get("series") or {}).items():
        if not is_phase1_series(meta or {}):
            continue
        if not include_derived and _src(meta).get("provider") == "derived":
            continue
        out.append(sid)
    return out


def is_phase3_series(meta: dict) -> bool:
    """Monthly official (EIA/FRED/Pink Sheet) or AEMO daily regional means."""
    if meta.get("fetch") is False:
        return False
    src = _src(meta)
    if _is_tbd(src):
        return False
    provider = src.get("provider")
    if provider in PHASE3_FILE_PROVIDERS:
        return True
    freq = (meta.get("freq") or "").lower()
    if freq != "monthly":
        return False
    if provider in PHASE1_PROVIDERS:
        return True
    fb = _src(meta, "fallback")
    return fb.get("provider") in PHASE1_PROVIDERS and not _is_tbd(fb)


def phase3_series_ids() -> list[str]:
    cfg = load_supporting_config()
    return [
        sid
        for sid, meta in (cfg.get("series") or {}).items()
        if is_phase3_series(meta or {})
    ]


def official_series_ids(*, include_derived: bool = True) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sid in phase1_series_ids(include_derived=include_derived) + phase3_series_ids():
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def series_phase(meta: dict) -> int | None:
    if is_phase1_series(meta):
        return 1
    if is_phase3_series(meta):
        return 3
    return None


def _redact(text: str) -> str:
    return _SECRET_QS.sub(r"\1***", text)


def _http_error(resp: requests.Response) -> RuntimeError:
    return RuntimeError(f"HTTP {resp.status_code} {_redact(resp.url)}")


def _api_key(provider: str) -> str:
    env = "EIA_API_KEY" if provider == "eia" else "FRED_API_KEY"
    return (os.environ.get(env) or "").strip().strip("\"'")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": wiki_user_agent()})
    return s


def _norm_date(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s + "-01"
    m_pink = re.fullmatch(r"(\d{4})M(\d{2})", s, re.I)
    if m_pink:
        return f"{m_pink.group(1)}-{m_pink.group(2)}-01"
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    m = re.fullmatch(r"(\d{4})-W(\d{2})", s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {int(m.group(2))} 5", "%G %V %u"
            ).date().isoformat()
        except ValueError:
            return None
    return None


def _to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in (".", "na", "null", "nan", "none", "…", "...", "—", "-"):
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supporting_series (
            series_id TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            source TEXT NOT NULL,
            source_id TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (series_id, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supporting_series_fetch (
            series_id TEXT PRIMARY KEY,
            provider TEXT,
            source_id TEXT,
            n_points INTEGER,
            first_date TEXT,
            last_date TEXT,
            last_error TEXT,
            fetched_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def last_stored_date(series_id: str, db_path: str = DEFAULT_DB) -> str | None:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT MAX(date) FROM supporting_series WHERE series_id=?",
        (series_id,),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def upsert_points(
    series_id: str,
    points: list[dict],
    *,
    source: str,
    source_id: str,
    db_path: str = DEFAULT_DB,
) -> int:
    if not points:
        return 0
    fetched_at = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    n = 0
    for p in points:
        d = p.get("date")
        v = p.get("value")
        if not d or v is None:
            continue
        conn.execute(
            """
            INSERT INTO supporting_series
                (series_id, date, value, source, source_id, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_id, date) DO UPDATE SET
                value=excluded.value,
                source=excluded.source,
                source_id=excluded.source_id,
                fetched_at=excluded.fetched_at
            """,
            (series_id, d, float(v), source, source_id, fetched_at),
        )
        n += 1
    dates = [p["date"] for p in points if p.get("date")]
    conn.execute(
        """
        INSERT INTO supporting_series_fetch
            (series_id, provider, source_id, n_points, first_date, last_date,
             last_error, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
        ON CONFLICT(series_id) DO UPDATE SET
            provider=excluded.provider,
            source_id=excluded.source_id,
            n_points=(SELECT COUNT(*) FROM supporting_series WHERE series_id=?),
            first_date=(SELECT MIN(date) FROM supporting_series WHERE series_id=?),
            last_date=(SELECT MAX(date) FROM supporting_series WHERE series_id=?),
            last_error=NULL,
            fetched_at=excluded.fetched_at
        """,
        (
            series_id,
            source,
            source_id,
            n,
            min(dates) if dates else None,
            max(dates) if dates else None,
            fetched_at,
            series_id,
            series_id,
            series_id,
        ),
    )
    conn.commit()
    conn.close()
    return n


def _record_error(series_id: str, err: str, db_path: str = DEFAULT_DB) -> None:
    conn = _conn(db_path)
    conn.execute(
        """
        INSERT INTO supporting_series_fetch
            (series_id, last_error, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(series_id) DO UPDATE SET
            last_error=excluded.last_error,
            fetched_at=excluded.fetched_at
        """,
        (series_id, err[:400], datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def fetch_yfinance(native_id: str, *, start: str | None = None) -> list[dict]:
    """Daily Yahoo futures/spot (e.g. TIO=F SGX 62% Fe iron ore)."""
    import yfinance as yf

    sym = (native_id or "").strip()
    if not sym:
        raise RuntimeError("empty yfinance symbol")
    kwargs: dict[str, Any] = {"auto_adjust": True}
    if start:
        kwargs["start"] = start[:10]
    else:
        kwargs["period"] = "max"
    hist = yf.Ticker(sym).history(**kwargs)
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance {sym}: no history")
    out = []
    for dt, row in hist.iterrows():
        try:
            d = dt.strftime("%Y-%m-%d")
        except Exception:
            d = str(dt)[:10]
        v = _to_float(row.get("Close"))
        if d and v is not None:
            out.append({"date": d, "value": v})
    if not out:
        raise RuntimeError(f"yfinance {sym}: no numeric closes")
    return out


def fetch_fred(
    native_id: str,
    *,
    start: str | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    key = _api_key("fred")
    if not key:
        raise RuntimeError("FRED_API_KEY missing")
    sess = session or _session()
    params: dict[str, Any] = {
        "series_id": native_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }
    if start:
        params["observation_start"] = start
    r = sess.get(FRED_OBS_URL, params=params, timeout=60)
    if not r.ok:
        raise _http_error(r)
    payload = r.json()
    if payload.get("error_code"):
        raise RuntimeError(
            f"FRED {payload.get('error_code')}: {payload.get('error_message')}"
        )
    out = []
    for o in payload.get("observations") or []:
        d = _norm_date(o.get("date"))
        v = _to_float(o.get("value"))
        if d and v is not None:
            out.append({"date": d, "value": v})
    return out


def fetch_eia(
    native_id: str,
    *,
    start: str | None = None,
    session: requests.Session | None = None,
    route: str | None = None,
) -> list[dict]:
    key = _api_key("eia")
    if not key:
        raise RuntimeError("EIA_API_KEY missing")
    if route:
        return _fetch_eia_route(
            route, native_id, start=start, session=session, api_key=key
        )
    sess = session or _session()
    url = EIA_SERIES_URL.format(series_id=native_id)
    out: list[dict] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "api_key": key,
            "offset": offset,
            "length": EIA_PAGE,
        }
        if start:
            params["start"] = start
        r = sess.get(url, params=params, timeout=60)
        if r.status_code == 403:
            raise RuntimeError("EIA API_KEY_INVALID or forbidden")
        if not r.ok:
            raise _http_error(r)
        payload = r.json()
        if payload.get("error"):
            err = payload["error"]
            if isinstance(err, dict):
                raise RuntimeError(err.get("message") or err.get("code") or str(err))
            raise RuntimeError(str(err))
        resp = payload.get("response") or {}
        rows = resp.get("data") or []
        total = int(resp.get("total") or 0)
        for row in rows:
            d = _norm_date(row.get("period"))
            v = _to_float(row.get("value"))
            if d and v is not None:
                out.append({"date": d, "value": v})
        offset += len(rows)
        if not rows or len(rows) < EIA_PAGE or (total and offset >= total):
            break
        time.sleep(REQUEST_SLEEP)
    # newest-first pages can leave duplicates; keep last
    by_date = {p["date"]: p for p in out}
    return [by_date[k] for k in sorted(by_date)]


def _fetch_eia_route(
    route: str,
    native_id: str,
    *,
    start: str | None,
    session: requests.Session | None,
    api_key: str,
    frequency: str = "monthly",
) -> list[dict]:
    """EIA v2 facet route (Baker Hughes lives on petroleum/crd/drill, not seriesid)."""
    sess = session or _session()
    url = EIA_ROUTE_DATA_URL.format(route=str(route).strip("/"))
    out: list[dict] = []
    offset = 0
    eia_start = start[:7] if start and len(start) >= 7 else start
    while True:
        params: list[tuple[str, Any]] = [
            ("api_key", api_key),
            ("frequency", frequency),
            ("data[0]", "value"),
            ("facets[series][]", native_id),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", offset),
            ("length", EIA_PAGE),
        ]
        if eia_start:
            params.append(("start", eia_start))
        r = sess.get(url, params=params, timeout=60)
        if r.status_code == 403:
            raise RuntimeError("EIA API_KEY_INVALID or forbidden")
        if not r.ok:
            raise _http_error(r)
        payload = r.json()
        if payload.get("error"):
            err = payload["error"]
            if isinstance(err, dict):
                raise RuntimeError(err.get("message") or err.get("code") or str(err))
            raise RuntimeError(str(err))
        resp = payload.get("response") or {}
        rows = resp.get("data") or []
        total = int(resp.get("total") or 0)
        for row in rows:
            d = _norm_date(row.get("period"))
            v = _to_float(row.get("value"))
            if d and v is not None:
                out.append({"date": d, "value": v})
        offset += len(rows)
        if not rows or len(rows) < EIA_PAGE or (total and offset >= total):
            break
        time.sleep(REQUEST_SLEEP)
    by_date = {p["date"]: p for p in out}
    return [by_date[k] for k in sorted(by_date)]


def _download_headers() -> dict[str, str]:
    # AEMO / thedocs.worldbank.org 403 a generic research UA; Chrome-like is accepted.
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }


def _wb_discover_pink_url(session: requests.Session) -> str | None:
    try:
        r = session.get(WORLDBANK_COMMODITY_PAGE, timeout=45)
    except Exception as e:
        logger.warning("worldbank commodity page: %s", e)
        return None
    if not r.ok:
        return None
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', r.text, re.I)
    for h in hrefs:
        if "CMO-Historical-Data-Monthly.xlsx" in h:
            if h.startswith("//"):
                return "https:" + h
            return h
    return None


def _download_pink_sheet(session: requests.Session) -> bytes:
    urls = list(WORLDBANK_PINK_URLS)
    discovered = _wb_discover_pink_url(session)
    if discovered and discovered not in urls:
        urls.insert(0, discovered)
    last_err = "no url"
    for url in urls:
        try:
            r = session.get(url, timeout=90)
        except Exception as e:
            last_err = str(e)
            continue
        if r.ok and len(r.content) > 10_000 and r.content[:2] == b"PK":
            return r.content
        last_err = f"HTTP {r.status_code} {len(r.content)}b"
    raise RuntimeError(f"Pink Sheet monthly xlsx download failed: {last_err}")


def _pink_header_key(cell: str) -> str | None:
    raw = re.sub(r"\*+", "", str(cell or "")).strip().lower()
    raw = re.sub(r"\s+", " ", raw)
    if not raw or raw in ("nan", "none"):
        return None
    for code, aliases in WORLDBANK_HEADER_ALIASES.items():
        if raw == code.lower() or raw in aliases:
            return code
    return None


def _parse_pink_sheet(content: bytes) -> dict[str, list[dict]]:
    import pandas as pd

    xl = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    sheet = "Monthly Prices" if "Monthly Prices" in xl.sheet_names else xl.sheet_names[0]
    df = xl.parse(sheet, header=None, dtype=str)
    if df.empty:
        raise RuntimeError("Pink Sheet Monthly Prices sheet is empty")

    header_row = None
    col_map: dict[str, int] = {}
    for i in range(min(12, len(df))):
        found: dict[str, int] = {}
        for j, cell in enumerate(df.iloc[i].tolist()):
            key = _pink_header_key(str(cell or ""))
            if key and key not in found:
                found[key] = j
        if found:
            header_row = i
            col_map = found
            break
    if header_row is None or not col_map:
        raise RuntimeError("Pink Sheet header row (COAL_AUS / LNG_JPN / …) not found")

    by_code: dict[str, list[dict]] = {k: [] for k in col_map}
    for i in range(header_row + 1, len(df)):
        raw_date = str(df.iat[i, 0] or "").strip()
        d = _norm_date(raw_date)
        if not d:
            continue
        for code, col in col_map.items():
            v = _to_float(df.iat[i, col] if col < df.shape[1] else None)
            if v is not None:
                by_code[code].append({"date": d, "value": v})
    return by_code


def fetch_worldbank(
    native_id: str,
    *,
    start: str | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    global _pink_cache
    sess = session or _session()
    sess.headers.update(_download_headers())
    if _pink_cache is None:
        _pink_cache = _parse_pink_sheet(_download_pink_sheet(sess))
        logger.info(
            "Pink Sheet columns: %s",
            {k: len(v) for k, v in _pink_cache.items()},
        )
    code = (native_id or "").strip().upper()
    if code not in _pink_cache:
        have = ", ".join(sorted(_pink_cache)) or "none"
        raise RuntimeError(f"Pink Sheet has no column for {code} (have {have})")
    pts = _pink_cache[code]
    if start:
        pts = [p for p in pts if p["date"] >= start[:10]]
    return pts


def _aemo_settlement_date(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    date_part = s.split()[0]
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_part, fmt).date().isoformat()
        except ValueError:
            continue
    return _norm_date(date_part)


def _aemo_month_span(start: str | None, full: bool) -> list[tuple[int, int]]:
    today = date.today()
    if full:
        begin = AEMO_FULL_START
    elif start:
        try:
            begin = datetime.fromisoformat(start[:10]).date().replace(day=1)
        except ValueError:
            begin = date(today.year, today.month, 1) - timedelta(days=32)
            begin = begin.replace(day=1)
    else:
        y = today.year
        m = today.month - (AEMO_DEFAULT_MONTHS - 1)
        while m <= 0:
            y -= 1
            m += 12
        begin = date(y, m, 1)
    out: list[tuple[int, int]] = []
    y, m = begin.year, begin.month
    while (y, m) <= (today.year, today.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _aemo_daily_means(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    if "SETTLEMENTDATE" not in text[:400].upper() or "," not in text[:200]:
        raise RuntimeError("AEMO response is not a PRICE_AND_DEMAND CSV")
    reader = csv.DictReader(io.StringIO(text))
    buckets: dict[str, list[float]] = {}
    for row in reader:
        if not row:
            continue
        period = (row.get("PERIODTYPE") or "").strip().upper()
        if period and period not in ("TRADE", "TRADE INTERVAL", ""):
            continue
        d = _aemo_settlement_date(row.get("SETTLEMENTDATE") or "")
        v = _to_float(row.get("RRP"))
        if d and v is not None:
            buckets.setdefault(d, []).append(v)
    return [
        {"date": d, "value": round(sum(vs) / len(vs), 4)}
        for d, vs in sorted(buckets.items())
        if vs
    ]


def fetch_aemo(
    native_id: str,
    *,
    start: str | None = None,
    full: bool = False,
    session: requests.Session | None = None,
) -> list[dict]:
    region = (native_id or "").strip().upper()
    if not region:
        raise RuntimeError("AEMO region id missing")
    sess = session or _session()
    sess.headers.update(_download_headers())
    months = _aemo_month_span(start, full)
    if not months:
        return []
    out: list[dict] = []
    errors = 0
    last_err = ""
    for y, m in months:
        url = AEMO_CSV_URL.format(yyyymm=f"{y:04d}{m:02d}", region=region)
        r = None
        for attempt in range(2):
            try:
                r = sess.get(url, timeout=90)
                break
            except Exception as e:
                last_err = str(e)
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                errors += 1
                r = None
        if r is None:
            continue
        if r.status_code == 404:
            time.sleep(REQUEST_SLEEP)
            continue
        if not r.ok:
            errors += 1
            last_err = f"HTTP {r.status_code}"
            time.sleep(REQUEST_SLEEP)
            continue
        try:
            out.extend(_aemo_daily_means(r.content))
        except Exception as e:
            errors += 1
            last_err = str(e)
        time.sleep(REQUEST_SLEEP)
    if not out:
        raise RuntimeError(
            f"AEMO {region}: no monthly CSVs ({len(months)} tried; last {last_err})"
        )
    if errors:
        logger.warning("AEMO %s: %s month errors (last %s)", region, errors, last_err)
    by_date = {p["date"]: p for p in out}
    return [by_date[k] for k in sorted(by_date)]


def _mmcf_month_to_bcfd(points: list[dict]) -> list[dict]:
    out = []
    for p in points:
        d = _norm_date(p.get("date"))
        v = p.get("value")
        if not d or v is None:
            continue
        try:
            dt = datetime.fromisoformat(d).date()
            days = calendar.monthrange(dt.year, dt.month)[1]
        except ValueError:
            continue
        if days <= 0:
            continue
        out.append({"date": d, "value": round(float(v) / days / 1000.0, 4)})
    return out


def _drop_future_points(points: list[dict]) -> list[dict]:
    today = date.today().isoformat()
    return [p for p in points if p.get("date") and p["date"] <= today]


def _apply_store_transform(
    series_id: str, meta: dict, points: list[dict]
) -> list[dict]:
    kind = (meta.get("transform") or "").strip().lower()
    if kind == "mmcf_month_to_bcfd":
        return _mmcf_month_to_bcfd(points)
    if kind == "drop_future":
        return _drop_future_points(points)
    return points


def fetch_bakerhughes_aogr(
    native_id: str,
    *,
    start: str | None = None,
    session: requests.Session | None = None,
) -> list[dict]:
    """Weekly US oil/gas rotary rigs from AOGR's Baker Hughes table.

    EIA petroleum/crd/drill monthly series stopped updating after 2026-02.
    AOGR republishes the weekly Baker Hughes count (oil / gas / misc).
    native_id: oil | gas
    """
    kind = (native_id or "oil").strip().lower()
    if kind not in ("oil", "gas"):
        raise RuntimeError(f"bakerhughes kind must be oil|gas, got {native_id!r}")
    sess = session or _session()
    sess.headers.update(_download_headers())
    r = sess.get(AOGR_RIG_URL, timeout=60)
    if not r.ok:
        raise _http_error(r)
    html = r.text or ""
    marker = "Data made available by Baker Hughes"
    if marker in html:
        html = html.split(marker, 1)[1]
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    # "Oil (Wk./Wk.) -5 (447)" — the count in parentheses is the level.
    pat = re.compile(
        rf"{re.escape(kind.capitalize())}\s*\(Wk\./Wk\.\)[^(]{{0,48}}\((\d+)\)",
        re.I,
    )
    points: list[dict] = []
    total_parsed = 0
    parts = re.split(r"(\d{2}/\d{2}/\d{4})", html)
    for i in range(1, len(parts) - 1, 2):
        raw_d = parts[i]
        body = parts[i + 1]
        m = pat.search(body)
        if not m:
            continue
        try:
            dt = datetime.strptime(raw_d, "%m/%d/%Y").date()
        except ValueError:
            continue
        total_parsed += 1
        d = dt.isoformat()
        if start and d < start:
            continue
        points.append({"date": d, "value": float(m.group(1))})
    if total_parsed < 8:
        raise RuntimeError(
            f"AOGR Baker Hughes {kind}: only {total_parsed} weekly points parsed"
        )
    by_date = {p["date"]: p for p in points}
    return [by_date[k] for k in sorted(by_date)]


def _fetch_provider(
    provider: str,
    native_id: str,
    *,
    start: str | None,
    session: requests.Session,
    extra: dict | None = None,
    full: bool = False,
) -> list[dict]:
    extra = extra or {}
    if provider == "fred":
        return fetch_fred(native_id, start=start, session=session)
    if provider == "eia":
        return fetch_eia(
            native_id,
            start=start,
            session=session,
            route=extra.get("route"),
        )
    if provider == "worldbank":
        return fetch_worldbank(native_id, start=start, session=session)
    if provider == "aemo":
        return fetch_aemo(native_id, start=start, full=full, session=session)
    if provider == "yfinance":
        return fetch_yfinance(native_id, start=start)
    if provider == "bakerhughes":
        return fetch_bakerhughes_aogr(native_id, start=start, session=session)
    raise RuntimeError(f"unsupported provider {provider}")


def _overlap_start(
    last_date: str | None, full: bool, *, freq: str | None = None
) -> str | None:
    if full or not last_date:
        return None
    days = 100 if (freq or "").lower() == "monthly" else 21
    try:
        d = datetime.fromisoformat(last_date).date() - timedelta(days=days)
        return d.isoformat()
    except ValueError:
        return last_date


def collect_series(
    series_id: str,
    *,
    full: bool = False,
    db_path: str = DEFAULT_DB,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    cfg = load_supporting_config()
    meta = (cfg.get("series") or {}).get(series_id)
    if not meta:
        return {"series_id": series_id, "ok": False, "error": "not in catalog"}
    if _src(meta).get("provider") == "derived":
        return compute_derived(series_id, db_path=db_path)

    attempts: list[dict] = []
    src = _src(meta)
    fb = _src(meta, "fallback")
    if src.get("provider") in FETCHABLE_PROVIDERS and not _is_tbd(src):
        attempts.append(src)
    if (
        fb.get("provider") in FETCHABLE_PROVIDERS
        and not _is_tbd(fb)
        and _native_id(fb) != _native_id(src)
    ):
        attempts.append(fb)
    if not attempts:
        err = "no fetchable source"
        _record_error(series_id, err, db_path)
        return {"series_id": series_id, "ok": False, "error": err}

    start = _overlap_start(
        last_stored_date(series_id, db_path),
        full,
        freq=str(meta.get("freq") or ""),
    )
    sess = session or _session()
    errors: list[str] = []
    last_ok: dict[str, Any] | None = None
    for src in attempts:
        provider = src["provider"]
        native = _native_id(src)
        if provider in ("eia", "fred") and not _api_key(provider):
            errors.append(f"{provider}: missing API key")
            continue
        if provider == "yfinance" and not _native_id(src):
            errors.append("yfinance: empty symbol")
            continue
        try:
            points = _fetch_provider(
                provider,
                native,
                start=start,
                session=sess,
                extra=src,
                full=full,
            )
            points = _apply_store_transform(series_id, meta, points)
            more_sources = attempts.index(src) < len(attempts) - 1
            if not points and more_sources:
                errors.append(f"{provider}/{native}: returned 0 points")
                time.sleep(REQUEST_SLEEP)
                continue
            stored = (
                upsert_points(
                    series_id,
                    points,
                    source=provider,
                    source_id=native,
                    db_path=db_path,
                )
                if points
                else 0
            )
            time.sleep(REQUEST_SLEEP)
            latest = max((str(p.get("date") or "") for p in points), default="")
            # EIA monthly drill (Baker Hughes) stopped at 2026-02; try next source.
            freq = str(meta.get("freq") or "").lower()
            stale_after = 150 if freq == "monthly" else 21
            age_days = None
            if latest:
                try:
                    age_days = (date.today() - datetime.fromisoformat(latest).date()).days
                except ValueError:
                    age_days = None
            result = {
                "series_id": series_id,
                "ok": True,
                "provider": provider,
                "source_id": native,
                "stored": stored,
                "start": start,
                "n": len(points),
            }
            if age_days is not None and age_days > stale_after and more_sources:
                errors.append(
                    f"{provider}/{native}: latest {latest} is {age_days}d old"
                )
                last_ok = result
                continue
            return result
        except Exception as e:
            logger.warning("fetch %s via %s %s: %s", series_id, provider, native, e)
            errors.append(f"{provider}/{native}: {e}")
            time.sleep(REQUEST_SLEEP)

    if last_ok:
        return last_ok
    err = "; ".join(errors) or "fetch failed"
    _record_error(series_id, err, db_path)
    return {"series_id": series_id, "ok": False, "error": err}


def compute_derived(series_id: str, db_path: str = DEFAULT_DB) -> dict[str, Any]:
    cfg = load_supporting_config()
    meta = (cfg.get("series") or {}).get(series_id) or {}
    if series_id != "crack_321":
        err = f"no derived recipe for {series_id}"
        _record_error(series_id, err, db_path)
        return {"series_id": series_id, "ok": False, "error": err}

    wti = {p["date"]: p["value"] for p in get_series("crude_oil_wti", db_path=db_path)}
    rbob = {p["date"]: p["value"] for p in get_series("rbob_gasoline", db_path=db_path)}
    ulsd = {p["date"]: p["value"] for p in get_series("ulsd_heating_oil", db_path=db_path)}
    dates = sorted(set(wti) & set(rbob) & set(ulsd))
    points = []
    for d in dates:
        # 2*RBOB + 1*ULSD − 3*WTI; products are USD/gal
        val = 2.0 * rbob[d] * 42.0 + ulsd[d] * 42.0 - 3.0 * wti[d]
        points.append({"date": d, "value": round(val, 4)})
    if not points:
        err = "crack_321 needs WTI + RBOB + ULSD on overlapping dates"
        _record_error(series_id, err, db_path)
        return {"series_id": series_id, "ok": False, "error": err}
    stored = upsert_points(
        series_id, points, source="derived", source_id="crack_321", db_path=db_path
    )
    return {
        "series_id": series_id,
        "ok": True,
        "provider": "derived",
        "source_id": "crack_321",
        "stored": stored,
        "n": len(points),
    }


def _collect_ids(
    wanted: list[str],
    *,
    full: bool,
    db_path: str,
    allow_unphased: bool = False,
) -> dict[str, Any]:
    fetchable = []
    derived = []
    catalog = load_supporting_config().get("series") or {}
    for sid in wanted:
        meta = catalog.get(sid)
        if not meta:
            derived.append({"series_id": sid, "ok": False, "error": "not in catalog"})
            continue
        phased = is_phase1_series(meta) or is_phase3_series(meta)
        if not phased and not allow_unphased:
            derived.append(
                {
                    "series_id": sid,
                    "ok": False,
                    "error": "not a Phase-1/Phase-3 official series",
                }
            )
            continue
        if _src(meta).get("provider") == "derived":
            derived.append(sid)
        else:
            fetchable.append(sid)

    sess = _session()
    results = []
    for sid in fetchable:
        results.append(collect_series(sid, full=full, db_path=db_path, session=sess))
    for item in derived:
        if isinstance(item, dict):
            results.append(item)
        else:
            results.append(compute_derived(item, db_path=db_path))

    ok = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    return {
        "ok": not bad,
        "fetched": len(ok),
        "failed": len(bad),
        "stored": sum(int(r.get("stored") or 0) for r in ok),
        "results": results,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def collect_phase1(
    *,
    series_ids: list[str] | None = None,
    full: bool = False,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    wanted = series_ids or phase1_series_ids(include_derived=True)
    if series_ids:
        catalog = load_supporting_config().get("series") or {}
        extra = []
        keep = []
        for sid in wanted:
            meta = catalog.get(sid) or {}
            if is_phase1_series(meta):
                keep.append(sid)
            else:
                extra.append(
                    {
                        "series_id": sid,
                        "ok": False,
                        "error": "not phase-1 daily/weekly",
                    }
                )
        result = _collect_ids(keep, full=full, db_path=db_path)
        if extra:
            result["results"] = extra + result["results"]
            result["failed"] = int(result.get("failed") or 0) + len(extra)
            result["ok"] = False
        return result
    return _collect_ids(wanted, full=full, db_path=db_path)


def collect_phase3(
    *,
    series_ids: list[str] | None = None,
    full: bool = False,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    wanted = series_ids or phase3_series_ids()
    if series_ids:
        catalog = load_supporting_config().get("series") or {}
        wanted = [sid for sid in wanted if is_phase3_series(catalog.get(sid) or {})]
    return _collect_ids(wanted, full=full, db_path=db_path)


def collect_official(
    *,
    series_ids: list[str] | None = None,
    full: bool = False,
    phases: set[int] | None = None,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    if series_ids:
        return _collect_ids(
            series_ids, full=full, db_path=db_path, allow_unphased=True
        )
    wanted: list[str] = []
    use = phases or {1, 3}
    if 1 in use:
        wanted.extend(phase1_series_ids(include_derived=True))
    if 3 in use:
        wanted.extend(phase3_series_ids())
    # preserve order, drop dupes
    seen: set[str] = set()
    ordered = []
    for sid in wanted:
        if sid not in seen:
            seen.add(sid)
            ordered.append(sid)
    return _collect_ids(ordered, full=full, db_path=db_path)


def get_series(series_id: str, db_path: str = DEFAULT_DB) -> list[dict]:
    conn = _conn(db_path)
    rows = conn.execute(
        """
        SELECT date, value, source, source_id
        FROM supporting_series
        WHERE series_id=?
        ORDER BY date
        """,
        (series_id,),
    ).fetchall()
    conn.close()
    return [
        {"date": r[0], "value": r[1], "last": r[1], "source": r[2], "source_id": r[3]}
        for r in rows
    ]


def collection_status(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    cfg = load_supporting_config()
    conn = _conn(db_path)
    fetch_rows = {
        r[0]: {
            "series_id": r[0],
            "provider": r[1],
            "source_id": r[2],
            "n_points": r[3],
            "first_date": r[4],
            "last_date": r[5],
            "last_error": r[6],
            "fetched_at": r[7],
        }
        for r in conn.execute(
            """
            SELECT series_id, provider, source_id, n_points, first_date, last_date,
                   last_error, fetched_at
            FROM supporting_series_fetch
            """
        )
    }
    counts = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT series_id, COUNT(*) FROM supporting_series GROUP BY series_id"
        )
    }
    conn.close()

    series_out = []
    for sid in official_series_ids():
        meta = (cfg.get("series") or {}).get(sid) or {}
        rec = fetch_rows.get(sid) or {}
        n = counts.get(sid) or rec.get("n_points") or 0
        series_out.append(
            {
                "id": sid,
                "display": meta.get("display") or sid,
                "unit": meta.get("unit") or "",
                "freq": meta.get("freq") or "",
                "phase": series_phase(meta),
                "n": n,
                "first_date": rec.get("first_date"),
                "last_date": rec.get("last_date"),
                "provider": rec.get("provider"),
                "source_id": rec.get("source_id"),
                "last_error": rec.get("last_error"),
                "fetched_at": rec.get("fetched_at"),
            }
        )
    p1 = sum(1 for s in series_out if s.get("phase") == 1)
    p3 = sum(1 for s in series_out if s.get("phase") == 3)
    return {
        "db": db_path,
        "phase1_n": p1,
        "phase3_n": p3,
        "series": series_out,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Company join (energy_pilot / watchlist — same math as Commodities tab)
# ---------------------------------------------------------------------------

RATIO_Z_LOOKBACK = 90
CHART_POINTS = 180
CRACK_CLUSTERS = frozenset({"majors", "japan_refiner", "korea_refiner"})
_points_cache: dict[str, list[dict]] = {}
_stock_hist_cache: dict[str, list[dict]] = {}
_exposure_index: dict[str, dict] | None = None


def _points(series_id: str, db_path: str = DEFAULT_DB) -> list[dict]:
    if series_id not in _points_cache:
        pts = get_series(series_id, db_path=db_path)
        if not pts:
            try:
                from data.commodity_prices import get_series as get_barchart

                pts = get_barchart(series_id) or []
            except Exception:
                pts = []
        _points_cache[series_id] = pts
    return _points_cache[series_id]


def clear_join_caches() -> None:
    _points_cache.clear()
    _stock_hist_cache.clear()
    global _exposure_index
    _exposure_index = None


def _sid(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or "").strip()
    return str(item or "").strip()


def _role(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("role") or "")
    return ""


def get_exposure(ticker: str) -> dict | None:
    """Resolved cluster + extras for a ticker, or None."""
    global _exposure_index
    t = (ticker or "").strip().upper()
    if not t:
        return None
    if _exposure_index is None:
        cfg = load_supporting_config()
        clusters = cfg.get("clusters") or {}
        idx: dict[str, dict] = {}
        for e in cfg.get("exposures") or []:
            et = (e.get("ticker") or "").strip().upper()
            if not et:
                continue
            cluster = clusters.get(e.get("cluster") or "") or {}
            series = list(cluster.get("series") or [])
            seen = {_sid(s) for s in series}
            for extra in e.get("extra_series") or []:
                sid = _sid(extra)
                if sid and sid not in seen:
                    series.append(extra)
                    seen.add(sid)
            idx[et] = {
                "ticker": et,
                "name": e.get("name") or et,
                "cluster": e.get("cluster") or "",
                "cluster_display": cluster.get("display") or e.get("cluster") or "",
                "primary": e.get("primary") or cluster.get("primary"),
                "series": series,
                "universe": e.get("universe") or [],
                "notes": e.get("notes") or cluster.get("notes") or "",
            }
        _exposure_index = idx
    return _exposure_index.get(t)


def _series_snap(series_id: str, db_path: str = DEFAULT_DB) -> dict:
    from data.commodity_prices import series_stats

    cfg = load_supporting_config()
    meta = (cfg.get("series") or {}).get(series_id) or {}
    pts = _points(series_id, db_path)
    st = series_stats(pts)
    rets = st.get("returns") or {}
    return {
        "id": series_id,
        "display": meta.get("display") or series_id,
        "unit": meta.get("unit") or "",
        "freq": meta.get("freq") or "",
        "last": st.get("last"),
        "last_date": st.get("last_date"),
        "n": st.get("n") or 0,
        "ret_1m": rets.get("1m"),
        "ret_3m": rets.get("3m"),
        "ret_6m": rets.get("6m"),
        "drawdown_from_peak_pct": st.get("drawdown_from_peak_pct"),
    }


def _series_ids(exp: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    primary = exp.get("primary")
    if primary:
        out.append(primary)
        seen.add(primary)
    for item in exp.get("series") or []:
        sid = item if isinstance(item, str) else _sid(item)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _pick_primary(exp: dict, db_path: str = DEFAULT_DB) -> str | None:
    """Catalog primary, else first mapped series that actually has points."""
    ordered = _series_ids(exp)
    primary = exp.get("primary")
    if primary and _points(primary, db_path):
        return primary
    for sid in ordered:
        if _points(sid, db_path):
            return sid
    return primary


def _stock_history(ticker: str, days: int = 400) -> list[dict]:
    sym = (ticker or "").strip().upper()
    if sym in _stock_hist_cache:
        return _stock_hist_cache[sym]
    out: list[dict] = []
    try:
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=days)
        hist = yf.Ticker(sym).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if hist is not None and not hist.empty:
            for dt, row in hist.iterrows():
                c = row.get("Close")
                if c is not None and c == c:
                    out.append({"date": dt.strftime("%Y-%m-%d"), "close": float(c)})
    except Exception as e:
        logger.debug("stock history %s: %s", sym, e)
    _stock_hist_cache[sym] = out
    return out


def _align_ratio(stock_hist: list[dict], cmdty: list[dict]) -> list[dict]:
    c_map = {p["date"]: p["last"] for p in cmdty if p.get("last")}
    out = []
    for p in stock_hist:
        d = p["date"]
        if d in c_map and p.get("close") and c_map[d]:
            out.append(
                {
                    "date": d,
                    "stock": p["close"],
                    "commodity": c_map[d],
                    "ratio": p["close"] / c_map[d],
                }
            )
    return out


def _zscore(values: list[float], latest: float) -> float | None:
    if len(values) < 5:
        return None
    try:
        import statistics

        mu = statistics.mean(values)
        sd = statistics.pstdev(values)
        if not sd or sd < 1e-12:
            return 0.0
        return round((latest - mu) / sd, 3)
    except Exception:
        return None


def _ret_hist(hist: list[dict], key: str, days: int) -> float | None:
    pts = [{"date": h["date"], "v": h.get(key)} for h in hist]
    if len(pts) < 2 or pts[-1].get("v") is None:
        return None
    end = pts[-1]
    try:
        end_d = datetime.fromisoformat(end["date"])
        start_target = (end_d - timedelta(days=int(days * 1.5))).date().isoformat()
    except Exception:
        start_target = pts[max(0, len(pts) - days)]["date"]
    base = next((p for p in pts if p["date"] >= start_target and p.get("v")), None)
    if not base or not base.get("v"):
        return None
    return round((end["v"] / base["v"] - 1) * 100, 2)


def _want_crack(exp: dict) -> bool:
    if (exp.get("cluster") or "") in CRACK_CLUSTERS:
        return True
    return any(_sid(s) == "crack_321" for s in (exp.get("series") or []))


def build_exposure_row(
    ticker: str,
    *,
    include_stock: bool = False,
    include_chart: bool = False,
    db_path: str = DEFAULT_DB,
) -> dict | None:
    """Join a ticker to its primary strip (+ crack / context). None if unmapped."""
    exp = get_exposure(ticker)
    if not exp:
        return None
    primary = _pick_primary(exp, db_path)
    snap = _series_snap(primary, db_path) if primary else {}
    roles = {_sid(s): _role(s) for s in (exp.get("series") or [])}
    if exp.get("primary"):
        roles.setdefault(exp["primary"], "revenue")

    crack = None
    if _want_crack(exp) and _points("crack_321", db_path):
        crack = _series_snap("crack_321", db_path)

    context = []
    seen = {primary, "crack_321" if crack else ""}
    for sid in _series_ids(exp):
        if sid in seen:
            continue
        pts = _points(sid, db_path)
        if not pts:
            continue
        ctx = _series_snap(sid, db_path)
        ctx["role"] = roles.get(sid) or ""
        context.append(ctx)
        seen.add(sid)
        if len(context) >= 4:
            break

    row: dict[str, Any] = {
        "ticker": exp["ticker"],
        "name": exp.get("name"),
        "cluster": exp.get("cluster"),
        "cluster_display": exp.get("cluster_display"),
        "primary": primary,
        "display": snap.get("display") or primary,
        "unit": snap.get("unit") or "",
        "role": roles.get(primary or "") or "",
        "last": snap.get("last"),
        "last_date": snap.get("last_date"),
        "n": snap.get("n") or 0,
        "ret_1m": snap.get("ret_1m"),
        "ret_3m": snap.get("ret_3m"),
        "ret_6m": snap.get("ret_6m"),
        "drawdown_from_peak_pct": snap.get("drawdown_from_peak_pct"),
        "crack": crack,
        "context": context,
        "notes": exp.get("notes") or "",
        "fallback_primary": bool(primary and primary != exp.get("primary")),
        "catalog_primary": exp.get("primary"),
    }

    if not include_stock and not include_chart:
        return row

    stock_hist = _stock_history(exp["ticker"])
    cmdty = _points(primary, db_path) if primary else []
    aligned = _align_ratio(stock_hist, cmdty)
    ratios = [a["ratio"] for a in aligned]
    recent = ratios[-RATIO_Z_LOOKBACK:] if len(ratios) > RATIO_Z_LOOKBACK else ratios
    ratio_now = ratios[-1] if ratios else None
    ratio_z = _zscore(recent, ratio_now) if ratio_now is not None else None
    stock_ret_3m = _ret_hist(stock_hist, "close", 63)
    stock_ret_6m = _ret_hist(stock_hist, "close", 126)
    relative_3m = None
    if stock_ret_3m is not None and row.get("ret_3m") is not None:
        relative_3m = round(stock_ret_3m - row["ret_3m"], 2)
    row.update(
        {
            "stock_ret_3m": stock_ret_3m,
            "stock_ret_6m": stock_ret_6m,
            "relative_3m": relative_3m,
            "ratio": round(ratio_now, 6) if ratio_now is not None else None,
            "ratio_z": ratio_z,
            "ratio_n": len(aligned),
            "depressed_vs_driver": bool(relative_3m is not None and relative_3m <= -10),
        }
    )

    if include_chart and aligned:
        crack_map = {p["date"]: p["last"] for p in _points("crack_321", db_path)} if crack else {}
        window = aligned[-CHART_POINTS:]
        s0 = window[0]["stock"]
        c0 = window[0]["commodity"]
        chart = []
        for p in window:
            chart.append(
                {
                    "date": p["date"],
                    "stock": p["stock"],
                    "stock_idx": round(p["stock"] / s0 * 100, 2) if s0 else None,
                    "commodity": p["commodity"],
                    "commodity_idx": round(p["commodity"] / c0 * 100, 2)
                    if c0 and p["commodity"]
                    else None,
                    "ratio": p["ratio"],
                    "crack": crack_map.get(p["date"]),
                }
            )
        row["chart"] = chart
        row["chart_meta"] = {
            "unit": row.get("unit") or "",
            "has_crack": bool(crack),
            "n": len(chart),
        }
    elif include_chart:
        row["chart"] = []
        row["chart_meta"] = {"unit": row.get("unit") or "", "has_crack": bool(crack), "n": 0}
    return row


def annotate_stocks_with_drivers(
    stocks: list[dict],
    *,
    db_path: str = DEFAULT_DB,
) -> list[dict]:
    """Attach a strip-only `driver` dict (no extra yfinance) when mapped."""
    for s in stocks or []:
        try:
            row = build_exposure_row(
                s.get("ticker") or "",
                include_stock=False,
                include_chart=False,
                db_path=db_path,
            )
        except Exception as e:
            logger.debug("driver annotate %s: %s", s.get("ticker"), e)
            row = None
        if row:
            s["driver"] = row
    return stocks or []


def attach_driver_detail(stock: dict, db_path: str = DEFAULT_DB) -> dict:
    """Full stock-vs-strip join + chart for a detail payload."""
    if not stock:
        return stock
    try:
        row = build_exposure_row(
            stock.get("ticker") or "",
            include_stock=True,
            include_chart=True,
            db_path=db_path,
        )
    except Exception as e:
        logger.warning("driver detail %s: %s", stock.get("ticker"), e)
        return stock
    if row:
        try:
            from data.operating_extract import latest_operating

            latest = latest_operating(stock.get("ticker") or "")
            if latest:
                strip = row.get("last")
                unit = (row.get("unit") or "").lower()
                real = latest.get("realized_price")
                vs = None
                if real and strip is not None and real.get("value") is not None:
                    ru = (real.get("unit") or "").lower()
                    if any(x in ru and x in unit for x in ("bbl", "boe", "mmbtu", "/t", "ton")):
                        vs = round(float(real["value"]) - float(strip), 4)
                    elif "bbl" in ru and "bbl" in unit:
                        vs = round(float(real["value"]) - float(strip), 4)
                row["operating"] = {
                    k: {
                        "value": v.get("value"),
                        "unit": v.get("unit"),
                        "period_end": v.get("period_end"),
                        "snippet": (v.get("snippet") or "")[:180],
                        "source": v.get("source"),
                        "commodity": v.get("commodity"),
                    }
                    for k, v in latest.items()
                }
                row["operating"]["realized_vs_strip"] = vs
        except Exception as e:
            logger.debug("operating attach %s: %s", stock.get("ticker"), e)
        stock["driver"] = row
    return stock


def list_id_is_energy_pilot(list_id: str | None) -> bool:
    return "energy_pilot" in (list_id or "").lower()
