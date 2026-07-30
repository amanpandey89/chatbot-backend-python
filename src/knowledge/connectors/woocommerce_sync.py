"""Pull WooCommerce catalog / WP content into knowledge items for sync jobs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from src.services.woocommerce import strip_html


def _base_url(tenant: dict) -> str:
    return (tenant.get("store_url") or "").rstrip("/")


def _auth_params(tenant: dict) -> Dict[str, str]:
    return {
        "consumer_key": tenant.get("consumer_key") or "",
        "consumer_secret": tenant.get("consumer_secret") or "",
    }


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (compatible; ASA-KnowledgeSync/1.0)",
        "Accept": "application/json",
    }


async def _paginate_wc(
    client: httpx.AsyncClient,
    url: str,
    params: Dict[str, Any],
    *,
    max_pages: int = 50,
) -> List[dict]:
    items: List[dict] = []
    page = 1
    while page <= max_pages:
        resp = await client.get(
            url,
            params={**params, "page": page, "per_page": 100},
            headers=_headers(),
            timeout=45.0,
        )
        if resp.status_code >= 400:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


async def _paginate_wp(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_pages: int = 20,
) -> List[dict]:
    items: List[dict] = []
    page = 1
    while page <= max_pages:
        resp = await client.get(
            url,
            params={"page": page, "per_page": 100, "status": "publish"},
            headers=_headers(),
            timeout=45.0,
        )
        if resp.status_code >= 400:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def _product_body(p: dict) -> str:
    title = p.get("name") or "Product"
    short = strip_html(p.get("short_description") or "")
    full = strip_html(p.get("description") or "")
    cats = ", ".join(c.get("name", "") for c in (p.get("categories") or []) if c.get("name"))
    tags = ", ".join(t.get("name", "") for t in (p.get("tags") or []) if t.get("name"))
    price = p.get("sale_price") or p.get("regular_price") or p.get("price") or ""
    sku = p.get("sku") or ""
    stock = p.get("stock_status") or ""
    attrs = []
    for a in p.get("attributes") or []:
        name = a.get("name") or ""
        opts = ", ".join(a.get("options") or [])
        if name and opts:
            attrs.append(f"{name}: {opts}")
    parts = [
        title,
        f"Price: {price}" if price else "",
        f"SKU: {sku}" if sku else "",
        f"Stock: {stock}" if stock else "",
        f"Categories: {cats}" if cats else "",
        f"Tags: {tags}" if tags else "",
        "\n".join(attrs),
        short,
        full,
    ]
    return "\n".join(p for p in parts if p).strip()


async def collect_woocommerce_knowledge(tenant: dict) -> List[Dict[str, Any]]:
    """
    Fetch WooCommerce products + categories, and public WP pages/posts when available.
    Requires store_url + consumer_key + consumer_secret on the tenant.
    """
    base = _base_url(tenant)
    key = (tenant.get("consumer_key") or "").strip()
    secret = (tenant.get("consumer_secret") or "").strip()
    if not base or not key or not secret:
        return []

    auth = _auth_params(tenant)
    out: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        products = await _paginate_wc(
            client,
            f"{base}/wp-json/wc/v3/products",
            {**auth, "status": "publish"},
        )
        for p in products:
            title = p.get("name") or "Product"
            out.append(
                {
                    "source_type": "product",
                    "external_id": str(p.get("id")),
                    "title": title,
                    "url": p.get("permalink") or "",
                    "body": _product_body(p),
                    "meta": {
                        "sku": p.get("sku"),
                        "stock_status": p.get("stock_status"),
                        "price": p.get("price"),
                    },
                }
            )

        categories = await _paginate_wc(
            client,
            f"{base}/wp-json/wc/v3/products/categories",
            auth,
        )
        for c in categories:
            title = c.get("name") or "Category"
            desc = strip_html(c.get("description") or "")
            out.append(
                {
                    "source_type": "collection",
                    "external_id": f"cat-{c.get('id')}",
                    "title": title,
                    "url": c.get("permalink") or "",
                    "body": f"{title}\n{desc}".strip(),
                    "meta": {"count": c.get("count")},
                }
            )

        # Public WordPress content (no WC auth required on many sites)
        for path, source_type in (("/wp-json/wp/v2/pages", "page"), ("/wp-json/wp/v2/posts", "blog")):
            try:
                entries = await _paginate_wp(client, f"{base}{path}")
            except Exception:
                entries = []
            for e in entries:
                title = strip_html(e.get("title", {}).get("rendered") or "") or source_type.title()
                body = strip_html(e.get("content", {}).get("rendered") or "")
                excerpt = strip_html(e.get("excerpt", {}).get("rendered") or "")
                text = f"{title}\n{excerpt}\n{body}".strip()
                if len(text) < 20:
                    continue
                out.append(
                    {
                        "source_type": source_type,
                        "external_id": f"wp-{e.get('id')}",
                        "title": title,
                        "url": e.get("link") or "",
                        "body": text,
                    }
                )

    return out


def woocommerce_sync_ready(tenant: Optional[dict]) -> bool:
    tenant = tenant or {}
    platform = (tenant.get("platform") or "woocommerce").lower()
    if platform in ("shopify",):
        return False
    return bool(
        (tenant.get("store_url") or "").strip()
        and (tenant.get("consumer_key") or "").strip()
        and (tenant.get("consumer_secret") or "").strip()
    )
