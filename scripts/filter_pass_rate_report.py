#!/usr/bin/env python3
"""
Read-only Burry / Templeton / Pariah pass-rate report on cached business lists.

Loads each available list with force_yf=False (cache + pending placeholders only),
evaluates gates with current dashboard defaults, and writes
docs/FILTER_PASS_RATE_REPORT.md.

Does not mutate filter defaults or refresh yfinance.

Usage:
  python scripts/filter_pass_rate_report.py
  python scripts/filter_pass_rate_report.py --lists "__builtin__:sp500,Energy businesses list.csv"
  python scripts/filter_pass_rate_report.py --skip-buybacks --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REPORT_PATH = ROOT / "docs" / "FILTER_PASS_RATE_REPORT.md"

# Mirror templates/index.html defaults (do not import from HTML).
BURRY_DEFAULTS = {
    "maxGapFromLow": 15,
    "strict10": False,
    "minPeersForSector": 5,
    "evEbitdaMedianMult": 1.0,
    "maxDebtToEquity": 0.5,
    "maxDebtToMcap": 0.35,
    "hardMaxDebtToEquity": 1.5,
    "maxPfcf": 15,
    "minFcfYield": 5,
    "maxEvFcf": 12,
    "requirePositiveFcf": True,
}

TEMPLETON_DEFAULTS = {
    "maxGapFromLow": 25,
    "maxDebtToEquity": 1.0,
    "maxNetDebtToEquity": 0.8,
    "maxDebtToEbitda": 3.0,
    "maxPb": 1.5,
    "strictPb": False,
    "minPeersForSector": 5,
    "evEbitdaMedianMult": 1.0,
    "requireLeverageData": True,
}


def _sf(val, default=None):
    if val is None or val == "":
        return default
    try:
        f = float(val)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _get_val(stock: dict, key: str):
    if key in stock and stock[key] not in (None, ""):
        return stock[key]
    m = stock.get("metrics") or {}
    return m.get(key)


def median_of(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def burry_metrics(stock: dict) -> dict:
    m = stock.get("metrics") or {}
    mcap = _sf(_get_val(stock, "market_cap"), 0) or 0
    cash_m = _sf(m.get("cash_on_hand_m"), 0) or 0
    net_cash_m = _sf(m.get("net_cash_m"), 0) or 0
    debt_m = cash_m - net_cash_m

    ev_m = _sf(m.get("ev_m"), 0) or 0
    if not ev_m and mcap > 0:
        ev_m = (mcap / 1e6) - cash_m + debt_m

    debt_to_mcap = _sf(m.get("debt_to_mcap"), 0) or 0
    if not debt_to_mcap and mcap > 0 and debt_m > 0:
        debt_to_mcap = (debt_m * 1e6) / mcap

    de = _sf(_get_val(stock, "debt_to_equity"), 0) or 0
    ev_ebitda = _sf(_get_val(stock, "ev_ebitda"), 0) or 0
    pfcf = _sf(_get_val(stock, "p_fcf"), 0) or 0
    fcf_yield = _sf(m.get("fcf_yield"), 0) or 0
    fcf_m = _sf(m.get("fcf_m"), 0) or 0
    ev_fcf = _sf(m.get("ev_fcf"), 0) or 0

    if fcf_m <= 0 and pfcf > 0 and mcap > 0:
        fcf_m = (mcap / pfcf) / 1e6
        fcf_yield = 100.0 / pfcf
    if ev_fcf <= 0 and fcf_m > 0 and ev_m > 0:
        ev_fcf = ev_m / fcf_m

    gap = _sf(stock.get("pct_from_52w_low"), 999)
    return {
        "gap": gap if gap is not None else 999,
        "ev_m": ev_m,
        "ev_ebitda": ev_ebitda,
        "pfcf": pfcf,
        "fcf_m": fcf_m,
        "fcf_yield": fcf_yield,
        "ev_fcf": ev_fcf,
        "de": de,
        "debt_to_mcap": debt_to_mcap,
        "net_cash_m": net_cash_m,
        "sector": (stock.get("sector") or "Unknown"),
        "pending": bool(stock.get("_pending_yf")),
    }


def templeton_metrics(stock: dict) -> dict:
    bm = burry_metrics(stock)
    m = stock.get("metrics") or {}
    net_de = _sf(m.get("net_debt_to_equity"), None)
    if net_de is None or net_de == 0:
        net_assets_m = _sf(m.get("net_assets_m"), 0) or 0
        net_debt_m = _sf(m.get("net_debt_m"), None)
        nd = net_debt_m if net_debt_m is not None else -bm["net_cash_m"]
        if net_assets_m > 0:
            net_de = nd / net_assets_m
        else:
            mcap = _sf(_get_val(stock, "market_cap"), 0) or 0
            pb = _sf(_get_val(stock, "pb"), 0) or 0
            if mcap > 0 and pb > 0:
                net_de = (nd * 1e6) / (mcap / pb)

    debt_to_ebitda = _sf(m.get("debt_to_ebitda"), None)
    if not debt_to_ebitda or debt_to_ebitda <= 0:
        if bm["ev_m"] > 0 and bm["ev_ebitda"] > 0:
            cash_m = _sf(m.get("cash_on_hand_m"), 0) or 0
            debt_m = cash_m - bm["net_cash_m"]
            ebitda_m = bm["ev_m"] / bm["ev_ebitda"]
            if ebitda_m > 0 and debt_m > 0:
                debt_to_ebitda = debt_m / ebitda_m

    pb = _sf(_get_val(stock, "pb"), 0) or 0
    return {
        **bm,
        "net_de": net_de,
        "debt_to_ebitda": debt_to_ebitda if debt_to_ebitda and debt_to_ebitda > 0 else None,
        "pb": pb,
    }


def build_peer_stats(stocks: list[dict]) -> dict:
    by_sector: dict[str, list[float]] = {}
    all_vals: list[float] = []
    for s in stocks:
        if s.get("_pending_yf"):
            continue
        bm = burry_metrics(s)
        if not (bm["ev_ebitda"] > 0):
            continue
        sec = bm["sector"] or "Unknown"
        by_sector.setdefault(sec, []).append(bm["ev_ebitda"])
        all_vals.append(bm["ev_ebitda"])
    sector_medians = {sec: median_of(vs) for sec, vs in by_sector.items()}
    sector_counts = {sec: len(vs) for sec, vs in by_sector.items()}
    return {
        "sectorMedians": sector_medians,
        "sectorCounts": sector_counts,
        "globalMedian": median_of(all_vals),
        "globalCount": len(all_vals),
    }


def evaluate_burry(stock: dict, peer_stats: dict, opts: dict | None = None) -> dict:
    opts = {**BURRY_DEFAULTS, **(opts or {})}
    bm = burry_metrics(stock)
    gates = {"nearLow": False, "fcf": False, "cheap": False, "debt": False, "evEbitda": False}
    notes: list[str] = []
    if bm["pending"]:
        return {"pass": False, "gates": gates, "notes": ["pending YF"], "incomplete": True}

    max_gap = 10 if opts.get("strict10") else opts["maxGapFromLow"]
    gates["nearLow"] = bm["gap"] <= max_gap
    if not gates["nearLow"]:
        notes.append(f"52w gap {bm['gap']:.1f}% > {max_gap}%")

    has_fcf = bm["fcf_m"] > 0 or bm["pfcf"] > 0 or bm["fcf_yield"] > 0
    gates["fcf"] = has_fcf if opts["requirePositiveFcf"] else True
    if not gates["fcf"]:
        notes.append("no FCF")

    gates["cheap"] = (
        (bm["pfcf"] > 0 and bm["pfcf"] <= opts["maxPfcf"])
        or (bm["fcf_yield"] >= opts["minFcfYield"])
        or (bm["ev_fcf"] > 0 and bm["ev_fcf"] <= opts["maxEvFcf"])
    )
    if has_fcf and not gates["cheap"]:
        notes.append("FCF not cheap enough")
    if not has_fcf and opts["requirePositiveFcf"]:
        gates["cheap"] = False

    de_ok = bm["de"] > 0 and bm["de"] <= opts["maxDebtToEquity"]
    net_cash_ok = bm["net_cash_m"] >= 0
    debt_mcap_ok = bm["debt_to_mcap"] > 0 and bm["debt_to_mcap"] <= opts["maxDebtToMcap"]
    unknown_debt = (
        not (bm["de"] > 0)
        and not (bm["debt_to_mcap"] > 0)
        and bm["net_cash_m"] == 0
        and not (bm["ev_m"] > 0)
    )
    extreme_de = bm["de"] > opts["hardMaxDebtToEquity"]
    if extreme_de and bm["net_cash_m"] < 0:
        gates["debt"] = False
        notes.append(f"D/E {bm['de']} too high")
    elif de_ok or net_cash_ok or debt_mcap_ok:
        gates["debt"] = True
    elif unknown_debt:
        gates["debt"] = False
        notes.append("debt unknown")
    else:
        gates["debt"] = False
        notes.append("debt not minimal")

    sec = bm["sector"] or "Unknown"
    sec_count = (peer_stats.get("sectorCounts") or {}).get(sec, 0)
    min_peers = opts["minPeersForSector"]
    if sec_count >= min_peers:
        median = (peer_stats.get("sectorMedians") or {}).get(sec)
    else:
        median = peer_stats.get("globalMedian")
    mult = opts["evEbitdaMedianMult"]
    if not (bm["ev_ebitda"] > 0):
        gates["evEbitda"] = False
        notes.append("no EV/EBITDA")
    elif median is None or not (median > 0):
        gates["evEbitda"] = False
        notes.append("no peer EV/EBITDA")
    else:
        cap = median * mult
        gates["evEbitda"] = bm["ev_ebitda"] <= cap
        if not gates["evEbitda"]:
            notes.append(f"EV/EBITDA {bm['ev_ebitda']} > {cap:.1f}")

    passed = all(gates.values())
    return {"pass": passed, "gates": gates, "notes": notes, "incomplete": False}


def evaluate_templeton(stock: dict, peer_stats: dict, opts: dict | None = None) -> dict:
    opts = {**TEMPLETON_DEFAULTS, **(opts or {})}
    tm = templeton_metrics(stock)
    gates = {
        "nearLow": False,
        "de": False,
        "netDe": True,
        "debtEbitda": False,
        "evEbitda": False,
        "pb": False,
    }
    notes: list[str] = []
    if tm["pending"]:
        return {"pass": False, "gates": gates, "notes": ["pending YF"], "incomplete": True}

    max_gap = opts["maxGapFromLow"]
    gates["nearLow"] = tm["gap"] <= max_gap
    if not gates["nearLow"]:
        notes.append(f"52w gap {tm['gap']:.1f}% > {max_gap}%")

    max_de = opts["maxDebtToEquity"]
    if tm["de"] > 0:
        gates["de"] = tm["de"] <= max_de
        if not gates["de"]:
            notes.append(f"D/E {tm['de']} > {max_de}")
    elif opts["requireLeverageData"]:
        gates["de"] = tm["net_cash_m"] >= 0
        if not gates["de"]:
            notes.append("D/E missing")
    else:
        gates["de"] = True

    if tm["net_de"] is not None:
        gates["netDe"] = tm["net_de"] <= opts["maxNetDebtToEquity"]
        if not gates["netDe"]:
            notes.append(f"net D/E {tm['net_de']:.2f}")

    max_dte = opts["maxDebtToEbitda"]
    if tm["debt_to_ebitda"] is not None and tm["debt_to_ebitda"] > 0:
        gates["debtEbitda"] = tm["debt_to_ebitda"] <= max_dte
        if not gates["debtEbitda"]:
            notes.append(f"debt/EBITDA {tm['debt_to_ebitda']:.1f}")
    elif tm["de"] > 0.5 or tm["net_cash_m"] < 0:
        gates["debtEbitda"] = not opts["requireLeverageData"]
        if not gates["debtEbitda"]:
            notes.append("debt/EBITDA unknown")
    else:
        gates["debtEbitda"] = True

    sec = tm["sector"] or "Unknown"
    sec_count = (peer_stats.get("sectorCounts") or {}).get(sec, 0)
    if sec_count >= opts["minPeersForSector"]:
        median = (peer_stats.get("sectorMedians") or {}).get(sec)
    else:
        median = peer_stats.get("globalMedian")
    mult = opts["evEbitdaMedianMult"]
    if not (tm["ev_ebitda"] > 0):
        gates["evEbitda"] = False
        notes.append("no EV/EBITDA")
    elif median is None or not (median > 0):
        gates["evEbitda"] = False
        notes.append("no peer EV/EBITDA")
    else:
        gates["evEbitda"] = tm["ev_ebitda"] <= median * mult
        if not gates["evEbitda"]:
            notes.append(f"EV/EBITDA {tm['ev_ebitda']} > {median * mult:.1f}")

    max_pb = 1.0 if opts.get("strictPb") else opts["maxPb"]
    if tm["pb"] > 0:
        gates["pb"] = tm["pb"] <= max_pb
        if not gates["pb"]:
            notes.append(f"P/B {tm['pb']} > {max_pb}")
    else:
        gates["pb"] = not opts["requireLeverageData"]
        if not gates["pb"]:
            notes.append("P/B missing")

    passed = all(gates.values())
    return {"pass": passed, "gates": gates, "notes": notes, "incomplete": False}


def summarize_gates(
    results: list[dict],
    gate_names: list[str],
) -> dict[str, Any]:
    n = len(results)
    complete = [r for r in results if not r.get("incomplete")]
    incomplete = n - len(complete)
    passed = sum(1 for r in complete if r.get("pass"))
    fail_hist: Counter[str] = Counter()
    gate_fail: Counter[str] = Counter()
    gate_pass: Counter[str] = Counter()
    for r in complete:
        gates = r.get("gates") or {}
        for g in gate_names:
            if gates.get(g):
                gate_pass[g] += 1
            else:
                gate_fail[g] += 1
        if not r.get("pass"):
            failed = [g for g in gate_names if not gates.get(g)]
            if failed:
                fail_hist[",".join(failed)] += 1
            else:
                fail_hist["(none?)"] += 1
    base = len(complete) or 1
    return {
        "n": n,
        "complete": len(complete),
        "incomplete": incomplete,
        "passed": passed,
        "pass_rate_complete_pct": round(100.0 * passed / base, 2) if complete else 0.0,
        "pass_rate_all_pct": round(100.0 * passed / n, 2) if n else 0.0,
        "gate_pass": dict(gate_pass),
        "gate_fail": dict(gate_fail),
        "gate_pass_pct": {g: round(100.0 * gate_pass[g] / base, 1) for g in gate_names} if complete else {},
        "top_fail_combos": fail_hist.most_common(12),
    }


def analyze_list(list_id: str, display_name: str, stocks: list[dict]) -> dict:
    from data.pariah_filter import evaluate_pariah_candidate

    peer = build_peer_stats(stocks)
    burry_results = [evaluate_burry(s, peer) for s in stocks]
    templeton_results = [evaluate_templeton(s, peer) for s in stocks]
    pariah_results = [evaluate_pariah_candidate(s) for s in stocks]

    burry_gates = ["nearLow", "fcf", "cheap", "debt", "evEbitda"]
    templeton_gates = ["nearLow", "de", "netDe", "debtEbitda", "evEbitda", "pb"]
    # Collect pariah gate names from complete rows
    pariah_gate_names: list[str] = []
    for r in pariah_results:
        if r.get("gates") and not r.get("incomplete"):
            for k in r["gates"]:
                if k not in pariah_gate_names:
                    pariah_gate_names.append(k)
    if not pariah_gate_names:
        pariah_gate_names = ["industry", "depressed_price", "clean_bs", "cheap_vs_capital"]

    return {
        "list_id": list_id,
        "display_name": display_name,
        "n_stocks": len(stocks),
        "pending_yf": sum(1 for s in stocks if s.get("_pending_yf")),
        "peer_global_median_ev_ebitda": peer.get("globalMedian"),
        "peer_ev_ebitda_count": peer.get("globalCount"),
        "burry": summarize_gates(burry_results, burry_gates),
        "templeton": summarize_gates(templeton_results, templeton_gates),
        "pariah": summarize_gates(pariah_results, pariah_gate_names),
        "pariah_gate_names": pariah_gate_names,
        "sample_passers": {
            "burry": [
                s.get("ticker")
                for s, r in zip(stocks, burry_results)
                if r.get("pass")
            ][:15],
            "templeton": [
                s.get("ticker")
                for s, r in zip(stocks, templeton_results)
                if r.get("pass")
            ][:15],
            "pariah": [
                s.get("ticker")
                for s, r in zip(stocks, pariah_results)
                if r.get("pass")
            ][:15],
        },
    }


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.2f}%"


def _fmt_median(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.1f}"


def render_markdown(results: list[dict], generated_at: str) -> str:
    lines: list[str] = []
    lines.append("# Filter pass-rate report (cached lists)")
    lines.append("")
    lines.append(f"Generated: **{generated_at}** (UTC).")
    lines.append("")
    lines.append("Read-only calibration: stocks loaded via `fetch_business_list_stocks(..., force_yf=False)`.")
    lines.append("No filter defaults were changed. Gates mirror `templates/index.html` defaults and `data/pariah_filter.py`.")
    lines.append("")
    lines.append("## Defaults used")
    lines.append("")
    lines.append("| Screen | Key defaults |")
    lines.append("|--------|--------------|")
    lines.append(
        "| **Burry** | max 52w gap 15%; require positive FCF; cheap if P/FCF≤15 or FCF yield≥5% or EV/FCF≤12; "
        "D/E≤0.5 or net cash or debt/mcap≤0.35; EV/EBITDA ≤ sector median (min 5 peers) else list median |"
    )
    lines.append(
        "| **Templeton** | max 52w gap 25%; D/E≤1.0; net D/E≤0.8; debt/EBITDA≤3 (est.); P/B≤1.5; "
        "EV/EBITDA ≤ peer median; require leverage data |"
    )
    lines.append(
        "| **Pariah** | industry/keyword membership; depressed (52w gap≤30% **or** 200w MA≤−20%); "
        "clean BS (D/E≤0.25 or net cash; hard D/E 1.0); cheap vs capital (P/B≤1.2 or net-assets vs mcap) |"
    )
    lines.append("")
    lines.append("**Pass rate (complete)** = passers / non-pending rows. **Pass rate (all)** = passers / full list (pending count as fail).")
    lines.append("")
    lines.append("## Summary table")
    lines.append("")
    lines.append(
        "| List | N | Pending | Burry % (complete) | Templeton % | Pariah % | "
        "List EV/EBITDA med |"
    )
    lines.append("|------|---:|--------:|-------------------:|------------:|---------:|-------------------:|")
    for r in results:
        b, t, p = r["burry"], r["templeton"], r["pariah"]
        lines.append(
            f"| {r['display_name']} | {r['n_stocks']} | {r['pending_yf']} | "
            f"{_pct(b['pass_rate_complete_pct'])} ({b['passed']}/{b['complete']}) | "
            f"{_pct(t['pass_rate_complete_pct'])} ({t['passed']}/{t['complete']}) | "
            f"{_pct(p['pass_rate_complete_pct'])} ({p['passed']}/{p['complete']}) | "
            f"{_fmt_median(r.get('peer_global_median_ev_ebitda'))} |"
        )
    lines.append("")

    # Aggregate gate fail rates across all complete rows (weighted by list size)
    lines.append("## Per-list detail")
    lines.append("")
    for r in results:
        lines.append(f"### {r['display_name']}")
        lines.append("")
        lines.append(f"- `list_id`: `{r['list_id']}`")
        lines.append(f"- Universe loaded: **{r['n_stocks']}** (pending YF: **{r['pending_yf']}**)")
        lines.append(
            f"- Peer EV/EBITDA: median **{_fmt_median(r.get('peer_global_median_ev_ebitda'))}** "
            f"over {r.get('peer_ev_ebitda_count') or 0} names with positive multiple"
        )
        lines.append("")
        for label, key, gates in (
            ("Burry", "burry", ["nearLow", "fcf", "cheap", "debt", "evEbitda"]),
            ("Templeton", "templeton", ["nearLow", "de", "netDe", "debtEbitda", "evEbitda", "pb"]),
            ("Pariah", "pariah", r.get("pariah_gate_names") or ["industry", "depressed_price", "clean_bs", "cheap_vs_capital"]),
        ):
            s = r[key]
            lines.append(f"#### {label}")
            lines.append("")
            lines.append(
                f"- Pass: **{s['passed']}** / {s['complete']} complete "
                f"(**{_pct(s['pass_rate_complete_pct'])}**); "
                f"all-list rate {_pct(s['pass_rate_all_pct'])} including {s['incomplete']} incomplete"
            )
            gp = s.get("gate_pass_pct") or {}
            if gp:
                lines.append("- Gate pass rates (among complete):")
                lines.append("")
                lines.append("| Gate | Pass % | Fail count |")
                lines.append("|------|-------:|-----------:|")
                gf = s.get("gate_fail") or {}
                for g in gates:
                    lines.append(f"| `{g}` | {gp.get(g, 0):.1f}% | {gf.get(g, 0)} |")
                lines.append("")
            combos = s.get("top_fail_combos") or []
            if combos:
                lines.append("- Top fail gate combinations (failed gates only):")
                lines.append("")
                for combo, cnt in combos[:8]:
                    lines.append(f"  - `{combo}`: {cnt}")
                lines.append("")
            samples = (r.get("sample_passers") or {}).get(key) or []
            if samples:
                lines.append(f"- Sample passers: {', '.join(samples)}")
            elif s["passed"] == 0:
                lines.append("- Sample passers: *(none)*")
            lines.append("")

    lines.append("## Interpretation notes")
    lines.append("")
    lines.append(
        "- **Peer EV/EBITDA** is computed **within each list** (same as the dashboard). "
        "Sector median requires ≥5 peers with EV/EBITDA; otherwise the list global median is used."
    )
    lines.append(
        "- **Pending YF** rows fail closed (incomplete) for all three screens — high pending "
        "deflates all-list pass rates."
    )
    lines.append(
        "- **Pariah** is intentionally narrow (capital-cycle sector + clean BS + cheap book). "
        "Expect low pass rates on broad equity indices."
    )
    lines.append(
        "- **Burry** is often bottlenecked by near-low + FCF cheapness + EV/EBITDA together; "
        "check per-gate tables before retuning a single threshold."
    )
    lines.append(
        "- This report does **not** save UI presets or change defaults. Retune only after reviewing fail combos."
    )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/filter_pass_rate_report.py")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only filter pass-rate report on cached lists")
    parser.add_argument(
        "--lists",
        default="",
        help="Comma-separated list_ids to include (default: all available except optional skips)",
    )
    parser.add_argument(
        "--skip-buybacks",
        action="store_true",
        help="Skip __builtin__:buybacks (large signal overlay list)",
    )
    parser.add_argument(
        "--skip-dup-japan",
        action="store_true",
        default=True,
        help="Skip japan_stocks.csv.csv if japan_stocks.csv present (default on)",
    )
    parser.add_argument("--no-skip-dup-japan", action="store_true")
    parser.add_argument("--json", action="store_true", help="Also print full JSON to stdout")
    parser.add_argument(
        "--out",
        default=str(REPORT_PATH),
        help=f"Markdown output path (default: {REPORT_PATH})",
    )
    args = parser.parse_args()

    from data.business_lists import (
        BUILTIN_BUYBACKS_LIST_ID,
        fetch_business_list_stocks,
        get_available_lists,
    )

    available = get_available_lists()
    if not available:
        print("No business lists registered.", file=sys.stderr)
        return 1

    want = {x.strip() for x in args.lists.split(",") if x.strip()} if args.lists else None
    skip_dup = args.skip_dup_japan and not args.no_skip_dup_japan
    list_ids_seen = {a.get("list_id") for a in available}

    targets: list[tuple[str, str]] = []
    for a in available:
        lid = a.get("list_id") or ""
        name = a.get("display_name") or lid
        if want is not None and lid not in want:
            continue
        if args.skip_buybacks and lid == BUILTIN_BUYBACKS_LIST_ID:
            continue
        if skip_dup and lid == "japan_stocks.csv.csv" and "japan_stocks.csv" in list_ids_seen:
            continue
        targets.append((lid, name))

    if not targets:
        print("No lists matched selection.", file=sys.stderr)
        return 1

    print(f"Evaluating {len(targets)} lists (cache only, force_yf=False)...")
    results: list[dict] = []
    for i, (lid, name) in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] {name} ({lid}) ...", flush=True)
        try:
            stocks = fetch_business_list_stocks(lid, force_yf=False, signals_only=False)
        except Exception as e:
            print(f"    ERROR loading: {e}", file=sys.stderr)
            results.append(
                {
                    "list_id": lid,
                    "display_name": name,
                    "n_stocks": 0,
                    "pending_yf": 0,
                    "error": str(e),
                    "burry": summarize_gates([], ["nearLow", "fcf", "cheap", "debt", "evEbitda"]),
                    "templeton": summarize_gates(
                        [], ["nearLow", "de", "netDe", "debtEbitda", "evEbitda", "pb"]
                    ),
                    "pariah": summarize_gates(
                        [], ["industry", "depressed_price", "clean_bs", "cheap_vs_capital"]
                    ),
                    "pariah_gate_names": [
                        "industry",
                        "depressed_price",
                        "clean_bs",
                        "cheap_vs_capital",
                    ],
                    "sample_passers": {"burry": [], "templeton": [], "pariah": []},
                }
            )
            continue
        analysis = analyze_list(lid, name, stocks)
        results.append(analysis)
        b, t, p = analysis["burry"], analysis["templeton"], analysis["pariah"]
        print(
            f"    n={analysis['n_stocks']} pending={analysis['pending_yf']} "
            f"Burry={b['passed']}/{b['complete']} "
            f"Templeton={t['passed']}/{t['complete']} "
            f"Pariah={p['passed']}/{p['complete']}",
            flush=True,
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    md = render_markdown(results, generated_at)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\nWrote {out_path}")

    # Console summary
    print("\n=== Pass rates (complete rows) ===")
    print(f"{'List':<40} {'N':>6} {'Burry':>10} {'Templeton':>10} {'Pariah':>10}")
    for r in results:
        print(
            f"{r['display_name'][:40]:<40} {r['n_stocks']:>6} "
            f"{r['burry']['pass_rate_complete_pct']:>9.2f}% "
            f"{r['templeton']['pass_rate_complete_pct']:>9.2f}% "
            f"{r['pariah']['pass_rate_complete_pct']:>9.2f}%"
        )

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
