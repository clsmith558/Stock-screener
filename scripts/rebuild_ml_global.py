#!/usr/bin/env python3
"""
Rebuild the global Unsupervised ML cache (K-Means, k-NN Comps, Isolation Forest,
Huber Regression, PCA/manifold) across all ~17,000+ cached global stocks.

Scheduled alternative to Flask's weekly background daemon thread so ML profiles
stay fresh even when the web UI server is not running.
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
logger = logging.getLogger("rebuild_ml_global")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild global Unsupervised ML cache")
    parser.add_argument("--neighbors", type=int, default=5, help="k-NN neighbors (default 5)")
    parser.add_argument("--clusters", type=int, default=6, help="K-Means clusters (default 6)")
    parser.add_argument(
        "--db-path",
        default=str(ROOT / "asx_announcements_cache.db"),
        help="Path to SQLite universe DB",
    )
    args = parser.parse_args()

    from scoring.unsupervised_ml import rebuild_global_unsupervised_ml

    logger.info(
        "=== rebuild_ml_global start clusters=%d neighbors=%d db=%s ===",
        args.clusters,
        args.neighbors,
        args.db_path,
    )
    res = rebuild_global_unsupervised_ml(
        n_neighbors=args.neighbors,
        n_clusters=args.clusters,
        db_path=args.db_path,
    )
    if res.get("status") == "already_running":
        logger.warning("ML rebuild skipped: lock already held")
        return 0

    modeled = int(res.get("modeled_stocks") or 0)
    total = int(res.get("total_stocks") or 0)
    computed_at = res.get("computed_at") or ""
    logger.info(
        "=== rebuild_ml_global done: modeled=%d total=%d computed_at=%s ===",
        modeled,
        total,
        computed_at,
    )
    print(f"ML global rebuild complete: {modeled}/{total} modeled at {computed_at}")
    return 0 if modeled > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
