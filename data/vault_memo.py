"""
Watchlist → vault opportunity-memo loop.

Creates/opens wiki/investing/opportunities/{TICKER}.md and pushes a
screener snapshot (Score, multiples, driver, filing extract) into
frontmatter without rewriting the thesis body.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = Path(r"C:\Obsidian\csmith_test")
MEMO_DIR = Path("wiki") / "investing" / "opportunities"
TEMPLATE_NAME = "_template.md"

# Scalars we own on update. Never touch title/tags/aliases/status/thesis.
SNAPSHOT_KEYS = (
    "quant_score",
    "qual_score",
    "composite_score",
    "coverage_tier",
    "snapshot_at",
    "snapshot_source",
    "price",
    "pe",
    "forward_pe",
    "pb",
    "p_fcf",
    "debt_to_equity",
    "pct_from_52w_low",
    "pct_from_200w_ma",
    "driver_id",
    "driver_last",
    "driver_last_date",
    "driver_unit",
    "production",
    "realized_price",
    "realized_vs_strip",
    "hedge_pct",
)

_FM_SPLIT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
# Yahoo-style symbols only. Rejects slashes, drive letters, and "..".
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,23}$")


class MemoPathError(ValueError):
    """Ticker cannot be mapped to a file inside the opportunities folder."""


def vault_root() -> Path | None:
    raw = (os.environ.get("OBSIDIAN_VAULT_PATH") or "").strip()
    if not raw:
        try:
            from data.wiki_bridge import load_registry

            _, meta = load_registry()
            raw = (meta.get("vault_path") or "").strip()
        except Exception:
            raw = ""
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p
    if DEFAULT_VAULT.is_dir():
        return DEFAULT_VAULT
    return None


def memo_rel(ticker: str) -> str:
    sym = _safe_sym(ticker)
    if not sym:
        return ""
    return (MEMO_DIR / f"{sym}.md").as_posix()


def memo_path(ticker: str, vault: Path | None = None) -> Path | None:
    """Resolved memo file under the opportunities folder.

    Returns None when no vault is configured. Raises MemoPathError when the
    ticker is not a plain symbol or the resolved file would leave that folder.
    """
    root = vault or vault_root()
    if not root:
        return None
    sym = _validated_sym(ticker)
    base = (root / MEMO_DIR).resolve()
    raw = base / f"{sym}.md"
    if raw.is_symlink():
        raise MemoPathError("Refusing to follow a symlink outside the opportunities folder")
    path = raw.resolve()
    if path.name != f"{sym}.md" or path.parent != base:
        raise MemoPathError("Memo path escapes the opportunities folder")
    try:
        path.relative_to(base)
    except ValueError as e:
        raise MemoPathError("Memo path escapes the opportunities folder") from e
    return path


def memo_exists(ticker: str, vault: Path | None = None) -> bool:
    try:
        p = memo_path(ticker, vault)
    except MemoPathError:
        return False
    return bool(p and p.is_file())


def memo_obsidian_uri(ticker: str, vault: Path | None = None) -> str | None:
    if not memo_exists(ticker, vault):
        return None
    root = vault or vault_root()
    vault_name = (root.name if root else "") or os.environ.get("OBSIDIAN_VAULT_NAME", "")
    if not vault_name:
        return None
    rel = memo_rel(ticker)
    file_no_ext = rel[:-3] if rel.endswith(".md") else rel
    return f"obsidian://open?vault={quote(vault_name)}&file={quote(file_no_ext)}"


def memo_status(ticker: str, vault: Path | None = None) -> dict[str, Any]:
    root = vault or vault_root()
    sym = _safe_sym(ticker)
    try:
        p = memo_path(ticker, root) if root and sym else None
    except MemoPathError:
        p = None
    exists = bool(p and p.is_file())
    fm: dict[str, Any] = {}
    if exists and p:
        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("memo frontmatter %s: %s", ticker, e)
    return {
        "ticker": sym or "",
        "vault_ok": bool(root),
        "vault_path": str(root) if root else None,
        "exists": exists,
        "path": str(p) if p else None,
        "rel": memo_rel(ticker) if root else None,
        "uri": memo_obsidian_uri(ticker, root) if exists else None,
        "quant_score": fm.get("quant_score"),
        "composite_score": fm.get("composite_score"),
        "snapshot_at": _as_str(fm.get("snapshot_at") or fm.get("enriched_at")),
        "status": _as_str(fm.get("status")),
    }


def attach_memo_status(stock: dict, vault: Path | None = None) -> dict:
    """Set wiki_memo_uri when the opportunity file exists (registry optional)."""
    if not stock:
        return stock
    st = memo_status(stock.get("ticker") or "", vault)
    stock["vault_memo_exists"] = bool(st.get("exists"))
    stock["vault_ok"] = bool(st.get("vault_ok"))
    if st.get("uri") and not stock.get("wiki_memo_uri"):
        stock["wiki_memo_uri"] = st["uri"]
    if st.get("exists"):
        stock.setdefault("vault_memo_rel", st.get("rel"))
    return stock


def build_snapshot(ticker: str, stock: dict | None = None) -> dict[str, Any]:
    """Assemble a dashboard/enrich snapshot for frontmatter + template fill."""
    row = dict(stock or {})
    sym = _sym(ticker or row.get("ticker"))
    try:
        from data.wiki_bridge import lookup_company

        entry = lookup_company(sym, row.get("name") or "") or {}
    except Exception:
        entry = {}
    wiki = entry.get("wiki") or {}
    driver = row.get("driver") or {}
    operating = driver.get("operating") if isinstance(driver, dict) else None
    if not operating:
        try:
            from data.operating_extract import latest_operating

            operating = latest_operating(sym) or {}
        except Exception:
            operating = {}
    if not driver.get("primary"):
        try:
            from data.supporting_series import build_exposure_row

            driver = build_exposure_row(sym, include_stock=False, include_chart=False) or {}
        except Exception:
            driver = driver or {}

    qc = row.get("quant_components") or {}
    signals = []
    if row.get("buyback_announced"):
        signals.append("buyback")
    if row.get("insider_buys_2026"):
        signals.append(f"insider×{row.get('insider_buys_2026')}")
    if driver.get("display") or driver.get("primary"):
        last = driver.get("last")
        unit = driver.get("unit") or ""
        last_s = f"{last:g}" if isinstance(last, (int, float)) else (str(last) if last is not None else "")
        bit = driver.get("display") or driver.get("primary")
        if last_s:
            bit = f"{bit} {last_s} {unit}".strip()
        signals.append(bit)
    comps = entry.get("comparables") or row.get("comparables") or []
    comps_txt = ", ".join(f"[[opportunities/{c}]]" if "/" not in str(c) and " " not in str(c) else str(c) for c in comps) or "—"

    def _op_val(key: str):
        block = (operating or {}).get(key) if isinstance(operating, dict) else None
        if isinstance(block, dict):
            return block.get("value")
        return None

    return {
        "ticker": sym,
        "name": row.get("name") or entry.get("names", [sym])[0] if entry.get("names") else (row.get("name") or sym),
        "date": date.today().isoformat(),
        "registry_id": entry.get("registry_id") or (wiki.get("opportunity_memo") or "").split("/")[-1] or sym,
        "coverage_tier": row.get("coverage_tier") or entry.get("coverage_tier") or "C",
        "quant_score": _round(row.get("quant_score") if row.get("quant_score") is not None else row.get("composite_score")),
        "qual_score": _round(row.get("qual_score")),
        "composite_score": _round(
            row.get("composite_score") if row.get("composite_score") is not None else row.get("quant_score")
        ),
        "price": _round(row.get("price"), 2),
        "market_cap": row.get("market_cap") or (row.get("metrics") or {}).get("market_cap"),
        "pe": _round(row.get("pe"), 2),
        "forward_pe": _round(row.get("forward_pe"), 2),
        "pb": _round(row.get("pb"), 2),
        "p_fcf": _round(row.get("p_fcf"), 2),
        "debt_to_equity": _round(row.get("debt_to_equity"), 2),
        "pct_from_52w_low": _round(row.get("pct_from_52w_low"), 1),
        "pct_from_200w_ma": _round(row.get("pct_from_200w_ma"), 1),
        "signals_summary": " · ".join(signals) or "screener snapshot",
        "sector_moc": wiki.get("sector_moc") or row.get("wiki_sector_moc") or "Company-Registry",
        "comparables": comps_txt,
        "sector_prefill": "",
        "driver_id": driver.get("primary") or "",
        "driver_display": driver.get("display") or "",
        "driver_last": _round(driver.get("last"), 4),
        "driver_last_date": driver.get("last_date") or "",
        "driver_unit": driver.get("unit") or "",
        "production": _round(_op_val("production"), 4),
        "realized_price": _round(_op_val("realized_price"), 4),
        "realized_vs_strip": _round(
            (operating or {}).get("realized_vs_strip") if isinstance(operating, dict) else None, 4
        ),
        "hedge_pct": _round(_op_val("hedge_pct"), 2),
        "quant_components": qc,
        "source": row.get("snapshot_source") or "screener",
    }


def create_or_update_memo(
    ticker: str,
    stock: dict | None = None,
    *,
    vault: Path | None = None,
    source: str = "screener",
    annual: list[dict] | None = None,
    open_existing: bool = True,
) -> dict[str, Any]:
    """Create the opportunity note if missing; always push the snapshot."""
    root = vault or vault_root()
    if not root:
        return {"ok": False, "error": "OBSIDIAN_VAULT_PATH not set / vault missing"}
    try:
        path = memo_path(ticker, root)
    except MemoPathError as e:
        return {"ok": False, "error": str(e)}
    if path is None:
        return {"ok": False, "error": "Invalid ticker"}
    snap = build_snapshot(ticker, stock)
    snap["source"] = source
    snap["snapshot_source"] = source
    snap["snapshot_at"] = datetime.now().isoformat(timespec="seconds")
    created = False
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_template(root, snap), encoding="utf-8")
        created = True
    elif open_existing:
        text = path.read_text(encoding="utf-8")
        text = upsert_frontmatter(text, _frontmatter_updates(snap))
        text = _fill_path_a_table(text, snap)
        if annual:
            text = _fill_financial_history(text, annual, snap["ticker"])
        text = _append_decision_log(
            text,
            snap["date"],
            "Create memo" if created else ("`/enrich`" if source == "enrich" else "Screener snapshot"),
            _log_notes(snap),
        )
        path.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "created": created,
        "updated": not created,
        "ticker": snap["ticker"],
        "path": str(path),
        "rel": memo_rel(ticker),
        "uri": memo_obsidian_uri(ticker, root),
        "snapshot": {k: snap.get(k) for k in SNAPSHOT_KEYS if snap.get(k) is not None},
    }


def push_enrich_to_memo(
    ticker: str,
    payload: dict,
    *,
    vault: Path | None = None,
) -> dict[str, Any]:
    """Used by enrich_ticker --export-vault."""
    yf = payload.get("yfinance") or {}
    scores = payload.get("scores") or {}
    stock = {
        "ticker": ticker,
        "name": yf.get("name") or ticker,
        "price": yf.get("price"),
        "market_cap": yf.get("market_cap"),
        "pe": yf.get("pe"),
        "forward_pe": yf.get("forward_pe"),
        "pb": yf.get("pb"),
        "p_fcf": yf.get("p_fcf"),
        "debt_to_equity": yf.get("debt_to_equity"),
        "pct_from_52w_low": yf.get("pct_from_52w_low"),
        "pct_from_200w_ma": yf.get("pct_from_200w_ma"),
        "quant_score": scores.get("quant_score"),
        "qual_score": scores.get("qual_score"),
        "composite_score": scores.get("composite_score"),
        "coverage_tier": scores.get("coverage_tier"),
        "snapshot_source": "enrich",
    }
    return create_or_update_memo(
        ticker,
        stock,
        vault=vault,
        source="enrich",
        annual=payload.get("annual") or [],
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _as_str(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _sym(ticker: str) -> str:
    return (ticker or "").strip().upper()


def _validated_sym(ticker: str) -> str:
    sym = _sym(ticker)
    if not _TICKER_RE.fullmatch(sym) or ".." in sym:
        raise MemoPathError(
            "Ticker must be a Yahoo-style symbol (letters, digits, dot, hyphen)"
        )
    return sym


def _safe_sym(ticker: str) -> str | None:
    try:
        return _validated_sym(ticker)
    except MemoPathError:
        return None


def _round(val: Any, nd: int = 1) -> float | None:
    if val is None or val == "":
        return None
    try:
        return round(float(val), nd)
    except (TypeError, ValueError):
        return None


def _yaml_scalar(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return f"{val:.4g}"
    s = str(val)
    if s == "" or re.search(r"[:#\[\]{},&*?|<>=!%@`]", s) or s[:1] in "-?'\"":
        return json.dumps(s, ensure_ascii=False)
    return s


def _parse_frontmatter(text: str) -> dict:
    m = _FM_SPLIT.match(text)
    if not m:
        return {}
    try:
        import yaml

        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def upsert_frontmatter(text: str, updates: dict[str, Any]) -> str:
    """Replace or append scalar keys; leave lists/other keys untouched."""
    clean = {k: v for k, v in updates.items() if v is not None and v != ""}
    if not clean:
        return text
    m = _FM_SPLIT.match(text)
    if not m:
        fm_lines = [f"{k}: {_yaml_scalar(v)}" for k, v in clean.items()]
        return "---\n" + "\n".join(fm_lines) + "\n---\n\n" + text
    fm = m.group(1)
    body = text[m.end() :]
    present: set[str] = set()
    out_lines: list[str] = []
    for line in fm.splitlines():
        km = re.match(r"^([A-Za-z0-9_]+)\s*:", line)
        if km and km.group(1) in clean:
            key = km.group(1)
            out_lines.append(f"{key}: {_yaml_scalar(clean[key])}")
            present.add(key)
        else:
            out_lines.append(line)
    missing = [k for k in SNAPSHOT_KEYS if k in clean and k not in present]
    extra = [k for k in clean if k not in present and k not in missing]
    insert = missing + extra
    if insert:
        if out_lines and out_lines[-1].strip():
            pass
        for k in insert:
            out_lines.append(f"{k}: {_yaml_scalar(clean[k])}")
    return "---\n" + "\n".join(out_lines) + "\n---\n" + body


def _frontmatter_updates(snap: dict) -> dict[str, Any]:
    out = {k: snap.get(k) for k in SNAPSHOT_KEYS}
    if snap.get("source") == "enrich":
        out["enriched_at"] = (snap.get("snapshot_at") or "")[:10]
    return out


def _render_template(vault: Path, snap: dict) -> str:
    tpl_path = vault / MEMO_DIR / TEMPLATE_NAME
    if tpl_path.is_file():
        text = tpl_path.read_text(encoding="utf-8")
    else:
        text = _fallback_template()
    repl = {
        "TICKER": snap["ticker"],
        "DATE": snap["date"],
        "REGISTRY_ID": snap.get("registry_id") or snap["ticker"],
        "TIER": snap.get("coverage_tier") or "C",
        "QUANT_SCORE": _dash(snap.get("quant_score")),
        "QUAL_SCORE": _dash(snap.get("qual_score")),
        "COMPOSITE_SCORE": _dash(snap.get("composite_score")),
        "NAME": snap.get("name") or snap["ticker"],
        "SIGNALS_SUMMARY": snap.get("signals_summary") or "screener snapshot",
        "SECTOR_MOC": snap.get("sector_moc") or "Company-Registry",
        "COMPARABLES": snap.get("comparables") or "—",
        "SECTOR_PREFILL": snap.get("sector_prefill") or "",
    }
    for key, val in repl.items():
        text = text.replace("{{" + key + "}}", str(val))
    text = upsert_frontmatter(text, _frontmatter_updates(snap))
    text = _fill_path_a_table(text, snap)
    return text


def _dash(v: Any) -> str:
    return "—" if v is None else str(v)


def _fill_path_a_table(text: str, snap: dict) -> str:
    mapping = {
        "pct_from_52w_low": snap.get("pct_from_52w_low"),
        "pct_from_200w_ma": snap.get("pct_from_200w_ma"),
        "pb": snap.get("pb"),
        "p_fcf": snap.get("p_fcf"),
        "pe": snap.get("pe"),
        "forward_pe": snap.get("forward_pe"),
        "debt_to_equity": snap.get("debt_to_equity"),
    }

    def repl(m: re.Match) -> str:
        key = m.group(2)
        val = mapping.get(key)
        if val is None:
            return m.group(0)
        return f"{m.group(1)} {val} {m.group(4)}"

    return re.sub(
        r"^(\|[^\n|]*`([a-z0-9_]+)`[^\n|]*\|)([^|\n]*)(\|.*)$",
        repl,
        text,
        flags=re.M,
    )


def _fill_financial_history(text: str, annual: list[dict], ticker: str) -> str:
    if not annual:
        return text
    header = "| FY | Net income | OCF | FCF | Total debt | Cash | Net debt | ROIC |"
    idx = text.find(header)
    if idx < 0:
        return text
    # replace the blank table (header + sep + nothing) with rows
    tail = text[idx:]
    lines = tail.splitlines()
    if len(lines) < 2:
        return text
    keep_head = lines[:2]
    i = 2
    while i < len(lines) and lines[i].startswith("|"):
        i += 1
    rows = []
    for row in annual:
        roic = row.get("roic")
        roic_s = f"{roic:.1f}%" if isinstance(roic, (int, float)) else "—"
        rows.append(
            "| {year} | {ni} | {ocf} | {fcf} | {debt} | {cash} | {net} | {roic} |".format(
                year=row.get("year") or "",
                ni=_money(row.get("net_income")),
                ocf=_money(row.get("ocf")),
                fcf=_money(row.get("fcf")),
                debt=_money(row.get("total_debt")),
                cash=_money(row.get("cash")),
                net=_money(row.get("net_debt")),
                roic=roic_s,
            )
        )
    new_tbl = "\n".join(keep_head + rows)
    rest = "\n".join(lines[i:])
    return text[:idx] + new_tbl + ("\n" + rest if rest else "")


def _money(val: Any) -> str:
    if val is None:
        return "—"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f" ${v:,.0f}"


def _log_notes(snap: dict) -> str:
    bits = []
    if snap.get("composite_score") is not None:
        bits.append(f"Score {snap['composite_score']}")
    if snap.get("driver_id") and snap.get("driver_last") is not None:
        bits.append(f"{snap.get('driver_display') or snap['driver_id']} {snap['driver_last']} {snap.get('driver_unit') or ''}".strip())
    if snap.get("production") is not None:
        bits.append(f"prod {snap['production']}")
    if snap.get("realized_vs_strip") is not None:
        bits.append(f"realized vs strip {snap['realized_vs_strip']}")
    return " · ".join(bits) or "dashboard snapshot"


def _append_decision_log(text: str, day: str, action: str, notes: str) -> str:
    marker = "| Date | Action | Notes |"
    idx = text.find(marker)
    if idx < 0:
        return text.rstrip() + (
            "\n\n## Decision log\n\n"
            "| Date | Action | Notes |\n|------|--------|-------|\n"
            f"| {day} | {action} | {notes} |\n"
        )
    # find the separator line and insert after existing rows
    tail = text[idx:]
    lines = tail.splitlines()
    insert_at = 0
    for i, ln in enumerate(lines):
        if i == 0:
            continue
        if ln.startswith("|"):
            insert_at = i + 1
        elif insert_at:
            break
    row = f"| {day} | {action} | {notes} |"
    last = lines[insert_at - 1] if insert_at else ""
    if last.strip() == row.strip():
        return text
    lines.insert(insert_at, row)
    return text[:idx] + "\n".join(lines)


def _fallback_template() -> str:
    return """---
title: "Opportunity — {{TICKER}}"
date: {{DATE}}
tags:
  - investing
  - opportunity
  - screener
ticker: {{TICKER}}
registry_id: {{REGISTRY_ID}}
coverage_tier: {{TIER}}
investment_type: undecided
quant_score: {{QUANT_SCORE}}
qual_score: {{QUAL_SCORE}}
composite_score: {{COMPOSITE_SCORE}}
status: draft
---

# Opportunity — {{NAME}} ({{TICKER}})

> [!abstract] Screener snapshot ({{DATE}})
> **Quant:** {{QUANT_SCORE}} · **Qual:** {{QUAL_SCORE}} (tier {{TIER}}) · **Composite:** {{COMPOSITE_SCORE}}
> **Signals:** {{SIGNALS_SUMMARY}}

**Wiki:** [[{{SECTOR_MOC}}]] · Comparables: {{COMPARABLES}} · **Checklist:** [[Opportunity-Checklist]]

## Decision log

| Date | Action | Notes |
|------|--------|-------|
| {{DATE}} | Draft created | |
"""
