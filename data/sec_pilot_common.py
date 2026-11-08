"""
Shared SEC EDGAR & US mining pilot utilities (identity, universe YAML, CIK/Company resolution, batch enrichment).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from config.identity import sec_edgar_identity

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = _PROJECT_ROOT / "config" / "us_mining_universe.yaml"
DEFAULT_DB = str(_PROJECT_ROOT / "asx_announcements_cache.db")

SUMMARY_CACHE_MAX_AGE_DAYS = 30
EDGAR_FETCH_SLEEP_DEFAULT = 0.35

_identity_set = False
_universe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def ensure_sec_identity() -> None:
    """Configure `edgartools` SEC User-Agent identity once per process."""
    global _identity_set
    if _identity_set:
        return
    try:
        from edgar import set_identity

        identity = sec_edgar_identity()
        if not identity:
            logger.warning(
                "SEC_EDGAR_IDENTITY / SCREENER_CONTACT_EMAIL not set — EDGAR requests may fail; see .env.example"
            )
            return
        set_identity(identity)
        _identity_set = True
    except Exception as e:
        logger.warning("SEC identity setup failed: %s", e)


def load_mining_universe(
    force: bool = False,
    *,
    universe_path: Path = UNIVERSE_PATH,
    extra_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load `config/us_mining_universe.yaml` with `mtime`-based in-memory caching."""
    defaults: dict[str, Any] = {
        "companies": [],
        "scan_years": 20,
        "fetch_sleep_seconds": EDGAR_FETCH_SLEEP_DEFAULT,
        "description": "",
    }
    if extra_defaults:
        defaults.update(extra_defaults)
    if not universe_path.exists():
        return dict(defaults)
    key = str(universe_path)
    try:
        mtime = universe_path.stat().st_mtime
        if not force and key in _universe_cache and _universe_cache[key][0] == mtime:
            cached = dict(_universe_cache[key][1])
            for k, v in defaults.items():
                cached.setdefault(k, v)
            return cached
        with open(universe_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k, v in defaults.items():
            if k not in raw:
                raw[k] = v
        _universe_cache[key] = (mtime, dict(raw))
        return raw
    except Exception as e:
        logger.warning("Failed to load mining universe: %s", e)
        return dict(defaults)


def norm_cik(cik: Any) -> str:
    if cik is None:
        return ""
    s = re.sub(r"\D", "", str(cik))
    return s.lstrip("0") or "0"


def resolve_edgar_company(entry: dict):
    """Return `edgar.Company` from `entry['cik']` and/or `entry['ticker']`."""
    from edgar import Company

    cik = entry.get("cik")
    ticker = (entry.get("ticker") or "").strip().upper()
    last_err = None
    if cik is not None and str(cik).strip():
        try:
            return Company(int(str(cik).strip()))
        except Exception as e:
            last_err = e
    if ticker:
        try:
            return Company(ticker)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f"Cannot resolve company for {entry}")


def preferred_commodities(entry: dict) -> list[str]:
    """Normalize commodity tags from a mining universe company entry."""
    raw = entry.get("commodities") or []
    out: list[str] = []
    for c in raw:
        s = str(c).strip().lower()
        if not s:
            continue
        if s in ("met", "metallurgical", "coke"):
            s = "coal"
        out.append(s)
    return out


def enrich_stocks_from_summary_map(
    stocks: list,
    list_summaries_fn: Callable[[str], list[dict]],
    attach_fn: Callable[..., Any],
    *,
    db_path: str = DEFAULT_DB,
    strip_ax: bool = True,
) -> list:
    """Batch-load pilot summaries once and attach matching entries in O(1) per stock."""
    if not stocks:
        return stocks
    try:
        by_ticker = {
            (s.get("ticker") or "").strip().upper(): s
            for s in list_summaries_fn(db_path)
            if s.get("ticker")
        }
    except Exception:
        by_ticker = {}
    if not by_ticker:
        return stocks
    for stock in stocks:
        raw_sym = (stock.get("ticker") or "").strip().upper()
        sym = raw_sym.replace(".AX", "") if strip_ax else raw_sym
        summary = by_ticker.get(sym)
        if summary:
            attach_fn(stock, summary=summary, ticker=raw_sym)
    return stocks
