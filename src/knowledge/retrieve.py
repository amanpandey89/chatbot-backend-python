"""Semantic retrieval over tenant-scoped chunks."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.knowledge import _conn, _lock, get_rag_settings
from src.knowledge.embeddings import cosine_similarity, embed_query, unpack_embedding


async def search(
    tenant_id: str,
    query: str,
    *,
    top_k: Optional[int] = None,
    source_types: Optional[List[str]] = None,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    settings = get_rag_settings(tenant_id)
    k = int(top_k or settings["top_k"] or 6)
    qvec = await embed_query(query, api_key=api_key, store_id=tenant_id)
    if not qvec:
        return []

    with _lock:
        with _conn() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.source_id, c.chunk_index, c.content, c.embedding,
                       c.metadata_json, s.source_type, s.title, s.url
                FROM knowledge_chunks c
                JOIN knowledge_sources s ON s.id = c.source_id
                WHERE c.tenant_id = ? AND s.status = 'indexed'
                """,
                (tenant_id,),
            ).fetchall()

    scored: List[Dict[str, Any]] = []
    allowed = set(source_types) if source_types else None
    for r in rows:
        meta = json.loads(r[5] or "{}")
        stype = r[6]
        if allowed and stype not in allowed:
            continue
        vec = unpack_embedding(r[4] or b"")
        score = cosine_similarity(qvec, vec)
        if score < 0:
            continue
        scored.append(
            {
                "chunk_id": r[0],
                "source_id": r[1],
                "chunk_index": r[2],
                "content": r[3],
                "score": round(float(score), 4),
                "source_type": stype,
                "title": r[7],
                "url": r[8],
                "metadata": meta,
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def format_retrieval_block(chunks: List[Dict[str, Any]]) -> str:
    if not chunks:
        return ""
    lines = []
    for i, c in enumerate(chunks, 1):
        title = c.get("title") or "Untitled"
        stype = (c.get("source_type") or "note").upper()
        url = c.get("url") or ""
        header = f"[{i}] ({stype}) {title}"
        if url:
            header += f" — {url}"
        lines.append(f"{header}\n{c.get('content') or ''}")
    return (
        "RETRIEVED STORE KNOWLEDGE (from tenant knowledge base — prefer these facts):\n"
        + "\n\n".join(lines)
    )
