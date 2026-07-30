"""REST API for tenant training + chat history (WordPress + Shopify)."""

from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.services.store import get_tenant, list_sessions_for_store, get_session_detail
from src.services.tenant_auth import (
    get_store_id_from_request,
    ensure_tenant_api_key,
    verify_tenant_api_key,
    tenant_auth_error,
)
from src.services import training as training_svc

router = APIRouter(prefix="/tenant", tags=["tenant"])


def _auth_store(request: Request, store_id: str) -> str:
    key = request.headers.get("X-Tenant-Key") or request.headers.get("X-API-Key")
    header_store = request.headers.get("X-Store-Id")
    if key and header_store:
        if header_store != store_id:
            raise HTTPException(status_code=403, detail="Store mismatch")
        if not verify_tenant_api_key(store_id, key):
            raise HTTPException(
                status_code=401, detail=tenant_auth_error(store_id, key)
            )
        return store_id
    resolved = get_store_id_from_request(request)
    if not resolved or resolved != store_id:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized. Send X-Store-Id and X-Tenant-Key headers.",
        )
    return store_id


class SettingsBody(BaseModel):
    tone: Optional[str] = None
    instructions: Optional[str] = None
    openai_api_key: Optional[str] = None


class KnowledgeBody(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    entry_type: str = "faq"
    active: bool = True


class KnowledgeUpdateBody(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    entry_type: Optional[str] = None
    active: Optional[bool] = None


@router.get("/{store_id}/settings")
def api_get_settings(request: Request, store_id: str):
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    from src.services.tenant_auth import tenant_has_openai_key

    settings = training_svc.get_tenant_settings(store_id)
    settings["openai_api_key_set"] = tenant_has_openai_key(store_id)
    return {"success": True, "settings": settings}


@router.put("/{store_id}/settings")
def api_put_settings(request: Request, store_id: str, body: SettingsBody):
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    settings = training_svc.update_tenant_settings(
        store_id, tone=body.tone, instructions=body.instructions
    )
    if body.openai_api_key is not None and str(body.openai_api_key).strip():
        from src.services.tenant_auth import set_tenant_openai_api_key

        set_tenant_openai_api_key(store_id, str(body.openai_api_key).strip())
    from src.services.tenant_auth import tenant_has_openai_key

    settings["openai_api_key_set"] = tenant_has_openai_key(store_id)
    return {"success": True, "settings": settings}


@router.get("/{store_id}/knowledge")
def api_list_knowledge(request: Request, store_id: str, entry_type: Optional[str] = None):
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    items = training_svc.list_knowledge(
        store_id, include_inactive=True, entry_type=entry_type
    )
    return {"success": True, "count": len(items), "entries": items}


@router.post("/{store_id}/knowledge")
def api_create_knowledge(request: Request, store_id: str, body: KnowledgeBody):
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    entry = training_svc.create_knowledge(
        store_id,
        title=body.title,
        content=body.content,
        entry_type=body.entry_type,
        active=body.active,
    )
    return {"success": True, "entry": entry}


@router.put("/{store_id}/knowledge/{entry_id}")
def api_update_knowledge(
    request: Request, store_id: str, entry_id: str, body: KnowledgeUpdateBody
):
    _auth_store(request, store_id)
    entry = training_svc.update_knowledge(
        store_id,
        entry_id,
        title=body.title,
        content=body.content,
        entry_type=body.entry_type,
        active=body.active,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"success": True, "entry": entry}


@router.delete("/{store_id}/knowledge/{entry_id}")
def api_delete_knowledge(request: Request, store_id: str, entry_id: str):
    _auth_store(request, store_id)
    ok = training_svc.delete_knowledge(store_id, entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"success": True}


@router.get("/{store_id}/chats")
def api_list_chats(request: Request, store_id: str, limit: int = 50):
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    sessions = list_sessions_for_store(store_id, limit=limit)
    return {"success": True, "count": len(sessions), "sessions": sessions}


@router.get("/{store_id}/chats/{session_id}")
def api_get_chat(request: Request, store_id: str, session_id: str):
    _auth_store(request, store_id)
    detail = get_session_detail(session_id, store_id=store_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"success": True, "session": detail}


@router.get("/{store_id}/api-key")
def api_get_tenant_key(request: Request, store_id: str):
    """Return/create tenant API key for WordPress plugin integrations."""
    _auth_store(request, store_id)
    if not get_tenant(store_id):
        raise HTTPException(status_code=404, detail="Store not found")
    key = ensure_tenant_api_key(store_id)
    return {
        "success": True,
        "store_id": store_id,
        "tenant_api_key": key,
        "headers": {
            "X-Store-Id": store_id,
            "X-Tenant-Key": key,
        },
    }


class MerchantSetupBody(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    store_url: str = ""


@router.get("/{store_id}/merchant/status")
def api_merchant_status(store_id: str):
    """Public: whether WordPress still needs first-time merchant account setup."""
    from src.services.tenant_auth import merchant_has_password_login

    tenant = get_tenant(store_id, include_inactive=True)
    if not tenant:
        raise HTTPException(status_code=404, detail="Store not found")
    return {
        "success": True,
        "store_id": store_id,
        "store_name": tenant.get("store_name") or store_id,
        "active": bool(tenant.get("active", True)),
        "login_configured": merchant_has_password_login(store_id),
    }


@router.post("/{store_id}/merchant/setup")
def api_merchant_setup(store_id: str, body: MerchantSetupBody):
    """
    First-time merchant username/password from WordPress (no Tenant API key).
    Rejected once login already exists — platform admin can reset afterwards.
    """
    from src.services.tenant_auth import bootstrap_merchant_credentials

    ok, message = bootstrap_merchant_credentials(
        store_id,
        username=body.username,
        password=body.password,
        store_url=body.store_url or "",
    )
    if not ok:
        code = 409 if "already exists" in message.lower() else 400
        if "not found" in message.lower():
            code = 404
        raise HTTPException(status_code=code, detail=message)
    return {
        "success": True,
        "message": message,
        "store_id": store_id,
        "username": (body.username or "").strip(),
        "login_url": f"/app/{store_id}/login",
    }
