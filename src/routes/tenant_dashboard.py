"""Merchant tenant dashboard — chats + Train AI (Shopify + API-key login)."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.services.store import get_tenant, list_sessions_for_store, get_session_detail
from src.services import training as training_svc
from src.services.tenant_auth import (
    require_tenant,
    set_tenant_cookie,
    clear_tenant_cookie,
    ensure_tenant_api_key,
    verify_tenant_api_key,
)

router = APIRouter(prefix="/app", tags=["tenant-dashboard"])
templates = Jinja2Templates(directory="src/templates")


def _fmt(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _require(request: Request, store_id: str):
    ok = require_tenant(request, expected_store_id=store_id)
    if not ok:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    tenant = get_tenant(store_id, include_inactive=False)
    if not tenant:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    return None


@router.get("/login", response_class=HTMLResponse)
def app_login_picker(request: Request):
    return _render(request, "tenant/login.html", {"store_id": "", "error": None})


@router.get("/{store_id}/login", response_class=HTMLResponse)
def app_login_page(request: Request, store_id: str):
    return _render(
        request, "tenant/login.html", {"store_id": store_id, "error": None}
    )


@router.post("/{store_id}/login")
def app_login(
    request: Request,
    store_id: str,
    tenant_api_key: str = Form(...),
):
    store_id = (store_id or "").strip()
    if not get_tenant(store_id, include_inactive=False):
        return _render(
            request,
            "tenant/login.html",
            {"store_id": store_id, "error": "Store not found or inactive."},
            status_code=404,
        )
    # Bootstrap: if no key yet, first login with empty is not allowed —
    # Shopify OAuth path sets cookie. Allow create-if-matches after ensure? 
    # For first-time API users: if no key exists, reject until they open from Shopify once.
    from src.services.store import get_tenant as gt

    tenant = gt(store_id, include_inactive=False, include_secrets=True) or {}
    if not tenant.get("tenant_api_key"):
        # Generate key only if they somehow know a placeholder — instead generate and show after Shopify
        return _render(
            request,
            "tenant/login.html",
            {
                "store_id": store_id,
                "error": "No API key yet. Open the app from Shopify admin once, then copy the key from Settings.",
            },
            status_code=400,
        )
    if not verify_tenant_api_key(store_id, tenant_api_key.strip()):
        return _render(
            request,
            "tenant/login.html",
            {"store_id": store_id, "error": "Invalid API key."},
            status_code=401,
        )
    resp = RedirectResponse(url=f"/app/{store_id}", status_code=303)
    set_tenant_cookie(resp, store_id)
    return resp


@router.post("/{store_id}/logout")
def app_logout(store_id: str):
    resp = RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    clear_tenant_cookie(resp)
    return resp


@router.get("/{store_id}", response_class=HTMLResponse)
def app_home(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    sessions = list_sessions_for_store(store_id, limit=8)
    knowledge = training_svc.list_knowledge(store_id, include_inactive=True)
    settings = training_svc.get_tenant_settings(store_id)
    return _render(
        request,
        "tenant/dashboard.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "sessions": sessions,
            "knowledge_count": len(knowledge),
            "settings": settings,
            "active_nav": "overview",
            "fmt": _fmt,
        },
    )


@router.get("/{store_id}/chats", response_class=HTMLResponse)
def app_chats(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    sessions = list_sessions_for_store(store_id, limit=100)
    return _render(
        request,
        "tenant/chats.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "sessions": sessions,
            "active_nav": "chats",
            "fmt": _fmt,
        },
    )


@router.get("/{store_id}/chats/{session_id}", response_class=HTMLResponse)
def app_chat_detail(request: Request, store_id: str, session_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    detail = get_session_detail(session_id, store_id=store_id)
    if not detail:
        return RedirectResponse(url=f"/app/{store_id}/chats", status_code=303)
    return _render(
        request,
        "tenant/chat_detail.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "session": detail,
            "active_nav": "chats",
            "fmt": _fmt,
        },
    )


@router.get("/{store_id}/train", response_class=HTMLResponse)
def app_train(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    settings = training_svc.get_tenant_settings(store_id)
    entries = training_svc.list_knowledge(store_id, include_inactive=True)
    flash = request.cookies.get("asa_tenant_flash")
    resp = _render(
        request,
        "tenant/train.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "settings": settings,
            "entries": entries,
            "flash": flash,
            "error": None,
            "active_nav": "train",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
    return resp


@router.post("/{store_id}/train/settings")
def app_train_settings(
    request: Request,
    store_id: str,
    tone: str = Form(""),
    instructions: str = Form(""),
):
    denied = _require(request, store_id)
    if denied:
        return denied
    training_svc.update_tenant_settings(store_id, tone=tone, instructions=instructions)
    resp = RedirectResponse(url=f"/app/{store_id}/train", status_code=303)
    resp.set_cookie("asa_tenant_flash", "Training settings saved.", max_age=6)
    return resp


@router.post("/{store_id}/train/knowledge")
def app_train_add(
    request: Request,
    store_id: str,
    title: str = Form(...),
    content: str = Form(...),
    entry_type: str = Form("faq"),
):
    denied = _require(request, store_id)
    if denied:
        return denied
    training_svc.create_knowledge(
        store_id, title=title, content=content, entry_type=entry_type, active=True
    )
    resp = RedirectResponse(url=f"/app/{store_id}/train", status_code=303)
    resp.set_cookie("asa_tenant_flash", "Knowledge entry added.", max_age=6)
    return resp


@router.post("/{store_id}/train/knowledge/{entry_id}/delete")
def app_train_delete(request: Request, store_id: str, entry_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    training_svc.delete_knowledge(store_id, entry_id)
    resp = RedirectResponse(url=f"/app/{store_id}/train", status_code=303)
    resp.set_cookie("asa_tenant_flash", "Entry deleted.", max_age=6)
    return resp


@router.get("/{store_id}/settings", response_class=HTMLResponse)
def app_settings(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    api_key = ensure_tenant_api_key(store_id)
    backend = str(request.base_url).rstrip("/")
    return _render(
        request,
        "tenant/settings.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "api_key": api_key,
            "backend": backend,
            "active_nav": "settings",
            "fmt": _fmt,
        },
    )
