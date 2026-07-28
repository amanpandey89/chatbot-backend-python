import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.services.admin_auth import (
    COOKIE_NAME,
    check_credentials,
    create_session_token,
    get_admin_display_name,
    get_admin_profile,
    get_admin_username,
    is_authenticated,
    login_redirect,
    update_admin_profile,
)
from src.services.store import (
    delete_tenant,
    get_tenant,
    list_tenants,
    register_tenant,
    set_tenant_active,
    tenant_stats,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="src/templates")


def _flash_redirect(url: str, message: str):
    resp = RedirectResponse(url=url, status_code=303)
    resp.set_cookie("asa_flash", message, max_age=8, httponly=False, samesite="lax")
    return resp


def _pop_flash(request: Request) -> Optional[str]:
    return request.cookies.get("asa_flash")


def _clear_flash(response):
    response.delete_cookie("asa_flash")
    return response


def _require_page_auth(request: Request):
    if not is_authenticated(request):
        return login_redirect()
    return None


def _format_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render(request: Request, name: str, context: dict, status_code: int = 200):
    context = {
        **context,
        "admin_username": get_admin_username(),
        "admin_display_name": get_admin_display_name(),
    }
    return templates.TemplateResponse(
        request, name, context, status_code=status_code
    )


@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/admin", status_code=303)
    return _render(request, "admin/login.html", {"error": None})


@router.post("/login")
def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not check_credentials(username, password):
        return _render(
            request,
            "admin/login.html",
            {"error": "Invalid username or password."},
            status_code=401,
        )
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        create_session_token(),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return resp


@router.post("/logout")
def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/profile", response_class=HTMLResponse)
def admin_profile_page(request: Request):
    denied = _require_page_auth(request)
    if denied:
        return denied
    flash = _pop_flash(request)
    response = _render(
        request,
        "admin/profile.html",
        {
            "profile": get_admin_profile(),
            "error": None,
            "flash": flash,
            "active_nav": "profile",
        },
    )
    if flash:
        _clear_flash(response)
    return response


