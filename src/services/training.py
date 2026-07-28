"""Per-tenant AI training (knowledge base, tone, rules) — SQLite."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from src.services.store import APP_DB, _lock


def _conn():
    return sqlite3.connect(APP_DB)


def ensure_training_tables():
    folder = __import__("os").path.dirname(APP_DB)
    if folder:
        __import__("os").makedirs(folder, exist_ok=True)
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_settings (
                store_id TEXT PRIMARY KEY,
                tone TEXT NOT NULL DEFAULT '',
                instructions TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                entry_type TEXT NOT NULL DEFAULT 'faq',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_store ON knowledge_entries(store_id)"
        )
        conn.commit()


ensure_training_tables()

VALID_TYPES = ("faq", "policy", "rule", "note")


def get_tenant_settings(store_id: str) -> Dict[str, Any]:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                "SELECT tone, instructions, updated_at FROM tenant_settings WHERE store_id = ?",
                (store_id,),
            ).fetchone()
    if not row:
        return {"store_id": store_id, "tone": "", "instructions": "", "updated_at": None}
    return {
        "store_id": store_id,
        "tone": row[0] or "",
        "instructions": row[1] or "",
        "updated_at": row[2],
    }


def update_tenant_settings(
    store_id: str, *, tone: Optional[str] = None, instructions: Optional[str] = None
) -> Dict[str, Any]:
    current = get_tenant_settings(store_id)
    next_tone = current["tone"] if tone is None else (tone or "").strip()
    next_instructions = (
        current["instructions"] if instructions is None else (instructions or "").strip()
    )
    now = time.time()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO tenant_settings (store_id, tone, instructions, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(store_id) DO UPDATE SET
                    tone = excluded.tone,
                    instructions = excluded.instructions,
                    updated_at = excluded.updated_at
                """,
                (store_id, next_tone, next_instructions, now),
            )
            conn.commit()
    return get_tenant_settings(store_id)


def _row_to_entry(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "store_id": row[1],
        "entry_type": row[2],
        "title": row[3],
        "content": row[4],
        "active": bool(row[5]),
        "created_at": row[6],
        "updated_at": row[7],
    }


def list_knowledge(
    store_id: str, *, include_inactive: bool = False, entry_type: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = (
        "SELECT id, store_id, entry_type, title, content, active, created_at, updated_at "
        "FROM knowledge_entries WHERE store_id = ?"
    )
    params: list = [store_id]
    if not include_inactive:
        query += " AND active = 1"
    if entry_type:
        query += " AND entry_type = ?"
        params.append(entry_type)
    query += " ORDER BY updated_at DESC"
    with _lock:
        with _conn() as conn:
            rows = conn.execute(query, params).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_knowledge(store_id: str, entry_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT id, store_id, entry_type, title, content, active, created_at, updated_at
                FROM knowledge_entries WHERE store_id = ? AND id = ?
                """,
                (store_id, entry_id),
            ).fetchone()
    return _row_to_entry(row) if row else None


def create_knowledge(
    store_id: str,
    *,
    title: str,
    content: str,
    entry_type: str = "faq",
    active: bool = True,
) -> Dict[str, Any]:
    entry_type = (entry_type or "faq").strip().lower()
    if entry_type not in VALID_TYPES:
        entry_type = "faq"
    now = time.time()
    entry_id = str(uuid.uuid4())
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_entries
                (id, store_id, entry_type, title, content, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    store_id,
                    entry_type,
                    (title or "").strip(),
                    (content or "").strip(),
                    1 if active else 0,
                    now,
                    now,
                ),
            )
            conn.commit()
    return get_knowledge(store_id, entry_id) or {}


def update_knowledge(
    store_id: str,
    entry_id: str,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    entry_type: Optional[str] = None,
    active: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    current = get_knowledge(store_id, entry_id)
    if not current:
        return None
    next_title = current["title"] if title is None else (title or "").strip()
    next_content = current["content"] if content is None else (content or "").strip()
    next_type = current["entry_type"]
    if entry_type is not None:
        next_type = entry_type.strip().lower()
        if next_type not in VALID_TYPES:
            next_type = current["entry_type"]
    next_active = current["active"] if active is None else bool(active)
    now = time.time()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE knowledge_entries
                SET title = ?, content = ?, entry_type = ?, active = ?, updated_at = ?
                WHERE store_id = ? AND id = ?
                """,
                (next_title, next_content, next_type, 1 if next_active else 0, now, store_id, entry_id),
            )
            conn.commit()
    return get_knowledge(store_id, entry_id)


def delete_knowledge(store_id: str, entry_id: str) -> bool:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_entries WHERE store_id = ? AND id = ?",
                (store_id, entry_id),
            )
            conn.commit()
            return cur.rowcount > 0


def build_training_prompt_block(store_id: str) -> str:
    """Text block injected into the AI system prompt."""
    settings = get_tenant_settings(store_id)
    entries = list_knowledge(store_id, include_inactive=False)
    parts = []
    tone = (settings.get("tone") or "").strip()
    instructions = (settings.get("instructions") or "").strip()
    if tone:
        parts.append(f"BRAND TONE / VOICE:\n{tone}")
    if instructions:
        parts.append(f"STORE-SPECIFIC INSTRUCTIONS (follow these):\n{instructions}")
    if entries:
        lines = []
        for e in entries:
            label = (e.get("entry_type") or "note").upper()
            title = e.get("title") or "(untitled)"
            content = e.get("content") or ""
            lines.append(f"[{label}] {title}\n{content}")
        parts.append(
            "MERCHANT TRAINING KNOWLEDGE (trusted store facts — use for support answers; "
            "do not invent policies that contradict this):\n"
            + "\n\n".join(lines)
        )
    if not parts:
        return ""
    return "\n\n".join(parts)
