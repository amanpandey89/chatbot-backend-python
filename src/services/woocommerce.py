import os
import re
import time
from typing import Dict, List, Optional, Tuple
import httpx

# In-memory product cache: store_url -> (expires_at, products)
_PRODUCT_CACHE: Dict[str, Tuple[float, list]] = {}
PRODUCT_CACHE_TTL = int(os.getenv("PRODUCT_CACHE_TTL", "600"))  # 10 minutes


# WP Engine bot-filter treats User-Agents containing "bot"/"crawler" specially
# (redirects and drops query-string API credentials). Use a normal browser UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


class WooConfigError(Exception):
    """Missing WooCommerce store URL or API keys — safe to show to shoppers."""

    pass


class WooAuthError(WooConfigError):
    """WooCommerce rejected API credentials (401/403)."""

    pass


def require_woo_credentials(tenant: dict) -> tuple:
    """Return (store_url, consumer_key, consumer_secret) or raise WooConfigError."""
    store_url = (tenant.get("store_url") or "").strip().rstrip("/")
    consumer_key = (tenant.get("consumer_key") or "").strip()
    consumer_secret = (tenant.get("consumer_secret") or "").strip()
    missing = []
    if not store_url:
        missing.append("store URL")
    if not consumer_key:
        missing.append("WooCommerce consumer key")
    if not consumer_secret:
        missing.append("WooCommerce consumer secret")
    if missing:
        raise WooConfigError(
            "Store configuration incomplete: missing "
            + ", ".join(missing)
            + ". Open Merchant Dashboard → Settings → Store connection and save them."
        )
    return store_url, consumer_key, consumer_secret


def woo_basic_auth(consumer_key: str, consumer_secret: str) -> httpx.Auth:
    return httpx.BasicAuth(consumer_key, consumer_secret)


def raise_for_woo_response(response: httpx.Response, *, context: str = "WooCommerce"):
    if response.status_code in (401, 403):
        raise WooAuthError(
            "WooCommerce API keys were rejected (cannot list products). "
            "Generate Read/Write keys on this exact site, paste BOTH key and secret "
            "into Settings → Store connection, click Save, then Test again. "
            "On WP Engine, ask support to allow REST API if it still fails."
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        raise Exception(
            f"{context} error {response.status_code}: {response.text[:200]}"
        )


async def woo_get(
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 15.0,
) -> httpx.Response:
    """
    GET WooCommerce REST with WP Engine-compatible auth.

    Important: do not use a User-Agent containing "bot" — WP Engine's bot filter
    redirects those requests and drops consumer_key/secret query params (always 401).
    """
    params = dict(params or {})
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
        **(headers or {}),
    }
    # Prefer HTTPS and avoid following a bot/cache redirect that strips credentials.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        qs_params = {
            **params,
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
        }
        response = await client.get(
            url,
            params=qs_params,
            headers=headers,
            timeout=timeout,
        )
        # One safe HTTPS redirect hop only (keep query string).
        if response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("location") or ""
            if loc.startswith("https://"):
                response = await client.get(
                    loc,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                print(f"── WooCommerce blocked unsafe redirect: {loc[:120]}")

        if response.status_code not in (401, 403):
            return response

        print("── WooCommerce query-string auth failed; retrying Basic Auth")
        response = await client.get(
            url,
            params=params,
            headers=headers,
            auth=woo_basic_auth(consumer_key, consumer_secret),
            timeout=timeout,
        )
        return response


def strip_html(html: str) -> str:
    """Remove HTML tags from a string"""
    if not html:
        return ""
    return re.sub(r"<[^>]+>", "", html).strip()


def normalize_product(p: dict) -> dict:
    """Normalize a product dictionary for LLM consumption"""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "description": strip_html(
            p.get("short_description") or p.get("description", "")
        ),
        "price": p.get("sale_price") or p.get("regular_price", "0"),
        "regular_price": p.get("regular_price", "0"),
        "sale_price": p.get("sale_price") or None,
        "sku": p.get("sku"),
        "in_stock": p.get("stock_status") == "instock",
        "categories": [c["name"] for c in p.get("categories", [])],
        "tags": [t["name"] for t in p.get("tags", [])],
        "stock_quantity": p.get("stock_quantity"),
        "attributes": {
            a["name"]: a.get("options", []) for a in p.get("attributes", [])
        },
        "product_url": p.get("permalink"),
        "image_url": p["images"][0]["src"] if p.get("images") else None,
    }


async def _fetch_products_uncached(tenant: dict) -> list:
    store_url, consumer_key, consumer_secret = require_woo_credentials(tenant)

    raw_products = []
    page = 1
    while True:
        response = await woo_get(
            f"{store_url}/wp-json/wc/v3/products",
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            params={
                "per_page": 100,
                "page": page,
                "status": "publish",
                "stock_status": "instock",
            },
            timeout=15.0,
        )

        print(f"── WooCommerce status: {response.status_code} (Page {page})")
        print(f"── Final URL: {response.url}")

        if response.status_code >= 400:
            print(f"── Error body: {response.text[:500]}")
        raise_for_woo_response(response)

        page_products = response.json()
        if not page_products:
            break

        raw_products.extend(page_products)

        if len(page_products) < 100:
            break

        page += 1

    all_products = [normalize_product(p) for p in raw_products]
    print(f"── Total products fetched: {len(all_products)}")
    return all_products


