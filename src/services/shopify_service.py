"""Shopify Admin API helpers — products, orders, shop info."""

import os
import re
from typing import Optional
import httpx

from src.services.woocommerce import strip_html

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")


def shopify_store_id(shop: str) -> str:
    shop = (shop or "").strip().lower()
    shop = shop.replace("https://", "").replace("http://", "").strip("/")
    if not shop or shop == ".myshopify.com":
        return ""

    if shop.endswith(".myshopify.com"):
        handle = shop[: -len(".myshopify.com")]
    else:
        handle = shop

    # Handle must be a simple store subdomain (letters, numbers, hyphens)
    if not handle or not all(c.isalnum() or c == "-" for c in handle):
        return ""
    if not any(c.isalnum() for c in handle):
        return ""
    return f"{handle}.myshopify.com"


def _admin_base(shop: str) -> str:
    shop = shopify_store_id(shop)
    return f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}"


def _headers(access_token: str) -> dict:
    return {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def normalize_shopify_product(p: dict, shop: str) -> dict:
    images = p.get("images") or []
    variants = p.get("variants") or [{}]
    variant = variants[0] if variants else {}
    price = str(variant.get("price") or "0")
    compare = variant.get("compare_at_price")
    tags = [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
    product_type = p.get("product_type") or ""
    handle = p.get("handle") or ""
    shop_host = shopify_store_id(shop)

    return {
        "id": p.get("id"),
        "variant_id": variant.get("id"),
        "name": p.get("title") or "Product",
        "description": strip_html(p.get("body_html") or ""),
        "price": price,
        "regular_price": str(compare or price),
        "sale_price": price if compare else None,
        "sku": variant.get("sku"),
        "in_stock": (variant.get("inventory_quantity") or 0) > 0
        or variant.get("inventory_management") is None,
        "categories": [product_type] if product_type else [],
        "tags": tags,
        "stock_quantity": variant.get("inventory_quantity"),
        "attributes": {},
        "product_url": f"https://{shop_host}/products/{handle}" if handle else None,
        "image_url": images[0]["src"] if images else None,
        "platform": "shopify",
    }


def normalize_shopify_order(order: dict) -> dict:
    status = (order.get("financial_status") or order.get("fulfillment_status") or "unknown")
    status = str(status).replace("_", " ")
    items = []
    for item in order.get("line_items") or []:
        items.append(
            {
                "name": item.get("title") or item.get("name"),
                "quantity": item.get("quantity"),
                "total": item.get("price"),
            }
        )
    email = (order.get("email") or order.get("contact_email") or "").strip().lower()
    return {
        "id": order.get("id"),
        "number": str(order.get("order_number") or order.get("name") or order.get("id")),
        "status": order.get("financial_status") or order.get("fulfillment_status"),
        "status_label": status.title(),
        "total": order.get("total_price"),
        "currency": order.get("currency"),
        "date_created": order.get("created_at"),
        "billing_email": email,
        "payment_method": (order.get("payment_gateway_names") or [""])[0]
        if order.get("payment_gateway_names")
        else "",
        "items": items,
        "fulfillment_status": order.get("fulfillment_status"),
    }


async def fetch_shopify_products(tenant: dict) -> list:
    shop = tenant.get("store_url") or tenant.get("shop")
    token = tenant.get("access_token")
    if not shop or not token:
        raise Exception("Shopify tenant missing store_url or access_token")

    products = []
    url = f"{_admin_base(shop)}/products.json"
    params = {"limit": 250, "status": "active"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        while url:
            res = await client.get(
                url, headers=_headers(token), params=params if "page_info" not in url else None
            )
            print(f"── Shopify products status: {res.status_code}")
            res.raise_for_status()
            data = res.json()
            batch = data.get("products") or []
            products.extend(
                [normalize_shopify_product(p, shop) for p in batch if p.get("status") == "active"]
            )

            # Cursor pagination via Link header
            link = res.headers.get("Link") or res.headers.get("link") or ""
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break
            url = next_url
            params = None

    print(f"── Shopify products fetched: {len(products)}")
    return products


async def fetch_shopify_order(tenant: dict, order_number: str) -> Optional[dict]:
    shop = tenant.get("store_url") or tenant.get("shop")
    token = tenant.get("access_token")
    if not shop or not token:
        return None

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Try by order name / number first
        res = await client.get(
            f"{_admin_base(shop)}/orders.json",
            headers=_headers(token),
            params={"name": order_number, "status": "any", "limit": 5},
        )
        if res.status_code == 200:
            orders = res.json().get("orders") or []
            for order in orders:
                number = str(order.get("order_number") or "")
                name = str(order.get("name") or "").lstrip("#")
                if number == str(order_number) or name == str(order_number):
                    return normalize_shopify_order(order)
            if orders:
                return normalize_shopify_order(orders[0])

        # Fallback: treat as Shopify order ID
        if str(order_number).isdigit():
            res = await client.get(
                f"{_admin_base(shop)}/orders/{order_number}.json",
                headers=_headers(token),
            )
            if res.status_code == 200:
                return normalize_shopify_order(res.json().get("order") or {})

    return None


async def lookup_shopify_order_status(tenant: dict, messages: list) -> Optional[dict]:
    from src.services.woocommerce import extract_order_details

    details = extract_order_details(messages)
    order_number = details.get("order_number")
    email = details.get("email")
    if not order_number:
        return None

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
        order = await fetch_shopify_order(tenant, order_number)
    except Exception as e:
        print(f"── Shopify order lookup error: {e}")
        return {
            "found": False,
            "order_number": order_number,
            "error": "lookup_failed",
            "message": "Could not reach the Shopify order system right now.",
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
                "Order was found, but the email does not match the email "
                "on that order. Ask the customer to confirm the checkout email."
            ),
        }

    return {"found": True, "order_number": order_number, "order": order}


async def fetch_shop_currency_symbol(tenant: dict) -> Optional[str]:
    shop = tenant.get("store_url") or tenant.get("shop")
    token = tenant.get("access_token")
    if not shop or not token:
        return None
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(
            f"{_admin_base(shop)}/shop.json", headers=_headers(token)
        )
        if res.status_code != 200:
            return None
        money = (res.json().get("shop") or {}).get("money_format") or ""
        # money_format like "${{amount}}" or "£{{amount}}"
        symbol = re.sub(r"\{\{.*?\}\}", "", money).strip()
        return symbol or (res.json().get("shop") or {}).get("currency")
