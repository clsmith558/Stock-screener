#!/usr/bin/env python3
"""
Multi-Market Signal Screener (ASX + US)
Personal local dashboard — bind to loopback by default.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv(Path(__file__).resolve().parent / ".env")

from data.asx import fetch_asx_stocks, get_asx_meta, update_asx_company_list
from data.us import fetch_us_stocks

logger = logging.getLogger(__name__)
app = Flask(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "all", "on"})
# Sent when SCREENER_API_TOKEN is unset. The value is not secret: cross-site
# pages cannot set a custom header without a CORS preflight, which this app
# does not answer. Must match LOCAL_MUTATION_TOKEN in templates/index.html.
_LOCAL_MUTATION_HEADER = "local"


def _bool_arg(name: str, default: bool = False) -> bool:
    raw = request.args.get(name)
    if raw is None:
        return default
    return str(raw).lower() in _TRUE_VALUES


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_arg(name: str, default: float = 0.0) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _is_loopback() -> bool:
    addr = (request.remote_addr or "").strip().lower()
    return addr in ("127.0.0.1", "::1", "localhost")


def _run_background(fn, *args, **kwargs):
    def _bg():
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Background job failed: %s", getattr(fn, "__name__", fn))

    threading.Thread(target=_bg, daemon=True).start()


def _json_error(message: str, code: int = 500, **extra):
    payload = {"status": "error", "error": message, **extra}
    return jsonify(payload), code


def _json_ok(message: str = "ok", **extra):
    payload = {"status": "ok", "message": message, **extra}
    return jsonify(payload)


@app.before_request
def _protect_mutating_routes():
    """Personal screener: mutating calls must come from this dashboard.

    A custom header blocks cross-site form posts and cross-origin fetch.
    Query-string tokens are ignored so the secret is not written to logs.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None

    provided = (request.headers.get("X-Screener-Token") or "").strip()
    token = os.environ.get("SCREENER_API_TOKEN", "").strip()
    expected = token or _LOCAL_MUTATION_HEADER
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({
            "error": "Unauthorized — provide X-Screener-Token header",
        }), 401

    allow_remote = os.environ.get("SCREENER_ALLOW_REMOTE", "").strip().lower() in _TRUE_VALUES
    if not allow_remote and not _is_loopback():
        return jsonify({
            "error": "Mutating API only allowed from localhost "
                     "(set SCREENER_ALLOW_REMOTE=1 only if you understand the risk)",
        }), 403
    return None


def get_stocks_for_market(
    market: str,
    full: bool = False,
    force_yf: bool = False,
    list_id: str = None,
    signals_only: bool = False,
    fast: bool = False,
):
    from data.watchlist import annotate_on_watchlist
    from data.yf_helpers import sanitize_stocks

    market = (market or "asx").lower()
    if market == "watchlist":
        from data.watchlist import fetch_watchlist_stocks

        stocks = fetch_watchlist_stocks()
    elif market == "pitches":
        from data.pitch_ledger import fetch_pitched_stocks

        stocks = fetch_pitched_stocks()
    elif market in ("business", "buybacks"):
        from data.business_lists import BUILTIN_BUYBACKS_LIST_ID, fetch_business_list_stocks
        lid = list_id or (BUILTIN_BUYBACKS_LIST_ID if market == "buybacks" else "")
        stocks = fetch_business_list_stocks(
            lid, force_yf=force_yf, signals_only=signals_only, fast=fast
        )
    elif market == "us":
        stocks = fetch_us_stocks(force_yf=force_yf)
    else:
        stocks = fetch_asx_stocks(full=full, force_yf=force_yf)
    stocks = sanitize_stocks(stocks)
    stocks = annotate_on_watchlist(stocks)
    try:
        from data.business_lists import BUILTIN_PARIAH_LIST_ID
        from data.supporting_series import annotate_stocks_with_drivers, list_id_is_energy_pilot

        if (not fast) and (
            market == "watchlist"
            or (
                market == "business"
                and (list_id_is_energy_pilot(list_id) or list_id == BUILTIN_PARIAH_LIST_ID)
            )
        ):
            stocks = annotate_stocks_with_drivers(stocks)
    except Exception as e:
        logger.debug("driver annotate skipped: %s", e)
    return stocks


def _get_market_meta(market: str, list_id: str = None):
    market = (market or "asx").lower()
    if market == "watchlist":
        from data.watchlist import list_watchlist

        n = len(list_watchlist())
        return {
            "data_source": "Personal watchlist (local SQLite)",
            "last_scraped": datetime.now().isoformat(),
            "market": "watchlist",
            "universe_count": n,
            "watchlist_count": n,
        }
    if market == "pitches":
        from data.pitch_ledger import list_pitch_ledger

        n = len(list_pitch_ledger())
        return {
            "data_source": "Daily Stock Pitcher ledger (local SQLite + Obsidian)",
            "last_scraped": datetime.now().isoformat(),
            "market": "pitches",
            "universe_count": n,
            "pitches_count": n,
        }
    if market == "business":
        try:
            from data.business_lists import get_business_meta
            return get_business_meta(list_id or "")
        except Exception:
            return {
                "data_source": "yfinance • Business lists",
                "last_scraped": datetime.now().isoformat(),
                "market": "business",
            }
    if market == "buybacks":
        from data.business_lists import BUILTIN_BUYBACKS_LIST_ID, get_business_meta
        return get_business_meta(list_id or BUILTIN_BUYBACKS_LIST_ID)
    if market == "us":
        try:
            from data.us import get_us_meta
            meta = get_us_meta()
        except Exception:
            meta = {
                "data_source": "yfinance + SEC EDGAR (ROIC, shares) + Form 4",
                "last_scraped": datetime.now().isoformat(),
            }
    else:
        try:
            meta = get_asx_meta()
        except Exception:
            meta = {
                "data_source": "yfinance + ASX Announcements (Markit Digital)",
                "last_scraped": datetime.now().isoformat(),
            }
    meta["market"] = market
    return meta


@app.route("/")
def index():
    return render_template(
        "index.html",
        screener_api_token=os.environ.get("SCREENER_API_TOKEN", "").strip(),
    )


@app.route("/api/pariah/config")
def api_pariah_config():
    """Capital-cycle pariah candidate filter config (sectors, keywords, thresholds)."""
    try:
        from data.pariah_filter import pariah_config_public
        return jsonify(pariah_config_public())
    except Exception as e:
        logger.exception("api_pariah_config failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/reorg")
def api_pariah_reorg():
    """Phase-1 bankruptcy / Item 1.03 status for US mining pilot."""
    try:
        from data.bankruptcy_reorg import list_reorg_summaries, reorg_status_public
        detail = request.args.get("detail", "").lower() in ("1", "true", "yes")
        status = reorg_status_public()
        if detail:
            status["summaries"] = list_reorg_summaries()
        return jsonify(status)
    except Exception as e:
        logger.exception("api_pariah_reorg failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/reorg/scan", methods=["POST"])
