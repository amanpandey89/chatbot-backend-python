import os
import re
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
