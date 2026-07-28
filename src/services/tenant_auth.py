"""Tenant (merchant) session auth for dashboard + training API."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

from src.services.store import get_tenant, register_tenant

COOKIE_NAME = "asa_tenant_session"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "") or hashlib.sha256(
    f"{os.getenv('ADMIN_USERNAME', 'admin')}:{os.getenv('ADMIN_PASSWORD', 'change-me')}".encode()
).hexdigest()


def _sign(store_id: str) -> str:
    sig = hmac.new(ADMIN_SECRET.encode(), store_id.encode(), hashlib.sha256).hexdigest()
    return f"{store_id}.{sig}"


def _verify_token(token: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    store_id, sig = token.rsplit(".", 1)
    expected = hmac.new(
        ADMIN_SECRET.encode(), store_id.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    tenant = get_tenant(store_id, include_inactive=False)
    if not tenant:
        return None
    return store_id


def create_tenant_session_token(store_id: str) -> str:
    return _sign(store_id)


def ensure_tenant_api_key(store_id: str) -> str:
    """Stable API key for WP plugin / integrations (stored on tenant)."""
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        raise ValueError("Store not found")
    key = tenant.get("tenant_api_key")
    if key:
        return key
    key = secrets.token_urlsafe(24)
    payload = {k: v for k, v in tenant.items() if k not in ("store_id", "active", "created_at", "updated_at")}
    payload["tenant_api_key"] = key
    register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    return key


def verify_tenant_api_key(store_id: str, api_key: str) -> bool:
    if not store_id or not api_key:
        return False
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True)
    if not tenant:
        return False
    stored = tenant.get("tenant_api_key") or ""
    if not stored:
        # Lazy-create then fail this request (client must refresh key from dashboard)
        return False
    return hmac.compare_digest(stored, api_key)


def get_store_id_from_request(request: Request) -> Optional[str]:
    # 1) Cookie session (Shopify dashboard)
    cookie = request.cookies.get(COOKIE_NAME)
    store_id = _verify_token(cookie or "")
    if store_id:
        return store_id

    # 2) API headers (WordPress / integrations)
    header_store = request.headers.get("X-Store-Id") or request.query_params.get("store_id")
    header_key = request.headers.get("X-Tenant-Key") or request.headers.get("X-API-Key")
    if header_store and header_key and verify_tenant_api_key(header_store, header_key):
        return header_store

    return None


def require_tenant(request: Request, expected_store_id: Optional[str] = None) -> Optional[str]:
    store_id = get_store_id_from_request(request)
    if not store_id:
        return None
    if expected_store_id and store_id != expected_store_id:
        return None
    return store_id


def set_tenant_cookie(response, store_id: str):
    response.set_cookie(
        COOKIE_NAME,
        create_tenant_session_token(store_id),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 12,
    )
    return response


def clear_tenant_cookie(response):
    response.delete_cookie(COOKIE_NAME)
    return response


def tenant_login_redirect(store_id: str = ""):
    if store_id:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    return RedirectResponse(url="/app/login", status_code=303)
