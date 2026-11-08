"""
Bridge stock screener tickers to Obsidian vault Company-Registry.
Loads data/company_registry.json (exported from vault YAML).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scoring.quant_score import build_peer_value_lists, compute_quant_score

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "company_registry.json"
_TICKER_INDEX: dict[str, dict] | None = None
_REGISTRY_META: dict | None = None

# Yahoo-style exchange suffixes. Do NOT strip these for vault lookup — RMS.AX
# (Ramelius, ASX) is not RMS.PA (Hermès, Paris).
_EXCHANGE_SUFFIX_RE = re.compile(
    r"\.(AX|HK|TO|L|PA|IR|T|DE|SW|SI|KS|KQ|SA|MX|NS|BO|NZ|JO|F|VI)$",
    re.I,
)


def _canonical_ticker(t: str | int | None) -> str:
    """Uppercase ticker; keep exchange suffix (RMS.AX stays RMS.AX)."""
    if t is None or t == "":
        return ""
    return str(t).upper().strip()


def _normalize_ticker(t: str) -> str:
    """
    Legacy helper: base symbol without exchange suffix / warrant class.

    Prefer _canonical_ticker + _index_keys_for_ticker for registry matching.
    Stripping suffixes is unsafe for cross-listing collisions (RMS.AX vs RMS.PA).
    """
    s = _canonical_ticker(t)
    s = _EXCHANGE_SUFFIX_RE.sub("", s)
    if s.endswith("-W"):
        s = s[:-2]
    return s


def _has_exchange_suffix(t: str) -> bool:
    return bool(_EXCHANGE_SUFFIX_RE.search(_canonical_ticker(t)))


def _index_keys_for_ticker(tick: str | int | None) -> list[str]:
    """
    Keys under which a registry ticker may be found.

    - Always index the full symbol (RMS.PA, AMD, CSU.TO).
    - Warrant classes (BABA-W) also index the root (BABA).
    - Never collapse exchange-suffixed symbols to a bare base — that maps
      RMS.AX → Hermès and AMD.AX → US AMD incorrectly.
    """
    full = _canonical_ticker(tick)
    if not full:
        return []
    keys = [full]
    if full.endswith("-W"):
        root = full[:-2]
        if root:
            keys.append(root)
    return keys


def _normalize_name(name: str) -> str:
    """Full normalized company name for exact registry name matching."""
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def load_registry(force: bool = False) -> tuple[dict, dict]:
    global _TICKER_INDEX, _REGISTRY_META
    if _TICKER_INDEX is not None and not force:
        return _TICKER_INDEX, _REGISTRY_META or {}

    index: dict[str, dict] = {}
    meta = {}
    if not _REGISTRY_PATH.exists():
        logger.warning("company_registry.json not found — run scripts/export_registry.py")
        _TICKER_INDEX = index
        _REGISTRY_META = meta
        return index, meta

    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("meta") or {}
        companies = data.get("companies") or {}
        for cid, entry in companies.items():
            entry = dict(entry)
            entry["registry_id"] = cid
            index[str(cid).upper()] = entry
            for tick in entry.get("tickers") or []:
                for key in _index_keys_for_ticker(tick):
                    index[key] = entry
            for name in entry.get("names") or []:
                key = _normalize_name(name)
                if key:
                    index[key] = entry
    except Exception as e:
        logger.warning("Failed to load registry: %s", e)

    _TICKER_INDEX = index
    _REGISTRY_META = meta
    return index, meta


def _vault_name() -> str:
    _, meta = load_registry()
    vault_path = (meta.get("vault_path") or os.environ.get("OBSIDIAN_VAULT_PATH", "")).strip()
    if vault_path:
        return Path(vault_path).name
    return os.environ.get("OBSIDIAN_VAULT_NAME", "").strip()


def build_obsidian_uri(file_rel: str, heading: str | None = None) -> str:
    """Open a vault note in Obsidian (file path without .md extension)."""
    vault = _vault_name()
    file_path = file_rel.replace("\\", "/")
    uri = f"obsidian://open?vault={quote(vault)}&file={quote(file_path)}"
    if heading:
        uri += f"&heading={quote(heading)}"
    return uri


def wiki_links_for_entry(entry: dict | None) -> dict:
    """Obsidian URIs for sector MOC, memo, and power map."""
    if not entry:
        return {}
    wiki = entry.get("wiki") or {}
    links = {}
    sector = wiki.get("sector_moc")
    if sector:
        links["wiki_sector_uri"] = build_obsidian_uri(
            f"wiki/investing/sectors/{sector}", wiki.get("anchor")
        )
    memo = wiki.get("opportunity_memo")
    if memo:
        links["wiki_memo_uri"] = build_obsidian_uri(f"wiki/investing/{memo}")
    power = wiki.get("power_map")
    if power:
        links["wiki_power_uri"] = build_obsidian_uri(f"wiki/investing/{power}")
    return links


def lookup_company(ticker: str, name: str = "") -> dict | None:
    """
    Match screener ticker/name to a vault registry entry.

    Ticker rules (exchange-aware):
      1. Exact full symbol (RMS.PA, AMD, NEM.AX)
      2. Warrant root (BABA-W → BABA) when registered that way
      3. Never strip exchange suffixes for a bare-base fallback
         (RMS.AX must not match Hermès RMS.PA; AMD.AX must not match US AMD)
      4. Exact normalized company name

    Dual listings of the *same* company need both tickers listed in the
    registry (e.g. NEM and NEM.AX).
    """
    index, _ = load_registry()
    full = _canonical_ticker(ticker)
    if full and full in index:
        return index[full]
    if full.endswith("-W"):
        root = full[:-2]
        if root and root in index:
            return index[root]
    # Intentionally no bare-base fallback when ticker has .AX/.PA/etc.
    nkey = _normalize_name(name)
    if nkey and nkey in index:
        return index[nkey]
    return None


def compute_qual_score(entry: dict | None) -> dict:
    """Qual overlay from registry entry."""
    if not entry:
        return {
            "qual_score": 30,
            "coverage_tier": "C",
            "registry_id": None,
            "wiki_sector_moc": None,
            "checklist_flags": {},
            "qual_note": "No vault match — thin coverage",
        }

    tier = (entry.get("coverage_tier") or "C").upper()
    base = float(entry.get("qual_base") or {"A": 75, "B": 60, "C": 35, "D": 20}.get(tier, 35))
    flags = entry.get("checklist_flags") or {}
    qual = base

    if flags.get("leverage") == "fail":
        qual = 0
    elif flags.get("leverage") == "review":
        qual = max(0, qual - 8)
    if flags.get("regulation") == "high":
        qual = max(0, qual - 10)
    elif flags.get("regulation") == "medium":
        qual = max(0, qual - 5)

    wiki = entry.get("wiki") or {}
    qual_payload = {
        "qual_score": round(min(100, qual), 1),
        "coverage_tier": tier,
        "registry_id": entry.get("registry_id"),
        "wiki_sector_moc": wiki.get("sector_moc"),
        "wiki_anchor": wiki.get("anchor"),
        "wiki_power_map": wiki.get("power_map"),
        "opportunity_memo": wiki.get("opportunity_memo"),
        "comparables": entry.get("comparables") or [],
        "checklist_flags": flags,
        "qual_note": entry.get("note"),
    }
    qual_payload.update(wiki_links_for_entry(entry))
    return qual_payload


def enrich_stock_opportunity(
    stock: dict,
    peers: list[dict] | None = None,
    peer_vals: dict | None = None,
    *,
    weights: dict | None = None,
    vault_root_path: Path | None = None,
    existing_memo_syms: set[str] | None = None,
) -> dict:
    """
    Add quant, vault qual metadata, and dashboard Score fields.

    Score (composite_score) is **quantitative only** — equal to quant_score.
    Vault coverage (qual_score / tier) is still attached for links and display
    but no longer scales the Score.
    """
    row = dict(stock)
    quant = compute_quant_score(row, peers=peers or [], weights=weights, peer_vals=peer_vals)
    row.update(quant)

    entry = lookup_company(row.get("ticker", ""), row.get("name", ""))
    qual = compute_qual_score(entry)
    row.update(qual)

    # Score column = pure quant (0–100). Do not multiply by vault qual.
    q = row.get("quant_score")
    row["composite_score"] = round(float(q), 1) if q is not None else None
    try:
        from data.vault_memo import attach_memo_status

        attach_memo_status(
            row,
            vault=vault_root_path,
            existing_syms=existing_memo_syms,
        )
    except Exception:
        pass
    return row


def enrich_stocks_with_opportunities(stocks: list[dict]) -> list[dict]:
    from scoring.quant_score import _load_weights

    load_registry()
    peer_vals = build_peer_value_lists(stocks)
    weights = _load_weights()
    vault_root_path = None
    existing_memo_syms = None
    try:
        from data.vault_memo import list_existing_memo_syms

        vault_root_path, existing_memo_syms = list_existing_memo_syms()
    except Exception:
        pass
    enriched = []
    for s in stocks:
        enriched.append(
            enrich_stock_opportunity(
                s,
                peers=stocks,
                peer_vals=peer_vals,
                weights=weights,
                vault_root_path=vault_root_path,
                existing_memo_syms=existing_memo_syms,
            )
        )
    enriched.sort(key=lambda x: x.get("composite_score") or 0, reverse=True)
    for i, row in enumerate(enriched, 1):
        row["opportunity_rank"] = i
    return enriched


def get_opportunities(
    stocks: list[dict],
    min_quant: float = 0,
    min_composite: float = 0,
    limit: int = 50,
) -> list[dict]:
    enriched = enrich_stocks_with_opportunities(stocks)
    filtered = [
        s for s in enriched
        if (s.get("quant_score") or 0) >= min_quant
        and (s.get("composite_score") or 0) >= min_composite
    ]
    return filtered[:limit]