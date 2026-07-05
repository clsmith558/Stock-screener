#!/usr/bin/env python3
"""
Multi-Market Signal Screener (ASX + US)
Clean, split version
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import Flask, jsonify, request, render_template
from datetime import datetime, timedelta
import logging

from data.asx import fetch_asx_stocks, get_asx_meta, update_asx_company_list
from data.us import fetch_us_stocks

logger = logging.getLogger(__name__)
app = Flask(__name__)

_asx_cache = {}
_us_cache = {}

def get_stocks_for_market(
    market: str,
    full: bool = False,
    force_yf: bool = False,
    list_id: str = None,
    signals_only: bool = False,
):
    market = (market or "asx").lower()
    if market in ("business", "buybacks"):
        from data.business_lists import BUILTIN_BUYBACKS_LIST_ID, fetch_business_list_stocks
        lid = list_id or (BUILTIN_BUYBACKS_LIST_ID if market == "buybacks" else "")
        return fetch_business_list_stocks(lid, force_yf=force_yf, signals_only=signals_only)
    if market == "us":
        return fetch_us_stocks(force_yf=force_yf)
    return fetch_asx_stocks(full=full, force_yf=force_yf)

def _get_market_meta(market: str, list_id: str = None):
    market = (market or "asx").lower()
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
                "last_scraped": datetime.now().isoformat()
            }
    else:
        try:
            meta = get_asx_meta()
        except Exception:
            meta = {
                "data_source": "yfinance + ASX Announcements (Markit Digital)",
                "last_scraped": datetime.now().isoformat()
            }
    meta["market"] = market
    return meta

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/stocks")
def api_stocks():
    market = request.args.get("market", "asx")
    list_id = request.args.get("list", "")
    full = request.args.get("full", "0").lower() in ("1", "true", "yes", "all")
    force_yf = request.args.get("force_yf", "0").lower() in ("1", "true", "yes")
    signals_only = request.args.get("signals_only", "0").lower() in ("1", "true", "yes")
    stocks = get_stocks_for_market(
        market, full=full, force_yf=force_yf, list_id=list_id, signals_only=signals_only,
    )
    rank = request.args.get("rank", "1").lower() not in ("0", "false", "no")
    if rank and stocks:
        try:
            from data.wiki_bridge import enrich_stocks_with_opportunities
            stocks = enrich_stocks_with_opportunities(stocks)
        except Exception as e:
            logger.warning("Opportunity enrichment failed: %s", e)
    meta = _get_market_meta(market, list_id=list_id)
    meta["full"] = bool(full)
    meta["force_yf"] = bool(force_yf)
    meta["list_id"] = list_id
    meta["signals_only"] = signals_only
    meta["rank_enriched"] = rank
    meta["total_universe"] = meta.get("universe_count") or (len(stocks) if not full else None)
    return jsonify({
        "stocks": stocks,
        "meta": meta
    })


@app.route("/api/opportunities")
def api_opportunities():
    """Ranked opportunities: quant + vault qual overlay."""
    market = request.args.get("market", "buybacks")
    list_id = request.args.get("list", "")
    force_yf = request.args.get("force_yf", "0").lower() in ("1", "true", "yes")
    min_quant = float(request.args.get("min_quant", 0) or 0)
    min_composite = float(request.args.get("min_composite", 0) or 0)
    limit = int(request.args.get("limit", 50) or 50)
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

@app.route("/api/kpis")
def api_kpis():
    market = request.args.get("market", "asx")
    full = request.args.get("full", "0").lower() in ("1", "true", "yes", "all")
    force_yf = request.args.get("force_yf", "0").lower() in ("1", "true", "yes")
    stocks = get_stocks_for_market(market, full=full, force_yf=force_yf)
    kpis = {
        "qualifying_stocks": len(stocks),
        "total_insider_buys": sum(s.get("insider_buys_2026", 0) for s in stocks),
        "companies_with_buybacks": sum(1 for s in stocks if s.get("buyback_announced")),
        "avg_discount_52w": round(sum(s.get("pct_from_52w_low", 0) for s in stocks) / max(len(stocks), 1), 1),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "meta": _get_market_meta(market)
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


@app.route("/api/stock/<ticker>")
def api_stock_detail(ticker):
    market = request.args.get("market", "asx")
    # For detail we do a targeted fresh lookup (fast for one ticker) + signals.
    # This gives rich metrics even if the list view was using batch-only data.
    t = (ticker or "").upper().replace(".AX", "")
    if market.lower() in ("buybacks", "business"):
        try:
            from data.business_lists import _fetch_single_business
            sym = (ticker or "").strip().upper()
            enriched = _fetch_single_business(sym)
            if enriched:
                return jsonify(enriched)
        except Exception as e:
            logger.warning(f"Business detail failed for {ticker}: {e}")
        list_id = request.args.get("list", "")
        market_key = "buybacks" if market.lower() == "buybacks" else "business"
        stocks = get_stocks_for_market(market_key, list_id=list_id)
        for s in stocks:
            if s["ticker"].upper() == (ticker or "").upper():
                return jsonify(s)
        return jsonify({"error": "Not found"}), 404

    if market.lower() == "us":
        # US detail: prefer fresh rich data (with price trend + earnings)
        try:
            from data.us import _fetch_single_us
            enriched = _fetch_single_us(t)
            if enriched:
                return jsonify(enriched)
        except Exception as e:
            logger.warning(f"Targeted US detail failed for {t}: {e}")

        # Fallback to the (small) list data
        stocks = get_stocks_for_market("us")
        for s in stocks:
            if s["ticker"].upper().replace(".AX", "") == t:
                return jsonify(s)
        return jsonify({"error": "Not found"}), 404

    # ASX detail: targeted
    try:
        from data.asx import _fetch_single_asx
        enriched = _fetch_single_asx(t)
        if enriched:
            return jsonify(enriched)
    except Exception as e:
        logger.warning(f"Targeted ASX detail failed for {t}: {e}")

    # Fallback: scan the cached full list
    stocks = get_stocks_for_market("asx", full=True)
    for s in stocks:
        if s["ticker"].upper().replace(".AX", "") == t:
            return jsonify(s)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    market = request.args.get("market", "asx")
    list_id = request.args.get("list", "")
    full = request.args.get("full", "0").lower() in ("1", "true", "yes", "all")
    force_yf = request.args.get("force_yf", "0").lower() in ("1", "true", "yes")
    if market == "business":
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
        from data.business_lists import _business_cache as biz_cache, BUILTIN_SP500_LIST_ID
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
        "meta": meta
    })


@app.route("/api/buybacks/refresh_hkex", methods=["POST"])
def api_buybacks_refresh_hkex():
    """Download missing HKEX daily repurchase XLS files and rebuild master CSV."""
    try:
        from data.buybacks import refresh_hkex_buybacks
        result = refresh_hkex_buybacks(download_gaps=True)
        if not result.get("ok"):
            return jsonify({
                "status": "error",
                "error": result.get("error") or result.get("build", {}).get("error", "HKEX refresh failed"),
            }), 500
        dl = result.get("download") or {}
        build = result.get("build") or {}
        return jsonify({
            "status": "ok",
            "message": (
                f"HKEX data updated ({dl.get('downloaded', 0)} new files, "
                f"{build.get('rows', result.get('rows', 0))} total rows)"
            ),
            "result": result,
            "meta": result.get("meta") or _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_hkex failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/buybacks/refresh_hk_insider", methods=["POST"])
def api_buybacks_refresh_hk_insider():
    """Download missing HKEX Disclosure of Interests daily summaries and rebuild insider CSV."""
    try:
        from data.buybacks import refresh_hk_insider_buybacks
        result = refresh_hk_insider_buybacks(download_gaps=True)
        if not result.get("ok"):
            return jsonify({
                "status": "error",
                "error": result.get("error") or result.get("build", {}).get("error", "HK insider refresh failed"),
            }), 500
        dl = result.get("download") or {}
        build = result.get("build") or {}
        return jsonify({
            "status": "ok",
            "message": (
                f"HK insider data updated ({dl.get('downloaded', 0)} new files, "
                f"{build.get('rows', result.get('rows', 0))} total rows)"
            ),
            "result": result,
            "meta": result.get("meta") or _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_hk_insider failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/buybacks/refresh_asx", methods=["POST"])
@app.route("/api/buybacks/refresh_asx_insider", methods=["POST"])
def api_buybacks_refresh_asx():
    """Paginated ASX market ingest + PDF-refine insider purchases + buyback announcements."""
    try:
        from data.buybacks import refresh_asx_buybacks_data
        backfill = request.args.get("backfill", "0").lower() in ("1", "true", "yes")
        incremental = request.args.get("incremental", "1").lower() in ("1", "true", "yes")
        if backfill:
            incremental = False
        result = refresh_asx_buybacks_data(
            days=365,
            use_pdf=True,
            incremental=incremental,
            backfill=backfill,
        )
        if not result.get("ok"):
            return jsonify({
                "status": "error",
                "error": result.get("error", "ASX refresh failed"),
            }), 500
        refine = result.get("refine") or {}
        ingest = result.get("ingest") or {}
        return jsonify({
            "status": "ok",
            "message": (
                f"ASX data updated ({result.get('total_buybacks', 0)} buybacks, "
                f"{result.get('total_purchases', 0)} insider purchases, "
                f"{ingest.get('pages_fetched', 0)} pages fetched, "
                f"{refine.get('pdf_checked', 0)} PDF-checked)"
            ),
            "result": result,
            "meta": result.get("meta") or _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_asx failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/buybacks/refresh_us/status", methods=["GET"])
def api_buybacks_refresh_us_status():
    """Progress for background US SEC buyback + Form 4 insider scan."""
    try:
        from data.buybacks import get_us_sec_refresh_status
        return jsonify(get_us_sec_refresh_status())
    except Exception as e:
        logger.exception("buybacks refresh_us status failed")
        return jsonify({"status": "error", "error": str(e)}), 500


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

        days = int(request.args.get("days", 365))
        max_tickers = request.args.get("max_tickers")
        max_tickers = int(max_tickers) if max_tickers else None
        reparse_10q = request.args.get("reparse_10q", "0").lower() in ("1", "true", "yes")

        if not mark_us_sec_refresh_started(max_tickers=max_tickers):
            return jsonify({
                "status": "already_running",
                "message": "US SEC scan already in progress.",
                "progress": get_us_sec_refresh_status(),
                "meta": _get_market_meta("buybacks"),
            })

        def _bg():
            try:
                refresh_us_buybacks_data(
                    days=days,
                    max_tickers=max_tickers,
                    reparse_10q=reparse_10q,
                )
                logger.info("Background US SEC buybacks scan finished.")
            except Exception as ex:
                logger.exception("US SEC buybacks scan error: %s", ex)

        import threading
        threading.Thread(target=_bg, daemon=True).start()
        progress = get_us_sec_refresh_status()
        return jsonify({
            "status": "started",
            "message": (
                "US SEC scan started in background (Russell 1000 + Nasdaq-100, "
                f"8-K/10-Q buybacks + Form 4 insider, last {days} days). "
                "Progress updates on the Buybacks tab."
            ),
            "progress": progress,
            "meta": _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_us failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/buybacks/refresh_jpx", methods=["POST"])
def api_buybacks_refresh_jpx():
    """Scrape JPX off-auction repurchases page and merge into project CSV."""
    try:
        from data.buybacks import refresh_jpx_buybacks
        result = refresh_jpx_buybacks(fetch_yfinance=False)
        if not result.get("ok"):
            return jsonify({"status": "error", "error": result.get("error", "JPX scrape failed")}), 500
        return jsonify({
            "status": "ok",
            "message": f"JPX data merged ({result.get('scraped', 0)} scraped, {result.get('total', 0)} total rows)",
            "result": result,
            "meta": result.get("meta") or _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks refresh_jpx failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/buybacks/rebuild_yf/status", methods=["GET"])
def api_buybacks_rebuild_yf_status():
    """Progress for background buybacks yfinance rebuild."""
    try:
        from data.buybacks import get_yf_rebuild_status
        return jsonify(get_yf_rebuild_status())
    except Exception as e:
        logger.exception("buybacks rebuild_yf status failed")
        return jsonify({"status": "error", "error": str(e)}), 500


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

        def _bg():
            try:
                rebuild_buybacks_yf_cache()
                logger.info("Background buybacks YF rebuild finished.")
            except Exception as ex:
                logger.exception("Buybacks YF rebuild error: %s", ex)

        import threading
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({
            "status": "started",
            "message": "YF rebuild started for buybacks registry. Table refreshes every 25 symbols.",
            "meta": _get_market_meta("buybacks"),
        })
    except Exception as e:
        logger.exception("buybacks rebuild_yf failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/business/rebuild_yf", methods=["POST"])
def api_business_rebuild_yf():
    """Force sequential yfinance refresh for a business list CSV."""
    list_id = request.args.get("list", "")
    if not list_id:
        return jsonify({"status": "error", "error": "list parameter required"}), 400
    try:
        from data.business_lists import _business_cache as biz_cache, rebuild_business_yf_cache
        biz_cache.clear()

        def _bg():
            try:
                rebuild_business_yf_cache(list_id)
                logger.info("Background business YF rebuild finished.")
            except Exception as ex:
                logger.exception("Business YF rebuild error: %s", ex)

        import threading
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({
            "status": "started",
            "message": f"YF rebuild started for business list '{list_id}'. Watch server logs for 'business yf progress'.",
            "meta": _get_market_meta("business", list_id=list_id),
        })
    except Exception as e:
        logger.exception("business rebuild_yf failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/asx/update_universe", methods=["POST"])
def api_update_asx_universe():
    """Trigger a refresh of the full ASX listed companies list from the official directory CSV.
    Intended to be run manually ~once every 6 months. Returns stats; follow with a data Refresh.
    """
    try:
        result = update_asx_company_list(force=True)
        # Clear the data cache so next load sees fresh universe for filtering
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
    """Force a full sequential yfinance refresh for the current ASX universe.
    This is *one call at a time* with long sleeps and can take 1-3+ hours for ~2000
    tickers. Run only every 2-4 weeks.
    The work runs in a background thread so this HTTP call returns quickly; watch the
    server console (the python app.py process) for progress like 'yf sequential progress: 50/1979'.
    Prefer the CLI for the heaviest runs:
      python -c \"from data.asx import rebuild_asx_yf_cache; rebuild_asx_yf_cache()\"
    """
    market = request.args.get("market", "asx")
    full = request.args.get("full", "1").lower() in ("1", "true", "yes", "all")
    try:
        from data.asx import _asx_cache as asx_c
        asx_c.clear()

        def _bg_rebuild():
            try:
                get_stocks_for_market(market, full=full, force_yf=True)
                logger.info('Background YF rebuild finished.')
            except Exception as ex:
                logger.exception('Background YF rebuild error: %s', ex)

        import threading
        threading.Thread(target=_bg_rebuild, daemon=True).start()

        return jsonify({
            "status": "started",
            "message": "YF rebuild started in background thread (sequential, slow). Monitor the server console/logs for 'yf sequential progress'. It will update the persistent cache.",
            "meta": _get_market_meta(market)
        })
    except Exception as e:
        logger.exception("rebuild_yf start failed")
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    print("Multi-Market Signal Screener")
    print("→ Open http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)