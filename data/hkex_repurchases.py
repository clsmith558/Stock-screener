"""
HKEX share repurchase reports — daily XLS from HKEX news.
https://www3.hkexnews.hk/reports/sharerepur/sbn.asp

Reports use predictable URLs (no calendar UI interaction required):
  .../reports/sharerepur/documents/SRRPT{YYYYMMDD}.xls
"""

import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
HK_DATA_DIR = _PROJECT_ROOT / "hk_stock_repurchases"
DEFAULT_CSV_PATH = HK_DATA_DIR / "hk_stock_repurchases.csv"
HKEX_REPORT_URL = (
    "https://www3.hkexnews.hk/reports/sharerepur/documents/SRRPT{yyyymmdd}.xls"
)
DEFAULT_START_DATE = date(2025, 1, 1)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}

SHARES_COL_ALIASES = [
    "Number of shares/units repurchased",
    "Number of securities purchased",
]
HIGH_PRICE_COL_ALIASES = [
    "Repurchase price or highest repurchase price per share/unit ($)",
    "Price per share  or highest price paid($)",
    "Price per share or highest price paid($)",
]


def _pick_column(columns, aliases: list):
    norm = {str(c).replace("\n", " ").strip(): c for c in columns}
    for alias in aliases:
        key = alias.replace("\n", " ").strip()
        if key in norm:
            return norm[key]
    for col in columns:
        flat = str(col).replace("\n", " ").strip().lower()
        for alias in aliases:
            if alias.replace("\n", " ").strip().lower() in flat:
                return col
    return None


DESIRED_COLUMNS = [
    "Trading Date",
    "Company",
    "Stock code",
    "Highest Repurchase Price",
    "No. of Shares to be Purchased",
    "sharesOutstanding",
    "trailingPE",
    "forwardPE",
    "trailingPegRatio",
    "heldPercentInsiders",
    "priceToBook",
    "totalCash",
    "marketCap",
    "totalDebt",
    "debtToEquity",
    "financialCurrency",
    "Share purchase pc share count",
    "Source_File",
    "Scrape_Timestamp",
]


def _month_dir(d: date) -> Path:
    path = HK_DATA_DIR / f"hkex_share_repurchase_{d.year}_{d.month:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _xls_path_for_date(d: date) -> Path:
    return _month_dir(d) / f"SRRPT{d.strftime('%Y%m%d')}.xls"