def api_pariah_reorg_scan():
    """Scan mining universe for Item 1.03 8-Ks (can take several minutes)."""
    try:
        from data.bankruptcy_reorg import reorg_status_public, scan_mining_universe
        force = _bool_arg("force")
        tickers_raw = (request.args.get("tickers") or "").strip()
        if not tickers_raw and request.is_json and request.json:
            tickers_raw = str(request.json.get("tickers") or "")
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()] or None
        result = scan_mining_universe(force=force, tickers=tickers)
        return jsonify({
            "scanned": result.get("scanned"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
            "finished_at": result.get("finished_at"),
            "status": reorg_status_public(),
        })
    except Exception as e:
        logger.exception("api_pariah_reorg_scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/reserves")
def api_pariah_reserves():
    """Phase-2 mining reserves / reserve-life status (10-K S-K 1300 parse)."""
    try:
        from data.mining_reserves import list_reserves_summaries, reserves_status_public
        detail = request.args.get("detail", "").lower() in ("1", "true", "yes")
        status = reserves_status_public()
        if detail:
            status["summaries"] = list_reserves_summaries()
        return jsonify(status)
    except Exception as e:
        logger.exception("api_pariah_reserves failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/reserves/scan", methods=["POST"])
def api_pariah_reserves_scan():
    """Scan mining universe 10-Ks for reserves / life (can take several minutes)."""
    try:
        from data.mining_reserves import reserves_status_public, scan_mining_reserves
        force = _bool_arg("force")
        tickers_raw = (request.args.get("tickers") or "").strip()
        if not tickers_raw and request.is_json and request.json:
            tickers_raw = str(request.json.get("tickers") or "")
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()] or None
        result = scan_mining_reserves(force=force, tickers=tickers)
        return jsonify({
            "scanned": result.get("scanned"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
            "finished_at": result.get("finished_at"),
            "status": reserves_status_public(),
        })
    except Exception as e:
        logger.exception("api_pariah_reserves_scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/costs")
def api_pariah_costs():
    """Phase-3 reported unit costs + pilot peer ranks (not industry curve)."""
    try:
        from data.mining_costs import costs_status_public, list_costs_summaries
        detail = request.args.get("detail", "").lower() in ("1", "true", "yes")
        status = costs_status_public()
        if detail:
            status["summaries"] = list_costs_summaries()
        return jsonify(status)
    except Exception as e:
        logger.exception("api_pariah_costs failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/costs/scan", methods=["POST"])
def api_pariah_costs_scan():
    """Scan mining 10-Ks for unit costs and recompute peer ranks."""
    try:
        from data.mining_costs import costs_status_public, scan_mining_costs
        force = _bool_arg("force")
        tickers_raw = (request.args.get("tickers") or "").strip()
        if not tickers_raw and request.is_json and request.json:
            tickers_raw = str(request.json.get("tickers") or "")
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()] or None
        result = scan_mining_costs(force=force, tickers=tickers)
        return jsonify({
            "scanned": result.get("scanned"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
            "finished_at": result.get("finished_at"),
            "ranks": result.get("ranks"),
            "status": costs_status_public(),
        })
    except Exception as e:
        logger.exception("api_pariah_costs_scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/asx-mining")
def api_pariah_asx_mining():
    """ASX mining pilot status (YAL/WHC/NHC/NEM phases 1–3)."""
    try:
        from data.asx_mining import asx_mining_status_public
        return jsonify(asx_mining_status_public())
    except Exception as e:
        logger.exception("api_pariah_asx_mining failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/pariah/asx-mining/scan", methods=["POST"])
def api_pariah_asx_mining_scan():
    """Scan ASX mining pilot (announcements + seed PDFs + optional US cross-fill)."""
    try:
        from data.asx_mining import asx_mining_status_public, scan_asx_mining_universe
        force = _bool_arg("force")
        tickers_raw = (request.args.get("tickers") or "").strip()
        if not tickers_raw and request.is_json and request.json:
            tickers_raw = str(request.json.get("tickers") or "")
        tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()] or None
        result = scan_asx_mining_universe(force=force, tickers=tickers)
        return jsonify({
            "scanned": result.get("scanned"),
            "skipped": result.get("skipped"),
            "errors": result.get("errors"),
            "finished_at": result.get("finished_at"),
            "ranks": result.get("ranks"),
            "status": asx_mining_status_public(),
        })
    except Exception as e:
        logger.exception("api_pariah_asx_mining_scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/indices")
def api_indices():
    """Major national indices: drawdowns + PE proxies (ETF / Multpl / local constituents)."""
    force = _bool_arg("force")
    try:
        from data.indices import get_indices_overview
        return jsonify(get_indices_overview(force=force))
    except Exception as e:
        logger.exception("api_indices failed")
        return jsonify({"indices": [], "error": str(e)}), 500


@app.route("/api/commodities")
def api_commodities():
    """Commodity strip overview from local daily CSV collection."""
    force = _bool_arg("force")
    try:
        from data.commodity_prices import commodities_overview
        return jsonify(commodities_overview(force=force))
    except Exception as e:
        logger.exception("api_commodities failed")
        return jsonify({"commodities": [], "error": str(e)}), 500


@app.route("/api/commodities/producers")
def api_commodities_producers():
    """Mining-pilot producers joined to commodity strips + unit costs + stock ratios."""
    force = _bool_arg("force")
    try:
        from data.commodity_producers import list_producers
        return jsonify(list_producers(force=force))
    except Exception as e:
        logger.exception("api_commodities_producers failed")
        return jsonify({"producers": [], "error": str(e)}), 500


