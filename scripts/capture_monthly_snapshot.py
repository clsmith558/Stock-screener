#!/usr/bin/env python3
"""
Capture the current calendar month's point-in-time global snapshot (if missing or
--force) and update realized forward returns on prior monthly snapshots.

Scheduled alternative to Flask's monthly snapshot daemon thread so monthly
snapshots record reliably even when the web UI server is closed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("capture_monthly_snapshot")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture monthly stock snapshot & update forward returns")
    parser.add_argument(
        "--month",
        default="",
        help="Target YYYY-MM month (default: current calendar month)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-capture target month even if already recorded",
    )
    parser.add_argument(
        "--db-path",
        default=str(ROOT / "asx_announcements_cache.db"),
        help="Path to SQLite universe DB",
    )
    args = parser.parse_args()

    from scoring.historical_snapshots import capture_monthly_snapshot

    month = args.month.strip() or None
    logger.info(
        "=== capture_monthly_snapshot start month=%s force=%s db=%s ===",
        month or "current",
        args.force,
        args.db_path,
    )
    res = capture_monthly_snapshot(
        snapshot_month=month,
        force=bool(args.force),
        db_path=args.db_path,
    )
    status = res.get("status")
    snap_month = res.get("snapshot_month")
    total = res.get("total_stocks")
    outliers = res.get("outlier_count")
    logger.info(
        "=== capture_monthly_snapshot done status=%s month=%s total=%s outliers=%s ===",
        status,
        snap_month,
        total,
        outliers,
    )
    print(f"Monthly snapshot {snap_month}: status={status} total={total} outliers={outliers}")
    return 0 if status in ("captured", "already_exists") else 1


if __name__ == "__main__":
    raise SystemExit(main())