async def fetch_products(tenant: dict, force_refresh: bool = False) -> list:
    """Fetch products with a short in-memory TTL cache per store."""
    cache_key = (tenant.get("store_url") or "").rstrip("/")
    now = time.time()

    if not force_refresh and cache_key in _PRODUCT_CACHE:
        expires_at, products = _PRODUCT_CACHE[cache_key]
        if now < expires_at:
            print(f"── Product cache HIT ({len(products)} items)")
            return products

    products = await _fetch_products_uncached(tenant)
    _PRODUCT_CACHE[cache_key] = (now + PRODUCT_CACHE_TTL, products)
    print(f"── Product cache STORE (ttl={PRODUCT_CACHE_TTL}s)")
    return products


def normalize_order(order: dict) -> dict:
    """Normalize a WooCommerce order for chatbot replies."""
    status = (order.get("status") or "unknown").replace("-", " ")
    items = []
    for item in order.get("line_items") or []:
        items.append(
            {
                "name": item.get("name"),
                "quantity": item.get("quantity"),
                "total": item.get("total"),
            }
        )

    billing = order.get("billing") or {}
    return {
        "id": order.get("id"),
        "number": str(order.get("number") or order.get("id")),
        "status": order.get("status"),
        "status_label": status.title(),
        "total": order.get("total"),
        "currency": order.get("currency"),
        "date_created": order.get("date_created"),
        "billing_email": (billing.get("email") or "").strip().lower(),
        "payment_method": order.get("payment_method_title") or "",
        "items": items,
    }


def extract_order_details(messages: list) -> dict:
    """Pull order number and email from recent user messages."""
    order_number = None
    email = None

    order_patterns = [
        r"order\s*(?:number|no\.?|#)?\s*(?:is|=|:)?\s*#?(\d+)",
        r"#(\d{1,10})",
        r"\border\s+#?(\d+)\b",
    ]
    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = msg.get("content") or ""

        for pattern in order_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                order_number = match.group(1)
                break

        email_match = re.search(email_pattern, text)
        if email_match:
            email = email_match.group(0).lower()

    return {"order_number": order_number, "email": email}


async def fetch_order(tenant: dict, order_number: str) -> Optional[dict]:
    """Fetch a WooCommerce order by ID / order number."""
    store_url, consumer_key, consumer_secret = require_woo_credentials(tenant)

    response = await woo_get(
        f"{store_url}/wp-json/wc/v3/orders/{order_number}",
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        timeout=15.0,
    )

    if response.status_code == 200:
        return normalize_order(response.json())

    response = await woo_get(
        f"{store_url}/wp-json/wc/v3/orders",
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        params={"search": order_number, "per_page": 10},
        timeout=15.0,
    )
    if response.status_code in (401, 403):
        raise_for_woo_response(response, context="WooCommerce orders")
    response.raise_for_status()
    orders = response.json() or []

    for order in orders:
        number = str(order.get("number") or "")
        oid = str(order.get("id") or "")
        if number == str(order_number) or oid == str(order_number):
            return normalize_order(order)

    if orders:
        return normalize_order(orders[0])

    return None


async def lookup_order_status(tenant: dict, messages: list) -> Optional[dict]:
    """
    If the user shared an order number, look it up in WooCommerce.
    Optionally verify billing email when provided.
    """
    details = extract_order_details(messages)
    order_number = details.get("order_number")
    email = details.get("email")

    if not order_number:
        return None

    # Avoid accidental lookups during product chats (e.g. "under 100")
    user_text = " ".join(
        (m.get("content") or "").lower()
        for m in messages
        if m.get("role") == "user"
    )
    order_intent = any(
        word in user_text
        for word in (
            "order",
            "track",
            "tracking",
            "shipment",
            "shipping",
            "delivery",
            "return",
            "refund",
        )
    )
    if not order_intent:
        return None

    try:
        order = await fetch_order(tenant, order_number)
    except Exception as e:
        print(f"── Order lookup error: {e}")
        return {
            "found": False,
            "order_number": order_number,
            "error": "lookup_failed",
            "message": "Could not reach the store order system right now.",
        }

    if not order:
        return {
            "found": False,
            "order_number": order_number,
            "error": "not_found",
            "message": f"No order found for #{order_number}.",
        }

    if email and order.get("billing_email") and email != order["billing_email"]:
        return {
            "found": False,
            "order_number": order_number,
            "error": "email_mismatch",
            "message": (
                "Order was found, but the email does not match the billing email "
                "on that order. Ask the customer to confirm the checkout email."
            ),
        }

    return {
        "found": True,
        "order_number": order_number,
        "order": order,
    }
