"""OpenAI embeddings + cosine search helpers."""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536


def pack_embedding(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def unpack_embedding(blob: bytes) -> List[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return -1.0
    return dot / ((na**0.5) * (nb**0.5))


async def embed_texts(
    texts: List[str], *, api_key: Optional[str] = None, store_id: Optional[str] = None
) -> List[List[float]]:
    if not texts:
        return []
    from src.services.openai_service import get_openai_client

    client = get_openai_client(api_key=api_key, store_id=store_id)
    out: List[List[float]] = []
    batch = 64
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        resp = await client.embeddings.create(model=EMBED_MODEL, input=chunk)
        ordered = sorted(resp.data, key=lambda d: d.index)
        out.extend([list(d.embedding) for d in ordered])
    return out


async def embed_query(
    text: str, *, api_key: Optional[str] = None, store_id: Optional[str] = None
) -> List[float]:
    vecs = await embed_texts([text], api_key=api_key, store_id=store_id)
    return vecs[0] if vecs else []
