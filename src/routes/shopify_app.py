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
    """
    Use Unified Admin OAuth URL.
    Correct form (note /admin/ before oauth):
      https://admin.shopify.com/store/{handle}/admin/oauth/authorize?...
    The older *.myshopify.com/admin/oauth/authorize often shows a false
    "store will be right back" page; /store/{handle}/oauth/authorize (no /admin/)
    returns "installation link is invalid".
    """
    handle = _shop_handle(shop)
    mode = (os.getenv("SHOPIFY_OAUTH_MODE") or "admin").strip().lower()
    if mode in ("legacy", "myshopify"):
        return f"https://{shopify_store_id(shop)}/admin/oauth/authorize?{urlencode(params)}"
    return (
        f"https://admin.shopify.com/store/{handle}/admin/oauth/authorize"
        f"?{urlencode(params)}"
    )


def _install_bounce_html(authorize_url: str, shop: str, backend: str) -> str:
    """Top-level redirect page so OAuth is not trapped in a Shopify iframe."""
    return f"""<!DOCTYPE html>
<html><head>
  <meta charset="utf-8"/>
  <title>Connecting Shopify…</title>
  <meta http-equiv="refresh" content="0;url={authorize_url}">
  <style>
    body{{font-family:system-ui,sans-serif;background:#141210;color:#f4f1ec;
      display:grid;place-items:center;min-height:100vh;margin:0}}
    .card{{max-width:520px;background:#221e1b;border:1px solid rgba(255,255,255,.08);
      border-radius:18px;padding:28px;line-height:1.5}}
    a{{color:#4fd18a}} code{{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:6px}}
  </style>
  <script>
    (function () {{
      var url = {authorize_url!r};
      try {{
        if (window.top && window.top !== window.self) {{
          window.top.location.href = url;
          return;
        }}
      }} catch (e) {{}}
      window.location.replace(url);
    }})();
  </script>
</head>
<body>
  <div class="card">
    <h1>Continue to Shopify</h1>
    <p>Connecting <code>{shop}</code>…</p>
    <p>If nothing happens, <a href="{authorize_url}">click here to approve the app</a>.</p>
    <p style="color:#a89f96;font-size:13px">Wrong shop? Use
      <code>{backend}/shopify/install?shop=your-store.myshopify.com</code>
    </p>
  </div>
</body></html>"""


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


def _is_valid_shop(shop: str) -> bool:
    shop = shopify_store_id(shop or "")
    if not shop.endswith(".myshopify.com"):
        return False
    handle = shop[: -len(".myshopify.com")]
    return bool(handle) and handle != ""


