"""
HKEX Disclosure of Interests — director dealings (insider purchases/sales).
Daily HTML summaries: https://di.hkex.com.hk/di/summary/DSM{YYYYMMDD}C2.htm
"""

import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
HK_INSIDER_DIR = _PROJECT_ROOT / "hk_stock_repurchases" / "hkex_insider_di"
DEFAULT_CSV_PATH = _PROJECT_ROOT / "hk_stock_repurchases" / "hk_insider_dealings.csv"
DI_SUMMARY_URL = "https://di.hkex.com.hk/di/summary/DSM{yyyymmdd}C2.htm"
# Earliest daily summary available via DSM{date}C2.htm (older dates return "temporarily unavailable")
DEFAULT_START_DATE = date(2026, 4, 27)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}

# HKEX DI reason codes for open-market / exchange purchases (Part XV)
PURCHASE_REASON_CODES = frozenset({
    "1101",  # acquisition on exchange
    "1201",  # purchase on exchange
    "1213",  # purchase through another exchange
})

SALE_REASON_PREFIXES = ("1102", "1202", "1112")

DESIRED_COLUMNS = [
    "Event Date",
    "Company",
    "Stock code",
    "Director",
    "Reason code",
    "Shares",
    "Average price",
    "Currency",
    "Serial No",
    "Source_File",
    "Scrape_Timestamp",
]


def _summary_path_for_date(d: date) -> Path:
    HK_INSIDER_DIR.mkdir(parents=True, exist_ok=True)
    return HK_INSIDER_DIR / f"DSM{d.strftime('%Y%m%d')}C2.htm"


def di_summary_url(d: date) -> str:
    return DI_SUMMARY_URL.format(yyyymmdd=d.strftime("%Y%m%d"))


def iter_weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def list_cached_summary_dates() -> set:
    if not HK_INSIDER_DIR.exists():
        return set()
    out = set()
    for p in HK_INSIDER_DIR.glob("DSM*C2.htm"):
        m = re.search(r"DSM(\d{8})C2", p.name)
        if m:
            try:
                out.add(datetime.strptime(m.group(1), "%Y%m%d").date())
            except ValueError:
                pass
    return out


def _parse_shares(val: str) -> int:
    if not val:
        return 0
    s = re.sub(r"\([LS]\)", "", str(val), flags=re.I)
    s = s.replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


def _parse_price(val: str) -> tuple:
    """Return (currency, price) from e.g. 'HKD 0.1620'."""
    if not val:
        return "", 0.0
    s = str(val).strip()
    m = re.match(r"([A-Z]{3})\s*([\d.]+)", s)
    if m:
        return m.group(1), float(m.group(2))
    try:
        return "", float(s.replace(",", ""))
    except ValueError:
        return "", 0.0


def _reason_code(raw: str) -> str:
    m = re.match(r"(\d+)", str(raw or "").strip())
    return m.group(1) if m else ""


def _is_purchase_row(reason: str, shares: int, company: str) -> bool:
    if not shares or shares <= 0:
        return False
    comp = (company or "").lower()
    if "withdrawn" in comp or "amendment to" in comp:
        return False
    code = _reason_code(reason)
    if not code:
        return False
    if code in PURCHASE_REASON_CODES:
        return True
    if code.startswith(SALE_REASON_PREFIXES):
        return False
    return False


