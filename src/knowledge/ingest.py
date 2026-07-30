"""Ingest sources → documents → chunks → embeddings."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.knowledge import (
    _conn,
    _lock,
    get_rag_settings,
    log_event,
    new_id,
    now,
)
from src.knowledge.chunking import (
    clean_html,
    content_hash,
    estimate_tokens,
    normalize_whitespace,
    split_chunks,
)
from src.knowledge.embeddings import embed_texts, pack_embedding


def is_excluded(tenant_id: str, *, url: str = "", external_id: str = "", title: str = "") -> bool:
    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT match_type, match_value FROM knowledge_exclusions
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchall()
    for match_type, value in rows:
        v = (value or "").strip().lower()
        if not v:
            continue
        if match_type == "url" and v in (url or "").lower():
            return True
        if match_type == "id" and v == (external_id or "").lower():
            return True
        if match_type == "path" and v and v in (url or "").lower():
            return True
        if match_type == "title" and v in (title or "").lower():
            return True
    return False


def upsert_source(
    tenant_id: str,
    *,
    source_type: str,
    external_id: str,
    title: str,
    url: str = "",
    body: str = "",
    html: str = "",
    meta: Optional[dict] = None,
    embed: bool = True,
) -> Dict[str, Any]:
    """Create/update a knowledge source and optionally index immediately."""
    if is_excluded(tenant_id, url=url, external_id=external_id, title=title):
        return {"skipped": True, "reason": "excluded"}

    text = normalize_whitespace(body or clean_html(html))
    if not text:
        return {"skipped": True, "reason": "empty"}

    chash = content_hash(text)
    ts = now()
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT id, content_hash, status FROM knowledge_sources
                WHERE tenant_id = ? AND source_type = ? AND external_id = ?
                """,
                (tenant_id, source_type, str(external_id)),
            ).fetchone()
            if row:
                source_id, old_hash, _status = row
                if old_hash == chash:
                    return {
                        "skipped": True,
                        "reason": "unchanged",
                        "source_id": source_id,
                    }
                conn.execute(
                    """
                    UPDATE knowledge_sources
                    SET title=?, url=?, content_hash=?, status='pending',
                        meta_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        title or "",
                        url or "",
                        chash,
                        json.dumps(meta or {}),
                        ts,
                        source_id,
                    ),
                )
            else:
                source_id = new_id()
                conn.execute(
                    """
                    INSERT INTO knowledge_sources
                    (id, tenant_id, source_type, external_id, title, url, status,
                     content_hash, last_synced_at, meta_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, ?)
                    """,
                    (
                        source_id,
                        tenant_id,
                        source_type,
                        str(external_id),
                        title or "",
                        url or "",
                        chash,
                        json.dumps(meta or {}),
                        ts,
                        ts,
                    ),
                )
            conn.commit()

    if embed:
        return index_source(tenant_id, source_id, text=text, title=title)
    return {"source_id": source_id, "status": "pending"}


def index_source(
    tenant_id: str, source_id: str, *, text: Optional[str] = None, title: str = ""
) -> Dict[str, Any]:
    settings = get_rag_settings(tenant_id)
    with _lock:
        with _conn() as conn:
            src = conn.execute(
                """
                SELECT title, url, source_type, external_id, content_hash
                FROM knowledge_sources WHERE id = ? AND tenant_id = ?
                """,
                (source_id, tenant_id),
            ).fetchone()
            if not src:
                return {"error": "source_not_found"}
            title = title or src[0]
            url, source_type, external_id, chash = src[1], src[2], src[3], src[4]
            if text is None:
                doc = conn.execute(
                    "SELECT body_text FROM knowledge_documents WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                text = (doc[0] if doc else "") or ""

    text = normalize_whitespace(text or "")
    if not text:
        return {"error": "empty"}

    chunks = split_chunks(
        text,
        chunk_size=int(settings["chunk_size"]),
        overlap=int(settings["chunk_overlap"]),
    )
    if not chunks:
        return {"error": "no_chunks"}

    # Sync path: embeddings generated by caller via asyncio — use sync wrapper
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Schedule from async context via nest — use concurrent future
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            vectors = pool.submit(
                lambda: asyncio.run(embed_texts(chunks, store_id=tenant_id))
            ).result()
    else:
        vectors = asyncio.run(embed_texts(chunks, store_id=tenant_id))

    ts = now()
    doc_id = new_id()
    with _lock:
        with _conn() as conn:
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,)
            )
            conn.execute(
                "DELETE FROM knowledge_documents WHERE source_id = ?", (source_id,)
            )
            conn.execute(
                """
                INSERT INTO knowledge_documents
                (id, tenant_id, source_id, title, body_text, lang, updated_at)
                VALUES (?, ?, ?, ?, ?, 'en', ?)
                """,
                (doc_id, tenant_id, source_id, title or "", text, ts),
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                meta = {
                    "tenant_id": tenant_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "external_id": external_id,
                    "url": url,
                    "title": title,
                    "updated_at": ts,
                    "chunk_index": i,
                }
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks
                    (id, tenant_id, document_id, source_id, chunk_index, content,
                     token_count, content_hash, embedding, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        tenant_id,
                        doc_id,
                        source_id,
                        i,
                        chunk,
                        estimate_tokens(chunk),
                        content_hash(chunk),
                        pack_embedding(vec),
                        json.dumps(meta),
                        ts,
                    ),
                )
            conn.execute(
                """
                UPDATE knowledge_sources
                SET status='indexed', last_synced_at=?, updated_at=?, content_hash=?
                WHERE id=?
                """,
                (ts, ts, chash or content_hash(text), source_id),
            )
            conn.commit()

    log_event(
        tenant_id,
        "source_indexed",
        f"Indexed {source_type}:{external_id} ({len(chunks)} chunks)",
        meta={"source_id": source_id, "chunks": len(chunks)},
    )
    return {
        "source_id": source_id,
        "document_id": doc_id,
        "chunks": len(chunks),
        "status": "indexed",
    }


async def index_source_async(
    tenant_id: str, source_id: str, *, text: str, title: str = ""
) -> Dict[str, Any]:
    settings = get_rag_settings(tenant_id)
    with _lock:
        with _conn() as conn:
            src = conn.execute(
                """
                SELECT title, url, source_type, external_id, content_hash
                FROM knowledge_sources WHERE id = ? AND tenant_id = ?
                """,
                (source_id, tenant_id),
            ).fetchone()
            if not src:
                return {"error": "source_not_found"}
            title = title or src[0]
            url, source_type, external_id, chash = src[1], src[2], src[3], src[4]

    text = normalize_whitespace(text or "")
    chunks = split_chunks(
        text,
        chunk_size=int(settings["chunk_size"]),
        overlap=int(settings["chunk_overlap"]),
    )
    if not chunks:
        return {"error": "no_chunks"}

    vectors = await embed_texts(chunks, store_id=tenant_id)
    ts = now()
    doc_id = new_id()
    with _lock:
        with _conn() as conn:
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,)
            )
            conn.execute(
                "DELETE FROM knowledge_documents WHERE source_id = ?", (source_id,)
            )
            conn.execute(
                """
                INSERT INTO knowledge_documents
                (id, tenant_id, source_id, title, body_text, lang, updated_at)
                VALUES (?, ?, ?, ?, ?, 'en', ?)
                """,
                (doc_id, tenant_id, source_id, title or "", text, ts),
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
                meta = {
                    "tenant_id": tenant_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "external_id": external_id,
                    "url": url,
                    "title": title,
                    "updated_at": ts,
                    "chunk_index": i,
                }
                conn.execute(
                    """
                    INSERT INTO knowledge_chunks
                    (id, tenant_id, document_id, source_id, chunk_index, content,
                     token_count, content_hash, embedding, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id(),
                        tenant_id,
                        doc_id,
                        source_id,
                        i,
                        chunk,
                        estimate_tokens(chunk),
                        content_hash(chunk),
                        pack_embedding(vec),
                        json.dumps(meta),
                        ts,
                    ),
                )
            conn.execute(
                """
                UPDATE knowledge_sources
                SET status='indexed', last_synced_at=?, updated_at=?
                WHERE id=?
                """,
                (ts, ts, source_id),
            )
            conn.commit()

    log_event(
        tenant_id,
        "source_indexed",
        f"Indexed {source_type}:{external_id} ({len(chunks)} chunks)",
        meta={"source_id": source_id, "chunks": len(chunks)},
    )
    return {
        "source_id": source_id,
        "document_id": doc_id,
        "chunks": len(chunks),
        "status": "indexed",
    }


