"""Pull Shopify catalog/content into knowledge items for sync jobs."""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from src.services.shopify_service import SHOPIFY_API_VERSION, shopify_store_id, _headers
from src.services.woocommerce import strip_html


async def _get_pages(client: httpx.AsyncClient, base: str, path: str) -> List[dict]:
    items: List[dict] = []
    url = f"{base}{path}"
    while url:
        resp = await client.get(url)
        if resp.status_code >= 400:
            break
        data = resp.json()
        # REST resources are keyed by plural name
        key = next((k for k in data.keys() if isinstance(data[k], list)), None)
        if key:
            items.extend(data[key])
        link = resp.headers.get("Link") or ""
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1 : part.find(">")]
                break
        url = next_url
        if len(items) > 2000:
            break
    return items


async def collect_shopify_knowledge(tenant: dict) -> List[Dict[str, Any]]:
    shop = shopify_store_id(tenant.get("store_url") or tenant.get("shop") or "")
    token = tenant.get("access_token") or ""
    if not shop or not token:
        return []

    base = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}"
    out: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(headers=_headers(token), timeout=45.0) as client:
        products = await _get_pages(client, base, "/products.json?limit=250")
        for p in products:
            handle = p.get("handle") or ""
            body = strip_html(p.get("body_html") or "")
            title = p.get("title") or "Product"
            tags = p.get("tags") or ""
            ptype = p.get("product_type") or ""
            text = f"{title}\nType: {ptype}\nTags: {tags}\n{body}".strip()
            out.append(
                {
                    "source_type": "product",
                    "external_id": str(p.get("id")),
                    "title": title,
                    "url": f"https://{shop}/products/{handle}" if handle else "",
                    "body": text,
                    "meta": {"vendor": p.get("vendor")},
                }
            )

        custom_collections = await _get_pages(
            client, base, "/custom_collections.json?limit=250"
        )
        smart_collections = await _get_pages(
            client, base, "/smart_collections.json?limit=250"
        )
        for c in custom_collections + smart_collections:
            handle = c.get("handle") or ""
            title = c.get("title") or "Collection"
            body = strip_html(c.get("body_html") or "")
            out.append(
                {
                    "source_type": "collection",
                    "external_id": str(c.get("id")),
                    "title": title,
                    "url": f"https://{shop}/collections/{handle}" if handle else "",
                    "body": f"{title}\n{body}".strip(),
                }
            )

        pages = await _get_pages(client, base, "/pages.json?limit=250")
        for page in pages:
            handle = page.get("handle") or ""
            title = page.get("title") or "Page"
            body = strip_html(page.get("body_html") or "")
            out.append(
                {
                    "source_type": "page",
                    "external_id": str(page.get("id")),
                    "title": title,
                    "url": f"https://{shop}/pages/{handle}" if handle else "",
                    "body": f"{title}\n{body}".strip(),
                }
            )

        blogs = await _get_pages(client, base, "/blogs.json?limit=250")
        for blog in blogs:
            blog_id = blog.get("id")
            blog_title = blog.get("title") or "Blog"
            out.append(
                {
                    "source_type": "blog",
                    "external_id": str(blog_id),
                    "title": blog_title,
                    "url": f"https://{shop}/blogs/{blog.get('handle') or ''}",
                    "body": blog_title,
                }
            )
            articles = await _get_pages(
                client, base, f"/blogs/{blog_id}/articles.json?limit=250"
            )
            for a in articles:
                handle = a.get("handle") or ""
                title = a.get("title") or "Article"
                body = strip_html(a.get("body_html") or "")
                out.append(
                    {
                        "source_type": "article",
                        "external_id": str(a.get("id")),
                        "title": title,
                        "url": f"https://{shop}/blogs/{blog.get('handle')}/{handle}",
                        "body": f"{title}\n{body}".strip(),
                        "meta": {"blog_id": blog_id},
                    }
                )

        # Policies (shipping, refund, privacy, terms)
        try:
            resp = await client.get(f"{base}/policies.json")
            if resp.status_code < 400:
                for pol in resp.json().get("policies") or []:
                    title = pol.get("title") or "Policy"
                    body = strip_html(pol.get("body") or "")
                    out.append(
                        {
                            "source_type": "policy",
                            "external_id": str(pol.get("id") or title),
                            "title": title,
                            "url": pol.get("url") or "",
                            "body": f"{title}\n{body}".strip(),
                        }
                    )
        except Exception:
            pass

    return out
