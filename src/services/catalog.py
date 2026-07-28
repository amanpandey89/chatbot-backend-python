"""Platform-aware catalog helpers (WooCommerce / Shopify)."""

from typing import Optional

from src.services import woocommerce as woo
from src.services import shopify_service as shopify


def _platform(tenant: dict) -> str:
    return (tenant.get("platform") or "woocommerce").lower()


async def fetch_products(tenant: dict, force_refresh: bool = False) -> list:
    platform = _platform(tenant)
    if platform == "shopify":
        # Reuse same in-memory cache key pattern via thin wrapper
        cache_key = f"shopify:{(tenant.get('store_url') or '').rstrip('/')}"
        import time
        from src.services.woocommerce import _PRODUCT_CACHE, PRODUCT_CACHE_TTL

        now = time.time()
        if not force_refresh and cache_key in _PRODUCT_CACHE:
            expires_at, products = _PRODUCT_CACHE[cache_key]
            if now < expires_at:
                print(f"── Shopify product cache HIT ({len(products)} items)")
                return products
        products = await shopify.fetch_shopify_products(tenant)
        _PRODUCT_CACHE[cache_key] = (now + PRODUCT_CACHE_TTL, products)
        return products

    return await woo.fetch_products(tenant, force_refresh=force_refresh)


async def lookup_order_status(tenant: dict, messages: list) -> Optional[dict]:
    platform = _platform(tenant)
    if platform == "shopify":
        return await shopify.lookup_shopify_order_status(tenant, messages)
    return await woo.lookup_order_status(tenant, messages)
