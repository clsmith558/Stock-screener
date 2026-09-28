#!/usr/bin/env python3
"""
Currency / dual-list consistency audit for Signal Screener caches.

Scans business_list_yf_cache (and optional ASX cache) for:
  - quote vs financial currency mismatches (and whether FX conversion ran)
  - suffix-implied currency vs stored quote currency
  - absurd net_cash_pct_mcap (unit/mcap glitches)
  - dual listings present in cache / registry / mining config

Writes docs/CURRENCY_DUAL_LIST_AUDIT.md and prints a summary.

Usage:
  python scripts/audit_currency_dual_list.py
  python scripts/audit_currency_dual_list.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REPORT_PATH = ROOT / "docs" / "CURRENCY_DUAL_LIST_AUDIT.md"

# Known intentional dual / multi listings (product config)
KNOWN_DUALS = [
    {
        "name": "Newmont",
        "tickers": ["NEM", "NEM.AX"],
        "notes": "US primary + ASX CDI; mining pilot us_crossfill NEM.AX <- NEM",
    },
    {
        "name": "Alibaba",
        "tickers": ["BABA", "9988.HK"],
        "notes": "US ADR + HK dual; registry includes both",
    },
    {
        "name": "Tencent",
        "tickers": ["TCEHY", "0700.HK"],
        "notes": "OTC ADR + HK primary",
    },
    {
        "name": "Hermes",
        "tickers": ["RMS.PA", "HESAY"],
        "notes": "Euronext + US OTC; exchange-aware wiki match required",
    },
    {
        "name": "LVMH",
        "tickers": ["MC.PA", "LVMUY"],
        "notes": "Euronext + US OTC",
    },
    {
        "name": "Constellation Software",
        "tickers": ["CSU.TO", "CNSWF"],
        "notes": "TSX + OTC",
    },
]


def _db() -> str:
    from data.asx import DEFAULT_UNIVERSE_DB

    return DEFAULT_UNIVERSE_DB


def _suffix_currency() -> dict[str, str]:
    from data.asx import _SUFFIX_CURRENCY

    return dict(_SUFFIX_CURRENCY)


def _load_business_cache(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT yf_symbol, price, metrics_json FROM business_list_yf_cache"
    ).fetchall()
    out = []
    for sym, price, mj in rows:
        try:
            m = json.loads(mj) if mj else {}
        except Exception:
            m = {}
        out.append(
            {
                "symbol": (sym or "").upper(),
                "price": price,
                "metrics": m,
                "currency": (m.get("currency") or "").upper(),
                "financial_currency": (m.get("financial_currency") or "").upper(),
                "currency_converted": bool(m.get("currency_converted")),
                "fx_to_quote": m.get("fx_to_quote"),
                "net_cash_m": m.get("net_cash_m"),
                "net_cash_pct_mcap": m.get("net_cash_pct_mcap"),
                "market_cap": m.get("market_cap"),
                "cash_on_hand_m": m.get("cash_on_hand_m"),
            }
        )
    return out


def _expected_currency(symbol: str, suffix_map: dict[str, str]) -> str | None:
    su = (symbol or "").upper()
    for sfx, cur in suffix_map.items():
        if su.endswith(sfx):
            return cur
    return None


def _base(symbol: str) -> str:
    su = (symbol or "").upper()
    m = re.match(r"^([A-Z0-9-]+)(\.[A-Z]+)?$", su)
    return m.group(1) if m else su


def audit_rows(rows: list[dict]) -> dict[str, Any]:
    sfx_map = _suffix_currency()
    quote_fin_mismatch = []
    unconverted_mismatch = []
    suffix_mismatch = []
    extreme_net_cash_pct = []
    dual_bases: dict[str, list[str]] = defaultdict(list)

    for r in rows:
        sym = r["symbol"]
        q, f = r["currency"], r["financial_currency"]
        dual_bases[_base(sym)].append(sym)

        if q and f and q != f:
            entry = {
                "symbol": sym,
                "quote": q,
                "financial": f,
                "converted": r["currency_converted"],
                "fx_to_quote": r["fx_to_quote"],
                "net_cash_pct_mcap": r["net_cash_pct_mcap"],
                "market_cap": r["market_cap"],
            }
            quote_fin_mismatch.append(entry)
            if not r["currency_converted"]:
                unconverted_mismatch.append(entry)

        exp = _expected_currency(sym, sfx_map)
        # Home-market suffix should usually match quote ccy; USD ADRs of foreign names
        # without suffix are fine. Flag only when local-suffix quote is clearly wrong.
        if exp and q and q != exp:
            # Common OK cases: none for .AX/.T/.KS — Yahoo should report local ccy
            suffix_mismatch.append(
                {"symbol": sym, "expected": exp, "quote": q, "financial": f}
            )

        ncp = r["net_cash_pct_mcap"]
        mcap = r["market_cap"] or 0
        try:
            ncp_f = float(ncp) if ncp is not None else None
        except (TypeError, ValueError):
            ncp_f = None
        if ncp_f is not None and abs(ncp_f) > 500:
            extreme_net_cash_pct.append(
                {
                    "symbol": sym,
                    "net_cash_pct_mcap": ncp_f,
                    "net_cash_m": r["net_cash_m"],
                    "market_cap": mcap,
                    "currency": q,
                    "financial_currency": f,
                    "converted": r["currency_converted"],
                }
            )

    duals = {
        b: sorted(set(syms))
        for b, syms in dual_bases.items()
        if len(set(syms)) > 1
    }

    # Pair analysis for known duals
    known_status = []
    by_sym = {r["symbol"]: r for r in rows}
    for kd in KNOWN_DUALS:
        present = [t for t in kd["tickers"] if t.upper() in by_sym]
        missing = [t for t in kd["tickers"] if t.upper() not in by_sym]
        detail = []
        for t in present:
            r = by_sym[t.upper()]
            detail.append(
                {
                    "symbol": t.upper(),
                    "currency": r["currency"],
                    "financial_currency": r["financial_currency"],
                    "converted": r["currency_converted"],
                    "mcap": r["market_cap"],
                    "net_cash_pct": r["net_cash_pct_mcap"],
                }
            )
        known_status.append(
            {
                **kd,
                "present": present,
                "missing_from_cache": missing,
                "detail": detail,
            }
        )

    # FX pair frequency
    pair_ctr = Counter(
        (e["quote"], e["financial"]) for e in quote_fin_mismatch
    )

    return {
        "n_rows": len(rows),
        "quote_fin_mismatch_n": len(quote_fin_mismatch),
        "unconverted_mismatch_n": len(unconverted_mismatch),
        "suffix_mismatch_n": len(suffix_mismatch),
        "extreme_net_cash_pct_n": len(extreme_net_cash_pct),
        "dual_bases_n": len(duals),
        "fx_pairs": pair_ctr.most_common(20),
        "unconverted_sample": unconverted_mismatch[:25],
        "suffix_mismatch_sample": suffix_mismatch[:25],
        "extreme_net_cash_sample": sorted(
            extreme_net_cash_pct,
            key=lambda x: -abs(float(x["net_cash_pct_mcap"] or 0)),
        )[:25],
        "known_duals": known_status,
        "dual_bases_sample": dict(list(sorted(duals.items(), key=lambda x: -len(x[1]))[:30])),
    }


def audit_registry() -> dict[str, Any]:
    path = ROOT / "data" / "company_registry.json"
    if not path.exists():
        return {"present": False, "multi_ticker": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    multi = []
    for cid, e in (data.get("companies") or {}).items():
        ticks = [str(t).upper() for t in (e.get("tickers") or [])]
        # normalize int 9988
        ticks = [str(t) for t in ticks]
        if len(set(ticks)) > 1:
            multi.append(
                {
                    "registry_id": cid,
                    "tickers": ticks,
                    "names": e.get("names") or [],
                }
            )
    return {"present": True, "multi_ticker": multi, "n": len(multi)}


def audit_mining_duals() -> list[dict]:
    import yaml

    out: list[dict] = []
    asx_path = ROOT / "config" / "asx_mining_universe.yaml"
    if asx_path.exists():
        cfg = yaml.safe_load(asx_path.read_text(encoding="utf-8")) or {}
        for e in cfg.get("companies") or []:
            if isinstance(e, dict) and e.get("us_crossfill"):
                out.append(
                    {
                        "asx": e.get("yf_symbol") or e.get("ticker"),
                        "us_crossfill": e.get("us_crossfill"),
                        "notes": e.get("notes") or "",
                    }
                )

    cp = ROOT / "config" / "commodity_producers.yaml"
    if cp.exists():
        cfg = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
        nems = [
            p.get("ticker")
            for p in (cfg.get("producers") or [])
            if str(p.get("ticker") or "").upper().startswith("NEM")
        ]
        if nems:
            out.append({"commodity_producers_nem": nems})

    return out


def write_report(audit: dict, registry: dict, mining: list) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Currency / dual-list consistency audit",
        "",
        f"_Generated {now} by `scripts/audit_currency_dual_list.py`._",
        "",
        "## Executive summary",
        "",
        f"| Check | Count |",
        f"|-------|------:|",
        f"| Business YF cache rows | {audit['n_rows']} |",
        f"| Quote ≠ financial currency | {audit['quote_fin_mismatch_n']} |",
        f"| Mismatch **without** FX conversion | {audit['unconverted_mismatch_n']} |",
        f"| Suffix vs quote currency mismatches | {audit['suffix_mismatch_n']} |",
        f"| \\|net_cash % mcap\\| > 500% (suspect units) | {audit['extreme_net_cash_pct_n']} |",
        f"| Dual bases in cache (same root, >1 symbol) | {audit['dual_bases_n']} |",
        f"| Registry multi-ticker companies | {registry.get('n', 0)} |",
        "",
        "## How the screener is designed",
        "",
        "1. **Quote currency** (`info.currency` / exchange suffix): trading price and `marketCap`.",
        "2. **Financial currency** (`info.financialCurrency`): cash, debt, FCF, equity totals when Yahoo reports them separately.",
        "3. **`financial_to_quote_fx`** multiplies BS/CF absolute amounts into quote units before net cash, EV, P/FCF.",
        "4. **Display** uses quote currency labels so net cash % of mcap is meaningful **when FX succeeds**.",
        "5. **Dual listings** are separate Yahoo symbols (e.g. `NEM` vs `NEM.AX`); mining pilot uses explicit `us_crossfill`.",
        "6. **Vault registry** must list **both** exchange tickers for duals; bare-base matching was removed (RMS.AX ≠ RMS.PA).",
        "",
        "## Findings",
        "",
        "### 1. FX conversion coverage",
        "",
    ]
    if audit["unconverted_mismatch_n"] == 0:
        lines.append(
            f"- **Good:** All {audit['quote_fin_mismatch_n']} quote≠financial cases in cache "
            "have `currency_converted=true` (FX path ran)."
        )
    else:
        lines.append(
            f"- **Action:** {audit['unconverted_mismatch_n']} rows have quote≠financial "
            "but conversion false — FX missing or failed."
        )
        for e in audit["unconverted_sample"][:15]:
            lines.append(
                f"  - `{e['symbol']}`: {e['quote']} vs {e['financial']}"
            )
    lines.append("")
    lines.append("Top FX pairs (quote → financial):")
    lines.append("")
    lines.append("| Quote | Financial | N |")
    lines.append("|-------|-----------|--:|")
    for (q, f), n in audit["fx_pairs"][:15]:
        lines.append(f"| {q} | {f} | {n} |")
    lines.append("")

    lines += [
        "### 2. Extreme net cash % of market cap",
        "",
        "Values with \\|net cash / mcap\\| > 500% almost always indicate **tiny/wrong `marketCap`** "
        "or residual unit issues — not a healthy balance sheet. UI should treat these as unreliable.",
        "",
        f"Count: **{audit['extreme_net_cash_pct_n']}**",
        "",
        "| Symbol | net_cash% | net_cash_m | marketCap | quote | fin | FX? |",
        "|--------|----------:|-----------:|----------:|-------|-----|-----|",
    ]
    for e in audit["extreme_net_cash_sample"][:20]:
        lines.append(
            f"| `{e['symbol']}` | {e['net_cash_pct_mcap']:.1f} | {e['net_cash_m']} | "
            f"{e['market_cap']} | {e['currency']} | {e['financial_currency']} | {e['converted']} |"
        )
    lines.append("")

    lines += [
        "### 3. Exchange suffix vs quote currency",
        "",
        f"Strict mismatches: **{audit['suffix_mismatch_n']}** "
        "(local suffix e.g. `.AX`/`.T` should usually match AUD/JPY).",
        "",
    ]
    if audit["suffix_mismatch_sample"]:
        for e in audit["suffix_mismatch_sample"][:15]:
            lines.append(
                f"- `{e['symbol']}`: expected {e['expected']}, quote {e['quote']}"
            )
    else:
        lines.append("- No suffix mismatches in current cache.")
    lines.append("")

    lines += [
        "### 4. Known dual listings",
        "",
        "| Name | Tickers | In cache | Notes |",
        "|------|---------|----------|-------|",
    ]
    for kd in audit["known_duals"]:
        ticks = ", ".join(f"`{t}`" for t in kd["tickers"])
        present = ", ".join(f"`{t}`" for t in kd["present"]) or "—"
        miss = ", ".join(f"`{t}`" for t in kd["missing_from_cache"]) or "—"
        lines.append(
            f"| {kd['name']} | {ticks} | present: {present}; missing: {miss} | {kd['notes']} |"
        )
    lines.append("")
    lines.append("Detail for present legs:")
    lines.append("")
    for kd in audit["known_duals"]:
        if not kd["detail"]:
            continue
        lines.append(f"**{kd['name']}**")
        for d in kd["detail"]:
            lines.append(
                f"- `{d['symbol']}`: quote={d['currency']} fin={d['financial_currency']} "
                f"FX={d['converted']} mcap={d['mcap']} net_cash%={d['net_cash_pct']}"
            )
        lines.append("")

    lines += [
        "### 5. Vault registry multi-ticker rows",
        "",
    ]
    if not registry.get("present"):
        lines.append("_No `data/company_registry.json` in repo (often gitignored)._")
    else:
        lines.append(f"Multi-ticker companies: **{registry.get('n', 0)}**")
        lines.append("")
        for m in registry.get("multi_ticker") or []:
            lines.append(
                f"- `{m['registry_id']}`: {', '.join('`'+t+'`' for t in m['tickers'])} "
                f"— {(m.get('names') or [''])[0]}"
            )
    lines.append("")

    lines += [
        "### 6. Mining / commodity dual config",
        "",
        "```json",
        json.dumps(mining, indent=2, default=str)[:3000],
        "```",
        "",
        "## Recommendations",
        "",
        "1. **Keep** quote/financial FX path as source of truth; unconverted count should stay ~0.",
        "2. **Clamp or null** `net_cash_pct_mcap` when abs > 500% (or mcap below a floor) so dashboard filters are not polluted by penny quotes.",
        "3. **Dual listings:** treat each Yahoo symbol separately for price/mcap; use explicit cross-fill (NEM) for SEC/ASX mining fields — do not merge currencies blindly.",
        "4. **Registry:** always list full exchange symbols for duals; never strip `.AX`/`.PA` on lookup.",
        "5. **Re-run this audit** after large YF rebuilds (`python scripts/audit_currency_dual_list.py`).",
        "6. Optional: surface `currency_converted` badge in detail modal when quote ≠ financial.",
        "",
        "## Per-market expectations",
        "",
        "| Suffix | Typical quote ccy | Notes |",
        "|--------|-------------------|-------|",
        "| (none) US | USD | ADRs may still have foreign financialCurrency |",
        "| `.AX` | AUD | ASX |",
        "| `.T` | JPY | TSE |",
        "| `.KS` / `.KQ` | KRW | KRX |",
        "| `.HK` | HKD | |",
        "| `.L` | GBP | GBp/GBX normalized to GBP for FX |",
        "| `.PA` / `.DE` | EUR | |",
        "| `.TO` | CAD | |",
        "",
    ]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Currency / dual-list consistency audit")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="Do not write markdown report")
    args = parser.parse_args()

    conn = sqlite3.connect(_db())
    rows = _load_business_cache(conn)
    conn.close()

    audit = audit_rows(rows)
    registry = audit_registry()
    mining = audit_mining_duals()

    if not args.no_write:
        write_report(audit, registry, mining)
        print(f"Wrote {REPORT_PATH}")

    print(
        f"rows={audit['n_rows']} quote!=fin={audit['quote_fin_mismatch_n']} "
        f"unconverted={audit['unconverted_mismatch_n']} "
        f"suffix_mis={audit['suffix_mismatch_n']} "
        f"extreme_net_cash%={audit['extreme_net_cash_pct_n']} "
        f"dual_bases={audit['dual_bases_n']}"
    )
    if args.json:
        print(
            json.dumps(
                {"audit": audit, "registry": registry, "mining": mining},
                indent=2,
                default=str,
            )[:50000]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
