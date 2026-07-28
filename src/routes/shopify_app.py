"""Shopify app OAuth, install callback, and uninstall webhook."""

import os
import hmac
import hashlib
import secrets
import base64
import sqlite3
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.services.store import (
    register_tenant,
    get_tenant,
    set_tenant_active,
    list_tenants,
    APP_DB,
)
from src.services.shopify_service import shopify_store_id, fetch_shop_currency_symbol

router = APIRouter(prefix="/shopify", tags=["shopify"])

OAUTH_STATE_TTL = 600  # 10 minutes


def _api_key() -> str:
    return os.getenv("SHOPIFY_API_KEY", "")


def _api_secret() -> str:
    return os.getenv("SHOPIFY_API_SECRET", "")


def _scopes() -> str:
    return os.getenv(
        "SHOPIFY_SCOPES",
        "read_products,read_orders,read_customers",
    )


def _app_url() -> str:
    return os.getenv("APP_URL", "").rstrip("/")


def _require_shopify_config():
    if not _api_key() or not _api_secret():
        raise HTTPException(
            status_code=500,
            detail="Shopify is not configured. Set SHOPIFY_API_KEY and SHOPIFY_API_SECRET.",
        )


def _backend_url(request: Request) -> str:
    if _app_url():
        return _app_url()
    return str(request.base_url).rstrip("/")


def _shop_handle(shop: str) -> str:
    shop = shopify_store_id(shop)
    return shop.replace(".myshopify.com", "")


def _oauth_authorize_url(shop: str, params: dict) -> str:
    """Prefer admin.shopify.com — *.myshopify.com/admin/oauth often shows store-down page."""
    handle = _shop_handle(shop)
    return f"https://admin.shopify.com/store/{handle}/oauth/authorize?{urlencode(params)}"


