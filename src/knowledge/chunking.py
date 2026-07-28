"""Text cleaning and semantic chunking (~500–800 tokens, overlap)."""

from __future__ import annotations

import hashlib
import re
from html import unescape
from typing import List


def estimate_tokens(text: str) -> int:
    # Rough OpenAI token estimate
    return max(1, len(re.findall(r"\S+", text or "")) * 4 // 3)


def clean_html(html: str) -> str:
    if not html:
        return ""
    text = unescape(html)
    text = re.sub(r"(?is)<(script|style|noscript|svg|iframe).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</(div|h[1-6]|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def split_chunks(
    text: str, *, chunk_size: int = 650, overlap: int = 100
) -> List[str]:
    """Split into semantic-ish chunks targeting chunk_size tokens with overlap."""
    text = normalize_whitespace(text)
    if not text:
        return []

    # Prefer paragraph / heading boundaries
    parts = re.split(r"\n{2,}|(?=\n#{1,6}\s)|(?=\n[A-Z][^\n]{0,80}:\s*\n)", text)
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        parts = [text]

    chunks: List[str] = []
    buf = ""
    for part in parts:
        candidate = f"{buf}\n\n{part}".strip() if buf else part
        if estimate_tokens(candidate) <= chunk_size:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        if estimate_tokens(part) <= chunk_size:
            buf = part
        else:
            # Hard-split long part by sentences/words
            words = part.split()
            step = max(40, chunk_size * 3 // 4)
            ov = max(10, overlap * 3 // 4)
            i = 0
            while i < len(words):
                piece = " ".join(words[i : i + step])
                chunks.append(piece)
                i += max(1, step - ov)
            buf = ""
    if buf:
        chunks.append(buf)

    # Apply overlap between adjacent chunks when missing
    if overlap > 0 and len(chunks) > 1:
        overlapped: List[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_words = chunks[i - 1].split()
            tail = " ".join(prev_words[-max(1, overlap * 3 // 4) :])
            cur = chunks[i]
            if not cur.startswith(tail[:40]):
                cur = f"{tail} {cur}".strip()
            overlapped.append(cur)
        chunks = overlapped

    return [c for c in chunks if c.strip()]