def parse_di_summary_html(html: str, source_name: str = "") -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    main = None
    for table in soup.find_all("table"):
        if table.find(string=re.compile(r"Serial\s+No")):
            main = table
            break
    if not main:
        return pd.DataFrame()

    rows = []
    for tr in main.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 9:
            continue
        serial = cells[0]
        if not serial.startswith("DA"):
            continue

        company = cells[1]
        code = cells[2].strip()
        director = cells[4]
        event_date_raw = cells[5]
        reason = cells[6]
        shares_raw = cells[7]
        price_raw = cells[8] if len(cells) > 8 else ""

        shares = _parse_shares(shares_raw)
        if not _is_purchase_row(reason, shares, company):
            continue

        currency, price = _parse_price(price_raw)
        try:
            event_dt = datetime.strptime(event_date_raw, "%d/%m/%Y")
            event_date = event_dt.strftime("%Y-%m-%d")
        except ValueError:
            event_date = ""

        rows.append({
            "Event Date": event_date,
            "Company": company,
            "Stock code": code,
            "Director": director,
            "Reason code": _reason_code(reason),
            "Shares": shares,
            "Average price": price,
            "Currency": currency,
            "Serial No": serial,
            "Source_File": source_name,
            "Scrape_Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    return pd.DataFrame(rows)


def fetch_di_summary(d: date, dest: Path = None, session: requests.Session = None) -> Path | None:
    dest = Path(dest or _summary_path_for_date(d))
    if dest.exists() and dest.stat().st_size > 500:
        return dest

    sess = session or requests.Session()
    url = di_summary_url(d)
    try:
        r = sess.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 404 or len(r.content) < 500:
            return None
        r.raise_for_status()
        if "Serial No" not in r.text and "serial no" not in r.text.lower():
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except requests.RequestException as e:
        logger.warning("HK DI fetch failed %s: %s", d.isoformat(), e)
        return None


def download_missing_summaries(
    start: date = None,
    end: date = None,
    sleep_seconds: float = 0.35,
) -> dict:
    start = start or DEFAULT_START_DATE
    end = end or date.today()
    cached = list_cached_summary_dates()
    sess = requests.Session()

    checked = downloaded = skipped = not_found = 0
    for d in iter_weekdays(start, end):
        if d in cached:
            skipped += 1
            continue
        checked += 1
        path = fetch_di_summary(d, session=sess)
        if path:
            downloaded += 1
            cached.add(d)
            logger.info("HK DI downloaded %s", d.isoformat())
        else:
            not_found += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "checked": checked,
        "downloaded": downloaded,
        "skipped_local": skipped,
        "not_found": not_found,
        "local_total": len(cached),
    }


def build_master_csv_from_cache(csv_path: Path = None) -> dict:
    csv_path = Path(csv_path or DEFAULT_CSV_PATH)
    files = sorted(HK_INSIDER_DIR.glob("DSM*C2.htm")) if HK_INSIDER_DIR.exists() else []
    if not files:
        return {"ok": False, "error": "No HK DI summary files found", "rows": 0}

    frames = []
    for path in files:
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
            chunk = parse_di_summary_html(html, source_name=path.name)
            if not chunk.empty:
                frames.append(chunk)
        except Exception as e:
            logger.warning("HK DI parse failed %s: %s", path.name, e)

    if not frames:
        return {"ok": False, "error": "No purchase rows parsed from HK DI files", "rows": 0}

    combined = pd.concat(frames, ignore_index=True)
    combined["Stock code"] = combined["Stock code"].astype(str).str.strip()
    combined = combined.sort_values(["Event Date", "Stock code", "Serial No"], ascending=[False, True, True])
    before = len(combined)
    combined = combined.drop_duplicates(subset=["Serial No"], keep="first")
    combined = combined[[c for c in DESIRED_COLUMNS if c in combined.columns]]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(csv_path, index=False, encoding="utf-8")

    return {
        "ok": True,
        "rows": len(combined),
        "files_parsed": len(frames),
        "files_total": len(files),
        "deduped": before - len(combined),
        "csv_path": str(csv_path),
    }


def refresh_hk_insider_csv(
    start: date = None,
    end: date = None,
    download_gaps: bool = True,
    csv_path: Path = None,
) -> dict:
    dl_stats = {}
    if download_gaps:
        dl_stats = download_missing_summaries(start=start, end=end)

    build_stats = build_master_csv_from_cache(csv_path=csv_path)
    if not build_stats.get("ok"):
        return {**build_stats, "download": dl_stats, "ok": False}

    return {
        "ok": True,
        "download": dl_stats,
        "build": build_stats,
        "rows": build_stats.get("rows", 0),
        "csv_path": build_stats.get("csv_path"),
    }