@app.route("/api/commodities/<commodity_id>/panel")
def api_commodities_panel(commodity_id):
    """Commodity strip vs all mapped producers' unit costs (no producer selected)."""
    try:
        from data.commodity_producers import commodity_cohort_panel

        payload = commodity_cohort_panel(commodity_id)
        if payload.get("error"):
            return jsonify(payload), 404
        return jsonify(payload)
    except Exception as e:
        logger.exception("api_commodities_panel failed for %s", commodity_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/commodities/producers/<ticker>")
def api_commodities_producer_detail(ticker):
    """Single producer detail + dual chart series."""
    try:
        from data.commodity_producers import producer_detail
        payload = producer_detail(ticker)
        if payload.get("error"):
            return jsonify(payload), 404
        return jsonify(payload)
    except Exception as e:
        logger.exception("api_commodities_producer_detail failed for %s", ticker)
        return jsonify({"error": str(e)}), 500


@app.route("/api/operating/<path:ticker>")
def api_operating_ticker(ticker):
    """Phase-2 filing extracts (production / realized / hedge / capex)."""
    try:
        from data.operating_extract import get_operating_history, latest_operating

        hist = get_operating_history(ticker)
        return jsonify(
            {
                "ticker": (ticker or "").upper(),
                "latest": latest_operating(ticker),
                "history": hist,
                "count": len(hist),
            }
        )
    except Exception as e:
        logger.exception("api_operating failed for %s", ticker)
        return jsonify({"error": str(e), "history": []}), 500


@app.route("/api/supporting/exposures/<path:ticker>")
def api_supporting_exposure(ticker):
    """Stock vs official driver strip (energy_pilot / watchlist join)."""
    try:
        from data.supporting_series import build_exposure_row

        row = build_exposure_row(ticker, include_stock=True, include_chart=True)
        if not row:
            return jsonify({"error": "no supporting-series exposure", "ticker": ticker}), 404
        return jsonify({"driver": row, "ticker": row.get("ticker")})
    except Exception as e:
        logger.exception("api_supporting_exposure failed for %s", ticker)
        return jsonify({"error": str(e)}), 500


@app.route("/api/commodities/<commodity_id>/history")
def api_commodities_history(commodity_id):
    """Full cleaned history for one commodity id."""
    try:
        from data.commodity_prices import get_series, load_commodity_config
        cfg = load_commodity_config()
        meta = (cfg.get("commodities") or {}).get(commodity_id) or {}
        series = get_series(commodity_id)
        return jsonify({
            "id": commodity_id,
            "display": meta.get("display") or commodity_id,
            "unit": meta.get("unit") or "",
            "series": series,
            "count": len(series),
        })
    except Exception as e:
        logger.exception("api_commodities_history failed for %s", commodity_id)
        return jsonify({"error": str(e), "series": []}), 500


@app.route("/api/commodities/cost-history")
def api_commodities_cost_history_status():
    """Multi-period unit cost history status (all tickers)."""
    try:
        from data.unit_cost_history import cost_history_status
        return jsonify(cost_history_status())
    except Exception as e:
        logger.exception("api cost history status failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/commodities/cost-history/scan", methods=["POST"])
def api_commodities_cost_history_scan():
    """Collect multi-period costs from ASX quarterlies + US SEC filings."""
    try:
        from data.unit_cost_history import collect_all_cost_history, cost_history_status
        tickers_raw = (request.args.get("tickers") or "").strip()
        if not tickers_raw and request.is_json and request.json:
            tickers_raw = str(request.json.get("tickers") or "")
        tickers = [t.strip() for t in tickers_raw.split(",") if t.strip()] or None
        result = collect_all_cost_history(tickers=tickers)
        return jsonify({"result": result, "status": cost_history_status()})
    except Exception as e:
        logger.exception("api cost history scan failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/indices/<index_id>")
def api_index_detail(index_id):
    """Full monthly price series (+ PE history when available) for one index."""
    force = _bool_arg("force")
    try:
        from data.indices import get_index_detail
        detail = get_index_detail(index_id, force=force)
        if not detail:
            return jsonify({"error": "Unknown index"}), 404
        return jsonify(detail)
    except Exception as e:
        logger.exception("api_index_detail failed for %s", index_id)
        return jsonify({"error": str(e)}), 500


@app.route("/api/stocks")
def api_stocks():
    market = request.args.get("market", "asx")
    list_id = request.args.get("list", "")
    full = _bool_arg("full")
    force_yf = _bool_arg("force_yf")
    signals_only = _bool_arg("signals_only")
    fast = _bool_arg("fast")
    stocks = get_stocks_for_market(
        market,
        full=full,
        force_yf=force_yf,
        list_id=list_id,
        signals_only=signals_only,
        fast=fast,
    )
    rank = request.args.get("rank", "1").lower() not in ("0", "false", "no")
    if rank and stocks and market not in ("watchlist", "pitches"):
        try:
            from data.wiki_bridge import enrich_stocks_with_opportunities
            stocks = enrich_stocks_with_opportunities(stocks)
        except Exception as e:
            logger.warning("Opportunity enrichment failed: %s", e)
    elif market in ("watchlist", "pitches") and stocks:
        # Light rank pass when scores already present from cache; skip heavy recompute
        try:
            from data.wiki_bridge import enrich_stocks_with_opportunities
            stocks = enrich_stocks_with_opportunities(stocks)
        except Exception as e:
            logger.debug("Watchlist/Pitches opportunity enrich: %s", e)
    meta = _get_market_meta(market, list_id=list_id)
    meta["full"] = bool(full)
    meta["force_yf"] = bool(force_yf)
    meta["list_id"] = list_id
    meta["signals_only"] = signals_only
    meta["fast"] = bool(fast)
    meta["rank_enriched"] = rank
    meta["total_universe"] = meta.get("universe_count") or (len(stocks) if not full else None)
    return jsonify({"stocks": stocks, "meta": meta})


@app.route("/api/superinvestors", methods=["GET"])
def api_superinvestors_get():
    """Return Superinvestor 13F & ASX substantial holder dashboard + stock summaries (Step 5)."""
    try:
        from data.superinvestor_13f import (
            get_superinvestor_dashboard_data,
            get_superinvestor_summary_for_ticker,
        )

        ticker = (request.args.get("ticker") or "").strip().upper()
        if ticker:
            return jsonify({
                "ticker": ticker,
                "summary": get_superinvestor_summary_for_ticker(ticker),
            })
        dash = get_superinvestor_dashboard_data()
        dash["ok"] = True
        dash["managers_tracked"] = (dash.get("kpi") or {}).get("managers_tracked", 0)
        dash["total_tickers_held"] = (dash.get("kpi") or {}).get("total_unique_tickers", 0)
        dash["top_holdings"] = dash.get("common_holdings", [])[:100]
        return jsonify(dash)
    except Exception as e:
        logger.exception("api_superinvestors_get failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/superinvestors/refresh", methods=["POST"])
def api_superinvestors_refresh():
    """Fetch latest SEC 13F-HR filings for curated Superinvestors and rebuild stock summaries."""
    try:
        from data.superinvestor_13f import (
            collect_and_rebuild_superinvestor_13f,
            get_superinvestor_dashboard_data,
        )

        fetch_sec = _bool_arg("fetch_sec", default=True)
        res = collect_and_rebuild_superinvestor_13f(fetch_sec=fetch_sec)
        dash = get_superinvestor_dashboard_data()
        return jsonify({"ok": True, "refresh": res, **dash})
    except Exception as e:
        logger.exception("api_superinvestors_refresh failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    """List watchlist entries and optional stock rows."""
    try:
        from data.watchlist import fetch_watchlist_stocks, list_watchlist

        entries = list_watchlist()
        enrich = request.args.get("enrich", "1").lower() not in ("0", "false", "no")
        stocks = fetch_watchlist_stocks() if enrich else []
        return jsonify(
            {
                "entries": entries,
                "stocks": stocks,
                "tickers": [e["ticker"] for e in entries],
                "count": len(entries),
            }
        )
    except Exception as e:
        logger.exception("api_watchlist_get failed")
        return jsonify({"error": str(e), "entries": [], "stocks": []}), 500


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    """Add ticker to watchlist. JSON: { ticker, name?, market?, notes? }."""
    try:
        from data.watchlist import add_ticker, is_watched

        body = request.get_json(silent=True) or {}
        ticker = (body.get("ticker") or request.args.get("ticker") or "").strip()
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        entry = add_ticker(
            ticker,
            name=body.get("name") or "",
            market=body.get("market") or "",
            notes=body.get("notes") or "",
        )
        return jsonify({"ok": True, "entry": entry, "on_watchlist": is_watched(ticker)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("api_watchlist_add failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist/<path:ticker>", methods=["DELETE"])
def api_watchlist_remove(ticker):
    """Remove ticker from watchlist."""
    try:
        from data.watchlist import is_watched, remove_ticker

        ok = remove_ticker(ticker)
        return jsonify({"ok": ok, "ticker": (ticker or "").upper(), "on_watchlist": is_watched(ticker)})
    except Exception as e:
        logger.exception("api_watchlist_remove failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/score-weights", methods=["GET"])
def api_score_weights_get():
    """Current quant Score weights from config/opportunity_weights.yaml."""
    try:
        from scoring.quant_score import get_weights_public

        return jsonify(get_weights_public())
    except Exception as e:
        logger.exception("api_score_weights_get failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/score-weights", methods=["POST", "PUT"])
def api_score_weights_save():
    """Update Score weights (component weights re-normalized to sum 1)."""
    try:
        from scoring.quant_score import save_weights

        body = request.get_json(silent=True) or {}
        # Accept flat body or { weights: {...} }
        updates = body.get("weights") if isinstance(body.get("weights"), dict) else body
        result = save_weights(updates or {})
        return jsonify({"ok": True, **result})
    except Exception as e:
        logger.exception("api_score_weights_save failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist/alerts", methods=["GET"])
def api_watchlist_alerts_get():
    """Evaluate watchlist for 52w-low / Burry alerts; returns newly fired + active."""
    try:
        from data.watchlist_alerts import evaluate_watchlist_alerts, get_alert_prefs

        evaluate = request.args.get("evaluate", "1").lower() not in ("0", "false", "no")
        if evaluate:
            result = evaluate_watchlist_alerts()
        else:
            result = {
                "enabled": get_alert_prefs().get("enabled"),
                "new_alerts": [],
                "active": [],
                "prefs": get_alert_prefs(),
            }
        return jsonify(result)
    except Exception as e:
        logger.exception("api_watchlist_alerts_get failed")
        return jsonify({"error": str(e), "new_alerts": [], "active": []}), 500


@app.route("/api/watchlist/alerts/prefs", methods=["GET", "POST"])
def api_watchlist_alerts_prefs():
    """Get or update watchlist alert preferences."""
    try:
        from data.watchlist_alerts import get_alert_prefs, set_alert_prefs

        if request.method == "GET":
            return jsonify({"prefs": get_alert_prefs()})
        body = request.get_json(silent=True) or {}
        prefs = body.get("prefs") if isinstance(body.get("prefs"), dict) else body
        return jsonify({"ok": True, "prefs": set_alert_prefs(prefs or {})})
    except Exception as e:
        logger.exception("api_watchlist_alerts_prefs failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stock/<path:ticker>/announcements")
def api_stock_announcements(ticker):
    """Latest company announcements with source URLs and NLP catalyst classification for the detail modal."""
    try:
        from data.company_announcements import get_announcements
        from scoring.nlp_filings import classify_announcement_catalyst

        market = request.args.get("market", "")
        limit = _int_arg("limit", 15)
        payload = get_announcements(ticker, market=market, limit=limit)
        for a in payload.get("announcements") or []:
            cat = classify_announcement_catalyst(a.get("headline") or "", a.get("type") or "")
            a["catalyst_label"] = cat["catalyst_label"]
            a["tone"] = cat["tone"]
            a["tone_score"] = cat["tone_score"]
            a["badge_cls"] = cat["badge_cls"]
        return jsonify(payload)
    except Exception as e:
        logger.exception("api_stock_announcements failed")
        return jsonify(
            {
                "ticker": ticker,
                "announcements": [],
                "error": str(e),
                "count": 0,
            }
        ), 500


@app.route("/api/opportunities")
def api_opportunities():
    """Ranked opportunities: quant + vault qual overlay."""
    market = request.args.get("market", "buybacks")
    list_id = request.args.get("list", "")
    force_yf = _bool_arg("force_yf")
    min_quant = _float_arg("min_quant", 0)
    min_composite = _float_arg("min_composite", 0)
    limit = _int_arg("limit", 50)
    stocks = get_stocks_for_market(market, force_yf=force_yf, list_id=list_id)
    try:
        from data.wiki_bridge import get_opportunities, load_registry
        load_registry()
        opportunities = get_opportunities(
            stocks, min_quant=min_quant, min_composite=min_composite, limit=limit
        )
    except Exception as e:
        logger.exception("api_opportunities failed")
        return jsonify({"error": str(e), "opportunities": []}), 500
    meta = _get_market_meta(market, list_id=list_id)
    meta["min_quant"] = min_quant
    meta["min_composite"] = min_composite
    return jsonify({
        "opportunities": opportunities,
        "count": len(opportunities),
        "meta": meta,
    })


@app.route("/api/eda/summary")
def api_eda_summary():
    """
    Cross-sectional Statistical / EDA summary for a market or business list:
    - Sector & global robust distributions (median, MAD, IQR, Bowley skewness)
    - Sector-adjusted Robust Z-Scores, percentile ranks, and Multi-Factor
      Value/Quality Composite Scores (`vq_score`, `val_z`, `qual_z`, `posture_z`).
    """
    market = request.args.get("market", "business")
    list_id = request.args.get("list", "")
    full = _bool_arg("full", default=True)
    min_peers = max(2, _int_arg("min_peers", 5))
    val_w = _float_arg("val_w", 0.45)
    qual_w = _float_arg("qual_w", 0.40)
    post_w = _float_arg("post_w", 0.15)
    include_profiles = _bool_arg("profiles", default=False)
    try:
        from scoring.eda_stats import compute_cross_sectional_eda

        stocks = get_stocks_for_market(market, full=full, list_id=list_id, fast=True)
        eda = compute_cross_sectional_eda(
            stocks,
            min_sector_peers=min_peers,
            value_weight=val_w,
            quality_weight=qual_w,
            posture_weight=post_w,
        )
        if not include_profiles:
            eda.pop("profiles", None)
        eda["meta"] = _get_market_meta(market, list_id=list_id)
        return jsonify(eda)
    except Exception as e:
        logger.exception("api_eda_summary failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ml/unsupervised")
def api_ml_unsupervised():
    """
    Unsupervised Learning (`scikit-learn`) endpoint:
    1. K-Means Fundamental Archetype Clustering (`KMeans` + `RobustScaler` + `SimpleImputer`/`KNNImputer`)
    2. k-Nearest Neighbors (`NearestNeighbors`) Mathematical Comps & Peer Valuation Discount
    3. Anomaly Detection (`IsolationForest` + Mahalanobis Distance + Huber Regression Residual)
       for surfacing Mispriced Quality Outliers.

    By default (`scope=global`), serves the precomputed **full-dataset** model (~17,000+ global
    stocks across all lists/markets) that refreshes in the background once per week.
    """
    scope = (request.args.get("scope") or "global").strip().lower()
    market = request.args.get("market", "business")
    list_id = request.args.get("list", "")
    full = _bool_arg("full", default=True)
    n_neighbors = max(1, min(15, _int_arg("k", 5)))
    n_clusters = max(2, min(12, _int_arg("clusters", 6)))
    contamination = max(0.02, min(0.25, _float_arg("contamination", 0.08)))
    ticker = (request.args.get("ticker") or "").strip().upper()
    include_profiles = _bool_arg("profiles", default=True)
    try:
        from scoring.unsupervised_ml import (
            get_ml_job_status,
            load_persisted_global_ml,
            run_unsupervised_analysis,
            start_weekly_ml_scheduler,
            trigger_global_ml_rebuild_async,
        )

        # Ensure the weekly background daemon scheduler is active
        start_weekly_ml_scheduler()

        if scope != "list":
            persisted = load_persisted_global_ml()
            if not persisted:
                trigger_global_ml_rebuild_async()
                return jsonify({
                    "scope": "global",
                    "building": True,
                    "job": get_ml_job_status(),
                    "total_stocks": 0,
                    "modeled_stocks": 0,
                    "clusters": [],
                    "top_mispriced_outliers": [],
                    "profiles": {},
                    "note": "Initial full-dataset ML model is building in the background (~3s)...",
                })
            if not persisted.get("is_fresh"):
                # Serve existing snapshot immediately and refresh in background
                trigger_global_ml_rebuild_async()

            if ticker:
                profs = persisted.get("profiles") or {}
                prof = profs.get(ticker) or profs.get(ticker + ".AX") or profs.get(ticker.replace(".AX", ""))
                return jsonify({
                    "scope": "global",
                    "computed_at": persisted.get("computed_at"),
                    "next_scheduled_refresh": persisted.get("next_scheduled_refresh"),
                    "ticker": ticker,
                    "profile": prof,
                    "clusters": persisted.get("clusters", []),
                    "modeled_stocks": persisted.get("modeled_stocks", 0),
                })

            out = dict(persisted)
            out["job"] = get_ml_job_status()
            if not include_profiles:
                out.pop("profiles", None)
            elif market and market not in ("all", "global"):
                # Still return all profiles (or subset if requested) — returning the full
                # profile dictionary ensures any stock or k-NN peer clicked in the UI resolves!
                pass
            return jsonify(out)

        # Optional fallback if caller explicitly passes ?scope=list
        stocks = get_stocks_for_market(market, full=full, list_id=list_id, fast=True)
        res = run_unsupervised_analysis(
            stocks,
            n_neighbors=n_neighbors,
            n_clusters=n_clusters,
            contamination=contamination,
        )
        res["scope"] = "list"
        if ticker:
            prof = (res.get("profiles") or {}).get(ticker)
            if not prof and not ticker.endswith(".AX") and market == "asx":
                prof = (res.get("profiles") or {}).get(ticker + ".AX")
            return jsonify({
                "scope": "list",
                "ticker": ticker,
                "profile": prof,
                "clusters": res.get("clusters", []),
                "modeled_stocks": res.get("modeled_stocks", 0),
            })
        if not include_profiles:
            res.pop("profiles", None)
        res["meta"] = _get_market_meta(market, list_id=list_id)
        return jsonify(res)
    except Exception as e:
        logger.exception("api_ml_unsupervised failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ml/unsupervised/status")
def api_ml_unsupervised_status():
    """Return status of the weekly global Unsupervised ML background job."""
    try:
        from scoring.unsupervised_ml import get_ml_job_status, start_weekly_ml_scheduler

        start_weekly_ml_scheduler()
        return jsonify(get_ml_job_status())
    except Exception as e:
        logger.exception("api_ml_unsupervised_status failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ml/unsupervised/rebuild", methods=["POST"])
def api_ml_unsupervised_rebuild():
    """Force an immediate background rebuild of the global full-dataset Unsupervised ML model."""
    try:
        from scoring.unsupervised_ml import (
            get_ml_job_status,
            start_weekly_ml_scheduler,
            trigger_global_ml_rebuild_async,
        )

        start_weekly_ml_scheduler()
        started = trigger_global_ml_rebuild_async()
        return jsonify({
            "status": "started" if started else "already_running",
            "message": (
                "Full-dataset Unsupervised ML rebuild started in background (~3–5s)."
                if started
                else "Full-dataset Unsupervised ML rebuild is already running."
            ),
            "job": get_ml_job_status(),
        })
    except Exception as e:
        logger.exception("api_ml_unsupervised_rebuild failed")
        return _json_error(str(e))


@app.route("/api/ml/snapshots")
def api_ml_snapshots():
    """
    Return monthly point-in-time snapshot history, historical outlier cohort forward
    return performance, and k-NN Historical Analog return predictions for present-day stocks.
    """
    market = request.args.get("market", "")
    list_id = request.args.get("list", "")
    full = _bool_arg("full", default=True)
    scope = (request.args.get("scope") or "global").strip().lower()
    try:
        from scoring.historical_snapshots import (
            capture_monthly_snapshot,
            get_snapshots_overview,
            has_snapshot_for_month,
            start_monthly_snapshot_scheduler,
        )

        start_monthly_snapshot_scheduler()
        # If no snapshot exists yet for this calendar month, capture it now (~0.4s from cached global ML)
        if not has_snapshot_for_month():
            capture_monthly_snapshot(force=False)

        predict_stocks = None
        if scope == "list" and market:
            predict_stocks = get_stocks_for_market(market, full=full, list_id=list_id, fast=True)

        overview = get_snapshots_overview(predict_for_stocks=predict_stocks)
        return jsonify(overview)
    except Exception as e:
        logger.exception("api_ml_snapshots failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ml/snapshots/capture", methods=["POST"])
def api_ml_snapshots_capture():
    """Force or refresh the current calendar month's point-in-time snapshot and update forward returns."""
    force = _bool_arg("force", default=True)
    snapshot_month = (request.args.get("month") or "").strip() or None
    try:
        from scoring.historical_snapshots import capture_monthly_snapshot, start_monthly_snapshot_scheduler

        start_monthly_snapshot_scheduler()
        res = capture_monthly_snapshot(snapshot_month=snapshot_month, force=force)
        return jsonify(res)
    except Exception as e:
        logger.exception("api_ml_snapshots_capture failed")
        return _json_error(str(e))


@app.route("/api/ml/econometrics")
def api_ml_econometrics():
    """
    Time-Series Econometrics & Macro / Commodity Beta endpoint:
    Computes univariate & rolling 26w betas, Up/Down market convexity, Multi-Factor Macro
    Attribution (`Driver + DXY + 10Y TIPS + HY OAS`), and Engle-Granger Cointegration +
    Ornstein-Uhlenbeck half-life & mean-reverting spread Z-score for `ticker`.
    """
    ticker = (request.args.get("ticker") or "").strip().upper()
    market = (request.args.get("market") or "asx").strip().lower()
    factor_override = (request.args.get("factor") or "").strip() or None
    if not ticker:
        return _json_error("ticker parameter required", 400)
    try:
        from scoring.macro_econometrics import compute_stock_macro_econometrics

        t_base = ticker.replace(".AX", "")
        stock = None
        if market in ("business", "buybacks") or ("." in ticker and not ticker.endswith(".AX")):
            from data.business_lists import _fetch_single_business

            stock = _fetch_single_business(ticker)
        elif market == "us" or (not ticker.endswith(".AX") and "." not in ticker):
            from data.us import _fetch_single_us

            stock = _fetch_single_us(t_base)
        else:
            from data.asx import _fetch_single_asx

            stock = _fetch_single_asx(t_base)

        if not stock:
            return _json_error(f"Could not fetch price trend for {ticker}", 404)
        if not factor_override:
            try:
                from data.supporting_series import attach_driver_detail

                stock = attach_driver_detail(stock)
            except Exception:
                pass
        res = compute_stock_macro_econometrics(stock, primary_factor_override=factor_override)
        return jsonify(res)
    except Exception as e:
        logger.exception("api_ml_econometrics failed")
        return _json_error(str(e))


@app.route("/api/ml/supervised")
def api_ml_supervised():
    """
    Section 5: Supervised Learning & Backtesting (Signal Calibration) endpoint:
    Trains L2 Logistic Regression + HistGradientBoosting Classifier/Regressor on dated
    historical corporate signal events, runs 5-fold TimeSeriesSplit walk-forward backtesting,
    calibrates optimal opportunity_weights.yaml pillar weights, and ranks present-day candidates.
    """
    market = request.args.get("market", "")
    list_id = request.args.get("list", "")
    full = _bool_arg("full", default=True)
    scope = (request.args.get("scope") or "list").strip().lower()
    force = _bool_arg("force", default=False)
    try:
        from scoring.supervised_calibration import run_supervised_calibration

        cand_stocks = None
        if scope == "list" and market:
            cand_stocks = get_stocks_for_market(market, full=full, list_id=list_id, fast=True)
        res = run_supervised_calibration(stocks=cand_stocks, force_rebuild=force)
        return jsonify(res)
    except Exception as e:
        logger.exception("api_ml_supervised failed")
        return _json_error(str(e))


@app.route("/api/ml/supervised/apply-weights", methods=["POST"])
def api_ml_supervised_apply_weights():
    """Apply the empirically ML-calibrated pillar weights to config/opportunity_weights.yaml."""
    try:
        from scoring.quant_score import save_weights
        from scoring.supervised_calibration import run_supervised_calibration

        cal = run_supervised_calibration(force_rebuild=False)
        wc = (cal.get("weight_calibration") or {}).get("calibrated_weights")
        if not wc:
            return _json_error("Calibrated weights not available", 400)
        saved = save_weights(wc)
        return jsonify({
            "status": "ok",
            "message": "Applied ML-calibrated opportunity weights to config/opportunity_weights.yaml.",
            "calibrated_weights": wc,
            "weights_public": saved,
        })
    except Exception as e:
        logger.exception("api_ml_supervised_apply_weights failed")
        return _json_error(str(e))


@app.route("/api/ml/nlp")
def api_ml_nlp():
    """
    Section 6: Natural Language Processing (NLP) on Filings & Announcements endpoint:
    Runs Loughran-McDonald Financial Sentiment & Capital-Cycle Lexicon scoring, TF-IDF + NMF
    Topic Modeling, Tone Momentum, and Bullish Filing Divergence detection across cached
    ASX announcements, SEC 8-K/10-Q/10-K/Form 4 filings, and mining/energy extracts.
    """
    market = request.args.get("market", "")
    list_id = request.args.get("list", "")
    full = _bool_arg("full", default=True)
    scope = (request.args.get("scope") or "global").strip().lower()
    force = _bool_arg("force", default=False)
    try:
        from scoring.nlp_filings import run_corpus_nlp_analysis

        cand_stocks = None
        if scope == "list" and market:
            cand_stocks = get_stocks_for_market(market, full=full, list_id=list_id, fast=True)
        res = run_corpus_nlp_analysis(stocks=cand_stocks, force_rebuild=force)
        return jsonify(res)
    except Exception as e:
        logger.exception("api_ml_nlp failed")
        return _json_error(str(e))


@app.route("/api/kpis")
def api_kpis():
    market = request.args.get("market", "asx")
    full = _bool_arg("full")
    force_yf = _bool_arg("force_yf")
    stocks = get_stocks_for_market(market, full=full, force_yf=force_yf)
    kpis = {
        "qualifying_stocks": len(stocks),
        "total_insider_buys": sum(s.get("insider_buys_2026", 0) for s in stocks),
        "companies_with_buybacks": sum(1 for s in stocks if s.get("buyback_announced")),
        "avg_discount_52w": round(
            sum(s.get("pct_from_52w_low", 0) for s in stocks) / max(len(stocks), 1), 1
        ),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "meta": _get_market_meta(market),
    }
    kpis["meta"]["full"] = bool(full)
    kpis["meta"]["force_yf"] = bool(force_yf)
    return jsonify(kpis)


@app.route("/api/business/lists")
def api_business_lists():
    try:
        from data.business_lists import get_available_lists
        return jsonify({"lists": get_available_lists()})
    except Exception as e:
        return jsonify({"lists": [], "error": str(e)}), 500


@app.route("/api/stock/<path:ticker>")
def api_stock_detail(ticker):
    from data.watchlist import is_watched
    from data.yf_helpers import sanitize_stock_figures

    market = (request.args.get("market", "asx") or "asx").lower()
    raw = (ticker or "").strip().upper()
    t = raw.replace(".AX", "")

    # Watchlist / ambiguous: route by symbol shape
    if market == "watchlist":
        if raw.endswith(".AX"):
            market = "asx"
        elif "." in raw:
            market = "business"
        else:
            market = "us"

    def _with_watch(s):
        if not s:
            return s
        s = sanitize_stock_figures(s)
        s["on_watchlist"] = is_watched(s.get("ticker") or raw)
        try:
            from data.supporting_series import attach_driver_detail

            s = attach_driver_detail(s)
        except Exception as e:
            logger.debug("driver detail skipped for %s: %s", raw, e)
        try:
            from scoring.macro_econometrics import compute_stock_macro_econometrics

            factor_override = (request.args.get("macro_factor") or "").strip() or None
            s["macro_econometrics"] = compute_stock_macro_econometrics(
                s,
                primary_factor_override=factor_override,
            )
        except Exception as e:
            logger.debug("macro econometrics skipped for %s: %s", raw, e)
        try:
            from scoring.supervised_calibration import compute_stock_supervised_profile

            s["supervised_calibration"] = compute_stock_supervised_profile(s)
        except Exception as e:
            logger.debug("supervised calibration skipped for %s: %s", raw, e)
        try:
            from scoring.nlp_filings import compute_stock_nlp_profile

            s["nlp_profile"] = compute_stock_nlp_profile(s)
        except Exception as e:
            logger.debug("nlp profile skipped for %s: %s", raw, e)
        try:
            from data.wiki_bridge import enrich_stock_opportunity

            s = enrich_stock_opportunity(s, peers=[s])
        except Exception as e:
            logger.debug("detail score attach skipped for %s: %s", raw, e)
            try:
                from data.vault_memo import attach_memo_status

                attach_memo_status(s)
            except Exception:
                pass
        return s

    if market in ("buybacks", "business"):
        try:
            from data.business_lists import _fetch_single_business
            sym = raw
            enriched = _fetch_single_business(sym)
            if enriched:
                return jsonify(_with_watch(enriched))
        except Exception as e:
            logger.warning("Business detail failed for %s: %s", ticker, e)
        list_id = request.args.get("list", "")
        market_key = "buybacks" if market == "buybacks" else "business"
        stocks = get_stocks_for_market(market_key, list_id=list_id)
        for s in stocks:
            if s["ticker"].upper() == raw:
                return jsonify(_with_watch(s))
        return jsonify({"error": "Not found"}), 404

    if market == "us":
        try:
            from data.us import _fetch_single_us
            enriched = _fetch_single_us(t)
            if enriched:
                return jsonify(_with_watch(enriched))
        except Exception as e:
            logger.warning("Targeted US detail failed for %s: %s", t, e)

        stocks = get_stocks_for_market("us")
        for s in stocks:
            if s["ticker"].upper().replace(".AX", "") == t:
                return jsonify(_with_watch(s))
        return jsonify({"error": "Not found"}), 404

    try:
        from data.asx import _fetch_single_asx
        enriched = _fetch_single_asx(t)
        if enriched:
            return jsonify(_with_watch(enriched))
    except Exception as e:
        logger.warning("Targeted ASX detail failed for %s: %s", t, e)

    stocks = get_stocks_for_market("asx", full=True)
    for s in stocks:
        if s["ticker"].upper().replace(".AX", "") == t:
            return jsonify(_with_watch(s))
    return jsonify({"error": "Not found"}), 404


@app.route("/api/vault/memo/<path:ticker>", methods=["GET"])
def api_vault_memo_get(ticker):
    """Does an opportunity memo exist in the local Obsidian vault?"""
    try:
        from data.vault_memo import memo_status

        return jsonify(memo_status(ticker))
    except Exception as e:
        logger.exception("api_vault_memo_get failed")
        return jsonify({"ok": False, "error": str(e), "ticker": ticker}), 500


@app.route("/api/vault/memo/<path:ticker>", methods=["POST"])
def api_vault_memo_post(ticker):
    """Create the opportunity memo if missing; always push a screener snapshot."""
    try:
        from data.vault_memo import create_or_update_memo

        body = request.get_json(silent=True) or {}
        result = create_or_update_memo(ticker, stock=body, source="screener")
        code = 200 if result.get("ok") else 400
        return jsonify(result), code
    except Exception as e:
        logger.exception("api_vault_memo_post failed")
        return jsonify({"ok": False, "error": str(e), "ticker": ticker}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    market = request.args.get("market", "asx")
    list_id = request.args.get("list", "")
    full = _bool_arg("full")
    force_yf = _bool_arg("force_yf")
    if market == "pitches":
        from data.pitch_ledger import backfill_pitch_forensics, sync_obsidian_pitches
        from data.superinvestor_13f import collect_and_rebuild_superinvestor_13f

        try:
            collect_and_rebuild_superinvestor_13f(fetch_sec=False)
        except Exception as ex:
            logger.debug("superinvestor_13f rebuild skipped on pitches refresh: %s", ex)
        sync_obsidian_pitches(fetch_live_yf=True)
        backfill_pitch_forensics(force=True)
        stocks = get_stocks_for_market("pitches", force_yf=force_yf)
        msg = f"Pitches ledger refreshed ({len(stocks)} pitches)"
    elif market == "business":
        from data.business_lists import _business_cache as biz_cache
        biz_cache.clear()
        stocks = get_stocks_for_market("business", force_yf=force_yf, list_id=list_id)
        msg = f"Business list refreshed ({list_id or 'no list'})"
    elif market == "buybacks":
        from data.buybacks import _buybacks_cache as bb_cache
        bb_cache.clear()
        stocks = get_stocks_for_market("buybacks", force_yf=force_yf)
        msg = f"Buybacks registry refreshed ({len(stocks)} companies)"
    elif market == "us":
        from data.us import _us_cache as us_cache
        from data.business_lists import BUILTIN_SP500_LIST_ID, _business_cache as biz_cache
        us_cache.clear()
        biz_cache.pop(BUILTIN_SP500_LIST_ID, None)
        stocks = fetch_us_stocks(force_yf=force_yf)
        msg = f"US S&P 500 refreshed ({len(stocks)} tickers, cache-first)"
    else:
        from data.asx import _asx_cache as asx_cache
        asx_cache.clear()
        stocks = fetch_asx_stocks(full=full, force_yf=force_yf)
        view = "full ASX list" if full else "signals (insider/buyback) subset"
        extra = " (forced yf rebuild)" if force_yf else ""
        msg = f"ASX data refreshed ({view}){extra}"

    meta = _get_market_meta(market, list_id=list_id)
    meta["full"] = bool(full)
    meta["force_yf"] = bool(force_yf)
    meta["list_id"] = list_id
    return jsonify({
        "status": "ok",
        "message": msg,
        "stocks": stocks,
        "meta": meta,
    })


def _buybacks_refresh_envelope(result: dict, ok_message: str, fail_default: str):
    if not result.get("ok"):
        err = result.get("error") or (result.get("build") or {}).get("error") or fail_default
        return _json_error(err, result=result)
    return _json_ok(
        ok_message,
        result=result,
        meta=result.get("meta") or _get_market_meta("buybacks"),
    )


@app.route("/api/buybacks/refresh_hkex", methods=["POST"])
def api_buybacks_refresh_hkex():
    """Download missing HKEX daily repurchase XLS files and rebuild master CSV."""
    try:
        from data.buybacks import refresh_hkex_buybacks
        result = refresh_hkex_buybacks(download_gaps=True)
        dl = result.get("download") or {}
        build = result.get("build") or {}
        msg = (
            f"HKEX data updated ({dl.get('downloaded', 0)} new files, "
            f"{build.get('rows', result.get('rows', 0))} total rows)"
        )
        return _buybacks_refresh_envelope(result, msg, "HKEX refresh failed")
    except Exception as e:
        logger.exception("buybacks refresh_hkex failed")
        return _json_error(str(e))


@app.route("/api/buybacks/refresh_hk_insider", methods=["POST"])
def api_buybacks_refresh_hk_insider():
    """Download missing HKEX Disclosure of Interests daily summaries and rebuild insider CSV."""
    try:
        from data.buybacks import refresh_hk_insider_buybacks
        result = refresh_hk_insider_buybacks(download_gaps=True)
        dl = result.get("download") or {}
        build = result.get("build") or {}
        msg = (
            f"HK insider data updated ({dl.get('downloaded', 0)} new files, "
            f"{build.get('rows', result.get('rows', 0))} total rows)"
        )
        return _buybacks_refresh_envelope(result, msg, "HK insider refresh failed")
    except Exception as e:
        logger.exception("buybacks refresh_hk_insider failed")
        return _json_error(str(e))


@app.route("/api/buybacks/refresh_asx", methods=["POST"])
@app.route("/api/buybacks/refresh_asx_insider", methods=["POST"])
def api_buybacks_refresh_asx():
    """Paginated ASX market ingest + PDF-refine insider purchases + buyback announcements."""
    try:
        from data.buybacks import refresh_asx_buybacks_data
        backfill = _bool_arg("backfill")
        incremental = _bool_arg("incremental", default=True)
        if backfill:
            incremental = False
        result = refresh_asx_buybacks_data(
            days=365,
            use_pdf=True,
            incremental=incremental,
            backfill=backfill,
        )
        refine = result.get("refine") or {}
        ingest = result.get("ingest") or {}
        msg = (
            f"ASX data updated ({result.get('total_buybacks', 0)} buybacks, "
            f"{result.get('total_purchases', 0)} insider purchases, "
            f"{ingest.get('pages_fetched', 0)} pages fetched, "
            f"{refine.get('pdf_checked', 0)} PDF-checked)"
        )
        return _buybacks_refresh_envelope(result, msg, "ASX refresh failed")
    except Exception as e:
        logger.exception("buybacks refresh_asx failed")
        return _json_error(str(e))


@app.route("/api/buybacks/refresh_us/status", methods=["GET"])
def api_buybacks_refresh_us_status():
    """Progress for background US SEC buyback + Form 4 insider scan."""
    try:
        from data.buybacks import get_us_sec_refresh_status
        return jsonify(get_us_sec_refresh_status())
    except Exception as e:
        logger.exception("buybacks refresh_us status failed")
        return _json_error(str(e))


@app.route("/api/buybacks/refresh_us", methods=["POST"])
def api_buybacks_refresh_us():
    """Scan US Russell 1000 + Nasdaq-100 SEC EDGAR buybacks and Form 4 insider (background)."""
    try:
        from data.buybacks import (
            get_us_sec_refresh_status,
            mark_us_sec_refresh_started,
            refresh_us_buybacks_data,
        )

        if get_us_sec_refresh_status().get("running"):
            return jsonify({
                "status": "already_running",
                "message": "US SEC scan already in progress.",
                "progress": get_us_sec_refresh_status(),
                "meta": _get_market_meta("buybacks"),
            })

        days = _int_arg("days", 365)
        max_tickers_raw = request.args.get("max_tickers")
        max_tickers = int(max_tickers_raw) if max_tickers_raw else None
        reparse_10q = _bool_arg("reparse_10q")

        if not mark_us_sec_refresh_started(max_tickers=max_tickers):
            return jsonify({
                "status": "already_running",
                "message": "US SEC scan already in progress.",
                "progress": get_us_sec_refresh_status(),
                "meta": _get_market_meta("buybacks"),
            })

        def _job():
            refresh_us_buybacks_data(
                days=days,
                max_tickers=max_tickers,
                reparse_10q=reparse_10q,
            )
            logger.info("Background US SEC buybacks scan finished.")

        _run_background(_job)
        return jsonify({
            "status": "started",
            "message": (
                "US SEC scan started in background (Russell 1000 + Nasdaq-100, "
                f"8-K/10-Q buybacks + Form 4 insider, last {days} days). "
                "Progress updates on the Buybacks tab."
            ),
            "progress": get_us_sec_refresh_status(),
            "meta": _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_us failed")
        return _json_error(str(e))


@app.route("/api/buybacks/refresh_jpx", methods=["POST"])
def api_buybacks_refresh_jpx():
    """Scrape JPX off-auction repurchases page and merge into project CSV."""
    try:
        from data.buybacks import refresh_jpx_buybacks
        result = refresh_jpx_buybacks(fetch_yfinance=False)
        msg = (
            f"JPX data merged ({result.get('scraped', 0)} scraped, "
            f"{result.get('total', 0)} total rows)"
        )
        return _buybacks_refresh_envelope(result, msg, "JPX scrape failed")
    except Exception as e:
        logger.exception("buybacks refresh_jpx failed")
        return _json_error(str(e))


@app.route("/api/buybacks/rebuild_yf/status", methods=["GET"])
def api_buybacks_rebuild_yf_status():
    """Progress for background buybacks yfinance rebuild."""
    try:
        from data.buybacks import get_yf_rebuild_status
        return jsonify(get_yf_rebuild_status())
    except Exception as e:
        logger.exception("buybacks rebuild_yf status failed")
        return _json_error(str(e))


@app.route("/api/buybacks/rebuild_yf", methods=["POST"])
def api_buybacks_rebuild_yf():
    """Force sequential yfinance refresh for buybacks registry symbols."""
    try:
        from data.buybacks import (
            _buybacks_cache as bb_cache,
            get_yf_rebuild_status,
            rebuild_buybacks_yf_cache,
        )
        status = get_yf_rebuild_status()
        if status.get("running"):
            return jsonify({
                "status": "already_running",
                "message": "YF rebuild already in progress.",
                "progress": status,
                "meta": _get_market_meta("buybacks"),
            })

        bb_cache.clear()

        def _job():
            rebuild_buybacks_yf_cache()
            logger.info("Background buybacks YF rebuild finished.")

        _run_background(_job)
        return _json_ok(
            "YF rebuild started for buybacks registry. Table refreshes every 25 symbols.",
            meta=_get_market_meta("buybacks"),
            status="started",
        )
    except Exception as e:
        logger.exception("buybacks rebuild_yf failed")
        return _json_error(str(e))


@app.route("/api/business/rebuild_yf", methods=["POST"])
def api_business_rebuild_yf():
    """Force sequential yfinance refresh for a business list CSV."""
    list_id = request.args.get("list", "")
    if not list_id:
        return _json_error("list parameter required", 400)
    try:
        from data.business_lists import _business_cache as biz_cache, rebuild_business_yf_cache
        biz_cache.clear()

        def _job():
            rebuild_business_yf_cache(list_id)
            logger.info("Background business YF rebuild finished.")

        _run_background(_job)
        return jsonify({
            "status": "started",
            "message": (
                f"YF rebuild started for business list '{list_id}'. "
                "Watch server logs for 'business yf progress'."
            ),
            "meta": _get_market_meta("business", list_id=list_id),
        })
    except Exception as e:
        logger.exception("business rebuild_yf failed")
        return _json_error(str(e))


@app.route("/api/asx/update_universe", methods=["POST"])
def api_update_asx_universe():
    """Refresh full ASX listed companies list from the official directory CSV."""
    try:
        result = update_asx_company_list(force=True)
        try:
            from data.asx import _asx_cache as asx_c
            asx_c.clear()
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return jsonify({"updated": False, "error": str(e)}), 500


@app.route("/api/asx/rebuild_yf", methods=["POST"])
def api_rebuild_yf():
    """Force full sequential yfinance refresh for the ASX universe (background)."""
    market = request.args.get("market", "asx")
    full = _bool_arg("full", default=True)
    try:
        from data.asx import _asx_cache as asx_c
        asx_c.clear()

        def _job():
            get_stocks_for_market(market, full=full, force_yf=True)
            logger.info("Background YF rebuild finished.")

        _run_background(_job)
        return jsonify({
            "status": "started",
            "message": (
                "YF rebuild started in background thread (sequential, slow). "
                "Monitor the server console/logs for 'yf sequential progress'."
            ),
            "meta": _get_market_meta(market),
        })
    except Exception as e:
        logger.exception("rebuild_yf start failed")
        return _json_error(str(e))


if __name__ == "__main__":
    try:
        from scoring.historical_snapshots import start_monthly_snapshot_scheduler
        from scoring.unsupervised_ml import start_weekly_ml_scheduler
        start_weekly_ml_scheduler()
        start_monthly_snapshot_scheduler()
    except Exception:
        pass
    host = os.environ.get("SCREENER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("SCREENER_PORT", "5000") or 5000)
    debug = os.environ.get("FLASK_DEBUG", os.environ.get("SCREENER_DEBUG", "")).strip().lower() in _TRUE_VALUES
    print("Multi-Market Signal Screener (personal / local)")
    print(f"→ Open http://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        print("WARNING: listening on all interfaces — keep SCREENER_ALLOW_REMOTE unset "
              "and prefer SCREENER_HOST=127.0.0.1")
    if debug:
        print("WARNING: debug mode enabled (FLASK_DEBUG=1) — do not expose to the network")
    app.run(host=host, port=port, debug=debug, threaded=True)
