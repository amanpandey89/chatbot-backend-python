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
    verify_merchant_password,
    create_tenant_session_token,
    app_url_with_session,
    get_store_id_from_request,
    merchant_has_password_login,
)
from src.knowledge import SOURCE_TYPES, overview_stats, list_logs, get_rag_settings, update_rag_settings
from src.knowledge.ingest import list_sources, delete_source, upsert_and_index_async_prep, index_source_async
from src.knowledge.exclusions import list_exclusions, add_exclusion, delete_exclusion
from src.knowledge.retrieve import search as rag_search
from src.knowledge.sync import create_job, list_jobs, run_job, rebuild_embeddings
from src.knowledge.connectors.shopify_sync import collect_shopify_knowledge
from src.knowledge.connectors.crawler import crawl_urls

router = APIRouter(prefix="/app", tags=["tenant-dashboard"])
templates = Jinja2Templates(directory="src/templates")


def _fmt(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render(request: Request, name: str, ctx: dict, status_code: int = 200):
    store_id = ctx.get("store_id") or ""
    if store_id and "session_token" not in ctx:
        # Prefer existing valid session from request; else mint for nav links
        existing = (
            request.query_params.get("asa_session")
            or request.cookies.get("asa_tenant_session")
            or ""
        )
        from src.services.tenant_auth import _verify_token

        if existing and _verify_token(existing) == store_id:
            ctx["session_token"] = existing
        elif require_tenant(request, expected_store_id=store_id):
            ctx["session_token"] = create_tenant_session_token(store_id)
        else:
            ctx["session_token"] = ""
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _require(request: Request, store_id: str):
    ok = require_tenant(request, expected_store_id=store_id)
    if not ok:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    tenant = get_tenant(store_id, include_inactive=False)
    if not tenant:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    return None


def _authed_redirect(store_id: str, path: str = "", request: Optional[Request] = None):
    resp = RedirectResponse(url=app_url_with_session(store_id, path), status_code=303)
    set_tenant_cookie(resp, store_id, request=request)
    return resp


def _flash_redirect(store_id: str, path: str, message: str, request: Optional[Request] = None):
    resp = _authed_redirect(store_id, path, request=request)
    resp.set_cookie("asa_tenant_flash", message, max_age=8, path="/")
    return resp


@router.get("/login", response_class=HTMLResponse)
def app_login_picker(request: Request):
    return _render(request, "tenant/login.html", {"store_id": "", "error": None})


@router.get("/{store_id}/login", response_class=HTMLResponse)
def app_login_page(request: Request, store_id: str):
    if require_tenant(request, expected_store_id=store_id):
        return _authed_redirect(store_id, request=request)
    try:
        ensure_tenant_api_key(store_id)
    except Exception:
        pass
    return _render(
        request,
        "tenant/login.html",
        {
            "store_id": store_id,
            "error": None,
            "username": "",
            "session_token": "",
            "has_password_login": merchant_has_password_login(store_id),
        },
    )


@router.post("/{store_id}/login")
def app_login(
    request: Request,
    store_id: str,
    username: str = Form(""),
    password: str = Form(""),
):
    store_id = (store_id or "").strip()
    if not get_tenant(store_id, include_inactive=False):
        return _render(
            request,
            "tenant/login.html",
            {
                "store_id": store_id,
                "error": "Store not found or inactive.",
                "username": username,
                "session_token": "",
            },
            status_code=404,
        )

    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user or not pwd:
        return _render(
            request,
            "tenant/login.html",
            {
                "store_id": store_id,
                "error": "Enter username and password.",
                "username": user,
                "session_token": "",
            },
            status_code=400,
        )

    if not merchant_has_password_login(store_id):
        return _render(
            request,
            "tenant/login.html",
            {
                "store_id": store_id,
                "error": "Merchant login is not set up yet. Ask your admin to set username/password on this store.",
                "username": user,
                "session_token": "",
            },
            status_code=400,
        )

    if not verify_merchant_password(store_id, user, pwd):
        return _render(
            request,
            "tenant/login.html",
            {
                "store_id": store_id,
                "error": "Invalid username or password.",
                "username": user,
                "session_token": "",
            },
            status_code=401,
        )
    return _authed_redirect(store_id, request=request)


@router.post("/{store_id}/logout")
def app_logout(request: Request, store_id: str):
    resp = RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    clear_tenant_cookie(resp, request=request)
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
    rag = overview_stats(store_id)
    return _render(
        request,
        "tenant/dashboard.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "sessions": sessions,
            "knowledge_count": len(knowledge),
            "settings": settings,
            "rag": rag,
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
        return _authed_redirect(store_id, "chats")
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
            "rag": overview_stats(store_id),
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
    return _flash_redirect(store_id, "train", "Training settings saved.")


@router.post("/{store_id}/train/knowledge")
async def app_train_add(
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
    source_type = "faq" if entry_type == "faq" else (
        "policy" if entry_type == "policy" else "custom"
    )
    body = f"Q: {title}\nA: {content}" if source_type == "faq" else f"{title}\n{content}"
    prep = upsert_and_index_async_prep(
        store_id,
        source_type=source_type,
        external_id=f"{source_type}:{title[:60]}",
        title=title,
        body=body,
    )
    if not prep.get("skipped"):
        try:
            await index_source_async(
                store_id, prep["source_id"], text=prep["text"], title=title
            )
        except Exception as e:
            print(f"RAG index failed: {e}")
    return _flash_redirect(store_id, "train", "Knowledge added and indexed.")


@router.post("/{store_id}/train/knowledge/{entry_id}/delete")
def app_train_delete(request: Request, store_id: str, entry_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    training_svc.delete_knowledge(store_id, entry_id)
    return _flash_redirect(store_id, "train", "Entry deleted.")


@router.get("/{store_id}/train/sources", response_class=HTMLResponse)
def app_train_sources(request: Request, store_id: str, source_type: str = ""):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    flash = request.cookies.get("asa_tenant_flash")
    st = (source_type or "").strip() or None
    resp = _render(
        request,
        "tenant/train_sources.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "sources": list_sources(store_id, source_type=st, limit=300),
            "source_types": SOURCE_TYPES,
            "source_type": st or "",
            "exclusions": list_exclusions(store_id),
            "flash": flash,
            "active_nav": "sources",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
    return resp


@router.post("/{store_id}/train/sources/{source_id}/delete")
def app_train_source_delete(request: Request, store_id: str, source_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    delete_source(store_id, source_id)
    return _flash_redirect(store_id, "train/sources", "Source deleted.")


@router.post("/{store_id}/train/documents")
async def app_train_documents(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    form = await request.form()
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        return _flash_redirect(store_id, "train/sources", "No file uploaded.")
    raw = await upload.read()
    name = getattr(upload, "filename", None) or "document.txt"
    text = raw.decode("utf-8", errors="ignore")
    prep = upsert_and_index_async_prep(
        store_id,
        source_type="document",
        external_id=f"doc:{name}",
        title=name,
        body=text[:200000],
        meta={"filename": name},
    )
    if not prep.get("skipped"):
        await index_source_async(store_id, prep["source_id"], text=prep["text"], title=name)
    return _flash_redirect(store_id, "train/sources", "Document indexed.")


@router.post("/{store_id}/train/exclusions")
def app_train_exclusion_add(
    request: Request,
    store_id: str,
    match_type: str = Form("url"),
    match_value: str = Form(...),
    reason: str = Form(""),
):
    denied = _require(request, store_id)
    if denied:
        return denied
    add_exclusion(store_id, match_type, match_value, reason)
    return _flash_redirect(store_id, "train/sources", "Exclusion added.")


@router.post("/{store_id}/train/exclusions/{exclusion_id}/delete")
def app_train_exclusion_del(request: Request, store_id: str, exclusion_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    delete_exclusion(store_id, exclusion_id)
    return _flash_redirect(store_id, "train/sources", "Exclusion removed.")


@router.get("/{store_id}/train/sync", response_class=HTMLResponse)
def app_train_sync(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    flash = request.cookies.get("asa_tenant_flash")
    resp = _render(
        request,
        "tenant/train_sync.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "jobs": list_jobs(store_id),
            "rag_settings": get_rag_settings(store_id),
            "flash": flash,
            "active_nav": "sync",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
    return resp


@router.post("/{store_id}/train/sync/shopify")
async def app_train_sync_shopify(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True) or {}
    tenant = {**tenant, "store_id": store_id}
    job = create_job(store_id, "shopify_full_sync")
    items = await collect_shopify_knowledge(tenant)
    await run_job(store_id, job["id"], items)
    return _flash_redirect(store_id, "train/sync", f"Shopify sync finished ({len(items)} items).")


@router.post("/{store_id}/train/sync/rebuild")
async def app_train_sync_rebuild(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    await rebuild_embeddings(store_id)
    return _flash_redirect(store_id, "train/sync", "Embeddings rebuilt.")


@router.post("/{store_id}/train/sync/crawl")
async def app_train_sync_crawl(
    request: Request, store_id: str, seed_urls: str = Form("")
):
    denied = _require(request, store_id)
    if denied:
        return denied
    update_rag_settings(store_id, crawler_seed_urls=seed_urls)
    seeds = [u.strip() for u in seed_urls.splitlines() if u.strip()]
    job = create_job(store_id, "website_crawl")
    items = await crawl_urls(seeds)
    await run_job(store_id, job["id"], items)
    return _flash_redirect(store_id, "train/sync", f"Crawl finished ({len(items)} pages).")


@router.get("/{store_id}/train/search", response_class=HTMLResponse)
def app_train_search_page(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    return _render(
        request,
        "tenant/train_search.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "query": "",
            "results": None,
            "error": None,
            "active_nav": "search",
            "fmt": _fmt,
        },
    )


@router.post("/{store_id}/train/search", response_class=HTMLResponse)
async def app_train_search_run(
    request: Request, store_id: str, query: str = Form(...)
):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    error = None
    results = []
    try:
        results = await rag_search(store_id, query)
    except Exception as e:
        error = str(e)
    return _render(
        request,
        "tenant/train_search.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "query": query,
            "results": results,
            "error": error,
            "active_nav": "search",
            "fmt": _fmt,
        },
    )


@router.get("/{store_id}/train/logs", response_class=HTMLResponse)
def app_train_logs(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_secrets=False)
    return _render(
        request,
        "tenant/train_logs.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "logs": list_logs(store_id, limit=150),
            "active_nav": "train",
            "fmt": _fmt,
        },
    )


@router.get("/{store_id}/settings", response_class=HTMLResponse)
def app_settings(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    from src.services.tenant_auth import tenant_has_openai_key

    tenant = get_tenant(store_id, include_secrets=False)
    api_key = ensure_tenant_api_key(store_id)
    backend = str(request.base_url).rstrip("/")
    flash = request.cookies.get("asa_tenant_flash")
    resp = _render(
        request,
        "tenant/settings.html",
        {
            "tenant": tenant,
            "store_id": store_id,
            "api_key": api_key,
            "backend": backend,
            "openai_key_set": tenant_has_openai_key(store_id),
            "flash": flash,
            "error": None,
            "active_nav": "settings",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
    return resp


@router.post("/{store_id}/settings/openai-key")
def app_settings_openai_key(
    request: Request, store_id: str, openai_api_key: str = Form("")
):
    denied = _require(request, store_id)
    if denied:
        return denied
    from src.services.tenant_auth import set_tenant_openai_api_key

    key = (openai_api_key or "").strip()
    if not key:
        return _flash_redirect(store_id, "settings", "Enter an OpenAI API key.", request=request)
    set_tenant_openai_api_key(store_id, key)
    return _flash_redirect(store_id, "settings", "OpenAI API key saved.", request=request)
