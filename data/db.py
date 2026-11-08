"""
Centralized SQLite connection management and one-time schema initialization.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNIVERSE_DB = "asx_announcements_cache.db"

_init_lock = threading.Lock()
_initialized_schemas: set[tuple[str, str]] = set()
_wal_enabled_dbs: set[str] = set()


def _resolve_db_key(db_path: str | Path) -> str:
    try:
        p = Path(db_path)
        if not p.is_absolute():
            cand = _PROJECT_ROOT / p
            if str(db_path) == DEFAULT_UNIVERSE_DB or cand.exists():
                p = cand
        return str(p.resolve())
    except Exception:
        return str(db_path)


def connect_db(
    db_path: str | Path = DEFAULT_UNIVERSE_DB,
    *,
    schema_key: str | None = None,
    ddl: Iterable[str] | Callable[[sqlite3.Connection], None] | None = None,
    timeout: float = 15.0,
) -> sqlite3.Connection:
    """
    Open a SQLite connection with WAL mode and optional one-time schema initialization.

    `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` statements in `ddl`
    are executed only once per `(resolved_db_path, schema_key)` per process.
    """
    db_key = _resolve_db_key(db_path)
    conn = sqlite3.connect(db_key, timeout=timeout)

    if db_key not in _wal_enabled_dbs:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            _wal_enabled_dbs.add(db_key)
        except Exception:
            pass

    if schema_key and ddl is not None:
        init_token = (db_key, schema_key)
        if init_token not in _initialized_schemas:
            with _init_lock:
                if init_token not in _initialized_schemas:
                    if callable(ddl):
                        ddl(conn)
                    else:
                        for stmt in ddl:
                            if stmt and stmt.strip():
                                conn.execute(stmt)
                    conn.commit()
                    _initialized_schemas.add(init_token)
    return conn


@contextmanager
def db_session(
    db_path: str | Path = DEFAULT_UNIVERSE_DB,
    *,
    schema_key: str | None = None,
    ddl: Iterable[str] | Callable[[sqlite3.Connection], None] | None = None,
    commit: bool = False,
    timeout: float = 15.0,
):
    """Context manager that guarantees `conn.close()` (and optional `conn.commit()`)."""
    conn = connect_db(db_path, schema_key=schema_key, ddl=ddl, timeout=timeout)
    try:
        yield conn
        if commit:
            conn.commit()
    finally:
        conn.close()