def _begin_oauth(request: Request, shop: str):
    shop = shopify_store_id(shop)
    if not _is_valid_shop(shop):
        raise HTTPException(
            status_code=400,
            detail="Invalid shop domain. Use e.g. your-store.myshopify.com",
        )

    state = secrets.token_urlsafe(24)
    _save_oauth_state(state, shop)

    backend = _backend_url(request)
    redirect_uri = f"{backend}/shopify/callback"
    params = {
        "client_id": _api_key(),
        "scope": _scopes(),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = _oauth_authorize_url(shop, params)

    resp = HTMLResponse(content=_install_bounce_html(url, shop, backend))
    secure = backend.startswith("https")
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
    direct: int = Query(0, description="1 = jump straight to OAuth authorize"),
):
    """
    Start install for a shop.

    Custom-distribution apps often reject a raw /oauth/authorize URL with
    "installation link is invalid". Default page explains Partners Generate Link.
    Pass direct=1 to attempt classic OAuth authorize anyway.
    """
    _require_shopify_config()
    shop = shopify_store_id(shop)
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    if direct:
        return _begin_oauth(request, shop)

    backend = _backend_url(request)
    handle = _shop_handle(shop)
    oauth_url = f"{backend}/shopify/install?shop={shop}&direct=1"
    admin_oauth = (
        f"https://admin.shopify.com/oauth/install?client_id={_api_key()}"
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>Install AI Shopping Assistant</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#141210;color:#f4f1ec;
    display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}}
  .card{{max-width:640px;background:#221e1b;border:1px solid rgba(255,255,255,.08);
    border-radius:18px;padding:28px;line-height:1.55}}
  h1{{margin:0 0 10px;font-size:24px}}
  ol{{padding-left:20px;color:#cfc6bc}}
  li{{margin:8px 0}}
  a.btn{{display:inline-flex;margin:8px 8px 0 0;padding:11px 16px;border-radius:12px;
    background:#4fd18a;color:#102418;font-weight:700;text-decoration:none}}
  a.secondary{{background:rgba(255,255,255,.08);color:#f4f1ec}}
  code{{background:rgba(0,0,0,.35);padding:2px 6px;border-radius:6px;font-size:13px}}
  .warn{{margin-top:16px;padding:12px 14px;border-radius:12px;
    background:rgba(255,166,0,.12);border:1px solid rgba(255,166,0,.28);color:#ffd18a;font-size:14px}}
  .muted{{color:#a89f96;font-size:13px}}
</style></head>
<body><div class="card">
  <h1>Install on <code>{shop}</code></h1>
  <p class="muted">If Shopify shows <strong>“installation link is invalid”</strong>, do not use a raw OAuth URL.
  Use the Partners <strong>custom distribution</strong> install link first.</p>

  <h2 style="font-size:16px;margin:22px 0 8px">Recommended (Custom distribution)</h2>
  <ol>
    <li>Open <strong>Shopify Partners / Dev Dashboard</strong> → your app → <strong>Distribution</strong></li>
    <li>Choose <strong>Custom distribution</strong></li>
    <li>Add store: <code>{shop}</code></li>
    <li>Click <strong>Generate link</strong> and open that link while logged in as the <strong>store owner</strong> (incognito helps)</li>
    <li>Set App URL to <code>{backend}/shopify</code> and redirect URL to <code>{backend}/shopify/callback</code></li>
    <li>On the app <strong>version</strong>, keep scopes only:
      <code>read_products,read_orders,read_customers</code> — uncheck everything else, then Release</li>
  </ol>

  <p style="margin-top:18px">
    <a class="btn" href="{admin_oauth}" target="_blank" rel="noopener">Try Shopify install page</a>
    <a class="btn secondary" href="{oauth_url}">Try direct OAuth</a>
    <a class="btn secondary" href="{backend}/admin/shopify">Back to admin</a>
  </p>

  <div class="warn">
    Direct OAuth often fails for custom apps with “installation link is invalid”.
    The Partners <strong>Generate link</strong> flow is the supported path.
    As a fallback, create an Admin API access token in the store
    (Settings → Apps → Develop apps) and add the store manually in
    <a href="{backend}/admin/stores/new" style="color:#ffd18a">Add store</a>.
  </div>
</div></body></html>"""
    return HTMLResponse(content=html)


def _page_styles() -> str:
    return """
      body{font-family:system-ui,sans-serif;background:#141210;color:#f4f1ec;display:grid;place-items:center;min-height:100vh;margin:0;padding:24px}
      .card{max-width:560px;background:#221e1b;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:28px;line-height:1.55}
      a{color:#4fd18a} code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:6px}
      a.btn{display:inline-flex;margin:8px 8px 0 0;padding:11px 16px;border-radius:12px;
        background:#4fd18a;color:#102418;font-weight:700;text-decoration:none}
      a.secondary{background:rgba(255,255,255,.08);color:#f4f1ec}
      .muted{color:#a89f96;font-size:14px}
    """


def _shop_from_host(host: Optional[str]) -> Optional[str]:
    """Decode Shopify host param (base64) → shop domain when possible."""
    if not host:
        return None
    try:
        padded = host + ("=" * (-len(host) % 4))
        decoded = base64.b64decode(padded).decode("utf-8")
        # e.g. admin.shopify.com/store/swagdealscollection
        if "/store/" in decoded:
            handle = decoded.split("/store/", 1)[1].split("/", 1)[0].strip()
            if handle:
                return shopify_store_id(handle)
    except Exception:
        return None
    return None


def _tenant_has_token(shop: str) -> bool:
    tenant = get_tenant(shop, include_inactive=True, include_secrets=True)
    return bool(tenant and tenant.get("access_token"))


def _connected_page(request: Request, shop: str) -> HTMLResponse:
    from src.services.tenant_auth import set_tenant_cookie, ensure_tenant_api_key

    backend = _backend_url(request)
    tenant = get_tenant(shop, include_inactive=True, include_secrets=False) or {}
    store_name = tenant.get("store_name") or shop.replace(".myshopify.com", "").title()
    dash_url = f"{backend}/app/{shop}"
    try:
        ensure_tenant_api_key(shop)
    except Exception as e:
        print(f"tenant api key bootstrap skipped: {e}")

    # Prefer merchant dashboard over static connected card
    resp = RedirectResponse(url=dash_url, status_code=302)
    set_tenant_cookie(resp, shop)
    # Fallback HTML if redirect blocked in some embeds
    resp.headers["Refresh"] = f"0;url={dash_url}"
    return resp


def _resolve_shop(request: Request, shop: Optional[str] = None) -> str:
    shop = shopify_store_id(shop or request.query_params.get("shop") or "")
    if not shop:
        shop = _shop_from_host(request.query_params.get("host")) or ""
    return shop


def _handle_app_open(request: Request, shop: str):
    """
    Shopify opens App URL after custom-distribution install / when merchant clicks the app.
    Params usually include shop, host, timestamp, hmac — but no OAuth code.
    """
    shop = shopify_store_id(shop)
    if _tenant_has_token(shop):
        return _connected_page(request, shop)
    # Need offline Admin API token via authorization code grant
    return _begin_oauth(request, shop)


@router.get("/version")
def shopify_version():
    """Quick check that the latest Shopify fix is deployed."""
    return {
        "ok": True,
        "shopify_handler": "v3-valid-shop-required",
        "hint": "App URL must be /shopify?shop=your-store.myshopify.com (opened from Shopify admin)",
    }


@router.get("")
@router.get("/")
def shopify_app_entry(
    request: Request,
    shop: Optional[str] = None,
    hmac: Optional[str] = None,
):
    """
    App URL entry — set this in Partners to:
      https://YOUR_BACKEND/shopify
    Shopify opens it with ?shop=...&host=...&hmac=... (no code).
    """
    _require_shopify_config()
    shop = _resolve_shop(request, shop)
    if shop and _is_valid_shop(shop):
        query = dict(request.query_params)
        # HMAC is present on real Shopify opens; skip only if absent (local tests)
        if hmac and not verify_shopify_hmac(query, _api_secret()):
            raise HTTPException(status_code=400, detail="Invalid HMAC")
        return _handle_app_open(request, shop)

    backend = _backend_url(request)
    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><title>AI Shopping Assistant</title>
        <style>{_page_styles()}</style></head><body><div class="card">
          <h1>AI Shopping Assistant</h1>
          <p class="muted">Open this app from Shopify admin, or install with a shop domain.</p>
          <p>Partners <strong>App URL</strong> must be <code>{backend}/shopify</code>
          (not <code>/shopify/callback</code>).</p>
          <p><a class="btn" href="{backend}/shopify/install?shop=swagdealscollection.myshopify.com">Install swagdealscollection</a></p>
          <p><a class="btn secondary" href="{backend}/admin/shopify">Admin install help</a></p>
        </div></body></html>"""
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
    shop = _resolve_shop(request, shop)

    # App URL was wrongly set to /callback, or Shopify reopened app without a code.
    if not code:
        if shop:
            if hmac and not verify_shopify_hmac(query, _api_secret()):
                # Some app-open payloads still verify; if not, still try open flow
                print("── Shopify callback without code: HMAC mismatch, continuing with shop")
            return _handle_app_open(request, shop)
        backend = _backend_url(request)
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><title>Fix App URL</title>
            <style>{_page_styles()}</style></head><body><div class="card">
              <h1>App URL misconfigured</h1>
              <p>Partners <strong>App URL</strong> must be:</p>
              <p><code>{backend}/shopify</code></p>
              <p class="muted">Allowed redirection URL should stay:</p>
              <p><code>{backend}/shopify/callback</code></p>
              <p><a class="btn" href="{backend}/admin/shopify">Admin install help</a></p>
            </div></body></html>""",
            status_code=400,
        )

    if hmac and not verify_shopify_hmac(query, _api_secret()):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    if not shop:
        raise HTTPException(status_code=400, detail="Missing shop")

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
    elif not state and _tenant_has_token(shop):
        # Rare: already connected
        return _connected_page(request, shop)

    if not state_ok:
        # After custom distribution, first authorize may lack our cookie/state —
        # still accept HMAC-verified callback with shop+code (one-time code).
        if state and hmac:
            print(f"── Shopify OAuth state cookie miss; accepting HMAC+code for {shop}")
            state_ok = True

    if not state_ok:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid OAuth state. Open the app again from Shopify admin, or "
                f"{_backend_url(request)}/shopify/install?shop={shop}&direct=1"
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
    html = f"""<!DOCTYPE html>
    <html><head><title>Shopify app installed</title>
    <style>{_page_styles()}</style>
    </head>
    <body><div class="card">
      <h1>App installed</h1>
      <p><strong>{store_name}</strong> is connected as a Shopify store.</p>
      <p>Store ID: <code>{shop}</code></p>
      <p class="muted">Enable <strong>AI Shopping Chat</strong> in Themes → Customize → App embeds.
      Backend URL: <code>{backend}</code></p>
      <p><a class="btn" href="{admin_url}" target="_blank" rel="noopener">Open in admin dashboard</a></p>
    </div></body></html>"""
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
