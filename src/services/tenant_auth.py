"""Tenant (merchant) session auth for dashboard + training API."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional
from urllib.parse import quote

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


def _use_secure_cookies(request: Optional[Request] = None) -> bool:
    app_url = (os.getenv("APP_URL") or "").strip().lower()
    if app_url.startswith("https://"):
        return True
    if request is not None:
        proto = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or ""
        ).lower()
        if proto == "https":
            return True
    return False


def ensure_tenant_api_key(store_id: str) -> str:
    """Stable API key for WP plugin / integrations (stored on tenant)."""
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        raise ValueError("Store not found")
    key = tenant.get("tenant_api_key")
    if key:
        return key
    key = secrets.token_urlsafe(24)
    payload = {
        k: v
        for k, v in tenant.items()
        if k not in ("store_id", "active", "created_at", "updated_at")
    }
    payload["tenant_api_key"] = key
    register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    return key


def set_tenant_openai_api_key(store_id: str, openai_api_key: str) -> bool:
    """Save merchant OpenAI key on the tenant (empty string clears it)."""
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        return False
    payload = {
        k: v
        for k, v in tenant.items()
        if k not in ("store_id", "active", "created_at", "updated_at")
    }
    key = (openai_api_key or "").strip()
    if key:
        payload["openai_api_key"] = key
    else:
        payload.pop("openai_api_key", None)
    register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    return True


def tenant_has_openai_key(store_id: str) -> bool:
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True) or {}
    return bool((tenant.get("openai_api_key") or "").strip())


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"{salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    if not password or not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return hmac.compare_digest(check, digest)


def set_merchant_credentials(
    store_id: str, *, username: str, password: str = ""
) -> bool:
    """Set merchant dashboard username/password. Empty password keeps existing hash."""
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        return False
    username = (username or "").strip()
    if not username:
        return False
    payload = {
        k: v
        for k, v in tenant.items()
        if k not in ("store_id", "active", "created_at", "updated_at")
    }
    payload["merchant_username"] = username
    pwd = (password or "").strip()
    if pwd:
        payload["merchant_password_hash"] = _hash_password(pwd)
    register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    return True


def verify_merchant_password(store_id: str, username: str, password: str) -> bool:
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True)
    if not tenant:
        return False
    stored_user = (tenant.get("merchant_username") or "").strip()
    stored_hash = tenant.get("merchant_password_hash") or ""
    if not stored_user or not stored_hash:
        return False
    if not hmac.compare_digest(stored_user.lower(), (username or "").strip().lower()):
        return False
    return _verify_password(password or "", stored_hash)


def merchant_has_password_login(store_id: str) -> bool:
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True) or {}
    return bool(
        (tenant.get("merchant_username") or "").strip()
        and (tenant.get("merchant_password_hash") or "").strip()
    )


def normalize_store_host(url: str) -> str:
    """Compare store ownership by host (ignore scheme / www / path)."""
    from urllib.parse import urlparse

    raw = (url or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path.split("/")[0] or "").strip()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip(".")


def bootstrap_merchant_credentials(
    store_id: str,
    *,
    username: str,
    password: str,
    store_url: str = "",
) -> tuple[bool, str]:
    """
    First-time merchant username/password from WordPress (no Tenant API key).
    Only allowed when login is not already configured.
    """
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        return False, f'Store ID "{store_id}" was not found on the backend.'
    if not tenant.get("active", True):
        return False, f'Store "{store_id}" is disabled.'
    if merchant_has_password_login(store_id):
        return (
            False,
            "Merchant login already exists. Use your username/password, "
            "or ask the platform admin to reset it.",
        )

    user = (username or "").strip()
    pwd = (password or "").strip()
    if len(user) < 3:
        return False, "Username must be at least 3 characters."
    if len(pwd) < 8:
        return False, "Password must be at least 8 characters."

    claimed = normalize_store_host(store_url)
    # First-time only; once login exists this endpoint rejects further changes.
    if claimed and not normalize_store_host(tenant.get("store_url") or ""):
        payload = {
            k: v
            for k, v in tenant.items()
            if k not in ("store_id", "active", "created_at", "updated_at")
        }
        payload["store_url"] = (store_url or "").strip().rstrip("/")
        register_tenant(store_id, payload, active=bool(tenant.get("active", True)))

    if not set_merchant_credentials(store_id, username=user, password=pwd):
        return False, "Could not save merchant login."
    return True, "Merchant login created. You can open the dashboard now."


def verify_tenant_api_key(store_id: str, api_key: str) -> bool:
    if not store_id or not api_key:
        return False
    tenant = get_tenant(store_id, include_inactive=False, include_secrets=True)
    if not tenant:
        return False
    candidates = [
        tenant.get("tenant_api_key") or "",
        tenant.get("consumer_secret") or "",
        tenant.get("access_token") or "",
    ]
    for stored in candidates:
        if stored and hmac.compare_digest(str(stored), str(api_key)):
            return True
    return False


def tenant_auth_error(store_id: str, api_key: str) -> str:
    """Human-readable auth failure for API clients (WordPress)."""
    tenant = get_tenant(store_id, include_inactive=True, include_secrets=True)
    if not tenant:
        return (
            f'Store ID "{store_id}" was not found on the backend. '
            "Use the exact Store ID from Admin → Stores."
        )
    if not tenant.get("active", True):
        return f'Store "{store_id}" is disabled on the backend.'
    if not api_key:
        return "Tenant API key is missing."
    if verify_tenant_api_key(store_id, api_key):
        return ""
    return (
        "Invalid Tenant API key. Copy a fresh key from Admin → store detail "
        f"(or /app/{store_id}/settings), or use the store Consumer Secret / Shopify access token."
    )


def get_store_id_from_request(request: Request) -> Optional[str]:
    # 1) Query session token (required for Shopify admin iframe / third-party cookies)
    q_token = (
        request.query_params.get("asa_session")
        or request.query_params.get("t")
        or ""
    ).strip()
    if q_token:
        store_id = _verify_token(q_token)
        if store_id:
            return store_id

    # 2) Cookie session
    cookie = request.cookies.get(COOKIE_NAME)
    store_id = _verify_token(cookie or "")
    if store_id:
        return store_id

    # 3) API headers (WordPress / integrations)
    header_store = request.headers.get("X-Store-Id") or request.query_params.get(
        "store_id"
    )
    header_key = request.headers.get("X-Tenant-Key") or request.headers.get("X-API-Key")
    if header_store and header_key and verify_tenant_api_key(header_store, header_key):
        return header_store

    return None


def require_tenant(
    request: Request, expected_store_id: Optional[str] = None
) -> Optional[str]:
    store_id = get_store_id_from_request(request)
    if not store_id:
        return None
    if expected_store_id and store_id != expected_store_id:
        return None
    return store_id


def set_tenant_cookie(response, store_id: str, request: Optional[Request] = None):
    """
    Shopify admin embeds the app in a cross-site iframe.
    SameSite=Lax cookies are dropped there — use None+Secure on HTTPS.
    """
    secure = _use_secure_cookies(request)
    response.set_cookie(
        COOKIE_NAME,
        create_tenant_session_token(store_id),
        httponly=True,
        secure=secure,
        samesite="none" if secure else "lax",
        max_age=60 * 60 * 12,
        path="/",
    )
    return response


def clear_tenant_cookie(response, request: Optional[Request] = None):
    secure = _use_secure_cookies(request)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=secure,
        samesite="none" if secure else "lax",
    )
    return response


def app_url_with_session(store_id: str, path: str = "") -> str:
    """Dashboard URL that works inside Shopify iframe (session in query)."""
    token = create_tenant_session_token(store_id)
    base = f"/app/{store_id}"
    if path:
        path = path.lstrip("/")
        base = f"{base}/{path}"
    return f"{base}?asa_session={quote(token, safe='')}"


def tenant_login_redirect(store_id: str = ""):
    if store_id:
        return RedirectResponse(url=f"/app/{store_id}/login", status_code=303)
    return RedirectResponse(url="/app/login", status_code=303)
