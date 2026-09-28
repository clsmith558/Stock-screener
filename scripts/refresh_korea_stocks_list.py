#!/usr/bin/env python3
"""
Refresh Business lists Korea equities (KOSPI + KOSDAQ) for Yahoo Finance.

Produces Yahoo-style symbols:
  KOSPI  -> 005930.KS  (Samsung)
  KOSDAQ -> 035420.KQ  (NAVER often listed KOSPI actually; KOSDAQ uses .KQ)

Primary source: FinanceDataReader KRX listings (live marcap snapshot).
Fallback: FinanceData GitHub KRX listing cache CSV when data.kr is unreachable.

Usage:
  python scripts/refresh_korea_stocks_list.py
  python scripts/refresh_korea_stocks_list.py --include-konex
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "Business lists" / "korea_stocks.csv"
UA = "SignalScreener/1.0 (research; contact: research@localhost)"
GITHUB_CACHE_API = (
    "https://api.github.com/repos/FinanceData/fdr_krx_data_cache/contents/data/listing/krx"
)
GITHUB_CACHE_RAW = (
    "https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/master/data/listing/krx"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def _normalize_code(code: str) -> str:
    c = str(code or "").strip().upper()
    c = c.replace(".0", "")
    # zero-pad numeric 6-digit KRX codes
    if re.fullmatch(r"\d{1,6}", c):
        return c.zfill(6)
    if re.fullmatch(r"\d{6}[A-Z]?", c):
        return c
    return ""


def _market_to_suffix(market: str, market_id: str = "") -> str:
    m = (market or "").upper()
    mid = (market_id or "").upper()
    if mid == "STK" or "KOSPI" in m:
        return "KS"
    if mid == "KSQ" or "KOSDAQ" in m:
        return "KQ"
    if mid == "KNX" or "KONEX" in m:
        return "KQ"  # Yahoo often lacks KONEX; keep KQ as best-effort skip later
    # default KOSPI-style
    return "KS"


def codes_to_yf(df: pd.DataFrame, include_konex: bool = False) -> list[str]:
    """Build Yahoo symbols from a DataFrame with Code + Market/MarketId."""
    code_col = None
    for c in ("Code", "Symbol", "ISU_SRT_CD", "code", "symbol"):
        if c in df.columns:
            code_col = c
            break
    if code_col is None:
        raise ValueError(f"No code column in {list(df.columns)}")

    mkt_col = "Market" if "Market" in df.columns else None
    mid_col = "MarketId" if "MarketId" in df.columns else None

    out: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        code = _normalize_code(row[code_col])
        if not code:
            continue
        market = str(row[mkt_col]) if mkt_col else ""
        mid = str(row[mid_col]) if mid_col else ""
        if not include_konex and ("KONEX" in market.upper() or mid.upper() == "KNX"):
            continue
        # Prefer shares only: skip preferred shares like 005935 if desired?
        # Keep preferred (005935.KS) — user can filter later; large caps use common.
        sfx = _market_to_suffix(market, mid)
        if sfx == "KQ" and mid.upper() == "KNX" and not include_konex:
            continue
        sym = f"{code}.{sfx}"
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def fetch_via_fdr(include_konex: bool) -> pd.DataFrame:
    import FinanceDataReader as fdr

    frames = [fdr.StockListing("KOSPI"), fdr.StockListing("KOSDAQ")]
    if include_konex:
        try:
            frames.append(fdr.StockListing("KONEX"))
        except Exception:
            pass
    df = pd.concat(frames, ignore_index=True)
    # Drop exact duplicate codes if any
    if "Code" in df.columns:
        df = df.drop_duplicates(subset=["Code"], keep="first")
    return df


def fetch_via_github_cache(session: requests.Session) -> pd.DataFrame:
    """Latest daily KRX listing CSV mirrored by FinanceData on GitHub."""
    r = session.get(GITHUB_CACHE_API, timeout=60)
    r.raise_for_status()
    items = r.json()
    names = sorted(
        [
            it["name"]
            for it in items
            if isinstance(it, dict) and str(it.get("name", "")).endswith(".csv")
        ]
    )
    if not names:
        raise RuntimeError("No CSV files in fdr_krx_data_cache listing")
    latest = names[-1]
    url = f"{GITHUB_CACHE_RAW}/{latest}"
    print(f"GitHub cache: {url}")
    df = pd.read_csv(url, dtype={"Code": str, "MarketId": str, "Dept": str})
    return df


def write_list(symbols: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # One ticker per line (easier to diff than Japan single-line blob)
    # Also accepted by business_lists parser
    path.write_text("\n".join(symbols) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Korea KRX stock list for screener")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--include-konex",
        action="store_true",
        help="Include KONEX names (default: KOSPI + KOSDAQ only)",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "fdr", "github"),
        default="auto",
        help="Listing source (default auto: FDR then GitHub cache)",
    )
    args = parser.parse_args()

    session = _session()
    df = None
    source_used = ""

    if args.source in ("auto", "fdr"):
        try:
            df = fetch_via_fdr(args.include_konex)
            source_used = "FinanceDataReader"
            print(f"Loaded via FinanceDataReader: {len(df)} rows")
        except Exception as e:
            print(f"FinanceDataReader failed: {e}")
            if args.source == "fdr":
                return 1

    if df is None and args.source in ("auto", "github"):
        try:
            df = fetch_via_github_cache(session)
            source_used = "GitHub fdr_krx_data_cache"
            print(f"Loaded via GitHub cache: {len(df)} rows")
        except Exception as e:
            print(f"GitHub cache failed: {e}", file=sys.stderr)
            return 1

    assert df is not None
    symbols = codes_to_yf(df, include_konex=args.include_konex)
    if not symbols:
        print("ERROR: no symbols produced", file=sys.stderr)
        return 1

    samples = {
        "005930.KS": "Samsung Electronics",
        "000660.KS": "SK hynix",
        "035420.KS": "NAVER (if KOSPI)",
        "035420.KQ": "NAVER (if KOSDAQ)",
        "051910.KS": "LG Chem",
        "006400.KS": "Samsung SDI",
        "068270.KS": "Celltrion",
    }
    print(f"Source: {source_used}")
    print(f"Parsed {len(symbols)} Yahoo symbols (KOSPI .KS / KOSDAQ .KQ)")
    for sym, label in samples.items():
        print(f"  {sym} ({label}): {'yes' if sym in symbols else 'no'}")

    has_samsung = "005930.KS" in symbols
    if not has_samsung:
        print("WARNING: 005930.KS (Samsung) missing", file=sys.stderr)

    write_list(symbols, args.out)
    print(f"Wrote {args.out} ({len(symbols)} tickers)")

    # Optional name sidecar for human browsing (not required by screener)
    try:
        if "Code" in df.columns and "Name" in df.columns:
            meta_path = args.out.with_suffix(".meta.csv")
            rows = []
            for _, row in df.iterrows():
                code = _normalize_code(row["Code"])
                if not code:
                    continue
                market = str(row.get("Market", ""))
                mid = str(row.get("MarketId", ""))
                if not args.include_konex and (
                    "KONEX" in market.upper() or mid.upper() == "KNX"
                ):
                    continue
                sfx = _market_to_suffix(market, mid)
                rows.append(
                    {
                        "ticker": f"{code}.{sfx}",
                        "code": code,
                        "name": row.get("Name", ""),
                        "market": market,
                    }
                )
            pd.DataFrame(rows).drop_duplicates("ticker").to_csv(
                meta_path, index=False, encoding="utf-8-sig"
            )
            print(f"Wrote {meta_path} (names)")
    except Exception as e:
        print(f"Meta CSV skipped: {e}")

    return 0 if has_samsung else 2


if __name__ == "__main__":
    raise SystemExit(main())