@router.post("/profile")
def admin_profile_update(
    request: Request,
    display_name: str = Form(...),
    username: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    denied = _require_page_auth(request)
    if denied:
        return denied

    profile = get_admin_profile()
    if not check_credentials(profile["username"], current_password):
        return _render(
            request,
            "admin/profile.html",
            {
                "profile": profile,
                "error": "Current password is incorrect.",
                "active_nav": "profile",
            },
            status_code=400,
        )

    username = username.strip()
    display_name = display_name.strip()
    new_password = (new_password or "").strip()
    confirm_password = (confirm_password or "").strip()

    if not username:
        return _render(
            request,
            "admin/profile.html",
            {
                "profile": profile,
                "error": "Username is required.",
                "active_nav": "profile",
            },
            status_code=400,
        )

    if new_password or confirm_password:
        if len(new_password) < 6:
            return _render(
                request,
                "admin/profile.html",
                {
                    "profile": profile,
                    "error": "New password must be at least 6 characters.",
                    "active_nav": "profile",
                },
                status_code=400,
            )
        if new_password != confirm_password:
            return _render(
                request,
                "admin/profile.html",
                {
                    "profile": profile,
                    "error": "New password and confirmation do not match.",
                    "active_nav": "profile",
                },
                status_code=400,
            )

    try:
        updated = update_admin_profile(
            username=username,
            display_name=display_name,
            new_password=new_password or None,
        )
    except ValueError as e:
        return _render(
            request,
            "admin/profile.html",
            {
                "profile": profile,
                "error": str(e),
                "active_nav": "profile",
            },
            status_code=400,
        )

    resp = _flash_redirect("/admin/profile", "Profile updated successfully.")
    # Refresh session cookie if username changed
    resp.set_cookie(
        COOKIE_NAME,
        create_session_token(updated["username"]),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return resp


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    denied = _require_page_auth(request)
    if denied:
        return denied

    stats = tenant_stats()
    stores = list_tenants(include_inactive=True, include_secrets=False)
    flash = _pop_flash(request)
    response = _render(
        request,
        "admin/dashboard.html",
        {
            "stats": stats,
            "stores": stores,
            "session_counts": stats.get("sessions_by_store") or {},
            "flash": flash,
            "active_nav": "overview",
        },
    )
    if flash:
        _clear_flash(response)
    return response


@router.get("/stores", response_class=HTMLResponse)
def admin_stores(request: Request, platform: Optional[str] = None):
    denied = _require_page_auth(request)
    if denied:
        return denied

    platform = (platform or "").strip().lower() or None
    stats = tenant_stats()
    stores = list_tenants(
        platform=platform, include_inactive=True, include_secrets=False
    )
    flash = _pop_flash(request)
    response = _render(
        request,
        "admin/stores.html",
        {
            "stores": stores,
            "platform": platform or "",
            "session_counts": stats.get("sessions_by_store") or {},
            "flash": flash,
            "active_nav": "stores",
        },
    )
    if flash:
        _clear_flash(response)
    return response


@router.get("/shopify", response_class=HTMLResponse)
def admin_shopify_install_page(request: Request):
    denied = _require_page_auth(request)
    if denied:
        return denied
    backend = str(request.base_url).rstrip("/")
    return _render(
        request,
        "admin/shopify_install.html",
        {"error": None, "active_nav": "shopify", "backend_url": backend},
    )


@router.get("/stores/new", response_class=HTMLResponse)
def admin_store_new_page(request: Request):
    denied = _require_page_auth(request)
    if denied:
        return denied
    return _render(request, "admin/store_new.html", {"error": None, "active_nav": "add"})


@router.post("/stores/new")
def admin_store_new(
    request: Request,
    platform: str = Form("woocommerce"),
    store_name: str = Form(...),
    store_url: str = Form(...),
    store_id: str = Form(""),
    consumer_key: str = Form(""),
    consumer_secret: str = Form(""),
    access_token: str = Form(""),
):
    denied = _require_page_auth(request)
    if denied:
        return denied

    platform = (platform or "woocommerce").strip().lower()
    if platform == "wordpress":
        platform = "woocommerce"

    raw_store_id = (store_id or "").strip()
    store_url = store_url.strip().rstrip("/")
    if platform == "shopify":
        from src.services.shopify_service import shopify_store_id

        store_url = shopify_store_id(store_url)
        store_id = raw_store_id or store_url
    else:
        store_id = raw_store_id or str(uuid.uuid4())

    payload = {
        "platform": platform,
        "store_name": store_name.strip(),
        "store_url": store_url,
    }
    if platform == "shopify":
        payload["shop"] = store_url
    if consumer_key.strip():
        payload["consumer_key"] = consumer_key.strip()
    if consumer_secret.strip():
        payload["consumer_secret"] = consumer_secret.strip()
    if access_token.strip():
        payload["access_token"] = access_token.strip()

    if not payload["store_url"]:
        return _render(
            request,
            "admin/store_new.html",
            {"error": "Store URL is required.", "active_nav": "add"},
            status_code=400,
        )

    register_tenant(store_id, payload, active=True)
    from src.services.tenant_auth import ensure_tenant_api_key

    ensure_tenant_api_key(store_id)
    return _flash_redirect(f"/admin/stores/{store_id}", "Store connected successfully.")


@router.get("/stores/{store_id}", response_class=HTMLResponse)
def admin_store_detail(request: Request, store_id: str):
    denied = _require_page_auth(request)
    if denied:
        return denied

    store = get_tenant(store_id, include_inactive=True, include_secrets=False)
    if not store:
        return _flash_redirect("/admin/stores", "Store not found.")

    from src.services.tenant_auth import ensure_tenant_api_key

    try:
        tenant_api_key = ensure_tenant_api_key(store_id)
    except ValueError:
        tenant_api_key = ""

    stats = tenant_stats()
    backend = str(request.base_url).rstrip("/")
    embed = (
        f'<script src="{backend}/static/chatbot.js" '
        f'data-store-id="{store_id}" '
        f'data-backend-url="{backend}" defer></script>'
    )
    flash = _pop_flash(request)
    response = _render(
        request,
        "admin/store_detail.html",
        {
            "store": store,
            "session_count": (stats.get("sessions_by_store") or {}).get(store_id, 0),
            "updated_label": _format_ts(store.get("updated_at")),
            "embed_snippet": embed,
            "tenant_api_key": tenant_api_key,
            "flash": flash,
            "active_nav": "stores",
        },
    )
    if flash:
        _clear_flash(response)
    return response


@router.post("/stores/{store_id}/toggle")
def admin_store_toggle(request: Request, store_id: str):
    denied = _require_page_auth(request)
    if denied:
        return denied

    store = get_tenant(store_id, include_inactive=True, include_secrets=False)
    if not store:
        return _flash_redirect("/admin/stores", "Store not found.")

    new_state = not bool(store.get("active"))
    set_tenant_active(store_id, new_state)
    msg = "Store enabled." if new_state else "Store disabled."
    return _flash_redirect(f"/admin/stores/{store_id}", msg)


@router.post("/stores/{store_id}/delete")
def admin_store_delete(request: Request, store_id: str):
    denied = _require_page_auth(request)
    if denied:
        return denied

    delete_tenant(store_id)
    return _flash_redirect("/admin/stores", "Store deleted.")
