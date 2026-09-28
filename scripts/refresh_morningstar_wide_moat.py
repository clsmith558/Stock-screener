#!/usr/bin/env python3
"""
Refresh Business lists/Morningstar wide moat.csv from public sources.

Official complete Wide Economic Moat screen is paywalled (~169 US names after
Morningstar's March 2026 review). This list is an honest free proxy:

  1. Morningstar Best Companies to Own CSVs (Wide moat + Standard/Exemplary
     capital allocation + Low/Medium fair-value uncertainty)
  2. Public compilation of 98 selected US-listed names (Aug 2026)
  3. VanEck MOAT ETF holdings when scrapeable (Wide Moat Focus Index)

Usage (from repo root):
  python scripts/refresh_morningstar_wide_moat.py
  python scripts/refresh_morningstar_wide_moat.py --best-companies-only
  python scripts/refresh_morningstar_wide_moat.py --json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config" / "morningstar_wide_moat.yaml"
DEFAULT_OUT = ROOT / "Business lists" / "Morningstar wide moat.csv"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

CSV_URL_RE = re.compile(
    r"https://cosmos-cdn\.morningstar\.com/playfair/[A-Za-z0-9]+/data\.csv",
    re.I,
)
SKIP_TICKERS = frozenset(
    {
        "CASH",
        "USD",
        "N/A",
        "NA",
        "-",
        "NONE",
        "OTHER",
        "COMPARE",
        "INDUSTRY",
        "SCREENER",
        "HOLDINGS",
        "WATCHLIST",
        "DOWNLOAD",
        "FILTER",
        "SECTOR",
        "PRO",
        "ETF",
        "MOAT",
        "QUOTE",
        "CHART",
        "NEWS",
        "OVERVIEW",
        "STATISTICS",
        "FINANCIALS",
        "FORECAST",
        "DIVIDEND",
        "SUBSCRIBE",
        "LOGIN",
    }
)
# Yahoo class-share: BF.B / BRK.B / BF/B → BF-B / BRK-B
CLASS_SHARE_RE = re.compile(r"^([A-Z]{1,5})[./]([A-Z])$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("refresh_wide_moat")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Missing config: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected YAML in {CONFIG_PATH}")
    return data


def to_yf_symbol(raw: str) -> str:
    """Morningstar `stocks:XNAS:AMZN` / `BF.B` / `BF/B` → Yahoo `AMZN` / `BF-B`."""
    t = (raw or "").strip().strip('"').strip("'")
    if not t or t.startswith("#"):
        return ""
    if t.lower().startswith("stocks:"):
        parts = t.split(":")
        t = parts[-1] if parts else t
    t = t.strip().upper()
    if not t or t in SKIP_TICKERS:
        return ""
    t = t.replace(" ", "")
    m = CLASS_SHARE_RE.match(t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    t = t.replace("/", "-")
    if not re.fullmatch(r"[A-Z][A-Z0-9._\-]*", t):
        return ""
    if len(t) > 16:
        return ""
    return t


def _looks_like_header_cell(cell: str) -> bool:
    return (cell or "").strip().lower() in {
        "ticker",
        "symbol",
        "name",
        "holding",
        "holdings",
    }


def _parse_best_companies_csv(text: str, sector: str) -> list[dict]:
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        return rows
    ticker_field = next(
        (
            f
            for f in reader.fieldnames
            if (f or "").strip().lower() in ("ticker", "symbol")
        ),
        None,
    )
    name_field = next(
        (f for f in reader.fieldnames if (f or "").strip().lower() == "name"),
        None,
    )
    country_field = next(
        (
            f
            for f in reader.fieldnames
            if "country" in (f or "").strip().lower()
        ),
        None,
    )
    sector_field = next(
        (
            f
            for f in reader.fieldnames
            if "sector" in (f or "").strip().lower()
        ),
        None,
    )
    if not ticker_field:
        return rows
    for raw in reader:
        sym = to_yf_symbol((raw or {}).get(ticker_field) or "")
        if not _looks_like_header_cell(sym) and sym:
            rows.append(
                {
                    "ticker": sym,
                    "name": ((raw or {}).get(name_field) or "").strip() if name_field else "",
                    "sector": (
                        ((raw or {}).get(sector_field) or "").strip()
                        if sector_field
                        else sector
                    ),
                    "country": (
                        ((raw or {}).get(country_field) or "").strip()
                        if country_field
                        else ""
                    ),
                    "source": "best_companies",
                }
            )
    return rows


def discover_csv_urls(session: requests.Session, cfg: dict) -> list[tuple[str, str]]:
    """Return [(sector_label, url), ...] from article HTML, else 2026 fallbacks."""
    urls: list[str] = []
    for key in ("article_url", "article_url_generic"):
        page = (cfg.get(key) or "").strip()
        if not page:
            continue
        try:
            r = session.get(page, timeout=45)
            r.raise_for_status()
            found = CSV_URL_RE.findall(r.text)
            # Preserve order, unique
            seen: set[str] = set()
            for u in found:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            if urls:
                logger.info("Found %d CSV links on %s", len(urls), page)
                break
        except Exception as e:
            logger.warning("Article fetch failed (%s): %s", page, e)

    labeled: list[tuple[str, str]] = []
    fallbacks: dict = cfg.get("csv_url_fallback_2026") or {}
    if urls:
        # Map by matching fallback URL → sector name when possible
        inv = {v: k for k, v in fallbacks.items()}
        for i, u in enumerate(urls):
            labeled.append((inv.get(u, f"sector_{i+1}"), u))
        return labeled

    if not fallbacks:
        raise RuntimeError("No Best Companies CSV URLs (article parse failed and no fallbacks)")
    logger.info("Using 2026 fallback CSV URLs (%d)", len(fallbacks))
    return [(str(k), str(v)) for k, v in fallbacks.items()]


def fetch_best_companies(session: requests.Session, cfg: dict) -> list[dict]:
    out: list[dict] = []
    for sector, url in discover_csv_urls(session, cfg):
        try:
            r = session.get(url, timeout=45)
            r.raise_for_status()
            rows = _parse_best_companies_csv(r.text, sector)
            logger.info("Best Companies %s: %d names", sector, len(rows))
            out.extend(rows)
        except Exception as e:
            logger.warning("Best Companies CSV failed (%s): %s", url, e)
    if len(out) < 80:
        raise RuntimeError(f"Best Companies yield too small ({len(out)}); aborting")
    return out


def load_public_compilation(cfg: dict) -> list[dict]:
    rel = (cfg.get("public_compilation_csv") or "").strip()
    path = ROOT / rel if rel else ROOT / "config" / "morningstar_wide_moat_public.csv"
    if not path.is_file():
        logger.warning("Public compilation CSV missing: %s", path)
        return []
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows: list[dict] = []
    for raw in reader:
        sym = to_yf_symbol((raw or {}).get("ticker") or "")
        if not sym:
            continue
        rows.append(
            {
                "ticker": sym,
                "name": ((raw or {}).get("name") or "").strip(),
                "sector": "",
                "country": "United States",
                "source": "public_compilation",
            }
        )
    logger.info("Public compilation: %d names from %s", len(rows), path.name)
    return rows


def _parse_holdings_from_html(html: str) -> list[str]:
    """Pull ticker-like tokens from stockanalysis / companiesmarketcap tables."""
    found: list[str] = []
    # /stocks/veev/ — skip /stocks/compare/ and other site paths (len > 5)
    for m in re.finditer(r"/stocks/([a-z0-9]{1,5}(?:[.-][a-z])?)/", html, flags=re.I):
        found.append(m.group(1))
    for m in re.finditer(
        r"/quote/(?:NASDAQ|NYSE|AMEX|BATS|OTC)/([A-Z]{1,5}(?:[.-][A-Z])?)/",
        html,
        flags=re.I,
    ):
        found.append(m.group(1))
    out: list[str] = []
    seen: set[str] = set()
    for raw in found:
        sym = to_yf_symbol(raw)
        if (
            sym
            and sym not in seen
            and not _looks_like_header_cell(sym)
            and len(sym) <= 6
        ):
            seen.add(sym)
            out.append(sym)
    return out


def _parse_holdings_xlsx(content: bytes) -> list[str]:
    import pandas as pd

    bio = io.BytesIO(content)
    try:
        xl = pd.ExcelFile(bio, engine="openpyxl")
    except Exception:
        bio.seek(0)
        xl = pd.ExcelFile(bio)
    tickers: list[str] = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, dtype=str)
        if df.empty:
            continue
        col = None
        for c in df.columns:
            cl = str(c).strip().lower()
            if cl in ("ticker", "symbol", "holding ticker", "holdings ticker"):
                col = c
                break
        if col is None:
            for c in df.columns:
                if "ticker" in str(c).lower() or str(c).strip().lower() == "symbol":
                    col = c
                    break
        if col is None:
            continue
        for raw in df[col].tolist():
            sym = to_yf_symbol(str(raw or ""))
            if sym and not _looks_like_header_cell(sym):
                tickers.append(sym)
    seen: set[str] = set()
    out: list[str] = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _moat_rows(tickers: list[str], source: str) -> list[dict]:
    return [
        {
            "ticker": t,
            "name": "",
            "sector": "",
            "country": "",
            "source": source,
        }
        for t in tickers
        if t
    ]


def fetch_moat_holdings(session: requests.Session, cfg: dict) -> tuple[list[dict], str]:
    """Live scrape union last-known Focus Index snapshot (free pages are often top-25)."""
    live: list[str] = []
    live_url = ""
    urls = list(cfg.get("moat_holdings_urls") or [])
    for url in urls:
        try:
            r = session.get(url, timeout=45, allow_redirects=True)
            if r.status_code != 200:
                logger.info("MOAT URL %s → HTTP %s", url, r.status_code)
                continue
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = r.content
            if "excel" in ctype or "spreadsheet" in ctype or url.endswith(("xlsx", "xls")):
                tickers = _parse_holdings_xlsx(body)
            elif body[:2] == b"PK":
                tickers = _parse_holdings_xlsx(body)
            else:
                tickers = _parse_holdings_from_html(r.text)
            if len(tickers) > len(live):
                live, live_url = tickers, url
            if len(tickers) >= 40:
                break
            logger.info("MOAT URL %s yielded %d tickers", url, len(tickers))
        except Exception as e:
            logger.warning("MOAT fetch failed (%s): %s", url, e)

    fallback = [to_yf_symbol(t) for t in (cfg.get("moat_fallback_holdings") or [])]
    fallback = [t for t in fallback if t]
    rows: list[dict] = []
    if live:
        logger.info("MOAT live holdings from %s: %d tickers", live_url, len(live))
        rows.extend(_moat_rows(live, "moat_etf"))
    if fallback:
        logger.info("MOAT snapshot holdings: %d tickers", len(fallback))
        rows.extend(_moat_rows(fallback, "moat_etf_fallback"))
    if not rows:
        return [], "none"
    label = live_url if live and not fallback else (
        f"{live_url}+yaml_fallback" if live else "yaml_fallback"
    )
    return rows, label


def merge_rows(*groups: list[dict]) -> list[dict]:
    """Union by Yahoo ticker; first non-empty name/sector/country wins; sources joined."""
    by: dict[str, dict] = {}
    for group in groups:
        for row in group:
            t = row.get("ticker") or ""
            if not t:
                continue
            if t not in by:
                by[t] = {
                    "ticker": t,
                    "name": row.get("name") or "",
                    "sector": row.get("sector") or "",
                    "country": row.get("country") or "",
                    "source": row.get("source") or "",
                }
                continue
            cur = by[t]
            if not cur["name"] and row.get("name"):
                cur["name"] = row["name"]
            if not cur["sector"] and row.get("sector"):
                cur["sector"] = row["sector"]
            if not cur["country"] and row.get("country"):
                cur["country"] = row["country"]
            src = row.get("source") or ""
            if src and src not in cur["source"].split("|"):
                cur["source"] = "|".join(x for x in (cur["source"], src) if x)
    return [by[k] for k in sorted(by)]


def write_csv(path: Path, rows: list[dict], meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    header = [
        "# Morningstar wide-moat proxy — NOT the complete paywalled Wide Economic Moat universe.",
        f"# Morningstar reported ~{meta.get('full_universe_note', '169 US names after March 2026 review')}.",
        "# Union of:",
        "#   1) Best Companies to Own (official public CSVs: Wide moat + Standard/Exemplary",
        "#      capital allocation + Low/Medium fair-value uncertainty)",
        f"#      {meta.get('article_url', '')}",
        "#   2) Public compilation of 98 selected US-listed names (Aug 2026)",
        "#   3) VanEck MOAT ETF / Wide Moat Focus Index holdings (cheapest ~50 wide-moat names)",
        f"# Refreshed: {today}  best_companies={meta.get('n_best', 0)}  "
        f"public={meta.get('n_public', 0)}  moat={meta.get('n_moat', 0)}  union={len(rows)}",
        "# Refresh: python scripts/refresh_morningstar_wide_moat.py",
        "ticker,name,source,sector,country",
    ]
    lines = header[:]
    for r in rows:
        # Escape names that contain commas
        name = (r.get("name") or "").replace('"', '""')
        if "," in name or '"' in name:
            name = f'"{name}"'
        lines.append(
            ",".join(
                [
                    r.get("ticker") or "",
                    name,
                    r.get("source") or "",
                    (r.get("sector") or "").replace(",", " "),
                    (r.get("country") or "").replace(",", " "),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d tickers)", path, len(rows))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Morningstar wide-moat Business list (public proxy)"
    )
    parser.add_argument(
        "--best-companies-only",
        action="store_true",
        help="Skip public compilation and MOAT ETF holdings",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output CSV path",
    )
    parser.add_argument("--json", action="store_true", help="Print result JSON")
    args = parser.parse_args()

    cfg = _load_config()
    session = _session()

    best = fetch_best_companies(session, cfg)
    public: list[dict] = []
    moat: list[dict] = []
    moat_src = "skipped"
    if not args.best_companies_only:
        public = load_public_compilation(cfg)
        moat, moat_src = fetch_moat_holdings(session, cfg)

    rows = merge_rows(best, public, moat)
    meta = {
        "article_url": cfg.get("article_url") or "",
        "full_universe_note": "169 US names after March 2026 review",
        "n_best": len({r["ticker"] for r in best}),
        "n_public": len({r["ticker"] for r in public}),
        "n_moat": len({r["ticker"] for r in moat}),
        "moat_source": moat_src,
        "union": len(rows),
        "out": str(args.out),
    }
    write_csv(args.out, rows, meta)

    try:
        from data.business_lists import scan_business_lists

        scan_business_lists(force=True)
    except Exception as e:
        logger.warning("List scan failed (CSV still written): %s", e)

    if args.json:
        print(json.dumps(meta, indent=2))
    else:
        print("Morningstar wide-moat list refreshed:")
        print(f"  Best Companies:     {meta['n_best']}")
        print(f"  Public compilation: {meta['n_public']}")
        print(f"  MOAT holdings:      {meta['n_moat']} ({moat_src})")
        print(f"  Union:              {meta['union']} → {args.out}")
        print(
            "  Note: this is a free proxy, not Morningstar's complete ~169 US wide-moat file."
        )
        # Smoke: a few names that must be present from Best Companies
        have = {r["ticker"] for r in rows}
        for sample in ("MSFT", "AMZN", "JNJ", "V", "COST"):
            print(f"  check {sample}: {'ok' if sample in have else 'MISSING'}")

    if meta["n_best"] < 80:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
