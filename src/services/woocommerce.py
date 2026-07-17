import os
import re
from typing import Optional
import httpx


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


# async def fetch_products(tenant: dict) -> list:
#     """
#     Fetch Products from WooCommerce API
#     """

#     store_url = tenant["store_url"]
#     consumer_key = tenant["consumer_key"]
#     consumer_secret = tenant["consumer_secret"]

#     auth = (consumer_key, consumer_secret)

#     async with httpx.AsyncClient() as client:

#         response = await client.get(
#             f"{store_url}/wp-json/wc/v3/products",
#             params={
#                 "consumer_key": consumer_key,
#                 "consumer_secret": consumer_secret,
#                 "per_page": 100,
#                 "status": "publish",
#                 "stock_status": "instock",
#             },
#             timeout=15.0,
#         )

#         response.raise_for_status()

#         raw_products = response.json()

#         return [normalize_product(p) for p in raw_products]


async def fetch_products(tenant: dict) -> list:

    store_url = tenant["store_url"]
    consumer_key = tenant["consumer_key"]
    consumer_secret = tenant["consumer_secret"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ChatbotBot/1.0)",
        "Accept": "application/json",
    }

    raw_products = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        page = 1
        while True:
            response = await client.get(
                f"{store_url}/wp-json/wc/v3/products",
                params={
                    "consumer_key": consumer_key,
                    "consumer_secret": consumer_secret,
                    "per_page": 100,
                    "page": page,
                    "status": "publish",
                    "stock_status": "instock",
                },
                headers=headers,
                timeout=15.0,
            )

            print(f"── WooCommerce status: {response.status_code} (Page {page})")
            print(f"── Final URL: {response.url}")

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                print(f"── Error body: {response.text[:500]}")
                raise Exception(
                    f"WooCommerce error {response.status_code}: {response.text[:200]}"
                )

            page_products = response.json()
            if not page_products:
                break

            raw_products.extend(page_products)

            # If we received fewer than requested, we're on the last page
            if len(page_products) < 100:
                break

            page += 1

    all_products = [normalize_product(p) for p in raw_products]

    print(f"── Total products fetched: {len(all_products)}")

    return all_products


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
    store_url = tenant["store_url"]
    consumer_key = tenant["consumer_key"]
    consumer_secret = tenant["consumer_secret"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ChatbotBot/1.0)",
        "Accept": "application/json",
    }
    auth_params = {
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Prefer direct ID lookup (order #119 is usually id 119)
        response = await client.get(
            f"{store_url}/wp-json/wc/v3/orders/{order_number}",
            params=auth_params,
            headers=headers,
            timeout=15.0,
        )

        if response.status_code == 200:
            return normalize_order(response.json())

        # Fallback: search by order number string
        response = await client.get(
            f"{store_url}/wp-json/wc/v3/orders",
            params={**auth_params, "search": order_number, "per_page": 10},
            headers=headers,
            timeout=15.0,
        )
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