def delete_source(tenant_id: str, source_id: str) -> bool:
    with _lock:
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_sources WHERE id = ? AND tenant_id = ?",
                (source_id, tenant_id),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM knowledge_documents WHERE source_id = ?", (source_id,)
            )
            conn.execute(
                "DELETE FROM knowledge_chunks WHERE source_id = ?", (source_id,)
            )
            conn.commit()
    return True


def list_sources(
    tenant_id: str, source_type: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    q = """
        SELECT id, source_type, external_id, title, url, status, content_hash,
               last_synced_at, meta_json, created_at, updated_at
        FROM knowledge_sources WHERE tenant_id = ?
    """
    args: list = [tenant_id]
    if source_type:
        q += " AND source_type = ?"
        args.append(source_type)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _lock:
        with _conn() as conn:
            rows = conn.execute(q, args).fetchall()
    return [
        {
            "id": r[0],
            "source_type": r[1],
            "external_id": r[2],
            "title": r[3],
            "url": r[4],
            "status": r[5],
            "content_hash": r[6],
            "last_synced_at": r[7],
            "meta": json.loads(r[8] or "{}"),
            "created_at": r[9],
            "updated_at": r[10],
        }
        for r in rows
    ]


def upsert_and_index_async_prep(
    tenant_id: str,
    *,
    source_type: str,
    external_id: str,
    title: str,
    url: str = "",
    body: str = "",
    html: str = "",
    meta: Optional[dict] = None,
) -> Dict[str, Any]:
    """Upsert source without embedding; return payload for async index."""
    if is_excluded(tenant_id, url=url, external_id=external_id, title=title):
        return {"skipped": True, "reason": "excluded"}
    text = normalize_whitespace(body or clean_html(html))
    if not text:
        return {"skipped": True, "reason": "empty"}
    chash = content_hash(text)
    ts = now()
    with _lock:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT id, content_hash FROM knowledge_sources
                WHERE tenant_id = ? AND source_type = ? AND external_id = ?
                """,
                (tenant_id, source_type, str(external_id)),
            ).fetchone()
            if row and row[1] == chash:
                return {"skipped": True, "reason": "unchanged", "source_id": row[0]}
            if row:
                source_id = row[0]
                conn.execute(
                    """
                    UPDATE knowledge_sources
                    SET title=?, url=?, content_hash=?, status='pending',
                        meta_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (title or "", url or "", chash, json.dumps(meta or {}), ts, source_id),
                )
            else:
                source_id = new_id()
                conn.execute(
                    """
                    INSERT INTO knowledge_sources
                    (id, tenant_id, source_type, external_id, title, url, status,
                     content_hash, last_synced_at, meta_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?, ?)
                    """,
                    (
                        source_id,
                        tenant_id,
                        source_type,
                        str(external_id),
                        title or "",
                        url or "",
                        chash,
                        json.dumps(meta or {}),
                        ts,
                        ts,
                    ),
                )
            conn.commit()
    return {"source_id": source_id, "text": text, "title": title or "", "status": "pending"}
