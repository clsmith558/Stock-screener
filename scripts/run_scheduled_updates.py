#!/usr/bin/env python3
"""
Catch-up orchestrator for screener data jobs.

Designed for Windows Task Scheduler (or manual CLI):
  - Runs only when the OS is awake (sleep freezes the CPU).
  - Skips jobs whose last success is fresher than max_age_hours.
  - Logs to logs/scheduled_updates.log and state JSON for status.

Usage:
  python scripts/run_scheduled_updates.py
  python scripts/run_scheduled_updates.py --force
  python scripts/run_scheduled_updates.py --only asx_announcements
  python scripts/run_scheduled_updates.py --status
  python scripts/run_scheduled_updates.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# datetime used in _run_job timeout checks

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config" / "update_schedule.yaml"
DEFAULT_LOG_DIR = ROOT / "logs"
STATE_NAME = "scheduled_updates_state.json"
LOG_NAME = "scheduled_updates.log"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _load_yaml(path: Path) -> dict:
    import yaml

    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _state_path(log_dir: Path) -> Path:
    return log_dir / STATE_NAME


def _load_state(log_dir: Path) -> dict:
    p = _state_path(log_dir)
    if not p.exists():
        return {"jobs": {}, "last_orchestrator_run": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}, "last_orchestrator_run": None}


def _save_state(log_dir: Path, state: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    _state_path(log_dir).write_text(
        json.dumps(state, indent=2, default=str), encoding="utf-8"
    )


def _setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_NAME
    logger = logging.getLogger("scheduled_updates")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_now().tzinfo)
        return dt
    except Exception:
        return None


def _is_stale(last_success: str | None, max_age_hours: float) -> bool:
    if max_age_hours <= 0:
        return True
    dt = _parse_iso(last_success)
    if dt is None:
        return True
    return _now() - dt > timedelta(hours=max_age_hours)


def _on_battery() -> bool | None:
    """Return True if on battery, False if AC, None if unknown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", wintypes.BYTE),
                ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE),
                ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD),
            ]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        # 0 = offline (battery), 1 = AC
        if status.ACLineStatus == 0:
            return True
        if status.ACLineStatus == 1:
            return False
        return None
    except Exception:
        return None


def _resolve_python() -> Path:
    """Prefer project venv on Windows/Unix."""
    if sys.platform == "win32":
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return venv_py
    return Path(sys.executable)


def _run_job(
    job: dict,
    *,
    python: Path,
    logger: logging.Logger,
    dry_run: bool,
) -> dict[str, Any]:
    cmd_parts = job.get("command") or []
    if not cmd_parts:
        return {"ok": False, "error": "empty command", "skipped": False}

    script = cmd_parts[0]
    script_path = ROOT / script if not Path(script).is_absolute() else Path(script)
    args = [str(python), str(script_path), *cmd_parts[1:]]
    timeout = int(job.get("timeout_seconds") or 0) or None
    jid = job.get("id") or "job"

    result: dict[str, Any] = {
        "id": jid,
        "command": args,
        "started_at": _iso(),
        "ok": False,
        "skipped": False,
    }
    logger.info("RUN %s: %s", jid, " ".join(args))
    if dry_run:
        result["ok"] = True
        result["dry_run"] = True
        result["finished_at"] = _iso()
        logger.info("DRY-RUN skip execute %s", jid)
        return result

    try:
        # Stream child stdout/stderr live (capture_output would hide progress for hours)
        child_env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        started = datetime.now()
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            bufsize=1,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip("\n\r")
            if not text:
                continue
            lines.append(text)
            if len(lines) > 200:
                lines = lines[-150:]
            # Forward progress to orchestrator log + console
            logger.info("[%s] %s", jid, text)
            # Heartbeat if child is quiet for a long stretch is handled by child itself
            if timeout and (datetime.now() - started).total_seconds() > timeout:
                proc.kill()
                raise subprocess.TimeoutExpired(args, timeout)

        returncode = proc.wait(timeout=30)
        result["returncode"] = returncode
        result["ok"] = returncode == 0
        if lines:
            result["stdout_tail"] = "\n".join(lines[-40:])[-2000:]
        if returncode != 0:
            logger.error("[%s] exited with code %s", jid, returncode)
        else:
            logger.info("[%s] finished OK", jid)
    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["error"] = f"timeout after {timeout}s"
        logger.error("TIMEOUT %s after %ss", jid, timeout)
        try:
            proc.kill()  # type: ignore[name-defined]
        except Exception:
            pass
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.exception("FAIL %s: %s", jid, e)

    result["finished_at"] = _iso()
    return result


