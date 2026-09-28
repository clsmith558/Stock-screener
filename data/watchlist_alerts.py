"""
Watchlist alerts — fire when a watched name hits a new condition.

Default conditions:
  - recent_52w_low: days_since_52w_low ≤ N (default 20)
  - burry_pass: passes Burry gates (Python port of dashboard defaults)

State is stored in watchlist.db so the same condition does not re-fire
until it clears and re-triggers (or cooldown expires).

Also appends a line to logs/watchlist_alerts.log on each new fire.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = str(_PROJECT_ROOT / "data" / "watchlist.db")
ALERT_LOG = _PROJECT_ROOT / "logs" / "watchlist_alerts.log"

# Cooldown: do not re-alert the same (ticker, type) within this many hours
_DEFAULT_COOLDOWN_HOURS = 48


def _conn(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_alert_state (
            ticker TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            last_fired_at TEXT,
            last_payload TEXT,
            PRIMARY KEY (ticker, alert_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist_alert_prefs (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_alert_prefs(db_path: str = DEFAULT_DB) -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "alert_52w_low": True,
        "alert_burry": True,
        "days_52w_low": 20,
        "cooldown_hours": _DEFAULT_COOLDOWN_HOURS,
        "browser_notify": True,
    }
    conn = _conn(db_path)
    rows = conn.execute("SELECT key, value FROM watchlist_alert_prefs").fetchall()
    conn.close()
    for k, v in rows:
        if k not in defaults:
            continue
        if k in ("enabled", "alert_52w_low", "alert_burry", "browser_notify"):
            defaults[k] = str(v).lower() in ("1", "true", "yes", "on")
        elif k in ("days_52w_low", "cooldown_hours"):
            try:
                defaults[k] = int(float(v))
            except (TypeError, ValueError):
                pass
    return defaults


def set_alert_prefs(prefs: dict, db_path: str = DEFAULT_DB) -> dict[str, Any]:
    cur = get_alert_prefs(db_path)
    for k, v in (prefs or {}).items():
        if k in cur:
            cur[k] = v
    conn = _conn(db_path)
    for k, v in cur.items():
        conn.execute(
            "INSERT OR REPLACE INTO watchlist_alert_prefs (key, value) VALUES (?,?)",
            (k, json.dumps(v) if isinstance(v, (dict, list)) else str(v)),
        )
    conn.commit()
    conn.close()
    return cur


def _append_log(line: str):
    try:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception as e:
        logger.debug("alert log write failed: %s", e)


def _passes_burry(stock: dict) -> bool:
    """Lightweight Burry-ish gate aligned with dashboard defaults (no peer median)."""
    if stock.get("_pending_yf"):
        return False
    m = stock.get("metrics") or {}
    try:
        gap = float(stock.get("pct_from_52w_low") if stock.get("pct_from_52w_low") is not None else 999)
    except (TypeError, ValueError):
        gap = 999
    if gap > 15:
        return False
    pfcf = float(m.get("p_fcf") or stock.get("p_fcf") or 0)
    fcf_y = float(m.get("fcf_yield") or 0)
    fcf_m = float(m.get("fcf_m") or 0)
    ev_fcf = float(m.get("ev_fcf") or 0)
    has_fcf = fcf_m > 0 or pfcf > 0 or fcf_y > 0
    if not has_fcf:
        return False
    cheap = (pfcf > 0 and pfcf <= 15) or (fcf_y >= 5) or (ev_fcf > 0 and ev_fcf <= 12)
    if not cheap:
        return False
    de = float(stock.get("debt_to_equity") or m.get("debt_to_equity") or 0)
    net_cash = float(m.get("net_cash_m") or 0)
    debt_mcap = float(m.get("debt_to_mcap") or 0)
    de_ok = de > 0 and de <= 0.5
    net_ok = net_cash >= 0
    dm_ok = debt_mcap > 0 and debt_mcap <= 0.35
    if not (de_ok or net_ok or dm_ok):
        return False
    # EV/EBITDA peer gate skipped in alert path (list-scoped); soft pass if multiple present and not absurd
    ev_ebitda = float(m.get("ev_ebitda") or stock.get("ev_ebitda") or 0)
    if ev_ebitda > 0 and ev_ebitda > 25:
        return False
    return True


def _days_since_52w_low(stock: dict) -> int | None:
    m = stock.get("metrics") or {}
    d = stock.get("days_since_52w_low")
    if d is None:
        d = m.get("days_since_52w_low")
    if d is not None:
        try:
            return int(float(d))
        except (TypeError, ValueError):
            pass
    iso = stock.get("last_52w_low_date") or m.get("last_52w_low_date")
    if not iso:
        return None
    try:
        dt = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - dt).days
    except ValueError:
        return None


def evaluate_watchlist_alerts(
    stocks: list[dict] | None = None,
    *,
    db_path: str = DEFAULT_DB,
) -> dict[str, Any]:
    """
    Evaluate watchlist names and return newly fired alerts.

    If stocks is None, hydrates from watchlist caches.
    """
    prefs = get_alert_prefs(db_path)
    if not prefs.get("enabled"):
        return {"enabled": False, "new_alerts": [], "active": [], "prefs": prefs}

    if stocks is None:
        try:
            from data.watchlist import fetch_watchlist_stocks

            stocks = fetch_watchlist_stocks()
        except Exception as e:
            logger.warning("watchlist hydrate for alerts failed: %s", e)
            stocks = []

    # Attach light scores / Burry if possible
    try:
        from data.wiki_bridge import enrich_stocks_with_opportunities

        stocks = enrich_stocks_with_opportunities(stocks or [])
    except Exception:
        pass

    cooldown_h = int(prefs.get("cooldown_hours") or _DEFAULT_COOLDOWN_HOURS)
    days_n = int(prefs.get("days_52w_low") or 20)
    now = datetime.now()
    new_alerts: list[dict] = []
    active: list[dict] = []

    conn = _conn(db_path)
    for s in stocks or []:
        ticker = (s.get("ticker") or "").upper()
        if not ticker:
            continue
        name = s.get("name") or ticker
        candidates: list[tuple[str, str, dict]] = []

        if prefs.get("alert_52w_low"):
            days = _days_since_52w_low(s)
            gap = s.get("pct_from_52w_low")
            if days is not None and days <= days_n:
                candidates.append(
                    (
                        "recent_52w_low",
                        f"{ticker}: 52w low {days}d ago (gap {gap}%)",
                        {"days": days, "gap": gap, "last_52w_low_date": s.get("last_52w_low_date") or (s.get("metrics") or {}).get("last_52w_low_date")},
                    )
                )

        if prefs.get("alert_burry") and _passes_burry(s):
            candidates.append(
                (
                    "burry_pass",
                    f"{ticker}: passes Burry-style gates (gap {s.get('pct_from_52w_low')}%)",
                    {
                        "gap": s.get("pct_from_52w_low"),
                        "p_fcf": s.get("p_fcf") or (s.get("metrics") or {}).get("p_fcf"),
                        "quant_score": s.get("quant_score"),
                    },
                )
            )

        seen_types = {c[0] for c in candidates}
        for alert_type, message, payload in candidates:
            row = conn.execute(
                "SELECT active, last_fired_at FROM watchlist_alert_state WHERE ticker=? AND alert_type=?",
                (ticker, alert_type),
            ).fetchone()
            was_active = bool(row and row[0])
            last_fired = row[1] if row else None
            fire = False
            if not was_active:
                fire = True
            elif last_fired:
                try:
                    lf = datetime.fromisoformat(last_fired)
                    if now - lf >= timedelta(hours=cooldown_h):
                        fire = True
                except ValueError:
                    fire = True

            active.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "alert_type": alert_type,
                    "message": message,
                    "payload": payload,
                }
            )
            if fire:
                fired_at = now.isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO watchlist_alert_state (ticker, alert_type, active, last_fired_at, last_payload)
                    VALUES (?,?,1,?,?)
                    ON CONFLICT(ticker, alert_type) DO UPDATE SET
                        active=1, last_fired_at=excluded.last_fired_at, last_payload=excluded.last_payload
                    """,
                    (ticker, alert_type, fired_at, json.dumps(payload)),
                )
                alert = {
                    "ticker": ticker,
                    "name": name,
                    "alert_type": alert_type,
                    "message": message,
                    "payload": payload,
                    "fired_at": fired_at,
                }
                new_alerts.append(alert)
                _append_log(f"{fired_at}\t{alert_type}\t{ticker}\t{message}")

        # Clear active flag when condition no longer true
        for atype in ("recent_52w_low", "burry_pass"):
            if atype not in seen_types:
                conn.execute(
                    """
                    INSERT INTO watchlist_alert_state (ticker, alert_type, active, last_fired_at, last_payload)
                    VALUES (?,?,0,NULL,NULL)
                    ON CONFLICT(ticker, alert_type) DO UPDATE SET active=0
                    """,
                    (ticker, atype),
                )

    conn.commit()
    conn.close()
    return {
        "enabled": True,
        "new_alerts": new_alerts,
        "active": active,
        "prefs": prefs,
        "checked_at": now.isoformat(timespec="seconds"),
        "log_path": str(ALERT_LOG),
    }
