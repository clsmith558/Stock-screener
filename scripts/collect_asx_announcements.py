#!/usr/bin/env python3
"""
Daily ASX announcement collector — run via Windows Task Scheduler.

Incremental ingest (recent pages) + PDF-refine insider Appendix 3Y notices +
parse buy-back share/price metrics from Appendix 3C/3E PDFs + rebuild buybacks registry.

Windows Task Scheduler (weekdays ~7:30 AM Sydney):
  Program: <repo>\\.venv\\Scripts\\python.exe
  Arguments: scripts\\collect_asx_announcements.py
  Start in: <repo>

One-time backfill (~2-3 weeks of API history):
  python scripts/collect_asx_announcements.py --backfill

Parse share/price metrics for buy-backs already in the DB (no market re-fetch):
  python scripts/collect_asx_announcements.py --buyback-metrics-only --max-buyback-pdf 400
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("collect_asx")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect ASX insider/buyback announcements")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Paginate full API window (~2-3 weeks); default is incremental",
    )
    parser.add_argument("--days", type=int, default=365, help="Lookback window for DB/refine")
    parser.add_argument("--no-pdf", action="store_true", help="Skip PDF refinement")
    parser.add_argument("--no-registry", action="store_true", help="Skip buybacks registry rebuild")
    parser.add_argument(
        "--max-buyback-pdf",
        type=int,
        default=400,
        help="Max buy-back PDFs to parse for shares/price metrics (default 400)",
    )
    parser.add_argument(
        "--buyback-metrics-only",
        action="store_true",
        help="Skip market ingest; only parse buy-back PDFs missing share/price metrics",
    )
    args = parser.parse_args()

    from asx_announcements import refresh_asx_announcements, _refine_buyback_metrics_rows

    if args.buyback_metrics_only:
        logger.info(
            "Buyback metrics-only pass (days=%d, max_pdf=%d)",
            args.days,
            args.max_buyback_pdf,
        )
        bb = _refine_buyback_metrics_rows(
            days=args.days,
            use_pdf=not args.no_pdf,
            max_pdf_refine=args.max_buyback_pdf,
        )
        logger.info(
            "Buyback metrics: checked=%s parsed=%s shares=%s price=%s",
            bb.get("pdf_checked"),
            bb.get("parsed"),
            bb.get("with_shares"),
            bb.get("with_price"),
        )
        if not args.no_registry:
            from data.buybacks import (
                _buybacks_cache,
                _registry_set_meta,
                fetch_buyback_stocks,
            )
            _buybacks_cache.clear()
            _registry_set_meta({"asx_buyback_rows": ""})
            stocks = fetch_buyback_stocks(force_yf=False, force_registry=True)
            logger.info("Buybacks registry rebuilt: %d stocks", len(stocks))
        return 0

    logger.info(
        "Starting ASX collection (backfill=%s, days=%d)",
        args.backfill,
        args.days,
    )
    result = refresh_asx_announcements(
        days=args.days,
        use_pdf=not args.no_pdf,
        incremental=not args.backfill,
        backfill=args.backfill,
        max_buyback_pdf=args.max_buyback_pdf,
    )
    if not result.get("ok"):
        logger.error("ASX collection failed: %s", result.get("error"))
        return 1

    ingest = result.get("ingest") or {}
    refine = result.get("refine") or {}
    logger.info(
        "Ingest: %d pages, %d rows, stopped=%s",
        ingest.get("pages_fetched", 0),
        ingest.get("relevant_rows", 0),
        ingest.get("stopped_reason", "?"),
    )
    logger.info(
        "Refine: %d PDF-checked, %d purchases, %d sales",
        refine.get("pdf_checked", 0),
        refine.get("refined_purchase", 0),
        refine.get("refined_sale", 0),
    )
    bb_metrics = result.get("buyback_metrics") or {}
    logger.info(
        "Buyback metrics: %d PDF-checked, %d parsed, %d with shares, %d with price",
        bb_metrics.get("pdf_checked", 0),
        bb_metrics.get("parsed", 0),
        bb_metrics.get("with_shares", 0),
        bb_metrics.get("with_price", 0),
    )
    logger.info(
        "Totals: %d buybacks, %d insider purchases in DB",
        result.get("total_buybacks", 0),
        result.get("total_purchases", 0),
    )

    if not args.no_registry:
        from data.buybacks import (
            _buybacks_cache,
            _registry_set_meta,
            fetch_buyback_stocks,
            refresh_hkex_buybacks,
            refresh_hk_insider_buybacks,
            refresh_jpx_buybacks,
        )

        try:
            hk = refresh_hkex_buybacks(download_gaps=True)
            logger.info(
                "HKEX refresh: ok=%s rows=%s downloaded=%s",
                hk.get("ok"),
                hk.get("rows"),
                (hk.get("download") or {}).get("downloaded"),
            )
        except Exception:
            logger.exception("HKEX buyback refresh failed")
        try:
            di = refresh_hk_insider_buybacks(download_gaps=True)
            logger.info("HKEX insider refresh: %s", di)
        except Exception:
            logger.exception("HKEX insider refresh failed")
        try:
            jpx = refresh_jpx_buybacks(fetch_yfinance=False)
            logger.info("JPX refresh: %s", jpx)
        except Exception:
            logger.exception("JPX buyback refresh failed")
        try:
            import importlib.util

            asia_path = ROOT / "scripts" / "refresh_china_hk_index_lists.py"
            spec = importlib.util.spec_from_file_location("refresh_asia_lists", asia_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cn = mod.harvest_china_buyback_tickers()
            logger.info("A-share buyback harvest: %d tickers", len(cn or []))
        except Exception:
            logger.exception("A-share buyback harvest failed")

        _buybacks_cache.clear()
        _registry_set_meta({"asx_insider_rows": "", "asx_buyback_rows": ""})
        stocks = fetch_buyback_stocks(force_yf=False, force_registry=True)
        logger.info("Buybacks registry rebuilt: %d stocks", len(stocks))

    logger.info("ASX collection complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())