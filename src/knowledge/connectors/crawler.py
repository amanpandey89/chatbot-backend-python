"""Website crawler — fetch seed URLs and extract text."""

from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import httpx

from src.knowledge.chunking import clean_html


async def crawl_urls(seed_urls: List[str], *, max_pages: int = 25) -> List[Dict[str, Any]]:
    seeds = [u.strip() for u in seed_urls if u and u.strip()]
    if not seeds:
        return []

    seen = set()
    out: List[Dict[str, Any]] = []
    queue = list(seeds)

    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "AI-Shopping-Assistant-Crawler/1.0"},
    ) as client:
        while queue and len(out) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    continue
                ctype = (resp.headers.get("content-type") or "").lower()
                if "html" not in ctype and "text" not in ctype:
                    continue
                html = resp.text
                text = clean_html(html)
                if len(text) < 40:
                    continue
                title = url
                # crude title extraction
                import re

                m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
                if m:
                    title = clean_html(m.group(1)) or url
                out.append(
                    {
                        "source_type": "website",
                        "external_id": url,
                        "title": title[:200],
                        "url": url,
                        "body": text[:50000],
                    }
                )
                # Discover same-host links from first seed host only
                host = urlparse(seeds[0]).netloc
                for href in re.findall(r'(?i)href=["\']([^"\']+)["\']', html):
                    if href.startswith("#") or href.startswith("mailto:"):
                        continue
                    abs_url = urljoin(url, href)
                    if urlparse(abs_url).netloc != host:
                        continue
                    if abs_url not in seen and len(queue) < max_pages * 2:
                        queue.append(abs_url.split("#")[0])
            except Exception:
                continue
    return out
