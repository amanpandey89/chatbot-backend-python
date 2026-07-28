"""Shopify app OAuth, install callback, and uninstall webhook."""

import os
import hmac
import hashlib
import secrets
import base64
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.services.store import register_tenant, get_tenant, set_tenant_active, list_tenants
from src.services.shopify_service import shopify_store_id, fetch_shop_currency_symbol

router = APIRouter(prefix="/shopify", tags=["shopify"])

SHOPIFY_API_KEY = os.getenv("SHOPIFY_API_KEY", "")
SHOPIFY_API_SECRET = os.getenv("SHOPIFY_API_SECRET", "")
SHOPIFY_SCOPES = os.getenv(
    "SHOPIFY_SCOPES",
    "read_products,read_orders,read_customers",
)
APP_URL = os.getenv("APP_URL", "").rstrip("/")  # public backend URL


def _require_shopify_config():
    if not SHOPIFY_API_KEY or not SHOPIFY_API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Shopify is not configured. Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET.",
        )


def _backend_url(request: Request) -> str:
    if APP_URL:
        return APP_URL
    return str(request.base_url).rstrip("/")


def verify_shopify_hmac(query_params: dict, secret: str) -> bool:
    """Verify HMAC from Shopify OAuth callback query string."""
    params = {k: v for k, v in query_params.items() if k != "hmac"}
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return hmac.compare_digest(digest.hexdigest(), query_params.get("hmac", ""))


def verify_webhook_hmac(raw_body: bytes, hmac_header: str, secret: str) -> bool:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header or "")


@router.get("/install")
def shopify_install(
    request: Request,
    shop: str = Query(..., description="example.myshopify.com"),
):
    """Start OAuth install for a Shopify store."""
    _require_shopify_config()
    shop = shopify_store_id(shop)
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    state = secrets.token_urlsafe(16)
    redirect_uri = f"{_backend_url(request)}/shopify/callback"
    params = {
        "client_id": SHOPIFY_API_KEY,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(
        "shopify_oauth_state",
        state,
        httponly=True,
        samesite="lax",
        max_age=600,
    )
    resp.set_cookie(
        "shopify_oauth_shop",
        shop,
        httponly=True,
        samesite="lax",
        max_age=600,
    )
    return resp


@router.get("/callback")
async def shopify_callback(
    request: Request,
    shop: Optional[str] = None,
    code: Optional[str] = None,
    state: Optional[str] = None,
    hmac: Optional[str] = None,
):
    """OAuth callback — exchange code for access token and register tenant."""
    _require_shopify_config()

    query = dict(request.query_params)
    if not verify_shopify_hmac(query, SHOPIFY_API_SECRET):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    cookie_state = request.cookies.get("shopify_oauth_state")
    if not state or not cookie_state or state != cookie_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    shop = shopify_store_id(shop or "")
    if not shop or not code:
        raise HTTPException(status_code=400, detail="Missing shop or code")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_res = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_API_KEY,
                "client_secret": SHOPIFY_API_SECRET,
                "code": code,
            },
        )
        if token_res.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Token exchange failed: {token_res.text[:200]}",
            )
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        scope = token_data.get("scope", "")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access token returned")

    store_name = shop.replace(".myshopify.com", "").replace("-", " ").title()
    payload = {
        "platform": "shopify",
        "store_name": store_name,
        "store_url": shop,
        "shop": shop,
        "access_token": access_token,
        "scope": scope,
    }

    # Enrich with currency if possible
    try:
        symbol = await fetch_shop_currency_symbol(payload)
        if symbol:
            payload["currency_symbol"] = symbol
    except Exception as e:
        print(f"Shopify currency fetch skipped: {e}")

    register_tenant(shop, payload, active=True)

    # Register uninstall webhook (best effort)
    try:
        await _register_uninstall_webhook(shop, access_token, _backend_url(request))
    except Exception as e:
        print(f"Webhook registration skipped: {e}")

    backend = _backend_url(request)
    html = f"""
    <!DOCTYPE html>
    <html><head><title>Shopify app installed</title>
    <style>
      body{{font-family:system-ui,sans-serif;background:#0b1020;color:#eef1ff;display:grid;place-items:center;min-height:100vh;margin:0}}
      .card{{max-width:560px;background:#171d33;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:28px}}
      a{{color:#4fd1c5}} code{{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:6px}}
    </style></head>
    <body><div class="card">
      <h1>App installed</h1>
      <p><strong>{store_name}</strong> is connected as a Shopify store.</p>
      <p>Store ID: <code>{shop}</code></p>
      <p>Enable the <strong>AI Shopping Assistant</strong> theme app embed in Online Store → Themes → Customize → App embeds.</p>
      <p>Backend: <code>{backend}</code></p>
      <p><a href="{backend}/admin/stores/{shop}">Open in admin dashboard</a></p>
    </div></body></html>
    """
    resp = HTMLResponse(content=html)
    resp.delete_cookie("shopify_oauth_state")
    resp.delete_cookie("shopify_oauth_shop")
    return resp


async def _register_uninstall_webhook(shop: str, access_token: str, backend: str):
    api_version = os.getenv("SHOPIFY_API_VERSION", "2024-10")
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"https://{shop}/admin/api/{api_version}/webhooks.json",
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={
                "webhook": {
                    "topic": "app/uninstalled",
                    "address": f"{backend}/shopify/webhooks/app-uninstalled",
                    "format": "json",
                }
            },
        )


@router.post("/webhooks/app-uninstalled")
async def shopify_app_uninstalled(request: Request):
    _require_shopify_config()
    raw = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not verify_webhook_hmac(raw, hmac_header, SHOPIFY_API_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook HMAC")

    shop = request.headers.get("X-Shopify-Shop-Domain") or ""
    shop = shopify_store_id(shop)
    if shop:
        set_tenant_active(shop, False)
        print(f"── Shopify app uninstalled: {shop} (disabled)")
    return {"ok": True}


@router.get("/stores")
def shopify_connected_stores():
    """List connected Shopify stores (for debugging / extensions)."""
    stores = list_tenants(platform="shopify", include_inactive=True, include_secrets=False)
    return {"success": True, "count": len(stores), "stores": stores}


@router.get("/embed-config")
def shopify_embed_config(
    request: Request,
    shop: str = Query(...),
):
    """Public config for the theme extension widget."""
    shop = shopify_store_id(shop)
    tenant = get_tenant(shop, include_inactive=False, include_secrets=False)
    if not tenant:
        raise HTTPException(status_code=404, detail="Store not connected")
    backend = _backend_url(request)
    return {
        "success": True,
        "store_id": shop,
        "backend_url": backend,
        "store_name": tenant.get("store_name"),
        "currency_symbol": tenant.get("currency_symbol") or "$",
        "script_url": f"{backend}/static/chatbot.js",
    }
