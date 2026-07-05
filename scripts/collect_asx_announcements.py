#!/usr/bin/env python3
"""
Daily ASX announcement collector — run via Windows Task Scheduler.

Incremental ingest (recent pages) + PDF-refine insider Appendix 3Y notices +
rebuild buybacks registry.

Windows Task Scheduler (weekdays ~7:30 AM Sydney):
  Program: <repo>\\.venv\\Scripts\\python.exe
  Arguments: scripts\\collect_asx_announcements.py
  Start in: <repo>

One-time backfill (~2-3 weeks of API history):
  python scripts/collect_asx_announcements.py --backfill
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
    args = parser.parse_args()

    from asx_announcements import refresh_asx_announcements

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
        )

        _buybacks_cache.clear()
        _registry_set_meta({"asx_insider_rows": "", "asx_buyback_rows": ""})
        stocks = fetch_buyback_stocks(force_yf=False, force_registry=True)
        logger.info("Buybacks registry rebuilt: %d stocks", len(stocks))

    logger.info("ASX collection complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())