"""Merchant tenant dashboard — chats + Train AI (Shopify + API-key login)."""

import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from src.knowledge.sync import (
    create_job,
    list_jobs,
    run_job,
    rebuild_embeddings,
    fail_job,
    mark_job_fetching,
    has_active_job,
)
from src.knowledge.connectors.shopify_sync import collect_shopify_knowledge
from src.knowledge.connectors.woocommerce_sync import (
    collect_woocommerce_knowledge,
    woocommerce_sync_ready,
)
from src.knowledge.connectors.crawler import crawl_urls

router = APIRouter(prefix="/app", tags=["tenant-dashboard"])
templates = Jinja2Templates(directory="src/templates")


def _fire_and_forget(coro):
    """Run long sync work outside the HTTP request (avoids Render 502 timeouts)."""
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    task.add_done_callback(_done)
    return task


async def _bg_catalog_sync(store_id: str, job_id: str, tenant: dict, *, platform: str):
    try:
        mark_job_fetching(job_id)
        if platform == "shopify":
            items = await collect_shopify_knowledge(tenant)
            empty_msg = "Shopify sync found 0 items. Check store URL and access token."
        else:
            items = await collect_woocommerce_knowledge(tenant)
            empty_msg = (
                "WordPress sync found 0 items. Check store URL and WooCommerce API keys."
            )
        if not items:
            fail_job(
                job_id,
                empty_msg,
                totals={"total": 0, "indexed": 0, "skipped": 0, "failed": 0},
            )
            return
        await run_job(store_id, job_id, items)
    except Exception as e:
        fail_job(job_id, str(e))


async def _bg_crawl_sync(store_id: str, job_id: str, seeds: list):
    try:
        mark_job_fetching(job_id)
        items = await crawl_urls(seeds)
        if not items:
            fail_job(
                job_id,
                "Crawl found 0 pages. Check seed URLs.",
                totals={"total": 0, "indexed": 0, "skipped": 0, "failed": 0},
            )
            return
        await run_job(store_id, job_id, items)
    except Exception as e:
        fail_job(job_id, str(e))


async def _bg_rebuild(store_id: str):
    """rebuild_embeddings() creates and tracks its own job row."""
    try:
        await rebuild_embeddings(store_id)
    except Exception as e:
        job = create_job(store_id, "rebuild_embeddings")
        fail_job(job["id"], str(e))


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