def run_updates(
    *,
    force: bool = False,
    only: list[str] | None = None,
    dry_run: bool = False,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    cfg = _load_yaml(config_path)
    defaults = cfg.get("defaults") or {}
    log_dir = ROOT / (defaults.get("log_dir") or "logs")
    logger = _setup_logging(log_dir)
    state = _load_state(log_dir)
    state.setdefault("jobs", {})
    python = _resolve_python()

    only_set = {x.strip() for x in (only or []) if x.strip()}
    jobs = cfg.get("jobs") or []
    if only_set:
        jobs = [j for j in jobs if j.get("id") in only_set]

    summary: dict[str, Any] = {
        "started_at": _iso(),
        "force": force,
        "dry_run": dry_run,
        "python": str(python),
        "results": [],
        "ran": 0,
        "skipped": 0,
        "failed": 0,
    }

    on_batt = _on_battery()
    if defaults.get("skip_on_battery") and on_batt is True:
        logger.warning("On battery and skip_on_battery=true — aborting orchestrator")
        summary["aborted"] = "skip_on_battery"
        summary["finished_at"] = _iso()
        return summary

    logger.info(
        "=== scheduled updates start force=%s dry_run=%s battery=%s ===",
        force,
        dry_run,
        on_batt,
    )

    for job in jobs:
        jid = job.get("id") or "unnamed"
        if not job.get("enabled", True) and not (only_set and jid in only_set):
            logger.info("DISABLED %s", jid)
            summary["results"].append({"id": jid, "skipped": True, "reason": "disabled"})
            summary["skipped"] += 1
            continue

        max_age = float(job.get("max_age_hours") or 24)
        prev = (state.get("jobs") or {}).get(jid) or {}
        last_ok = prev.get("last_success_at")
        if not force and not _is_stale(last_ok, max_age):
            logger.info(
                "FRESH %s (last success %s, max_age=%sh) — skip",
                jid,
                last_ok,
                max_age,
            )
            summary["results"].append(
                {
                    "id": jid,
                    "skipped": True,
                    "reason": "fresh",
                    "last_success_at": last_ok,
                }
            )
            summary["skipped"] += 1
            continue

        result = _run_job(job, python=python, logger=logger, dry_run=dry_run)
        summary["results"].append(result)
        if result.get("skipped"):
            summary["skipped"] += 1
        elif result.get("ok"):
            summary["ran"] += 1
            if not dry_run:
                state["jobs"][jid] = {
                    "last_success_at": result.get("finished_at"),
                    "last_attempt_at": result.get("finished_at"),
                    "last_returncode": result.get("returncode", 0),
                    "description": job.get("description") or "",
                }
        else:
            summary["failed"] += 1
            if not dry_run:
                state["jobs"][jid] = {
                    **(state["jobs"].get(jid) or {}),
                    "last_attempt_at": result.get("finished_at"),
                    "last_returncode": result.get("returncode"),
                    "last_error": result.get("error") or result.get("stderr_tail", "")[-400:],
                    "description": job.get("description") or "",
                }

    summary["finished_at"] = _iso()
    state["last_orchestrator_run"] = summary["finished_at"]
    if not dry_run:
        _save_state(log_dir, state)
    logger.info(
        "=== done ran=%s skipped=%s failed=%s ===",
        summary["ran"],
        summary["skipped"],
        summary["failed"],
    )
    logger.info("Scheduled updates finished; console may close if launched by Task Scheduler.")
    return summary


def print_status(config_path: Path = CONFIG_PATH) -> int:
    cfg = _load_yaml(config_path)
    defaults = cfg.get("defaults") or {}
    log_dir = ROOT / (defaults.get("log_dir") or "logs")
    state = _load_state(log_dir)
    print(f"State file: {_state_path(log_dir)}")
    print(f"Log file:   {log_dir / LOG_NAME}")
    print(f"Last orchestrator run: {state.get('last_orchestrator_run') or '—'}")
    print()
    jobs = cfg.get("jobs") or []
    for job in jobs:
        jid = job.get("id")
        en = "on " if job.get("enabled", True) else "off"
        max_age = job.get("max_age_hours")
        prev = (state.get("jobs") or {}).get(jid) or {}
        last = prev.get("last_success_at") or "never"
        stale = _is_stale(prev.get("last_success_at"), float(max_age or 24))
        flag = "STALE" if stale else "fresh"
        print(
            f"  [{en}] {jid:20} max_age={max_age}h  last_ok={last}  ({flag})"
        )
        if prev.get("last_error"):
            print(f"         last_error: {str(prev.get('last_error'))[:120]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stale screener data update jobs")
    parser.add_argument("--force", action="store_true", help="Run enabled jobs even if fresh")
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated job ids (e.g. asx_announcements,unit_cost_history)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log what would run")
    parser.add_argument("--status", action="store_true", help="Show last success ages")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Path to update_schedule.yaml",
    )
    args = parser.parse_args()

    if args.status:
        return print_status(args.config)

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    summary = run_updates(
        force=args.force,
        only=only,
        dry_run=args.dry_run,
        config_path=args.config,
    )
    if summary.get("aborted"):
        return 2
    if summary.get("failed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
