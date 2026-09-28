"""
Capital-cycle pariah *candidate* evaluation.

Universe = explicit ticker list (mining/commodity/driller seeds + extras)
OR a tight Yahoo-industry allowlist. Quantitative gates run only on that set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "pariah_industries.yaml"
_config_cache: dict | None = None
_ticker_cache: set[str] | None = None


def load_pariah_config(force: bool = False) -> dict:
    global _config_cache, _ticker_cache
    if _config_cache is not None and not force:
        return _config_cache
    defaults = {
        "industries": [
            "oil & gas drilling",
            "thermal coal",
            "coking coal",
            "steel",
            "shipbuild",
            "marine shipping",
        ],
        "extra_tickers": [],
        "inherit": [],
        "thresholds": {
            "max_pct_from_52w_low": 30,
            "max_pct_from_200w_ma": -20,
            "max_debt_to_equity": 0.25,
            "hard_max_debt_to_equity": 1.0,
            "max_pb": 1.2,
            "min_net_assets_vs_mcap_pct": 0,
            "require_reorg_survivor": False,
            "min_reserve_life_years": None,
            "require_low_cost_quartile": False,
            "max_cost_rank_pct": None,
        },
        "notes": {
            "missing_for_full_thesis": [
                "replacement_cost_vs_market_cap",
                "bankruptcy_or_reorg_history",
                "reserve_life_cost_curve",
                "esg_lending_or_coverage_friction",
            ],
        },
    }
    _ticker_cache = None
    if not _CONFIG_PATH.exists():
        _config_cache = defaults
        return defaults
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k, v in defaults.items():
            if k not in raw:
                raw[k] = v
        th = dict(defaults["thresholds"])
        th.update(raw.get("thresholds") or {})
        raw["thresholds"] = th
        _config_cache = raw
        return raw
    except Exception:
        _config_cache = defaults
        return defaults


def _norm_ticker(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _ticker_keys(ticker: str) -> list[str]:
    s = _norm_ticker(ticker)
    if not s:
        return []
    keys = [s]
    if s.endswith(".AX"):
        keys.append(s[:-3])
    elif "." not in s:
        keys.append(s + ".AX")
    return keys


def _add_ticker(bucket: set[str], raw: Any) -> None:
    s = _norm_ticker(raw)
    if s:
        bucket.add(s)


def _commodities_of(row: dict) -> set[str]:
    return {str(c).strip().lower() for c in (row.get("commodities") or []) if c}


def _load_inherited_tickers(cfg: dict) -> set[str]:
    out: set[str] = set()
    for spec in cfg.get("inherit") or []:
        if isinstance(spec, str):
            spec = {"path": spec}
        if not isinstance(spec, dict):
            continue
        rel = spec.get("path") or ""
        path = _PROJECT_ROOT / rel
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        skip_cmdty = {str(c).lower() for c in (spec.get("skip_commodities") or [])}
        skip_tick = {_norm_ticker(t) for t in (spec.get("skip_tickers") or [])}
        clusters = {str(c) for c in (spec.get("clusters") or [])}

        if data.get("exposures") and clusters:
            for e in data.get("exposures") or []:
                if (e.get("cluster") or "") in clusters:
                    _add_ticker(out, e.get("ticker"))
            continue

        rows = data.get("companies") or data.get("producers") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            t = _norm_ticker(row.get("yf_symbol") or row.get("ticker"))
            if not t:
                continue
            if t in skip_tick or _norm_ticker(row.get("ticker")) in skip_tick:
                continue
            tags = _commodities_of(row)
            if skip_cmdty and tags and tags <= skip_cmdty:
                continue
            if t.endswith(".AX") or "." in t:
                _add_ticker(out, t)
            elif path.name.startswith("asx_"):
                _add_ticker(out, t + ".AX")
            else:
                _add_ticker(out, t)
    for t in cfg.get("extra_tickers") or []:
        _add_ticker(out, t)
    return out


def pariah_universe_tickers(force: bool = False) -> set[str]:
    global _ticker_cache
    cfg = load_pariah_config(force=force)
    if _ticker_cache is not None and not force:
        return _ticker_cache
    _ticker_cache = _load_inherited_tickers(cfg)
    return _ticker_cache


def _industry_blob(stock: dict) -> str:
    m = stock.get("metrics") or {}
    parts = [
        stock.get("industry") or "",
        m.get("industry") or "",
        stock.get("sector") or "",
        m.get("sector") or "",
    ]
    return " ".join(str(p) for p in parts).lower()


def is_pariah_universe(stock: dict) -> tuple[bool, str]:
    """Ticker on the seed list, or Yahoo industry hits the allowlist."""
    cfg = load_pariah_config()
    universe = pariah_universe_tickers()
    t = _norm_ticker(stock.get("ticker"))
    for key in _ticker_keys(t):
        if key in universe:
            return True, f"list:{t}"
    blob = _industry_blob(stock)
    if blob.strip():
        for needle in cfg.get("industries") or []:
            n = str(needle).strip().lower()
            if n and n in blob:
                return True, f"industry:{needle}"
    return False, "not in pariah universe"


def _sf(val, default=None):
    if val is None or val == "":
        return default
    try:
        f = float(val)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default


def evaluate_pariah_candidate(stock: dict, thresholds: dict | None = None) -> dict[str, Any]:
    """
    Return gate results for capital-cycle pariah *candidate* screen.
    pass=True only if all hard gates pass.
    """
    cfg = load_pariah_config()
    th = dict(cfg.get("thresholds") or {})
    if thresholds:
        th.update({k: v for k, v in thresholds.items() if v is not None})

    m = stock.get("metrics") or {}
    gates: dict[str, bool] = {}
    notes: list[str] = []
    details: dict[str, Any] = {}

    if stock.get("_pending_yf"):
        return {
            "pass": False,
            "gates": {},
            "notes": ["pending YF"],
            "label": "candidate",
            "in_universe": False,
            "incomplete": True,
            "missing_for_full_thesis": (cfg.get("notes") or {}).get("missing_for_full_thesis") or [],
        }

    # 1. Universe membership (ticker list or tight industry allowlist)
    ind_ok, ind_why = is_pariah_universe(stock)
    gates["industry"] = ind_ok
    details["industry_match"] = ind_why
    details["in_universe"] = ind_ok
    if not ind_ok:
        notes.append(ind_why)

    # 2. Depressed price: near 52w low OR under 200w MA
    gap = _sf(stock.get("pct_from_52w_low"), 999)
    ma_gap = _sf(stock.get("pct_from_200w_ma"), _sf(m.get("pct_from_200w_ma"), 0))
    max_gap = _sf(th.get("max_pct_from_52w_low"), 30)
    max_ma = _sf(th.get("max_pct_from_200w_ma"), -20)
    near_low = gap is not None and gap <= max_gap
    under_ma = ma_gap is not None and ma_gap <= max_ma
    gates["depressed_price"] = bool(near_low or under_ma)
    details["pct_from_52w_low"] = gap
    details["pct_from_200w_ma"] = ma_gap
    if not gates["depressed_price"]:
        notes.append(f"price not depressed (52w gap={gap}, 200w={ma_gap})")

    # 3. Clean BS
    de = _sf(stock.get("debt_to_equity"), _sf(m.get("debt_to_equity"), 0)) or 0
    net_cash = _sf(stock.get("net_cash_m"), _sf(m.get("net_cash_m"), None))
    max_de = _sf(th.get("max_debt_to_equity"), 0.25)
    hard_de = _sf(th.get("hard_max_debt_to_equity"), 1.0)
    clean = False
    if de > hard_de and (net_cash is None or net_cash < 0):
        clean = False
        notes.append(f"D/E {de} > {hard_de} with net debt")
    elif de > 0 and de <= max_de:
        clean = True
    elif net_cash is not None and net_cash >= 0:
        clean = True
    elif de == 0 and net_cash is None:
        clean = False
        notes.append("leverage unknown")
    else:
        notes.append(f"BS not clean (D/E={de}, net_cash={net_cash})")
    gates["clean_bs"] = clean
    details["debt_to_equity"] = de
    details["net_cash_m"] = net_cash

    # 4. Cheap vs capital stock (weak replacement-cost proxy)
    pb = _sf(stock.get("pb"), _sf(m.get("pb"), 0)) or 0
    na_pct = _sf(
        stock.get("net_assets_vs_mcap_pct"),
        _sf(m.get("net_assets_vs_mcap_pct"), None),
    )
    max_pb = _sf(th.get("max_pb"), 1.2)
    min_na = _sf(th.get("min_net_assets_vs_mcap_pct"), 0)
    cheap_cap = False
    if pb > 0 and pb <= max_pb:
        cheap_cap = True
    if na_pct is not None and na_pct >= min_na:
        cheap_cap = True
    if not cheap_cap:
        if pb <= 0 and na_pct is None:
            notes.append("P/B and net-assets vs mcap missing")
        else:
            notes.append(f"not cheap vs book (P/B={pb}, NA−mcap%={na_pct})")
    gates["cheap_vs_capital"] = cheap_cap
    details["pb"] = pb
    details["net_assets_vs_mcap_pct"] = na_pct

    # Soft signals (phase-1 reorg + phase-2 reserve life + phase-3 unit cost rank)
    reorg = stock.get("reorg") or {}
    reorg_survivor = bool(
        stock.get("reorg_survivor_candidate")
        or reorg.get("survivor_candidate")
    )
    reorg_103 = bool(stock.get("reorg_has_item_103") or reorg.get("has_item_103"))
    reserves = stock.get("reserves") or {}
    life = _sf(
        stock.get("reserve_life_years"),
        _sf(reserves.get("reserve_life_years"), None),
    )
    has_ex96 = bool(stock.get("has_exhibit_96") or reserves.get("has_exhibit_96"))
    costs = stock.get("unit_costs") or {}
    cost_val = _sf(stock.get("unit_cost_value"), _sf(costs.get("cost_value"), None))
    cost_rank = stock.get("unit_cost_rank")
    if cost_rank is None:
        cost_rank = costs.get("cost_rank")
    try:
        cost_rank_i = int(cost_rank) if cost_rank is not None else None
    except (TypeError, ValueError):
        cost_rank_i = None
    cost_rank_pct = _sf(costs.get("cost_rank_pct"), None)
    low_cost_q = bool(
        stock.get("unit_cost_low_quartile") or costs.get("low_cost_quartile")
    )
    soft = {
        "buyback": bool(stock.get("buyback_announced") or (stock.get("annual_buyback_pct") or 0) > 0),
        "insider": int(stock.get("insider_buys_2026") or 0) > 0,
        "deep_asset_discount": bool(na_pct is not None and na_pct >= 25),
        "reorg_survivor": reorg_survivor,
        "reorg_item_103": reorg_103,
        "long_reserve_life": bool(life is not None and life >= 10),
        "has_exhibit_96": has_ex96,
        "low_cost_quartile": low_cost_q,
        "has_unit_cost": cost_val is not None,
    }
    details["reorg"] = {
        "survivor_candidate": reorg_survivor,
        "has_item_103": reorg_103,
        "emerge_date": stock.get("reorg_emerge_date") or reorg.get("emerge_date"),
        "confirmation_date": reorg.get("confirmation_date"),
        "manual_reorg": bool(reorg.get("manual_reorg")),
        "manual_note": reorg.get("manual_note") or "",
    }
    details["reserves"] = {
        "reserve_life_years": life,
        "life_confidence": reserves.get("life_confidence"),
        "reserve_display": reserves.get("reserve_display") or "",
        "production_display": reserves.get("production_display") or "",
        "has_exhibit_96": has_ex96,
        "commodity": reserves.get("reserve_commodity") or "",
    }
    details["unit_costs"] = {
        "cost_value": cost_val,
        "cost_display": costs.get("cost_display") or "",
        "cost_metric": costs.get("cost_metric") or "",
        "cost_commodity": costs.get("cost_commodity") or "",
        "cost_rank": cost_rank_i,
        "peer_count": costs.get("peer_count"),
        "cost_rank_pct": cost_rank_pct,
        "low_cost_quartile": low_cost_q,
    }

    # Optional hard gate: require bankruptcy-cleaned survivor signal
    require_reorg = bool(th.get("require_reorg_survivor"))
    if require_reorg:
        gates["reorg_survivor"] = reorg_survivor
        if not reorg_survivor:
            notes.append("no Item 1.03 confirmation/emergence (or curated successor)")

    # Optional hard gate: minimum reserve life (years)
    min_life = th.get("min_reserve_life_years")
    min_life_f = _sf(min_life, None)
    if min_life_f is not None and min_life_f > 0:
        ok_life = life is not None and life >= min_life_f
        gates["reserve_life"] = bool(ok_life)
        if not ok_life:
            notes.append(
                f"reserve life {life}y < min {min_life_f}y"
                if life is not None
                else "reserve life missing"
            )

    # Optional hard gate: low-cost quartile within pilot peer cohort
    require_low_q = bool(th.get("require_low_cost_quartile"))
    if require_low_q:
        gates["low_cost_quartile"] = low_cost_q
        if not low_cost_q:
            notes.append("not low-cost quartile in pilot peer cohort (or no unit cost)")

    max_rank_pct = _sf(th.get("max_cost_rank_pct"), None)
    if max_rank_pct is not None:
        ok_pct = cost_rank_pct is not None and cost_rank_pct <= max_rank_pct
        gates["cost_rank_pct"] = bool(ok_pct)
        if not ok_pct:
            notes.append(
                f"cost rank pct {cost_rank_pct} > max {max_rank_pct}"
                if cost_rank_pct is not None
                else "unit cost rank missing"
            )

    passed = all(gates.values())
    missing = list((cfg.get("notes") or {}).get("missing_for_full_thesis") or [])
    # Phase-1 covers bankruptcy_or_reorg_history as a soft/optional hard signal
    if reorg_survivor or reorg_103:
        missing = [m for m in missing if m != "bankruptcy_or_reorg_history"]
    # Phase-2 + phase-3: life and pilot cost rank cover parts of reserve_life_cost_curve
    if life is not None or cost_val is not None:
        missing = [m for m in missing if m != "reserve_life_cost_curve"]
    if cost_val is not None:
        missing = [m for m in missing if m != "commodity_price_vs_cash_cost"]

    return {
        "pass": passed,
        "gates": gates,
        "notes": notes,
        "details": details,
        "soft": soft,
        "label": "candidate",
        "in_universe": ind_ok,
        "incomplete": False,
        "missing_for_full_thesis": missing,
    }


def pariah_config_public() -> dict:
    """JSON-serializable config for the dashboard."""
    cfg = load_pariah_config()
    tickers = sorted(pariah_universe_tickers())
    return {
        "tickers": tickers,
        "industries": cfg.get("industries") or [],
        "n_tickers": len(tickers),
        "thresholds": cfg.get("thresholds") or {},
        "missing_for_full_thesis": (cfg.get("notes") or {}).get("missing_for_full_thesis") or [],
        "description": cfg.get("description") or "",
    }
