"""
Shared DB connection layer — MySQL (pooled) when MYSQL_HOST is configured,
else falls back to the original SQLite file (APP_DB) unchanged.

Every call site across the app uses the same pattern that worked with plain
sqlite3.connect(APP_DB):

    with get_conn() as conn:
        row = conn.execute("SELECT ... WHERE x = ?", (val,)).fetchone()
        conn.commit()

For the SQLite path this returns a plain sqlite3.Connection (zero overhead,
zero behavior change). For the MySQL path it returns a pooled connection
wrapped so '?' placeholders, SQLite upsert syntax, and
'CREATE INDEX IF NOT EXISTS' keep working without editing the SQL in every
call site.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import threading
from typing import Any, Optional, Sequence

APP_DB = os.getenv("SESSIONS_DB", os.getenv("APP_DB", "data/app.db"))

MYSQL_HOST = os.getenv("MYSQL_HOST", "").strip()
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306") or "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "").strip()
MYSQL_USER = os.getenv("MYSQL_USER", "").strip()
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA", "").strip()
MYSQL_SSL_DISABLED = os.getenv("MYSQL_SSL_DISABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

USE_MYSQL = bool(MYSQL_HOST and MYSQL_DATABASE and MYSQL_USER)

# SQLite is a single-writer file — callers serialize writes through this lock.
# MySQL handles concurrent connections itself, so the lock is a no-op there
# (keeping a real-but-shared lock would needlessly serialize every request
# through one Python-level mutex and defeat the point of pooling connections).
_lock = contextlib.nullcontext() if USE_MYSQL else threading.Lock()

_DUP_KEY_ERRNO = 1061  # "Duplicate key name" — swallowed for IF-NOT-EXISTS index creation


def _ensure_sqlite_dir():
    folder = os.path.dirname(APP_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)


def _translate_sql(sql: str) -> str:
    """SQLite-isms -> MySQL equivalents. No-op for plain SELECT/INSERT/UPDATE/DELETE."""
    sql = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
    sql = re.sub(
        r"ON CONFLICT\s*\([^)]*\)\s*DO UPDATE SET",
        "ON DUPLICATE KEY UPDATE",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"excluded\.(\w+)", r"VALUES(\1)", sql)
    sql = sql.replace("?", "%s")
    return sql


def _is_create_index(sql: str) -> bool:
    return bool(re.match(r"\s*CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS", sql, flags=re.IGNORECASE))


class _MySQLCursorResult:
    """Thin pass-through so callers can keep using .fetchone()/.fetchall()/.rowcount."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class MySQLConnWrapper:
    """Pooled MySQL connection with just enough sqlite3.Connection-shaped API."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        params = tuple(params) if params is not None else ()
        if _is_create_index(sql):
            stripped = re.sub(
                r"IF\s+NOT\s+EXISTS", "", sql, count=1, flags=re.IGNORECASE
            )
            cur = self._conn.cursor()
            try:
                cur.execute(stripped)
            except Exception as e:  # pymysql.err.OperationalError / ProgrammingError
                errno = e.args[0] if getattr(e, "args", None) else None
                if errno == _DUP_KEY_ERRNO:
                    return _MySQLCursorResult(cur)
                raise
            return _MySQLCursorResult(cur)

        translated = _translate_sql(sql)
        cur = self._conn.cursor()
        cur.execute(translated, params)
        return _MySQLCursorResult(cur)

    def executescript(self, script: str):
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()  # returns the connection to the pool
        return False


_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        import time as _time

        import pymysql
        import pymysql.cursors
        from dbutils.pooled_db import PooledDB

        ssl_kwargs = {}
        if not MYSQL_SSL_DISABLED:
            ssl_kwargs["ssl"] = {"ca": MYSQL_SSL_CA} if MYSQL_SSL_CA else {}

        last_err = None
        for attempt in range(1, 4):
            try:
                _pool = PooledDB(
                    creator=pymysql,
                    mincached=1,
                    maxcached=5,
                    maxconnections=10,
                    blocking=True,
                    ping=1,  # ping and transparently reconnect before handing out a connection
                    host=MYSQL_HOST,
                    port=MYSQL_PORT,
                    user=MYSQL_USER,
                    password=MYSQL_PASSWORD,
                    database=MYSQL_DATABASE,
                    charset="utf8mb4",
                    autocommit=False,
                    cursorclass=pymysql.cursors.Cursor,
                    connect_timeout=10,
                    **ssl_kwargs,
                )
                return _pool
            except Exception as e:  # pymysql.err.OperationalError, socket.timeout, ...
                last_err = e
                if attempt < 3:
                    _time.sleep(2)

        raise RuntimeError(
            f"Could not connect to MySQL at {MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}/"
            f"{MYSQL_DATABASE} after 3 attempts ({last_err}). This is almost always a "
            "firewall/network-allow-list problem, not a code or credentials problem — "
            "a TCP timeout (vs. an auth error) means the connection never reached the "
            "server. Add this host's outbound IP range to the MySQL server's firewall: "
            "on Render, Dashboard -> service -> Connect -> Outbound tab gives the exact "
            "CIDR range(s) to allow (shared per-region, not a single static IP unless "
            "you buy Render's dedicated-outbound-IP add-on)."
        ) from last_err
    return _pool


def get_conn():
    """Drop-in replacement for sqlite3.connect(APP_DB) across the app."""
    if USE_MYSQL:
        return MySQLConnWrapper(_get_pool().connection())
    _ensure_sqlite_dir()
    return sqlite3.connect(APP_DB)
