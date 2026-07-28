"""Knowledge RAG — SQLite schema (migrate-ready for Postgres/pgvector)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from src.services.store import APP_DB, _lock

SOURCE_TYPES = (
    "product",
    "collection",
    "category",
    "page",
    "post",
    "blog",
    "article",
    "policy",
    "faq",
    "custom",
    "document",
    "website",
    "metaobject",
    "metafield",
)


def _conn():
    return sqlite3.connect(APP_DB)


def ensure_knowledge_tables():
    folder = os.path.dirname(APP_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_sources (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                content_hash TEXT NOT NULL DEFAULT '',
                last_synced_at REAL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(tenant_id, source_type, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ks_tenant ON knowledge_sources(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_ks_tenant_type ON knowledge_sources(tenant_id, source_type);

            CREATE TABLE IF NOT EXISTS knowledge_documents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                body_text TEXT NOT NULL DEFAULT '',
                lang TEXT NOT NULL DEFAULT 'en',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kd_tenant ON knowledge_documents(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_kd_source ON knowledge_documents(source_id);

            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                token_count INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',
                embedding BLOB,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kc_tenant ON knowledge_chunks(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_kc_source ON knowledge_chunks(source_id);
            CREATE INDEX IF NOT EXISTS idx_kc_document ON knowledge_chunks(document_id);

            CREATE TABLE IF NOT EXISTS knowledge_exclusions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                match_type TEXT NOT NULL,
                match_value TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(tenant_id, match_type, match_value)
            );

            CREATE TABLE IF NOT EXISTS sync_jobs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress_pct REAL NOT NULL DEFAULT 0,
                totals_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sj_tenant ON sync_jobs(tenant_id);

            CREATE TABLE IF NOT EXISTS sync_job_items (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS training_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                event TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tl_tenant ON training_logs(tenant_id);

            CREATE TABLE IF NOT EXISTS tenant_rag_settings (
                tenant_id TEXT PRIMARY KEY,
                top_k INTEGER NOT NULL DEFAULT 6,
                chunk_size INTEGER NOT NULL DEFAULT 650,
                chunk_overlap INTEGER NOT NULL DEFAULT 100,
                enabled_sources_json TEXT NOT NULL DEFAULT '[]',
                crawler_seed_urls TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            """
        )
        conn.commit()


ensure_knowledge_tables()


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> float:
    return time.time()


def log_event(
    tenant_id: str,
    event: str,
    message: str = "",
    level: str = "info",
    meta: Optional[dict] = None,
):
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO training_logs (id, tenant_id, level, event, message, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    tenant_id,
                    level,
                    event,
                    message,
                    json.dumps(meta or {}),
                    now(),
                ),
            )
            conn.commit()


def list_logs(tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT id, level, event, message, meta_json, created_at
                FROM training_logs WHERE tenant_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "level": r[1],
                "event": r[2],
                "message": r[3],
                "meta": json.loads(r[4] or "{}"),
                "created_at": r[5],
            }
        )
    return out


def get_rag_settings(tenant_id: str) -> Dict[str, Any]:
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT top_k, chunk_size, chunk_overlap, enabled_sources_json,
                       crawler_seed_urls, updated_at
                FROM tenant_rag_settings WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
    if not row:
        return {
            "tenant_id": tenant_id,
            "top_k": 6,
            "chunk_size": 650,
            "chunk_overlap": 100,
            "enabled_sources": list(SOURCE_TYPES),
            "crawler_seed_urls": "",
            "updated_at": None,
        }
    enabled = json.loads(row[3] or "[]") or list(SOURCE_TYPES)
    return {
        "tenant_id": tenant_id,
        "top_k": row[0],
        "chunk_size": row[1],
        "chunk_overlap": row[2],
        "enabled_sources": enabled,
        "crawler_seed_urls": row[4] or "",
        "updated_at": row[5],
    }


def update_rag_settings(tenant_id: str, **fields) -> Dict[str, Any]:
    current = get_rag_settings(tenant_id)
    top_k = int(fields.get("top_k", current["top_k"]))
    chunk_size = int(fields.get("chunk_size", current["chunk_size"]))
    chunk_overlap = int(fields.get("chunk_overlap", current["chunk_overlap"]))
    enabled = fields.get("enabled_sources", current["enabled_sources"])
    seeds = fields.get("crawler_seed_urls", current["crawler_seed_urls"])
    ts = now()
    with _lock:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO tenant_rag_settings
                (tenant_id, top_k, chunk_size, chunk_overlap, enabled_sources_json,
                 crawler_seed_urls, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                  top_k=excluded.top_k,
                  chunk_size=excluded.chunk_size,
                  chunk_overlap=excluded.chunk_overlap,
                  enabled_sources_json=excluded.enabled_sources_json,
                  crawler_seed_urls=excluded.crawler_seed_urls,
                  updated_at=excluded.updated_at
                """,
                (
                    tenant_id,
                    top_k,
                    chunk_size,
                    chunk_overlap,
                    json.dumps(enabled),
                    seeds or "",
                    ts,
                ),
            )
            conn.commit()
    return get_rag_settings(tenant_id)


def overview_stats(tenant_id: str) -> Dict[str, Any]:
    with _lock:
        with _conn() as conn:
            sources = conn.execute(
                "SELECT COUNT(*) FROM knowledge_sources WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()[0]
            indexed = conn.execute(
                "SELECT COUNT(*) FROM knowledge_sources WHERE tenant_id = ? AND status = 'indexed'",
                (tenant_id,),
            ).fetchone()[0]
            chunks = conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunks WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()[0]
            jobs = conn.execute(
                """
                SELECT COUNT(*) FROM sync_jobs
                WHERE tenant_id = ? AND status IN ('queued','running')
                """,
                (tenant_id,),
            ).fetchone()[0]
            by_type = conn.execute(
                """
                SELECT source_type, COUNT(*) FROM knowledge_sources
                WHERE tenant_id = ? GROUP BY source_type
                """,
                (tenant_id,),
            ).fetchall()
            last = conn.execute(
                """
                SELECT MAX(last_synced_at) FROM knowledge_sources WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()[0]
    return {
        "sources": sources,
        "indexed": indexed,
        "chunks": chunks,
        "active_jobs": jobs,
        "by_type": {r[0]: r[1] for r in by_type},
        "last_synced_at": last,
    }
