#!/usr/bin/env python3
"""
Refresh Business lists Japan equities from JPX "List of TSE-listed Issues".

Default: equity boards only — Prime + Standard + Growth (domestic stocks).
Excludes ETFs/ETNs, REITs, funds, and similar products when labeled in
JPX Section/Products.

Usage:
  python scripts/refresh_japan_stocks_list.py
  python scripts/refresh_japan_stocks_list.py --prime-only
  python scripts/refresh_japan_stocks_list.py --all-issues
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JPX_LIST_PAGE = (
    "https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html"
)

DEFAULT_OUT = ROOT / "Business lists" / "japan_stocks.csv"
LEGACY_OUT = ROOT / "Business lists" / "japan_stocks.csv.csv"
META_OUT = ROOT / "Business lists" / "japan_stocks.meta.csv"

UA = "SignalScreener/1.0 (research; contact: research@localhost)"

_EQUITY_SECTION_RE = re.compile(
    r"(prime|standard|growth|プライム|スタンダード|グロース)",
    re.I,
)
_FUND_SECTION_RE = re.compile(
    r"("
    r"etf|etn|reit|"
    r"etfs?\s*/\s*etns?|"
    r"infrastructure|"
    r"infra\s*fund|"
    r"出資|"
    r"投信|"
    r"foreign\s*stock|"
    r"pro\s*market|"
    r"preferred\s*equity\s*contribution|"
    r"subscription\s*warrant"
    r")",
    re.I,
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def find_excel_url(session: requests.Session) -> str:
    """Prefer English data_e (stable Section/Products labels), else data_j."""
    r = session.get(JPX_LIST_PAGE, timeout=90)
    r.raise_for_status()
    html = r.text
    candidates = re.findall(
        r'href=["\']([^"\']+\.(?:xlsx|xls))["\']',
        html,
        flags=re.I,
    )
    ranked: list[tuple[int, str]] = []
    for href in candidates:
        h = href.strip()
        low = h.lower()
        score = 0
        if "data_e" in low:
            score += 55
        if "data_j" in low or "data-j" in low:
            score += 45
        if "listed" in low or "issue" in low:
            score += 10
        if low.endswith((".xls", ".xlsx")):
            score += 5
        if "nls" in low:
            score -= 20
        ranked.append((score, urljoin(JPX_LIST_PAGE, h)))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        raise RuntimeError(f"No Excel links found on {JPX_LIST_PAGE}")
    best_score = ranked[0][0]
    best = [u for s, u in ranked if s == best_score]
    for u in best:
        if "data_e" in u.lower():
            return u
    return best[0]


def _normalize_code(val) -> str:
    s = str(val or "").strip().upper().replace(".0", "")
    m = re.fullmatch(r"(\d{4}[A-Z]?)", s)
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"(\d{4})(?:\.0+)?", str(val or "").strip())
    if m2:
        return m2.group(1)
    return ""


def _read_excel_rows(content: bytes) -> list[dict]:
    """Parse JPX sheet into {code, name, section}."""
    import pandas as pd

    bio = BytesIO(content)
    engine = "openpyxl" if content[:2] == b"PK" else "xlrd"
    try:
        xl = pd.ExcelFile(bio, engine=engine)
    except Exception:
        bio.seek(0)
        xl = pd.ExcelFile(bio)

    rows: list[dict] = []
    for sheet in xl.sheet_names:
        raw = xl.parse(sheet, header=None, dtype=str)
        if raw.empty:
            continue
        header_idx = None
        col_map: dict[str, int] = {}
        for i in range(min(15, len(raw))):
            cells = [str(c).strip() for c in raw.iloc[i].tolist()]
            joined = " | ".join(cells).lower()
            if not any(k in joined for k in ("local code", "code", "コード")):
                continue
            for j, c in enumerate(cells):
                cl = c.lower()
                if "local code" in cl or cl in ("code", "コード", "銘柄コード"):
                    col_map["code"] = j
                elif "section" in cl or "products" in cl or "市場" in c or "商品" in c:
                    col_map["section"] = j
                elif "name (english)" in cl or ("name" in cl and "english" in cl):
                    col_map["name"] = j
                elif "name" in cl and "name" not in col_map:
                    col_map["name"] = j
            if "code" in col_map:
                header_idx = i
                break
        if header_idx is None or "code" not in col_map:
            continue

        code_j = col_map["code"]
        sec_j = col_map.get("section")
        name_j = col_map.get("name")
        for i in range(header_idx + 1, len(raw)):
            code = _normalize_code(raw.iloc[i, code_j])
            if not code:
                continue
            section = str(raw.iloc[i, sec_j] or "").strip() if sec_j is not None else ""
            name = str(raw.iloc[i, name_j] or "").strip() if name_j is not None else ""
            if section in ("nan", "None"):
                section = ""
            if name in ("nan", "None"):
                name = ""
            rows.append({"code": code, "name": name, "section": section})
        if rows:
            break

    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    return out


def filter_rows(
    rows: list[dict],
    *,
    equity_only: bool,
    prime_only: bool,
) -> tuple[list[dict], dict]:
    stats = {
        "input": len(rows),
        "kept": 0,
        "dropped_fund": 0,
        "dropped_non_equity_board": 0,
        "dropped_non_prime": 0,
        "by_section": {},
    }
    kept: list[dict] = []
    for r in rows:
        sec = r.get("section") or ""
        stats["by_section"][sec] = stats["by_section"].get(sec, 0) + 1

        if not equity_only and not prime_only:
            kept.append(r)
            continue

        if equity_only:
            if _FUND_SECTION_RE.search(sec):
                stats["dropped_fund"] += 1
                continue
            if not sec or not _EQUITY_SECTION_RE.search(sec):
                stats["dropped_non_equity_board"] += 1
                continue

        if prime_only and not re.search(r"prime|プライム", sec, re.I):
            stats["dropped_non_prime"] += 1
            continue

        kept.append(r)

    stats["kept"] = len(kept)
    return kept, stats


def write_list(symbols: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(symbols) + "\n", encoding="utf-8")


def write_meta(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "code", "name", "section"])
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "ticker": f"{r['code']}.T",
                    "code": r["code"],
                    "name": r.get("name") or "",
                    "section": r.get("section") or "",
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Japan TSE stock list from JPX")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-legacy", action="store_true")
    parser.add_argument("--url", default="")
    parser.add_argument(
        "--all-issues",
        action="store_true",
        help="Keep every JPX row including ETFs/REITs (old behaviour)",
    )
    parser.add_argument(
        "--prime-only",
        action="store_true",
        help="Restrict to Prime Market equities only",
    )
    parser.add_argument(
        "--include-funds",
        action="store_true",
        help="Do not drop ETF/ETN/REIT rows",
    )
    args = parser.parse_args()

    equity_only = not args.all_issues and not args.include_funds
    prime_only = bool(args.prime_only)

    session = _session()
    url = (args.url or "").strip() or find_excel_url(session)
    print(f"Downloading: {url}")
    r = session.get(url, timeout=120)
    r.raise_for_status()
    print(f"Downloaded {len(r.content):,} bytes")

    rows = _read_excel_rows(r.content)
    if not rows:
        print("ERROR: no rows parsed from Excel", file=sys.stderr)
        return 1
    if equity_only and not any(r.get("section") for r in rows):
        print("ERROR: Section/Products column missing; cannot filter funds", file=sys.stderr)
        return 1

    kept, stats = filter_rows(rows, equity_only=equity_only, prime_only=prime_only)
    print(
        f"Filter: equity_only={equity_only} prime_only={prime_only} | "
        f"input={stats['input']} kept={stats['kept']} "
        f"dropped_fund={stats['dropped_fund']} "
        f"dropped_non_equity={stats['dropped_non_equity_board']} "
        f"dropped_non_prime={stats['dropped_non_prime']}"
    )
    print("Section/Products (input, top):")
    for sec, n in sorted(stats["by_section"].items(), key=lambda x: -x[1])[:15]:
        print(f"  {n:5}  {sec or '(blank)'}")

    symbols = [f"{r['code']}.T" for r in kept]
    has_toyota = "7203.T" in symbols
    print(f"Output {len(symbols)} Yahoo symbols; 7203.T present={has_toyota}")

    write_list(symbols, args.out)
    print(f"Wrote {args.out}")
    if not args.no_legacy:
        write_list(symbols, LEGACY_OUT)
        print(f"Wrote {LEGACY_OUT}")
    try:
        write_meta(kept, META_OUT)
        print(f"Wrote {META_OUT}")
    except Exception as e:
        print(f"Meta write skipped: {e}")

    for sample in ("7203.T", "6758.T", "9984.T", "8306.T", "6861.T", "1305.T", "1306.T"):
        print(f"  sample {sample}: {'yes' if sample in symbols else 'no'}")

    if equity_only and stats["kept"] < 500:
        print("ERROR: kept count suspiciously low", file=sys.stderr)
        return 2
    if equity_only and not has_toyota:
        print("ERROR: Toyota missing after filter", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
