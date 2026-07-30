from typing import Dict, Any, List, Optional, TypedDict
import json
import os
import sqlite3
import threading
import time

TENANTS_FILE = "tenants.json"
APP_DB = os.getenv("SESSIONS_DB", os.getenv("APP_DB", "data/app.db"))

tenants: dict = {}
_lock = threading.Lock()


class Session(TypedDict):
    store_id: str
    messages: List[Dict[str, str]]
    answers: Dict[str, str]
    user_context: Dict[str, Any]


def _ensure_db():
    folder = os.path.dirname(APP_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with sqlite3.connect(APP_DB) as conn:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                store_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT 'woocommerce',
                store_name TEXT NOT NULL DEFAULT 'My Store',
                store_url TEXT NOT NULL DEFAULT '',
                credentials TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


_ensure_db()


def _conn():
    return sqlite3.connect(APP_DB)


def _detect_platform(data: dict) -> str:
    platform = (data.get("platform") or "").strip().lower()
    if platform in ("woocommerce", "shopify", "magento", "wordpress"):
        if platform == "wordpress":
            return "woocommerce"
        return platform
    if data.get("access_token") or data.get("shopify_domain"):
        return "shopify"
    if data.get("consumer_key") or data.get("consumer_secret"):
        return "woocommerce"
    return "woocommerce"


def _split_tenant_payload(data: dict) -> tuple:
    """Split flat tenant dict into columns + credentials JSON."""
    data = dict(data or {})
    platform = _detect_platform(data)
    store_name = data.get("store_name") or "My Store"
    store_url = (data.get("store_url") or data.get("shopify_domain") or "").rstrip("/")

    reserved = {
        "platform",
        "store_name",
        "store_url",
        "shopify_domain",
        "active",
        "created_at",
        "updated_at",
    }
    credentials = {k: v for k, v in data.items() if k not in reserved and v is not None}
    # Keep store_url inside credentials too for older callers that only read flat dict
    return platform, store_name, store_url, credentials


def _row_to_tenant(row, include_secrets: bool = True) -> dict:
    (
        store_id,
        platform,
        store_name,
        store_url,
        credentials_json,
        active,
        created_at,
        updated_at,
    ) = row
    credentials = json.loads(credentials_json or "{}")
    flat = {
        "store_id": store_id,
        "platform": platform,
        "store_name": store_name,
        "store_url": store_url,
        "active": bool(active),
        "created_at": created_at,
        "updated_at": updated_at,
        **credentials,
    }
    if not include_secrets:
        for key in (
            "consumer_key",
            "consumer_secret",
            "access_token",
            "api_key",
            "api_secret",
            "tenant_api_key",
            "openai_api_key",
            "merchant_password_hash",
        ):
            if key in flat and flat[key]:
                val = str(flat[key])
                flat[key] = (val[:6] + "...") if len(val) > 6 else "***"
                flat[f"{key}_set"] = True
    return flat


def migrate_tenants_from_json():
    """One-time import of legacy tenants.json into SQLite."""
    if not os.path.exists(TENANTS_FILE):
        return 0
    try:
        with open(TENANTS_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Tenant migration skipped: {e}")
        return 0

    imported = 0
    for store_id, payload in (data or {}).items():
        if not isinstance(payload, dict):
            continue
        existing = get_tenant(store_id, include_inactive=True)
        if existing:
            continue
        register_tenant(store_id, payload, active=True)
        imported += 1
    if imported:
        print(f"✓ Migrated {imported} tenants from tenants.json → SQLite")
    return imported


def register_tenant(store_id: str, data: dict, active: bool = True):
    platform, store_name, store_url, credentials = _split_tenant_payload(data)
    now = time.time()
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "SELECT created_at, active FROM tenants WHERE store_id = ?",
                (store_id,),
            )
            row = cur.fetchone()
            created_at = row[0] if row else now
            if row and "active" in (data or {}) and data.get("active") is not None:
                active_val = 1 if data.get("active") else 0
            elif row:
                active_val = row[1]
            else:
                active_val = 1 if active else 0

            conn.execute(
                """
                INSERT INTO tenants
                (store_id, platform, store_name, store_url, credentials, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    platform=excluded.platform,
                    store_name=excluded.store_name,
                    store_url=excluded.store_url,
                    credentials=excluded.credentials,
                    active=excluded.active,
                    updated_at=excluded.updated_at
                """,
                (
                    store_id,
                    platform,
                    store_name,
                    store_url,
                    json.dumps(credentials),
                    active_val,
                    created_at,
                    now,
                ),
            )
            conn.commit()

    # Keep tenants.json in sync as a backup (non-blocking best effort)
    try:
        all_data = get_all_tenants(include_inactive=True, include_secrets=True)
        export = {}
        for sid, tenant in all_data.items():
            export[sid] = {
                k: v
                for k, v in tenant.items()
                if k
                not in (
                    "store_id",
                    "created_at",
                    "updated_at",
                    "active",
                )
                or k in ("store_name", "store_url", "platform")
            }
            export[sid]["store_name"] = tenant.get("store_name")
            export[sid]["store_url"] = tenant.get("store_url")
            export[sid]["platform"] = tenant.get("platform")
            # flatten credentials already in tenant
            for key in (
                "consumer_key",
                "consumer_secret",
                "access_token",
                "currency_symbol",
            ):
                if tenant.get(key) is not None:
                    export[sid][key] = tenant.get(key)
        with open(TENANTS_FILE, "w") as f:
            json.dump(export, f, indent=4)
    except Exception as e:
        print(f"Warning: could not sync tenants.json: {e}")


def get_tenant(
    store_id: str, include_inactive: bool = False, include_secrets: bool = True
) -> Optional[dict]:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                """
                SELECT store_id, platform, store_name, store_url, credentials,
                       active, created_at, updated_at
                FROM tenants WHERE store_id = ?
                """,
                (store_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    if not include_inactive and not row[5]:
        return None
    return _row_to_tenant(row, include_secrets=include_secrets)


def list_tenants(
    platform: Optional[str] = None,
    include_inactive: bool = True,
    include_secrets: bool = False,
) -> List[dict]:
    query = """
        SELECT store_id, platform, store_name, store_url, credentials,
               active, created_at, updated_at
        FROM tenants
        WHERE 1=1
    """
    params: list = []
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    if not include_inactive:
        query += " AND active = 1"
    query += " ORDER BY updated_at DESC"

    with _lock:
        with _conn() as conn:
            cur = conn.execute(query, params)
            rows = cur.fetchall()
    return [_row_to_tenant(r, include_secrets=include_secrets) for r in rows]


def set_tenant_active(store_id: str, active: bool) -> bool:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "UPDATE tenants SET active = ?, updated_at = ? WHERE store_id = ?",
                (1 if active else 0, time.time(), store_id),
            )
            conn.commit()
            return cur.rowcount > 0


def delete_tenant(store_id: str) -> bool:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM tenants WHERE store_id = ?",
                (store_id,),
            )
            conn.commit()
            return cur.rowcount > 0


