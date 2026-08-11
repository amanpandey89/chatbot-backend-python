"""Pull WordPress + WooCommerce content into knowledge items for sync jobs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from src.services.woocommerce import strip_html

# Same rule as catalog client: avoid "bot"/"crawler" in UA on WP Engine.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Built-in types we always try (in addition to discovered CPTs)
_CORE_WP_TYPES = ("posts", "pages")

# Skip noisy / non-content REST types
_SKIP_TYPES = {
    "attachment",
    "nav_menu_item",
    "wp_block",
    "wp_template",
    "wp_template_part",
    "wp_navigation",
    "wp_font_family",
    "wp_font_face",
    "wp_global_styles",
    "product_variation",  # covered via WC products
}


def _base_url(tenant: dict) -> str:
    return (tenant.get("store_url") or "").rstrip("/")


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }


def _map_source_type(rest_base: str, type_slug: str = "") -> str:
    key = (rest_base or type_slug or "").lower()
    if key in ("product", "products"):
        return "product"
    if key in ("page", "pages"):
        return "page"
    if key in ("post", "posts"):
        return "post"
    if "product_cat" in key or key in ("categories", "product-categories"):
        return "category"
    if "collection" in key:
        return "collection"
    return "custom"


async def _paginate(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    max_pages: int = 80,
    auth: Optional[httpx.Auth] = None,
) -> List[dict]:
    items: List[dict] = []
    page = 1
    base_params = dict(params or {})
    while page <= max_pages:
        resp = await client.get(
            url,
            params={**base_params, "page": page, "per_page": 100},
            headers=_headers(),
            auth=auth,
            timeout=60.0,
        )
        if resp.status_code >= 400:
            break
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        items.extend(batch)
        total_pages = resp.headers.get("X-WP-TotalPages")
        if total_pages and page >= int(total_pages):
            break
        if len(batch) < 100:
            break
        page += 1
    return items


def _product_body(p: dict) -> str:
    title = p.get("name") or "Product"
    short = strip_html(p.get("short_description") or "")
    full = strip_html(p.get("description") or "")
    cats = ", ".join(
        c.get("name", "") for c in (p.get("categories") or []) if c.get("name")
    )
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


def _wp_item_text(entry: dict) -> Tuple[str, str]:
    title_raw = entry.get("title")
    if isinstance(title_raw, dict):
        title = strip_html(title_raw.get("rendered") or "")
    else:
        title = strip_html(str(title_raw or ""))

    content_raw = entry.get("content")
    if isinstance(content_raw, dict):
        body = strip_html(content_raw.get("rendered") or "")
    else:
        body = strip_html(str(content_raw or ""))

    excerpt_raw = entry.get("excerpt")
    if isinstance(excerpt_raw, dict):
        excerpt = strip_html(excerpt_raw.get("rendered") or "")
    else:
        excerpt = strip_html(str(excerpt_raw or entry.get("short_description") or ""))

    # ACF / meta dump (common on custom post types)
    extra_bits: List[str] = []
    for key in ("acf", "meta", "yoast_head_json"):
        block = entry.get(key)
        if isinstance(block, dict) and block:
            for k, v in list(block.items())[:40]:
                if v is None or v == "" or isinstance(v, (dict, list)):
                    continue
                extra_bits.append(f"{k}: {strip_html(str(v))[:500]}")

    text = "\n".join(p for p in [title, excerpt, body, "\n".join(extra_bits)] if p).strip()
    return title or "Untitled", text


async def _discover_wp_types(client: httpx.AsyncClient, base: str) -> List[Tuple[str, str]]:
    """
    Return list of (rest_base, type_slug) for public content types.
    Falls back to posts + pages if types endpoint is blocked.
    """
    found: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    try:
        resp = await client.get(
            f"{base}/wp-json/wp/v2/types",
            headers=_headers(),
            timeout=30.0,
        )
        if resp.status_code < 400:
            data = resp.json()
            if isinstance(data, dict):
                for slug, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    if slug in _SKIP_TYPES:
                        continue
                    if info.get("rest_base") is None and not info.get("show_in_rest"):
                        continue
                    rest_base = (info.get("rest_base") or slug or "").strip("/")
                    if not rest_base or rest_base in seen or rest_base in _SKIP_TYPES:
                        continue
                    # Prefer public / show_in_rest types
                    if info.get("show_in_rest") is False:
                        continue
                    seen.add(rest_base)
                    found.append((rest_base, slug))
    except Exception:
        pass

    for core in _CORE_WP_TYPES:
        if core not in seen:
            found.append((core, core.rstrip("s")))
            seen.add(core)

    return found


async def collect_woocommerce_knowledge(tenant: dict) -> List[Dict[str, Any]]:
    """
    Full WordPress sync: WooCommerce products/categories + all WP REST
    post types (posts, pages, and custom post types exposed in REST).
    """
    base = _base_url(tenant)
    key = (tenant.get("consumer_key") or "").strip()
    secret = (tenant.get("consumer_secret") or "").strip()
    if not base or not key or not secret:
        return []

    auth_qs = {
        "consumer_key": key,
        "consumer_secret": secret,
    }
    out: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # --- WooCommerce products (query-string auth for WP Engine) ---
        products = await _paginate(
            client,
            f"{base}/wp-json/wc/v3/products",
            {**auth_qs, "status": "publish"},
        )
        for p in products:
            ext = f"product-{p.get('id')}"
            if ext in seen_ids:
                continue
            seen_ids.add(ext)
            title = p.get("name") or "Product"
            out.append(
                {
                    "source_type": "product",
                    "external_id": ext,
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

        # --- WooCommerce product categories ---
        categories = await _paginate(
            client,
            f"{base}/wp-json/wc/v3/products/categories",
            auth_qs,
        )
        for c in categories:
            ext = f"product-cat-{c.get('id')}"
            if ext in seen_ids:
                continue
            seen_ids.add(ext)
            title = c.get("name") or "Category"
            desc = strip_html(c.get("description") or "")
            out.append(
                {
                    "source_type": "category",
                    "external_id": ext,
                    "title": title,
                    "url": c.get("permalink") or "",
                    "body": f"{title}\n{desc}".strip(),
                    "meta": {"count": c.get("count")},
                }
            )

        # --- All WP REST types (posts, pages, CPTs) ---
        type_list = await _discover_wp_types(client, base)
        for rest_base, type_slug in type_list:
            if rest_base in ("product", "products"):
                # Prefer WC product payload (richer) already collected
                continue
            source_type = _map_source_type(rest_base, type_slug)
            try:
                entries = await _paginate(
                    client,
                    f"{base}/wp-json/wp/v2/{rest_base}",
                    {"status": "publish", "context": "view"},
                )
            except Exception:
                entries = []
            for e in entries:
                ext = f"wp-{type_slug}-{e.get('id')}"
                if ext in seen_ids:
                    continue
                seen_ids.add(ext)
                title, text = _wp_item_text(e)
                if len(text) < 15:
                    continue
                out.append(
                    {
                        "source_type": source_type,
                        "external_id": ext,
                        "title": title,
                        "url": e.get("link") or "",
                        "body": text,
                        "meta": {
                            "wp_type": type_slug,
                            "rest_base": rest_base,
                        },
                    }
                )

        # --- WP categories + tags (taxonomy terms) ---
        for tax_path, source_type, prefix in (
            ("categories", "category", "wp-cat"),
            ("tags", "custom", "wp-tag"),
        ):
            try:
                terms = await _paginate(
                    client,
                    f"{base}/wp-json/wp/v2/{tax_path}",
                    {},
                )
            except Exception:
                terms = []
            for t in terms:
                ext = f"{prefix}-{t.get('id')}"
                if ext in seen_ids:
                    continue
                name = t.get("name") or ""
                desc = strip_html(t.get("description") or "")
                if not name:
                    continue
                seen_ids.add(ext)
                out.append(
                    {
                        "source_type": source_type,
                        "external_id": ext,
                        "title": name,
                        "url": t.get("link") or "",
                        "body": f"{name}\n{desc}".strip(),
                        "meta": {"taxonomy": tax_path, "count": t.get("count")},
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
