#!/usr/bin/env python3
"""
Refresh vault Opportunity-Queue.md from screener quant + qual composite scores.

Merges multiple screener markets (buybacks, US, ASX, business lists), dedupes by
registry_id, and auto-updates the signal-watch section.

Usage (from Stock screener folder):
  python scripts/refresh_opportunity_queue.py
  python scripts/refresh_opportunity_queue.py --force-yf
  python scripts/refresh_opportunity_queue.py --markets buybacks,us,asx
  python scripts/refresh_opportunity_queue.py --no-score-all-memos
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_vault() -> Path | None:
    raw = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(raw) if raw else None
QUEUE_REL = Path("wiki/investing/Opportunity-Queue.md")

DEFAULT_MARKETS = ["buybacks", "us", "asx"]
DEFAULT_BUSINESS_LISTS = [
    "Large value businesses list.csv",
    "Large core businesses list.csv",
]

SIGNAL_WATCH_MIN_QUANT = 50.0
SIGNAL_WATCH_MAX_COMPOSITE = 35.0


def _signal_summary(row: dict) -> str:
    parts = []
    src_market = row.get("_source_market") or row.get("_market")
    if src_market:
        parts.append(str(src_market))
    src = (row.get("buyback_source") or "").upper()
    if src in ("HKEX", "TSE", "ASX", "SEC"):
        parts.append(f"{src} buybacks")
    elif row.get("buyback_announced"):
        parts.append("buyback")
    if row.get("insider_buys_2026"):
        parts.append(f"insider×{row['insider_buys_2026']}")
    gap = row.get("pct_from_52w_low")
    if gap is not None and gap < 15:
        parts.append(f"{gap:.0f}% from 52w low")
    return ", ".join(parts) if parts else "screened"


def _memo_link(row: dict) -> str:
    memo = row.get("opportunity_memo")
    if memo:
        name = Path(memo).name
        return f"[[opportunities/{name}]]"
    rid = row.get("registry_id")
    if rid:
        return f"[[opportunities/{rid}]]"
    return "—"


def _ticker_display(row: dict) -> str:
    ticker = row.get("ticker") or row.get("registry_id") or "?"
    rid = row.get("registry_id") or ""
    if rid and rid.upper() not in str(ticker).upper():
        return f"{rid} / {ticker}"
    return str(ticker)


def _build_table_rows(opportunities: list[dict]) -> list[str]:
    rows = []
    for i, row in enumerate(opportunities, 1):
        rows.append(
            "| {rank} | {ticker} | {comp} | {quant} | {qual} | {tier} | {signals} | {memo} |".format(
                rank=i,
                ticker=_ticker_display(row),
                comp=f"{row.get('composite_score', 0):.1f}",
                quant=f"{row.get('quant_score', 0):.1f}",
                qual=f"{row.get('qual_score', 0):.1f}",
                tier=row.get("coverage_tier") or "—",
                signals=_signal_summary(row),
                memo=_memo_link(row),
            )
        )
    return rows


def _best_by_registry_id(rows: list[dict]) -> dict[str, dict]:
    """Best composite row per registry_id (uppercase key)."""
    best: dict[str, dict] = {}
    for row in rows:
        rid = row.get("registry_id")
        if not rid:
            continue
        key = str(rid).upper()
        existing = best.get(key)
        if not existing or (row.get("composite_score") or 0) > (existing.get("composite_score") or 0):
            best[key] = row
    return best


def _dedupe_opportunities(opportunities: list[dict]) -> list[dict]:
    """Keep best composite per registry_id (or ticker when unmatched)."""
    best: dict[str, dict] = {}
    for row in opportunities:
        key = row.get("registry_id") or _normalize_ticker(row.get("ticker", "")) or row.get("ticker", "")
        if not key:
            continue
        existing = best.get(key)
        if not existing or (row.get("composite_score") or 0) > (existing.get("composite_score") or 0):
            best[key] = row
    ranked = sorted(best.values(), key=lambda x: x.get("composite_score") or 0, reverse=True)
    for i, row in enumerate(ranked, 1):
        row["opportunity_rank"] = i
    return ranked


def _normalize_ticker(t: str) -> str:
    if not t:
        return ""
    s = str(t).upper().strip()
    s = re.sub(r"\.(AX|HK|TO|L|PA|IR)$", "", s)
    return s.replace("-W", "")


def _signal_watch_rows(opportunities: list[dict], limit: int = 10) -> list[dict]:
    """High quant + registry match but qual penalties suppress composite rank."""
    watch = [
        o
        for o in opportunities
        if o.get("registry_id")
        and (o.get("quant_score") or 0) >= SIGNAL_WATCH_MIN_QUANT
        and (o.get("composite_score") or 0) <= SIGNAL_WATCH_MAX_COMPOSITE
    ]
    watch.sort(key=lambda x: x.get("quant_score") or 0, reverse=True)
    return watch[:limit]


def _update_memo_scores(vault: Path, row: dict) -> bool:
    memo_rel = row.get("opportunity_memo")
    rid = row.get("registry_id")
    if not memo_rel and rid:
        memo_rel = f"opportunities/{rid}"
    if not memo_rel:
        return False
    memo_path = vault / "wiki" / "investing" / memo_rel
    if not memo_path.suffix:
        memo_path = memo_path.with_suffix(".md")
    if not memo_path.exists():
        return False

    text = memo_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False

    end = text.find("---", 3)
    if end < 0:
        return False
    front = text[3:end]

    def _set_field(block: str, key: str, value) -> str:
        if value is None:
            return block
        val_str = str(value)
        pattern = rf"^{re.escape(key)}:.*$"
        replacement = f"{key}: {val_str}"
        if re.search(pattern, block, re.MULTILINE):
            return re.sub(pattern, replacement, block, count=1, flags=re.MULTILINE)
        return block.rstrip() + f"\n{replacement}\n"

    front = _set_field(front, "quant_score", row.get("quant_score"))
    front = _set_field(front, "qual_score", row.get("qual_score"))
    front = _set_field(front, "composite_score", row.get("composite_score"))

    new_text = f"---{front}---{text[end + 3:]}"
    memo_path.write_text(new_text, encoding="utf-8")
    return True


def _load_registry_companies() -> dict:
    path = PROJECT_ROOT / "data" / "company_registry.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("companies") or {}


def _registry_memo_candidates(vault: Path) -> list[tuple[str, str]]:
    """(registry_id, opportunity_memo path) for memos that exist on disk."""
    candidates: list[tuple[str, str]] = []
    for rid, entry in _load_registry_companies().items():
        wiki = entry.get("wiki") or {}
        memo_rel = wiki.get("opportunity_memo") or f"opportunities/{rid}"
        memo_path = vault / "wiki" / "investing" / memo_rel
        if not memo_path.suffix:
            memo_path = memo_path.with_suffix(".md")
        if memo_path.exists():
            candidates.append((rid, memo_rel))
    return candidates


def _score_all_registry_memos(vault: Path, best_by_id: dict[str, dict]) -> dict:
    """Update frontmatter on every registry-linked opportunity memo."""
    updated: list[str] = []
    no_market_data: list[str] = []
    for rid, memo_rel in _registry_memo_candidates(vault):
        row = best_by_id.get(rid.upper())
        if not row:
            no_market_data.append(rid)
            continue
        payload = dict(row)
        payload["registry_id"] = rid
        payload["opportunity_memo"] = memo_rel
        if _update_memo_scores(vault, payload):
            updated.append(rid)
    return {
        "memos_scored": len(updated),
        "memos_updated_ids": updated,
        "memos_no_market_data": no_market_data,
    }


def _load_merged_stocks(
    markets: list[str], business_lists: list[str], force_yf: bool = False
) -> list[dict]:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app import get_stocks_for_market

    merged: list[dict] = []
    for market in markets:
        market = market.strip().lower()
        if not market:
            continue
        try:
            rows = get_stocks_for_market(market, force_yf=force_yf)
            for row in rows:
                copy = dict(row)
                copy["_source_market"] = market
                merged.append(copy)
        except Exception as e:
            print(f"Warning: market '{market}' failed: {e}", file=sys.stderr)

    for list_id in business_lists:
        list_id = list_id.strip()
        if not list_id:
            continue
        try:
            rows = get_stocks_for_market("business", list_id=list_id, force_yf=force_yf)
            label = f"business:{Path(list_id).stem}"
            for row in rows:
                copy = dict(row)
                copy["_source_market"] = label
                merged.append(copy)
        except Exception as e:
            print(f"Warning: business list '{list_id}' failed: {e}", file=sys.stderr)

    return merged


def refresh_queue(
    vault: Path,
    markets: list[str] | None = None,
    business_lists: list[str] | None = None,
    limit: int = 20,
    min_composite: float = 0,
    registry_only: bool = True,
    signal_watch_limit: int = 10,
    force_yf: bool = False,
    score_all_memos: bool = True,
) -> dict:
    sys.path.insert(0, str(PROJECT_ROOT))
    from data.wiki_bridge import enrich_stock_opportunity, load_registry

    load_registry()
    markets = markets or DEFAULT_MARKETS
    business_lists = business_lists if business_lists is not None else DEFAULT_BUSINESS_LISTS

    stocks = _load_merged_stocks(markets, business_lists, force_yf=force_yf)
    enriched_all = [enrich_stock_opportunity(s, peers=stocks) for s in stocks]
    enriched_registry = [s for s in enriched_all if s.get("registry_id")]
    best_by_id = _best_by_registry_id(enriched_registry)

    enriched = [s for s in enriched_registry if (s.get("composite_score") or 0) >= min_composite]
    if not registry_only:
        enriched = [
            s
            for s in enriched_all
            if (s.get("composite_score") or 0) >= min_composite
        ]

    deduped = _dedupe_opportunities(enriched if registry_only else enriched_registry)
    opportunities = deduped[:limit]
    watch = _signal_watch_rows(deduped, limit=signal_watch_limit)

    queue_path = vault / QUEUE_REL
    if not queue_path.exists():
        raise FileNotFoundError(queue_path)

    today = date.today().isoformat()
    market_label = ", ".join(markets)
    if business_lists:
        market_label += " + " + ", ".join(Path(b).stem for b in business_lists)

    active_header = (
        "| Rank | Ticker | Composite | Quant | Qual | Tier | Signals | Memo |\n"
        "|------|--------|-----------|-------|------|------|---------|------|"
    )
    active_body = "\n".join(_build_table_rows(opportunities)) or "| — | — | — | — | — | — | — | — |"
    refreshed_note = (
        f"*Refreshed {today} from screener markets: {market_label} "
        f"— `python scripts/refresh_opportunity_queue.py`*"
    )

    watch_header = (
        "| Ticker | Composite | Quant | Qual | Tier | Signals | Memo |\n"
        "|--------|-----------|-------|------|------|---------|------|"
    )
    watch_body = "\n".join(
        "| {ticker} | {comp} | {quant} | {qual} | {tier} | {signals} | {memo} |".format(
            ticker=_ticker_display(row),
            comp=f"{row.get('composite_score', 0):.1f}",
            quant=f"{row.get('quant_score', 0):.1f}",
            qual=f"{row.get('qual_score', 0):.1f}",
            tier=row.get("coverage_tier") or "—",
            signals=_signal_summary(row),
            memo=_memo_link(row),
        )
        for row in watch
    ) or "| — | — | — | — | — | — | — |"

    content = queue_path.read_text(encoding="utf-8")

    active_section = (
        f"## Active opportunities\n\n{active_header}\n{active_body}\n\n{refreshed_note}\n\n"
        f"### Signal watch (high quant, qual-penalized)\n\n"
        f"Names with strong **timing signals** but checklist/regulatory flags that suppress composite rank.\n\n"
        f"{watch_header}\n{watch_body}\n"
    )

    section_re = re.compile(
        r"## Active opportunities\n\n.*?(?=\n---\n\n## Workflow)",
        re.DOTALL,
    )
    if not section_re.search(content):
        raise ValueError("Could not find '## Active opportunities' section in Opportunity-Queue.md")
    content = section_re.sub(active_section, content, count=1)
    queue_path.write_text(content, encoding="utf-8")

    memo_score_result = (
        _score_all_registry_memos(vault, best_by_id) if score_all_memos else {}
    )

    return {
        "markets": markets,
        "business_lists": business_lists,
        "force_yf": force_yf,
        "merged_count": len(stocks),
        "registry_matched": len(deduped),
        "count": len(opportunities),
        "watch_count": len(watch),
        "queue_path": str(queue_path),
        "score_all_memos": score_all_memos,
        "memo_score_result": memo_score_result,
        "top": opportunities[0] if opportunities else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Refresh vault Opportunity-Queue from screener")
    parser.add_argument("--vault", type=Path, default=None, help="Obsidian vault root (or set OBSIDIAN_VAULT_PATH)")
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated markets (buybacks, us, asx)",
    )
    parser.add_argument(
        "--business-lists",
        default=",".join(DEFAULT_BUSINESS_LISTS),
        help="Comma-separated business list CSV filenames (empty to skip)",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-composite", type=float, default=0)
    parser.add_argument("--all", action="store_true", help="Include non-registry tickers")
    parser.add_argument("--no-business-lists", action="store_true", help="Skip business list markets")
    parser.add_argument(
        "--force-yf",
        action="store_true",
        help="Fetch live Yahoo Finance data instead of SQLite cache only",
    )
    parser.add_argument(
        "--no-score-all-memos",
        action="store_true",
        help="Skip updating all registry-linked opportunity memos (queue only)",
    )
    args = parser.parse_args()

    vault = args.vault or _default_vault()
    if vault is None:
        print(
            "Error: pass --vault or set OBSIDIAN_VAULT_PATH in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    if args.no_business_lists:
        business_lists: list[str] = []
    else:
        business_lists = [b.strip() for b in args.business_lists.split(",") if b.strip()]

    result = refresh_queue(
        vault,
        markets=markets,
        business_lists=business_lists,
        limit=args.limit,
        min_composite=args.min_composite,
        registry_only=not args.all,
        force_yf=args.force_yf,
        score_all_memos=not args.no_score_all_memos,
    )
    yf_note = " (live yfinance)" if result.get("force_yf") else " (cached yfinance)"
    print(
        f"Merged {result['merged_count']} rows{yf_note} → {result['registry_matched']} registry matches "
        f"→ {result['count']} active + {result['watch_count']} watch"
    )
    print(f"Updated {result['queue_path']}")
    msr = result.get("memo_score_result") or {}
    if result.get("score_all_memos"):
        print(
            f"Memos scored: {msr.get('memos_scored', 0)} "
            f"(no market data: {len(msr.get('memos_no_market_data') or [])})"
        )
        no_data = msr.get("memos_no_market_data") or []
        if no_data:
            print(f"  Missing from merged markets: {', '.join(no_data)}")
    if result.get("top"):
        t = result["top"]
        print(
            f"Top: {t.get('registry_id')} ({t.get('ticker')}) "
            f"composite={t.get('composite_score')} quant={t.get('quant_score')} qual={t.get('qual_score')} "
            f"via {t.get('_source_market')}"
        )


if __name__ == "__main__":
    main()