def tenant_stats() -> dict:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                """
                SELECT platform,
                       COUNT(*) as total,
                       SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) as active_count
                FROM tenants
                GROUP BY platform
                """
            )
            platform_rows = cur.fetchall()
            cur = conn.execute("SELECT COUNT(*) FROM tenants")
            total = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM tenants WHERE active = 1")
            active = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM sessions")
            sessions = cur.fetchone()[0]
            cur = conn.execute(
                """
                SELECT store_id, COUNT(*) as cnt
                FROM sessions
                GROUP BY store_id
                """
            )
            session_by_store = {r[0]: r[1] for r in cur.fetchall()}

    by_platform = {}
    for platform, count, active_count in platform_rows:
        by_platform[platform] = {
            "total": count,
            "active": active_count or 0,
        }

    return {
        "total_stores": total,
        "active_stores": active,
        "inactive_stores": total - active,
        "total_sessions": sessions,
        "by_platform": by_platform,
        "sessions_by_store": session_by_store,
    }


def get_all_tenants(
    include_inactive: bool = True, include_secrets: bool = True
) -> dict:
    items = list_tenants(
        include_inactive=include_inactive, include_secrets=include_secrets
    )
    return {t["store_id"]: t for t in items}


# ── Sessions ──────────────────────────────────────────────────────────────


def _row_to_session(row) -> Session:
    return Session(
        store_id=row[0],
        messages=json.loads(row[1] or "[]"),
        answers=json.loads(row[2] or "{}"),
        user_context=json.loads(row[3] or "{}"),
    )


def create_session(
    session_id: str, store_id: str, user_context: Optional[Dict[str, Any]] = None
):
    now = time.time()
    with _lock:
        with _conn() as conn:
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
        with _conn() as conn:
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
        with _conn() as conn:
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
        with _conn() as conn:
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
        with _conn() as conn:
            conn.execute(
                "UPDATE sessions SET user_context = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(user_context or {}), time.time(), session_id),
            )
            conn.commit()


def get_all_sessions() -> dict:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "SELECT session_id, store_id, messages, answers, user_context FROM sessions"
            )
            rows = cur.fetchall()
    result = {}
    for row in rows:
        result[row[0]] = _row_to_session(row[1:])
    return result


def list_sessions_for_store(store_id: str, limit: int = 50) -> list:
    """Recent chat sessions for a tenant (for merchant dashboard)."""
    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT session_id, store_id, messages, answers, user_context, updated_at
                FROM sessions
                WHERE store_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (store_id, max(1, min(limit, 200))),
            ).fetchall()
    out = []
    for row in rows:
        messages = json.loads(row[2] or "[]")
        preview = ""
        for m in reversed(messages):
            if m.get("role") == "user" and (m.get("content") or "").strip():
                preview = (m.get("content") or "").strip()[:120]
                break
        out.append(
            {
                "session_id": row[0],
                "store_id": row[1],
                "message_count": len(messages),
                "preview": preview,
                "updated_at": row[5],
                "user_context": json.loads(row[4] or "{}"),
            }
        )
    return out


def get_session_detail(session_id: str, store_id: Optional[str] = None) -> Optional[dict]:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT session_id, store_id, messages, answers, user_context, updated_at
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
    if not row:
        return None
    if store_id and row[1] != store_id:
        return None
    return {
        "session_id": row[0],
        "store_id": row[1],
        "messages": json.loads(row[2] or "[]"),
        "answers": json.loads(row[3] or "{}"),
        "user_context": json.loads(row[4] or "{}"),
        "updated_at": row[5],
    }
