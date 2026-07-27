from typing import Dict, Any, List, Optional, TypedDict
import json
import os
import sqlite3
import threading
import time

TENANTS_FILE = "tenants.json"
SESSIONS_DB = os.getenv("SESSIONS_DB", "data/sessions.db")

tenants: dict = {}
_lock = threading.Lock()


class Session(TypedDict):
    store_id: str
    messages: List[Dict[str, str]]
    answers: Dict[str, str]
    user_context: Dict[str, Any]


def _ensure_db():
    folder = os.path.dirname(SESSIONS_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with sqlite3.connect(SESSIONS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]',
                answers TEXT NOT NULL DEFAULT '{}',
                user_context TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


_ensure_db()


def _row_to_session(row) -> Session:
    return Session(
        store_id=row[0],
        messages=json.loads(row[1] or "[]"),
        answers=json.loads(row[2] or "{}"),
        user_context=json.loads(row[3] or "{}"),
    )


def _load_tenants() -> Dict[str, Any]:
    if os.path.exists(TENANTS_FILE):
        with open(TENANTS_FILE, "r") as f:
            return json.load(f)
    return {}


def _save_tenants(tenants_data: Dict[str, Any]):
    with open(TENANTS_FILE, "w") as f:
        json.dump(tenants_data, f, indent=4)


def register_tenant(store_id: str, data: dict):
    tenants_data = _load_tenants()
    tenants_data[store_id] = data
    _save_tenants(tenants_data)


def get_tenant(store_id: str):
    tenants_data = _load_tenants()
    return tenants_data.get(store_id)


def create_session(
    session_id: str, store_id: str, user_context: Optional[Dict[str, Any]] = None
):
    now = time.time()
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (session_id, store_id, messages, answers, user_context, updated_at)
                VALUES (?, ?, '[]', '{}', ?, ?)
                """,
                (session_id, store_id, json.dumps(user_context or {}), now),
            )
            conn.commit()


def get_session(session_id: str) -> Optional[Session]:
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            cur = conn.execute(
                "SELECT store_id, messages, answers, user_context FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return _row_to_session(row)


def add_message(session_id: str, role: str, content: str):
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            cur = conn.execute(
                "SELECT messages FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            messages = json.loads(row[0] or "[]")
            messages.append({"role": role, "content": content})
            conn.execute(
                "UPDATE sessions SET messages = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(messages), time.time(), session_id),
            )
            conn.commit()


def save_answer(session_id: str, key: str, value: str):
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            cur = conn.execute(
                "SELECT answers FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            answers = json.loads(row[0] or "{}")
            answers[key] = value
            conn.execute(
                "UPDATE sessions SET answers = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(answers), time.time(), session_id),
            )
            conn.commit()


def set_user_context(session_id: str, user_context: Dict[str, Any]):
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            conn.execute(
                "UPDATE sessions SET user_context = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(user_context or {}), time.time(), session_id),
            )
            conn.commit()


def get_all_tenants() -> dict:
    return _load_tenants()


def get_all_sessions() -> dict:
    with _lock:
        with sqlite3.connect(SESSIONS_DB) as conn:
            cur = conn.execute(
                "SELECT session_id, store_id, messages, answers, user_context FROM sessions"
            )
            rows = cur.fetchall()
    result = {}
    for row in rows:
        result[row[0]] = _row_to_session(row[1:])
    return result