def _flash_redirect(
    store_id: str,
    path: str,
    message: str,
    request: Optional[Request] = None,
    *,
    error: bool = False,
):
    resp = _authed_redirect(store_id, path, request=request)
    # Cookie values must be Latin-1; strip fancy punctuation that caused 500s.
    safe = (
        (message or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("…", "...")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )[:450]
    resp.set_cookie("asa_tenant_flash", safe, max_age=12, path="/")
    resp.set_cookie(
        "asa_tenant_flash_type",
        "error" if error else "ok",
        max_age=12,
        path="/",
    )
    return resp


def _openai_ready(store_id: str) -> tuple[bool, str]:
    try:
        from src.services.openai_service import resolve_openai_api_key

        resolve_openai_api_key(store_id=store_id)
        return True, ""
    except Exception as e:
        return False, str(e)


def _job_flash(job: dict, label: str) -> tuple[str, bool]:
    totals = job.get("totals") or {}
    indexed = int(totals.get("indexed") or 0)
    failed = int(totals.get("failed") or 0)
    skipped = int(totals.get("skipped") or 0)
    total = int(totals.get("total") or 0)
    err = (job.get("error") or "").strip()
    if failed and not indexed:
        msg = (
            f"{label} failed: 0 indexed / {failed} failed"
            + (f" of {total}." if total else ".")
        )
        if err:
            msg += f" Reason: {err[:220]}"
        else:
            msg += " Set an OpenAI API key in Settings, then sync again."
        return msg, True
    if failed:
        msg = f"{label} finished with errors: {indexed} indexed, {failed} failed, {skipped} skipped."
        if err:
            msg += f" First error: {err[:160]}"
        return msg, True
    return (
        f"{label} finished: {indexed} indexed"
        + (f", {skipped} skipped" if skipped else "")
        + f" (of {total}).",
        False,
    )


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
    tenant = get_tenant(store_id, include_secrets=True) or {}
    # Don't leak secrets into the template
    safe_tenant = {
        k: v
        for k, v in tenant.items()
        if k
        not in (
            "consumer_secret",
            "access_token",
            "tenant_api_key",
            "openai_api_key",
            "merchant_password_hash",
        )
    }
    safe_tenant["has_woo_credentials"] = woocommerce_sync_ready(tenant)
    openai_ok, openai_err = _openai_ready(store_id)
    safe_tenant["has_openai_key"] = openai_ok
    safe_tenant["openai_error"] = openai_err
    flash = request.cookies.get("asa_tenant_flash")
    flash_type = request.cookies.get("asa_tenant_flash_type") or "ok"
    jobs = list_jobs(store_id)
    sync_active = has_active_job(store_id)
    resp = _render(
        request,
        "tenant/train_sync.html",
        {
            "tenant": safe_tenant,
            "store_id": store_id,
            "jobs": jobs,
            "sync_active": sync_active,
            "rag_settings": get_rag_settings(store_id),
            "flash": flash,
            "flash_type": flash_type,
            "active_nav": "sync",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
        resp.delete_cookie("asa_tenant_flash_type")
    return resp


@router.get("/{store_id}/train/sync/status")
def app_train_sync_status(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    jobs = list_jobs(store_id, limit=10)
    return JSONResponse(
        {
            "success": True,
            "active": has_active_job(store_id),
            "jobs": jobs,
        }
    )


@router.post("/{store_id}/train/sync/shopify")
async def app_train_sync_shopify(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    ok, err = _openai_ready(store_id)
    if not ok:
        return _flash_redirect(
            store_id,
            "train/sync",
            f"Cannot sync: {err}",
            error=True,
        )
    if has_active_job(store_id):
        return _flash_redirect(
            store_id,
            "train/sync",
            "A sync is already running. Wait for it to finish.",
            error=True,
        )
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True) or {}
    tenant = {**tenant, "store_id": store_id}
    job = create_job(store_id, "shopify_full_sync")
    _fire_and_forget(_bg_catalog_sync(store_id, job["id"], tenant, platform="shopify"))
    return _flash_redirect(
        store_id,
        "train/sync",
        "Shopify sync started in the background. This page will refresh until it finishes.",
    )


@router.post("/{store_id}/train/sync/woocommerce")
async def app_train_sync_woocommerce(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True) or {}
    tenant = {**tenant, "store_id": store_id}
    if not woocommerce_sync_ready(tenant):
        return _flash_redirect(
            store_id,
            "train/sync",
            "WordPress sync needs store URL + consumer key/secret under Settings → Store connection.",
            error=True,
        )
    ok, err = _openai_ready(store_id)
    if not ok:
        return _flash_redirect(
            store_id,
            "train/sync",
            f"Cannot index content: {err}",
            error=True,
        )
    if has_active_job(store_id):
        return _flash_redirect(
            store_id,
            "train/sync",
            "A sync is already running. Wait for it to finish.",
            error=True,
        )
    job = create_job(store_id, "wordpress_full_sync")
    _fire_and_forget(
        _bg_catalog_sync(store_id, job["id"], tenant, platform="woocommerce")
    )
    return _flash_redirect(
        store_id,
        "train/sync",
        "WordPress sync started in the background. This page will refresh until it finishes.",
    )


@router.api_route("/{store_id}/train/sync/rebuild", methods=["GET", "POST"])
async def app_train_sync_rebuild(request: Request, store_id: str):
    denied = _require(request, store_id)
    if denied:
        return denied
    if request.method == "GET":
        return _flash_redirect(
            store_id,
            "train/sync",
            "Use the Rebuild embeddings button on the Sync page (do not open this URL directly).",
        )
    from src.knowledge.ingest import list_sources

    sources = list_sources(store_id)
    if not sources:
        return _flash_redirect(
            store_id,
            "train/sync",
            "Nothing to rebuild yet - run WordPress / Shopify sync (or crawl) first.",
            error=True,
        )
    ok, err = _openai_ready(store_id)
    if not ok:
        return _flash_redirect(
            store_id,
            "train/sync",
            f"Cannot rebuild: {err}",
            error=True,
        )
    if has_active_job(store_id):
        return _flash_redirect(
            store_id,
            "train/sync",
            "A sync is already running. Wait for it to finish.",
            error=True,
        )
    _fire_and_forget(_bg_rebuild(store_id))
    return _flash_redirect(
        store_id,
        "train/sync",
        "Rebuild started in the background. This page will refresh until it finishes.",
    )


@router.post("/{store_id}/train/sync/crawl")
async def app_train_sync_crawl(
    request: Request, store_id: str, seed_urls: str = Form("")
):
    denied = _require(request, store_id)
    if denied:
        return denied
    ok, err = _openai_ready(store_id)
    if not ok:
        return _flash_redirect(
            store_id,
            "train/sync",
            f"Cannot crawl/index: {err}",
            error=True,
        )
    if has_active_job(store_id):
        return _flash_redirect(
            store_id,
            "train/sync",
            "A sync is already running. Wait for it to finish.",
            error=True,
        )
    update_rag_settings(store_id, crawler_seed_urls=seed_urls)
    seeds = [u.strip() for u in seed_urls.splitlines() if u.strip()]
    job = create_job(store_id, "website_crawl")
    _fire_and_forget(_bg_crawl_sync(store_id, job["id"], seeds))
    return _flash_redirect(
        store_id,
        "train/sync",
        "Crawl started in the background. This page will refresh until it finishes.",
    )


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

    tenant = get_tenant(store_id, include_secrets=True) or {}
    api_key = ensure_tenant_api_key(store_id)
    backend = str(request.base_url).rstrip("/")
    flash = request.cookies.get("asa_tenant_flash")
    flash_type = request.cookies.get("asa_tenant_flash_type") or "ok"
    consumer_key_set = bool((tenant.get("consumer_key") or "").strip())
    consumer_secret_set = bool((tenant.get("consumer_secret") or "").strip())
    store_url_set = bool((tenant.get("store_url") or "").strip())
    woo_ready = store_url_set and consumer_key_set and consumer_secret_set
    # Do not send raw secrets to the browser
    safe_tenant = {
        **tenant,
        "consumer_key": "",
        "consumer_secret": "",
        "tenant_api_key": "",
        "openai_api_key": "",
        "access_token": "",
    }
    resp = _render(
        request,
        "tenant/settings.html",
        {
            "tenant": safe_tenant,
            "store_id": store_id,
            "api_key": api_key,
            "backend": backend,
            "openai_key_set": tenant_has_openai_key(store_id),
            "consumer_key_set": consumer_key_set,
            "consumer_secret_set": consumer_secret_set,
            "woo_ready": woo_ready,
            "flash": flash,
            "flash_type": flash_type,
            "error": None,
            "active_nav": "settings",
            "fmt": _fmt,
        },
    )
    if flash:
        resp.delete_cookie("asa_tenant_flash")
        resp.delete_cookie("asa_tenant_flash_type")
    return resp


@router.post("/{store_id}/settings/store-connection")
def app_settings_store_connection(
    request: Request,
    store_id: str,
    store_url: str = Form(""),
    store_name: str = Form(""),
    consumer_key: str = Form(""),
    consumer_secret: str = Form(""),
):
    denied = _require(request, store_id)
    if denied:
        return denied
    from src.services.store import register_tenant

    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        return _flash_redirect(
            store_id, "settings", "Store not found.", request=request, error=True
        )

    url = (store_url or "").strip().rstrip("/")
    if not url.startswith("http"):
        return _flash_redirect(
            store_id,
            "settings",
            "Store URL must be an absolute http(s) URL.",
            request=request,
            error=True,
        )

    key = (consumer_key or "").strip()
    secret = (consumer_secret or "").strip()
    name = (store_name or "").strip() or tenant.get("store_name") or store_id

    if not key and not (tenant.get("consumer_key") or "").strip():
        return _flash_redirect(
            store_id,
            "settings",
            "Enter a WooCommerce consumer key.",
            request=request,
            error=True,
        )
    if not secret and not (tenant.get("consumer_secret") or "").strip():
        return _flash_redirect(
            store_id,
            "settings",
            "Enter a WooCommerce consumer secret.",
            request=request,
            error=True,
        )

    payload = {
        k: v
        for k, v in tenant.items()
        if k not in ("store_id", "active", "created_at", "updated_at")
    }
    payload["platform"] = tenant.get("platform") or "woocommerce"
    payload["store_url"] = url
    payload["store_name"] = name
    if key:
        payload["consumer_key"] = key
    if secret:
        payload["consumer_secret"] = secret

    register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    return _flash_redirect(
        store_id, "settings", "Store connection saved.", request=request
    )


@router.post("/{store_id}/settings/test-woocommerce")
async def app_settings_test_woocommerce(request: Request, store_id: str):
    """Verify live WooCommerce REST access (same path chat uses for product cards)."""
    denied = _require(request, store_id)
    if denied:
        return denied
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True) or {}
    tenant = {**tenant, "store_id": store_id}
    try:
        from src.services.woocommerce import woo_get, require_woo_credentials

        store_url, key, secret = require_woo_credentials(tenant)
        key_hint = (key[:10] + "…") if len(key) > 10 else key
        resp = await woo_get(
            f"{store_url}/wp-json/wc/v3/products",
            consumer_key=key,
            consumer_secret=secret,
            params={"per_page": 3, "status": "publish"},
            timeout=20.0,
        )
        if resp.status_code in (401, 403):
            return _flash_redirect(
                store_id,
                "settings",
                (
                    f"WooCommerce test failed: API keys rejected (HTTP {resp.status_code}). "
                    f"Saved key starts with {key_hint}. "
                    "Generate a NEW key, paste BOTH key and secret into the fields "
                    "(do not leave blank), click Save store connection, then Test again."
                ),
                request=request,
                error=True,
            )
        if resp.status_code >= 400:
            return _flash_redirect(
                store_id,
                "settings",
                f"WooCommerce test failed: HTTP {resp.status_code} {resp.text[:160]}",
                request=request,
                error=True,
            )
        data = resp.json()
        count = len(data) if isinstance(data, list) else 0
        names = ", ".join(
            (p.get("name") or "") for p in (data or [])[:3] if isinstance(p, dict)
        )
        msg = f"WooCommerce OK — loaded {count} sample product(s) (key {key_hint})"
        if names:
            msg += f": {names}"
        return _flash_redirect(store_id, "settings", msg, request=request)
    except Exception as e:
        return _flash_redirect(
            store_id,
            "settings",
            f"WooCommerce test failed: {e}",
            request=request,
            error=True,
        )


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
