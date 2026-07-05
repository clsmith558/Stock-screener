"""
JPX off-auction own-shares repurchase scraper.
https://www.jpx.co.jp/english/markets/equities/off-auction-ownshares/index.html
"""

import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = _PROJECT_ROOT / "tse_stock_repurchases" / "jp_stock_repurchases.csv"
JPX_URL = "https://www.jpx.co.jp/english/markets/equities/off-auction-ownshares/index.html"

DESIRED_COLUMNS = [
    "Implementation Date", "Issue Name", "Code", "Price",
    "No. of Shares to be Purchased", "No. of Traded Shares",
    "Scrape_Timestamp",
    "trailingPE", "forwardPE", "trailingPegRatio", "sharesOutstanding",
    "heldPercentInsiders", "priceToBook", "totalCash", "marketCap",
    "Cash as pc mcap", "Share purchase pc share count", "totalDebt",
    "debtToEquity", "financialCurrency",
]


def scrape_stock_repurchases(url: str = JPX_URL) -> list:
    """Scrape the JPX repurchase table and return row dicts."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            )
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table", class_="fixedhead")
        if not table:
            logger.warning("JPX scrape: no table found")
            return []

        rows = table.find_all("tr")
        if not rows:
            return []

        header_cells = [th.get_text().strip() for th in rows[0].find_all("th") if th.get_text().strip()]
        if not header_cells:
            return []

        data = []
        for row in rows[1:]:
            cols = [td.get_text().strip() for td in row.find_all("td")]
            if not cols or len(cols) < len(header_cells):
                continue

            row_data = dict(zip(header_cells, cols))

            date_str = row_data.get("Implementation Date", "")
            if date_str:
                try:
                    parsed = datetime.strptime(date_str.replace(".", ""), "%b %d, %Y")
                    row_data["Implementation Date"] = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    row_data["Implementation Date"] = ""

            issue_name_code = row_data.get("Issue Name (Code)", "")
            if issue_name_code and "(" in issue_name_code and ")" in issue_name_code:
                issue_name, code = issue_name_code.rsplit("(", 1)
                row_data["Issue Name"] = issue_name.strip()
                row_data["Code"] = code.strip(")")
            else:
                row_data["Issue Name"] = issue_name_code
                row_data["Code"] = ""
            row_data.pop("Issue Name (Code)", None)

            row_data["Source_URL"] = url
            row_data["Scrape_Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data.append(row_data)

        return data
    except requests.RequestException as e:
        logger.error("JPX scrape request failed: %s", e)
        return []
    except Exception as e:
        logger.exception("JPX scrape error: %s", e)
        return []


def _normalize_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "Price(yen)": "Price",
        "No. of Shares to be Purchased(shares/units)": "No. of Shares to be Purchased",
        "No. of Traded Shares(shares/units)": "No. of Traded Shares",
    }
    df = df.rename(columns=rename)

    for col in ("No. of Shares to be Purchased", "No. of Traded Shares"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )

    for col in ("sharesOutstanding", "totalCash", "marketCap"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if (
        "No. of Shares to be Purchased" in df.columns
        and "sharesOutstanding" in df.columns
    ):
        valid = (
            df["No. of Shares to be Purchased"].notna()
            & df["sharesOutstanding"].notna()
            & (df["sharesOutstanding"] > 0)
        )
        df["Share purchase pc share count"] = None
        df.loc[valid, "Share purchase pc share count"] = (
            df.loc[valid, "No. of Shares to be Purchased"]
            / df.loc[valid, "sharesOutstanding"]
            * 100
        )

    if "totalCash" in df.columns and "marketCap" in df.columns:
        valid = (
            df["totalCash"].notna()
            & df["marketCap"].notna()
            & (df["marketCap"] > 0)
        )
        df["Cash as pc mcap"] = None
        df.loc[valid, "Cash as pc mcap"] = (
            df.loc[valid, "totalCash"] / df.loc[valid, "marketCap"] * 100
        )

    round_cols = [
        "trailingPE", "forwardPE", "trailingPegRatio", "heldPercentInsiders",
        "priceToBook", "Cash as pc mcap", "Share purchase pc share count", "debtToEquity",
    ]
    for col in round_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)

    cols = [c for c in DESIRED_COLUMNS if c in df.columns]
    return df[cols]


def merge_scrape_into_csv(
    new_rows: list,
    csv_path: Path = None,
    fetch_yfinance: bool = False,
) -> dict:
    """
    Merge freshly scraped rows into the cumulative CSV (dedupe on date+code).
    yfinance fetch is optional and off by default for fast dashboard refresh.
    """
    csv_path = Path(csv_path or DEFAULT_CSV_PATH)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not new_rows:
        return {"added": 0, "total": 0, "csv_path": str(csv_path)}

    new_df = _normalize_numeric_columns(pd.DataFrame(new_rows))

    if fetch_yfinance:
        logger.info(
            "JPX scrape: yfinance enrichment requested — use data.buybacks.rebuild_buybacks_yf_cache() after merge"
        )

    if csv_path.exists():
        try:
            existing_df = pd.read_csv(csv_path, encoding="utf-8")
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
        except Exception as e:
            logger.warning("Could not read existing JPX CSV, starting fresh: %s", e)
            combined_df = new_df
    else:
        combined_df = new_df

    combined_df["Scrape_Timestamp"] = pd.to_datetime(combined_df["Scrape_Timestamp"], errors="coerce")
    combined_df["Code"] = combined_df["Code"].astype(str)
    combined_df = combined_df.sort_values("Scrape_Timestamp", ascending=False)
    before = len(combined_df)
    combined_df = combined_df.drop_duplicates(subset=["Implementation Date", "Code"], keep="first")
    combined_df = _normalize_numeric_columns(combined_df)
    combined_df.to_csv(csv_path, index=False, encoding="utf-8")

    return {
        "added": len(combined_df) - (before - len(new_df)),
        "total": len(combined_df),
        "csv_path": str(csv_path),
        "scraped": len(new_rows),
    }


def refresh_jpx_csv(csv_path: Path = None, fetch_yfinance: bool = False) -> dict:
    """Scrape JPX page and merge into project CSV."""
    rows = scrape_stock_repurchases()
    if not rows:
        return {"ok": False, "error": "No rows scraped from JPX", "scraped": 0}
    stats = merge_scrape_into_csv(rows, csv_path=csv_path, fetch_yfinance=fetch_yfinance)
    stats["ok"] = True
    stats["scraped"] = len(rows)
    return stats