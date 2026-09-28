import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.asx import DEFAULT_UNIVERSE_DB

conn = sqlite3.connect(DEFAULT_UNIVERSE_DB)
pat = re.compile(
    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*(?:[-–—]|to)\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
    re.I,
)

print("sample ok labels:")
for r in conn.execute(
    """
    SELECT period_label, period_end, shares, yf_symbol FROM us_buyback_repurchases
    WHERE period_end <= date('now') AND period_end >= '2025-01-01'
    ORDER BY random() LIMIT 12
    """
):
    print(r)

print("--- future span days ---")
spans = []
for r in conn.execute(
    "SELECT yf_symbol, period_label, period_end, shares, filing_date "
    "FROM us_buyback_repurchases WHERE period_end > date('now')"
):
    m = pat.search(r[1] or "")
    if not m:
        print("no match", r)
        continue
    a = datetime.strptime(m.group(1), "%B %d, %Y")
    b = datetime.strptime(m.group(2), "%B %d, %Y")
    days = (b - a).days
    spans.append(days)
    print(f"{r[0]:6} span={days:5}d shares={r[3]} filing={r[4]} | {r[1][:75]}")

if spans:
    print(
        "span stats n",
        len(spans),
        "median",
        sorted(spans)[len(spans) // 2],
        "gt_90d",
        sum(1 for d in spans if d > 90),
        "gt_400d",
        sum(1 for d in spans if d > 400),
    )

n_long = n_all = 0
for (lab,) in conn.execute("SELECT period_label FROM us_buyback_repurchases"):
    n_all += 1
    m = pat.search(lab or "")
    if m:
        a = datetime.strptime(m.group(1), "%B %d, %Y")
        b = datetime.strptime(m.group(2), "%B %d, %Y")
        if (b - a).days > 45:
            n_long += 1
print("all rows", n_all, "span>45d", n_long)
