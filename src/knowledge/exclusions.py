"""Exclusions CRUD."""

from __future__ import annotations

from typing import Any, Dict, List

from src.knowledge import _conn, _lock, new_id, now


def list_exclusions(tenant_id: str) -> List[Dict[str, Any]]:
    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT id, match_type, match_value, reason, created_at
                FROM knowledge_exclusions WHERE tenant_id = ?
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            ).fetchall()
    return [
        {
            "id": r[0],
            "match_type": r[1],
            "match_value": r[2],
            "reason": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def add_exclusion(
    tenant_id: str, match_type: str, match_value: str, reason: str = ""
) -> Dict[str, Any]:
    eid = new_id()
    ts = now()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO knowledge_exclusions
                (id, tenant_id, match_type, match_value, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (eid, tenant_id, match_type, match_value.strip(), reason or "", ts),
            )
            conn.commit()
    return {
        "id": eid,
        "match_type": match_type,
        "match_value": match_value.strip(),
        "reason": reason or "",
        "created_at": ts,
    }


def delete_exclusion(tenant_id: str, exclusion_id: str) -> bool:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_exclusions WHERE id = ? AND tenant_id = ?",
                (exclusion_id, tenant_id),
            )
            conn.commit()
            return cur.rowcount > 0