def _ensure_oauth_table():
    folder = os.path.dirname(APP_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with sqlite3.connect(APP_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopify_oauth_states (
                state TEXT PRIMARY KEY,
                shop TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.commit()


_ensure_oauth_table()


def _save_oauth_state(state: str, shop: str):
    _ensure_oauth_table()
    now = time.time()
    with sqlite3.connect(APP_DB) as conn:
        conn.execute(
            "DELETE FROM shopify_oauth_states WHERE created_at < ?",
            (now - OAUTH_STATE_TTL,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO shopify_oauth_states (state, shop, created_at) VALUES (?, ?, ?)",
            (state, shop, now),
        )
        conn.commit()


def _consume_oauth_state(state: str) -> Optional[str]:
    """Validate and consume one-time state. Returns shop if valid."""
    if not state:
        return None
    _ensure_oauth_table()
    now = time.time()
    with sqlite3.connect(APP_DB) as conn:
        row = conn.execute(
            "SELECT shop, created_at FROM shopify_oauth_states WHERE state = ?",
            (state,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM shopify_oauth_states WHERE state = ?", (state,))
            conn.commit()
            shop, created_at = row
            if now - created_at <= OAUTH_STATE_TTL:
                return shop
    return None


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


def _begin_oauth(request: Request, shop: str) -> RedirectResponse:
    shop = shopify_store_id(shop)
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    state = secrets.token_urlsafe(24)
    _save_oauth_state(state, shop)

    redirect_uri = f"{_backend_url(request)}/shopify/callback"
    params = {
        "client_id": _api_key(),
        "scope": _scopes(),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = _oauth_authorize_url(shop, params)

    resp = RedirectResponse(url=url, status_code=302)
    # Best-effort cookies (may be blocked in iframes; server state is source of truth)
    secure = _backend_url(request).startswith("https")
    resp.set_cookie(
        "shopify_oauth_state",
        state,
        httponly=True,
        samesite="none" if secure else "lax",
        secure=secure,
        max_age=OAUTH_STATE_TTL,
    )
    resp.set_cookie(
        "shopify_oauth_shop",
        shop,
        httponly=True,
        samesite="none" if secure else "lax",
        secure=secure,
        max_age=OAUTH_STATE_TTL,
    )
    return resp


@router.get("/install")
def shopify_install(
    request: Request,
    shop: str = Query(..., description="example.myshopify.com"),
):
    """Start OAuth install for a Shopify store."""
    _require_shopify_config()
    return _begin_oauth(request, shop)


@router.get("")
@router.get("/")
def shopify_app_entry(
    request: Request,
    shop: Optional[str] = None,
    hmac: Optional[str] = None,
):
    """
    App URL entry — Shopify may open this with ?shop=...&hmac=...
    Start OAuth when shop is present.
    """
    _require_shopify_config()
    if shop:
        query = dict(request.query_params)
        if hmac and not verify_shopify_hmac(query, _api_secret()):
            raise HTTPException(status_code=400, detail="Invalid HMAC")
        return _begin_oauth(request, shop)

    backend = _backend_url(request)
    return HTMLResponse(
        f"""
        <!DOCTYPE html><html><head><title>AI Shopping Assistant</title>
        <style>
          body{{font-family:system-ui;background:#141210;color:#f4f1ec;display:grid;place-items:center;min-height:100vh;margin:0}}
          .card{{max-width:480px;background:#221e1b;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:28px}}
          a{{color:#4fd18a}} code{{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:6px}}
        </style></head><body><div class="card">
          <h1>AI Shopping Assistant</h1>
          <p>Install via:</p>
          <p><code>{backend}/shopify/install?shop=your-store.myshopify.com</code></p>
          <p><a href="{backend}/admin/shopify">Open admin install</a></p>
        </div></body></html>
        """
    )


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
    if not verify_shopify_hmac(query, _api_secret()):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    shop = shopify_store_id(shop or "")
    if not shop or not code:
        raise HTTPException(status_code=400, detail="Missing shop or code")

    # Prefer server-side one-time state (works in iframes); fall back to cookie
    saved_shop = _consume_oauth_state(state or "")
    cookie_state = request.cookies.get("shopify_oauth_state")
    state_ok = False
    if saved_shop:
        if shopify_store_id(saved_shop) != shop:
            raise HTTPException(status_code=400, detail="OAuth state shop mismatch")
        state_ok = True
    elif state and cookie_state and state == cookie_state:
        state_ok = True

    if not state_ok:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid OAuth state. Start install again from "
                f"{_backend_url(request)}/shopify/install?shop={shop}"
            ),
        )

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_res = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": _api_key(),
                "client_secret": _api_secret(),
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

    try:
        symbol = await fetch_shop_currency_symbol(payload)
        if symbol:
            payload["currency_symbol"] = symbol
    except Exception as e:
        print(f"Shopify currency fetch skipped: {e}")

    register_tenant(shop, payload, active=True)

    try:
        await _register_uninstall_webhook(shop, access_token, _backend_url(request))
    except Exception as e:
        print(f"Webhook registration skipped: {e}")

    backend = _backend_url(request)
    admin_url = f"{backend}/admin/stores/{shop}"
    # Break out of Shopify admin iframe so merchant sees success page
    html = f"""
    <!DOCTYPE html>
    <html><head><title>Shopify app installed</title>
    <style>
      body{{font-family:system-ui,sans-serif;background:#141210;color:#f4f1ec;display:grid;place-items:center;min-height:100vh;margin:0}}
      .card{{max-width:560px;background:#221e1b;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:28px}}
      a{{color:#4fd18a}} code{{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:6px}}
    </style>
    <script>
      if (window.top !== window.self) {{
        window.top.location.href = {admin_url!r};
      }}
    </script>
    </head>
    <body><div class="card">
      <h1>App installed</h1>
      <p><strong>{store_name}</strong> is connected as a Shopify store.</p>
      <p>Store ID: <code>{shop}</code></p>
      <p>Enable the <strong>AI Shopping Assistant</strong> theme app embed in Online Store → Themes → Customize → App embeds.</p>
      <p>Backend: <code>{backend}</code></p>
      <p><a href="{admin_url}">Open in admin dashboard</a></p>
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
    if not verify_webhook_hmac(raw, hmac_header, _api_secret()):
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
