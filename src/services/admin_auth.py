"""Simple cookie-based admin authentication with editable profile."""

import os
import hmac
import hashlib
import sqlite3
import time
from typing import Optional

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

COOKIE_NAME = "asa_admin_session"
APP_DB = os.getenv("SESSIONS_DB", os.getenv("APP_DB", "data/app.db"))

_ENV_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
_ENV_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
_ENV_DISPLAY = os.getenv("ADMIN_DISPLAY_NAME", "Admin")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "") or hashlib.sha256(
    f"{_ENV_USERNAME}:{_ENV_PASSWORD}".encode()
).hexdigest()


def _conn():
    folder = os.path.dirname(APP_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)
    return sqlite3.connect(APP_DB)


def _ensure_profile_table():
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT 'Admin',
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


_ensure_profile_table()


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _default_profile() -> dict:
    return {
        "username": _ENV_USERNAME,
        "password_hash": _hash_password(_ENV_PASSWORD),
        "display_name": _ENV_DISPLAY,
        "from_env": True,
    }


def get_admin_profile() -> dict:
    """Return current admin profile (DB override, else env defaults)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT username, password_hash, display_name, updated_at FROM admin_profile WHERE id = 1"
        ).fetchone()
    if not row:
        return _default_profile()
    return {
        "username": row[0],
        "password_hash": row[1],
        "display_name": row[2] or "Admin",
        "updated_at": row[3],
        "from_env": False,
    }


def update_admin_profile(
    *,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    new_password: Optional[str] = None,
) -> dict:
    current = get_admin_profile()
    next_username = (username or current["username"]).strip()
    next_display = (display_name if display_name is not None else current["display_name"]).strip() or "Admin"
    next_hash = current["password_hash"]
    if new_password:
        next_hash = _hash_password(new_password)

    if not next_username:
        raise ValueError("Username is required")

    now = time.time()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO admin_profile (id, username, password_hash, display_name, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                password_hash = excluded.password_hash,
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (next_username, next_hash, next_display, now),
        )
        conn.commit()
    return get_admin_profile()


def _sign(value: str) -> str:
    sig = hmac.new(ADMIN_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _verify(token: str) -> bool:
    if not token or "." not in token:
        return False
    value, sig = token.rsplit(".", 1)
    expected = hmac.new(
        ADMIN_SECRET.encode(), value.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    profile = get_admin_profile()
    return value == profile["username"]


def create_session_token(username: Optional[str] = None) -> str:
    return _sign(username or get_admin_profile()["username"])


def check_credentials(username: str, password: str) -> bool:
    profile = get_admin_profile()
    user_ok = hmac.compare_digest(
        hashlib.sha256(username.encode()).digest(),
        hashlib.sha256(profile["username"].encode()).digest(),
    )
    pass_ok = hmac.compare_digest(
        hashlib.sha256(_hash_password(password).encode()).digest(),
        hashlib.sha256(profile["password_hash"].encode()).digest(),
    )
    return user_ok and pass_ok


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return _verify(token or "")


def require_admin(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def login_redirect():
    return RedirectResponse(url="/admin/login", status_code=303)


def get_admin_username() -> str:
    return get_admin_profile()["username"]


def get_admin_display_name() -> str:
    return get_admin_profile()["display_name"]
