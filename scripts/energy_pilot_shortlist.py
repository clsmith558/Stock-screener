#!/usr/bin/env python3
"""
Rank energy_pilot Business list → daily shortlist markdown in Obsidian vault.

Usage (from repo root):
  python scripts/energy_pilot_shortlist.py
  python scripts/energy_pilot_shortlist.py --top 12 --export-vault
  python scripts/energy_pilot_shortlist.py --max-cache-days 0   # force YF refresh

Attention allocation only — does not invent thesis conclusions.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")
# Also allow main install env when running from a worktree without local .env
_main_env = Path(r"C:\Grok\Stock screener\.env")
if _main_env.is_file():
    load_dotenv(_main_env, override=False)

DEFAULT_VAULT = Path(r"C:\Obsidian\csmith_test")
LIST_PATH = PROJECT_ROOT / "Business lists" / "energy_pilot.csv"
# Fallback to main install list
if not LIST_PATH.is_file():
    LIST_PATH = Path(r"C:\Grok\Stock screener\Business lists\energy_pilot.csv")


def _vault() -> Path:
    raw = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_VAULT


def _load_pilot(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            t = (r.get("ticker") or "").strip().upper()
            if not t or t.startswith("#"):
                continue
            rows.append({
                "ticker": t,
                "name": (r.get("name") or t).strip(),
                "bucket": (r.get("bucket") or "").strip(),
                "notes": (r.get("notes") or "").strip(),
            })
    return rows


def _safe_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        x = float(v)
        if x != x:
            return default
        return x
    except (TypeError, ValueError):
        return default


def _fetch_row(meta: dict) -> dict:
    import yfinance as yf
    from data.business_lists import _calc_ma200w_gap, _metrics_from_yf_info, _safe_round
    from data.yf_helpers import pct_from_52w_low, reconcile_52w_range

    sym = meta["ticker"]
    yt = yf.Ticker(sym)
    info = yt.info or {}
    price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice")) or 0.0
    low, high = reconcile_52w_range(
        price, info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
    )
    ma_200w, pct_from_200w_ma = _calc_ma200w_gap(yt, price)
    metrics = _metrics_from_yf_info(info, price, ma_200w, pct_from_200w_ma, 0, ticker=sym)
    gap_low = pct_from_52w_low(price, low)

    row = {
        **meta,
        "price": _safe_round(price, 2) if price else None,
        "market_cap": metrics.get("market_cap"),
        "pct_from_52w_low": gap_low,
        "pct_from_200w_ma": metrics.get("pct_from_200w_ma"),
        "pe": metrics.get("pe"),
        "forward_pe": metrics.get("forward_pe"),
        "pb": metrics.get("pb"),
        "p_fcf": metrics.get("p_fcf"),
        "debt_to_equity": metrics.get("debt_to_equity"),
        "fetched_at": datetime.now().isoformat(),
    }
    return row


def _reason_codes(row: dict) -> list[str]:
    codes = []
    g = _safe_float(row.get("pct_from_52w_low"))
    m = _safe_float(row.get("pct_from_200w_ma"))
    pe = _safe_float(row.get("pe"))
    fpe = _safe_float(row.get("forward_pe"))
    pb = _safe_float(row.get("pb"))
    pfcf = _safe_float(row.get("p_fcf"))
    de = _safe_float(row.get("debt_to_equity"))

    if g is not None and g <= 15:
        codes.append("near_52w_low")
    elif g is not None and g <= 30:
        codes.append("within_30pct_52w_low")
    if m is not None and m < 0:
        codes.append("below_200w_ma")
    if pe is not None and 0 < pe <= 12:
        codes.append("low_pe")
    if fpe is not None and 0 < fpe <= 12:
        codes.append("low_fwd_pe")
    if pb is not None and 0 < pb <= 1.2:
        codes.append("low_pb")
    if pfcf is not None and 0 < pfcf <= 12:
        codes.append("low_p_fcf")
    if de is not None and de < 0.5:
        codes.append("low_leverage")
    if de is not None and de > 1.5:
        codes.append("high_leverage")
    notes = (row.get("notes") or "").lower()
    if "vault" in notes or "opp" in notes or "pabrai" in notes or "sector-energy" in notes:
        codes.append("vault_seed")
    bucket = row.get("bucket") or ""
    if bucket in ("met_coal", "thermal_coal", "asx_coal"):
        codes.append("pariah_coal_bucket")
    if bucket == "drillers":
        codes.append("cyclical_driller_bucket")
    if not codes:
        codes.append("monitor")
    return codes


def _attention_score(row: dict) -> float:
    """Higher = more attention. Heuristic only."""
    score = 0.0
    g = _safe_float(row.get("pct_from_52w_low"), 999)
    m = _safe_float(row.get("pct_from_200w_ma"), 0)
    pe = _safe_float(row.get("pe"))
    fpe = _safe_float(row.get("forward_pe"))
    pb = _safe_float(row.get("pb"))
    pfcf = _safe_float(row.get("p_fcf"))
    de = _safe_float(row.get("debt_to_equity"))

    # Prefer beaten-up names
    if g is not None:
        score += max(0.0, 40.0 - min(g, 80.0) * 0.5)
    if m is not None and m < 0:
        score += min(15.0, abs(m) * 0.3)
    if pe is not None and 0 < pe <= 15:
        score += 10.0 * (1.0 - pe / 15.0)
    if fpe is not None and 0 < fpe <= 15:
        score += 8.0 * (1.0 - fpe / 15.0)
    if pb is not None and 0 < pb <= 1.5:
        score += 8.0 * (1.0 - pb / 1.5)
    if pfcf is not None and 0 < pfcf <= 15:
        score += 10.0 * (1.0 - pfcf / 15.0)
    if de is not None and de < 0.5:
        score += 5.0
    if de is not None and de > 2.0:
        score -= 5.0
    codes = set(_reason_codes(row))
    if "vault_seed" in codes:
        score += 6.0
    if "pariah_coal_bucket" in codes:
        score += 4.0
    if "cyclical_driller_bucket" in codes:
        score += 3.0
    row["reason_codes"] = sorted(codes)
    row["attention_score"] = round(score, 1)
    return row["attention_score"]


def _fmt_mcap(v) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
        if abs(x) >= 1e12:
            return f"${x/1e12:.2f}T"
        if abs(x) >= 1e9:
            return f"${x/1e9:.2f}B"
        if abs(x) >= 1e6:
            return f"${x/1e6:.0f}M"
        return f"${x:,.0f}"
    except (TypeError, ValueError):
        return "—"


def _write_shortlist_md(vault: Path, rows: list[dict], top_n: int, list_path: Path) -> Path:
    today = date.today().isoformat()
    out_dir = vault / "wiki" / "investing" / "opportunities"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"Daily-Shortlist-{today}.md"

    ranked = sorted(rows, key=lambda r: r.get("attention_score") or 0, reverse=True)
    top = ranked[:top_n]

    lines = [
        "---",
        f"title: Daily Shortlist — Energy Pilot ({today})",
        f"date: {today}",
        "tags:",
        "  - investing",
        "  - shortlist",
        "  - energy",
        "  - screener",
        "  - ai-workflow",
        "status: draft",
        "universe: energy_pilot",
        "---",
        "",
        f"# Daily Shortlist — Energy Pilot ({today})",
        "",
        "> [!abstract] Attention only",
        f"> Ranked from `energy_pilot` ({len(rows)} names). **Not buy recommendations.** "
        "Reason codes allocate research time for filing extracts / thesis health.",
        "",
        f"**Universe file:** `{list_path}`  ",
        f"**Generated:** {datetime.now().isoformat(timespec='seconds')}  ",
        f"**Workflow:** [[AI-Augmented-Screener-Memo]] · sector [[Sector-Energy]]",
        "",
        "## Top research queue",
        "",
        "| Rank | Ticker | Bucket | Attn | 52w gap | 200w MA | P/E | Fwd P/E | P/B | P/FCF | D/E | Mcap | Codes |",
        "|------|--------|--------|------|---------|---------|-----|---------|-----|-------|-----|------|-------|",
    ]
    for i, r in enumerate(top, 1):
        codes = ", ".join(r.get("reason_codes") or [])
        lines.append(
            "| {rank} | **{t}** | {b} | {a} | {g} | {m} | {pe} | {fpe} | {pb} | {pfcf} | {de} | {mc} | {c} |".format(
                rank=i,
                t=r["ticker"],
                b=r.get("bucket") or "—",
                a=r.get("attention_score", "—"),
                g=_fmt_num(r.get("pct_from_52w_low"), 1, "%"),
                m=_fmt_num(r.get("pct_from_200w_ma"), 1, "%"),
                pe=_fmt_num(r.get("pe"), 1),
                fpe=_fmt_num(r.get("forward_pe"), 1),
                pb=_fmt_num(r.get("pb"), 2),
                pfcf=_fmt_num(r.get("p_fcf"), 1),
                de=_fmt_num(r.get("debt_to_equity"), 2),
                mc=_fmt_mcap(r.get("market_cap")),
                c=codes,
            )
        )

    lines.extend([
        "",
        "### Suggested next extracts (human gate)",
        "",
    ])
    for r in top[:5]:
        lines.append(
            f"- [ ] **{r['ticker']}** ({r.get('bucket')}) — codes: "
            f"{', '.join(r.get('reason_codes') or [])}; notes: {r.get('notes') or '—'}"
        )

    lines.extend([
        "",
        "## Full pilot ranking",
        "",
        "| Rank | Ticker | Name | Bucket | Attn | 52w gap | Codes |",
        "|------|--------|------|--------|------|---------|-------|",
    ])
    for i, r in enumerate(ranked, 1):
        lines.append(
            "| {rank} | {t} | {n} | {b} | {a} | {g} | {c} |".format(
                rank=i,
                t=r["ticker"],
                n=r.get("name") or r["ticker"],
                b=r.get("bucket") or "—",
                a=r.get("attention_score", "—"),
                g=_fmt_num(r.get("pct_from_52w_low"), 1, "%"),
                c=", ".join(r.get("reason_codes") or []),
            )
        )

    lines.extend([
        "",
        "## Reason code legend",
        "",
        "| Code | Meaning |",
        "|------|---------|",
        "| near_52w_low | ≤15% above 52-week low |",
        "| within_30pct_52w_low | ≤30% above 52-week low |",
        "| below_200w_ma | Price under ~200-week MA |",
        "| low_pe / low_fwd_pe / low_pb / low_p_fcf | Cheap vs simple multiples |",
        "| low_leverage / high_leverage | D/E screen |",
        "| vault_seed | Already in vault thesis / Sector-Energy |",
        "| pariah_coal_bucket / cyclical_driller_bucket | Capital-cycle focus buckets |",
        "| monitor | No strong quant flag |",
        "",
        "## Related",
        "",
        "- [[AI-Augmented-Screener-Memo]]",
        "- [[Sector-Energy]]",
        "- [[Opportunity-Queue]]",
        "- [[opportunities/OXY]] · [[opportunities/HCC]]",
        "",
    ])
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _fmt_num(v, digits=1, suffix="") -> str:
    x = _safe_float(v)
    if x is None:
        return "—"
    return f"{x:.{digits}f}{suffix}"


def main():
    parser = argparse.ArgumentParser(description="Energy pilot daily shortlist")
    parser.add_argument("--list", type=Path, default=LIST_PATH, help="Path to energy_pilot.csv")
    parser.add_argument("--top", type=int, default=12, help="Top N for research queue table")
    parser.add_argument("--export-vault", action="store_true", help="Write Daily-Shortlist markdown to vault")
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--sleep", type=float, default=0.35, help="Seconds between YF fetches")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON dump path")
    args = parser.parse_args()

    if not args.list.is_file():
        print(f"ERROR: list not found: {args.list}", file=sys.stderr)
        sys.exit(1)

    pilot = _load_pilot(args.list)
    print(f"Loaded {len(pilot)} pilot tickers from {args.list}")

    rows = []
    for i, meta in enumerate(pilot, 1):
        try:
            row = _fetch_row(meta)
            _attention_score(row)
            rows.append(row)
            print(
                f"  [{i}/{len(pilot)}] {row['ticker']}: attn={row['attention_score']} "
                f"52w={row.get('pct_from_52w_low')} pe={row.get('pe')} codes={row.get('reason_codes')}"
            )
        except Exception as e:
            print(f"  [{i}/{len(pilot)}] {meta['ticker']}: FAIL {e}", file=sys.stderr)
            fail = {**meta, "attention_score": 0, "reason_codes": ["fetch_failed"], "error": str(e)}
            rows.append(fail)
        time.sleep(args.sleep)

    ranked = sorted(rows, key=lambda r: r.get("attention_score") or 0, reverse=True)
    print("\n=== TOP", args.top, "===")
    for i, r in enumerate(ranked[: args.top], 1):
        print(
            f"{i:2d}. {r['ticker']:8s} attn={r.get('attention_score', 0):5.1f}  "
            f"52w={_fmt_num(r.get('pct_from_52w_low'), 1, '%'):>8s}  "
            f"{', '.join(r.get('reason_codes') or [])}"
        )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(ranked, indent=2, default=str), encoding="utf-8")
        print(f"JSON: {args.json_out}")

    if args.export_vault:
        v = args.vault or _vault()
        path = _write_shortlist_md(v, rows, args.top, args.list)
        print(f"Vault shortlist: {path}")


if __name__ == "__main__":
    main()