def _date_from_xls_path(path: Path) -> date | None:
    m = re.search(r"(\d{8})", path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def list_local_xls_files(data_dir: Path = None) -> list:
    root = Path(data_dir or HK_DATA_DIR)
    if not root.exists():
        return []
    files = []
    for pattern in ("**/*.xls", "**/*.XLS"):
        files.extend(root.glob(pattern))
    return sorted({p.resolve() for p in files})


def list_local_report_dates(data_dir: Path = None) -> set:
    return {
        d for p in list_local_xls_files(data_dir)
        for d in [_date_from_xls_path(p)] if d
    }


def iter_weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def hkex_report_url(d: date) -> str:
    return HKEX_REPORT_URL.format(yyyymmdd=d.strftime("%Y%m%d"))


def report_exists_on_hkex(d: date, session: requests.Session = None) -> bool:
    sess = session or requests.Session()
    try:
        r = sess.head(hkex_report_url(d), headers=HEADERS, timeout=20, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def download_report(d: date, dest: Path = None, session: requests.Session = None) -> Path | None:
    """Download one daily HKEX repurchase XLS; returns path or None if unavailable."""
    dest = Path(dest or _xls_path_for_date(d))
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    sess = session or requests.Session()
    url = hkex_report_url(d)
    try:
        r = sess.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except requests.RequestException as e:
        logger.warning("HKEX download failed for %s: %s", d.isoformat(), e)
        return None


def download_missing_reports(
    start: date = None,
    end: date = None,
    sleep_seconds: float = 0.35,
) -> dict:
    """
    Probe HKEX for each weekday in [start, end] and download reports not already stored locally.
    A 404 means no report was published that day (holiday or no repurchases).
    """
    start = start or DEFAULT_START_DATE
    end = end or date.today()
    local_dates = list_local_report_dates()
    sess = requests.Session()

    checked = 0
    downloaded = 0
    skipped_local = 0
    not_found = 0
    errors = 0

    for d in iter_weekdays(start, end):
        if d in local_dates:
            skipped_local += 1
            continue
        checked += 1
        path = download_report(d, session=sess)
        if path:
            downloaded += 1
            local_dates.add(d)
            logger.info("HKEX downloaded %s -> %s", d.isoformat(), path.name)
        else:
            not_found += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "checked": checked,
        "downloaded": downloaded,
        "skipped_local": skipped_local,
        "not_found": not_found,
        "errors": errors,
        "local_total": len(local_dates),
    }


def _extract_currency(value):
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return None, None
    s = str(value).strip()
    match = re.match(r"([A-Z]{3})\s*([\d\.]+)", s)
    if match:
        currency, number = match.groups()
        return currency, float(number)
    try:
        return None, float(s.replace(",", ""))
    except ValueError:
        return None, None


def parse_hkex_xls(xls_path: Path, source_date: date = None) -> pd.DataFrame:
    """Parse one HKEX daily XLS into normalized repurchase rows."""
    path = Path(xls_path)
    source_date = source_date or _date_from_xls_path(path)
    scrape_ts = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    try:
        raw = pd.read_excel(path)
    except Exception as e:
        logger.warning("Cannot read HKEX XLS %s: %s", path, e)
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    df = raw.dropna(subset=["Unnamed: 0"], how="all").reset_index(drop=True)
    df = df[
        ~df["Unnamed: 0"].astype(str).str.contains(
            "Date Printed|Note:|End Of Report|Whilst the Exchange",
            na=False,
            regex=True,
        )
    ].reset_index(drop=True)

    header_idx = df.index[
        df["Unnamed: 0"].astype(str).str.contains("Company", na=False)
        & df["Unnamed: 1"].astype(str).str.contains("Stock", na=False)
    ].tolist()
    if not header_idx:
        logger.warning("HKEX XLS %s: header row not found", path.name)
        return pd.DataFrame()

    header_row = df.loc[header_idx[0]].astype(str).str.replace("\n", " ").str.strip()
    df = df.loc[header_idx[0] + 1 :].reset_index(drop=True)
    df.columns = header_row

    code_col = "Stock code" if "Stock code" in df.columns else "Stock  code"
    if code_col not in df.columns:
        return pd.DataFrame()

    df = df[df[code_col].astype(str).str.match(r"^\d+$", na=False)].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame()

    date_col = "Trading date (yyyy/mm/dd)"
    shares_col = _pick_column(df.columns, SHARES_COL_ALIASES)
    high_col = _pick_column(df.columns, HIGH_PRICE_COL_ALIASES)

    if date_col not in df.columns or not shares_col or not high_col:
        logger.warning(
            "HKEX XLS %s: missing required columns (date=%s shares=%s price=%s)",
            path.name,
            date_col in df.columns,
            bool(shares_col),
            bool(high_col),
        )
        return pd.DataFrame()

    high_currency_col = "Highest Repurchase Price_Currency"

    def _split_price(val):
        cur, num = _extract_currency(val)
        return pd.Series([cur, num])

    df[[high_currency_col, "Highest Repurchase Price"]] = df[high_col].apply(_split_price)

    df[shares_col] = pd.to_numeric(
        df[shares_col].astype(str).str.replace(",", "", regex=False),
        errors="coerce",
    )
    df["Trading Date"] = pd.to_datetime(df[date_col], format="%Y/%m/%d", errors="coerce")

    out = pd.DataFrame({
        "Trading Date": df["Trading Date"].dt.strftime("%Y-%m-%d"),
        "Company": df.get("Company", df.get("Company ", "")),
        "Stock code": df[code_col].astype(str).str.strip(),
        "Highest Repurchase Price": pd.to_numeric(df["Highest Repurchase Price"], errors="coerce"),
        "No. of Shares to be Purchased": df[shares_col].values,
        "sharesOutstanding": None,
        "trailingPE": None,
        "forwardPE": None,
        "trailingPegRatio": None,
        "heldPercentInsiders": None,
        "priceToBook": None,
        "totalCash": None,
        "marketCap": None,
        "totalDebt": None,
        "debtToEquity": None,
        "financialCurrency": df[high_currency_col],
        "Share purchase pc share count": None,
        "Source_File": path.name,
        "Scrape_Timestamp": scrape_ts,
    })

    valid = (
        out["No. of Shares to be Purchased"].notna()
        & out["sharesOutstanding"].notna()
        & (out["sharesOutstanding"] > 0)
    )
    if valid.any():
        out.loc[valid, "Share purchase pc share count"] = (
            out.loc[valid, "No. of Shares to be Purchased"]
            / out.loc[valid, "sharesOutstanding"]
            * 100
        ).round(2)

    if source_date is not None:
        out.loc[out["Trading Date"].isna(), "Trading Date"] = source_date.isoformat()

    return out


def build_master_csv_from_xls(
    data_dir: Path = None,
    csv_path: Path = None,
    xls_files: list = None,
) -> dict:
    """Parse all local XLS files and write the consolidated HK master CSV."""
    csv_path = Path(csv_path or DEFAULT_CSV_PATH)
    files = xls_files or list_local_xls_files(data_dir)
    if not files:
        return {"ok": False, "error": "No HKEX XLS files found", "rows": 0}

    frames = []
    parsed_files = 0
    for path in files:
        chunk = parse_hkex_xls(path)
        if not chunk.empty:
            frames.append(chunk)
            parsed_files += 1

    if not frames:
        return {"ok": False, "error": "No rows parsed from HKEX XLS files", "rows": 0}

    combined = pd.concat(frames, ignore_index=True)
    combined["Stock code"] = combined["Stock code"].astype(str).str.strip()
    combined["Trading Date"] = combined["Trading Date"].astype(str)
    combined = combined.sort_values(["Trading Date", "Stock code"], ascending=[False, True])
    before = len(combined)
    combined = combined.drop_duplicates(subset=["Trading Date", "Stock code"], keep="first")
    combined = combined[[c for c in DESIRED_COLUMNS if c in combined.columns]]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(csv_path, index=False, encoding="utf-8")

    return {
        "ok": True,
        "rows": len(combined),
        "files_parsed": parsed_files,
        "files_total": len(files),
        "deduped": before - len(combined),
        "csv_path": str(csv_path),
    }


def refresh_hkex_csv(
    start: date = None,
    end: date = None,
    download_gaps: bool = True,
    csv_path: Path = None,
) -> dict:
    """Download missing HKEX daily reports (gap-fill) and rebuild the master CSV."""
    dl_stats = {}
    if download_gaps:
        dl_stats = download_missing_reports(start=start, end=end)

    build_stats = build_master_csv_from_xls(csv_path=csv_path)
    if not build_stats.get("ok"):
        return {**build_stats, "download": dl_stats, "ok": False}

    return {
        "ok": True,
        "download": dl_stats,
        "build": build_stats,
        "rows": build_stats.get("rows", 0),
        "csv_path": build_stats.get("csv_path"),
    }