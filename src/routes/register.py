import hmac
import re
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.services.store import register_tenant, get_tenant
from src.services.tenant_auth import ensure_tenant_api_key, normalize_store_host

router = APIRouter()


class RegisterRequest(BaseModel):
    store_url: str
    consumer_key: str
    consumer_secret: str
    store_name: str = "My Store"
    platform: str = "woocommerce"


class WooConnectRequest(BaseModel):
    """WordPress plugin auto-connect — creates/updates tenant without Admin UI."""

    store_url: str = Field(..., min_length=8)
    store_id: str = ""
    store_name: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""


def _slug_store_id(raw: str) -> str:
    text = (raw or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0]
    text = text.replace("www.", "")
    # Prefer first label for long hosts (mobilplaneten.wpenginepowered.com → mobilplaneten)
    if "." in text:
        first = text.split(".", 1)[0]
        if first and first not in ("www", "shop", "store"):
            text = first
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    return text[:64] or str(uuid.uuid4())[:8]


@router.post("/register")
def register_store(body: RegisterRequest):
    store_id = str(uuid.uuid4())
    platform = (body.platform or "woocommerce").strip().lower()
    if platform == "wordpress":
        platform = "woocommerce"

    register_tenant(
        store_id,
        {
            "platform": platform,
            "store_url": body.store_url.rstrip("/"),
            "consumer_key": body.consumer_key,
            "consumer_secret": body.consumer_secret,
            "store_name": body.store_name,
        },
    )

    return {
        "success": True,
        "store_id": store_id,
        "platform": platform,
        "embed_snippet": (
            f"<script src='/static/chatbot.js' data-store-id='{store_id}' "
            f"data-backend-url='' defer></script>"
        ),
    }


@router.post("/woocommerce/connect")
def woo_connect(body: WooConnectRequest):
    """
    Auto-register / refresh a WooCommerce tenant from the WordPress plugin.
    No Admin → Stores step required.
    """
    store_url = (body.store_url or "").strip().rstrip("/")
    if not store_url.startswith("http"):
        raise HTTPException(status_code=400, detail="store_url must be an absolute http(s) URL.")

    store_id = (body.store_id or "").strip() or _slug_store_id(store_url)
    store_id = _slug_store_id(store_id) if re.search(r"[^a-z0-9_-]", store_id.lower()) else store_id.lower()
    if len(store_id) < 2:
        raise HTTPException(status_code=400, detail="store_id is too short.")

    store_name = (body.store_name or "").strip() or store_id
    key = (body.consumer_key or "").strip()
    secret = (body.consumer_secret or "").strip()
    claim_host = normalize_store_host(store_url)

    existing = get_tenant(store_id, include_inactive=True, include_secrets=True)
    created = False

    if existing:
        existing_host = normalize_store_host(existing.get("store_url") or "")
        if (
            existing_host
            and claim_host
            and existing_host != claim_host
        ):
            # Prevent hijacking another site's store_id unless they know the WC secret.
            stored_secret = (existing.get("consumer_secret") or "").strip()
            if not secret or not stored_secret or not hmac.compare_digest(secret, stored_secret):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f'Store ID "{store_id}" is already used by another site. '
                        "Choose a different Store ID in plugin Settings."
                    ),
                )

        payload = {
            k: v
            for k, v in existing.items()
            if k not in ("store_id", "active", "created_at", "updated_at")
        }
        payload["platform"] = "woocommerce"
        payload["store_url"] = store_url
        payload["store_name"] = store_name or existing.get("store_name") or store_id
        if key:
            payload["consumer_key"] = key
        if secret:
            payload["consumer_secret"] = secret
        register_tenant(store_id, payload, active=bool(existing.get("active", True)))
    else:
        created = True
        payload = {
            "platform": "woocommerce",
            "store_url": store_url,
            "store_name": store_name,
        }
        if key:
            payload["consumer_key"] = key
        if secret:
            payload["consumer_secret"] = secret
        register_tenant(store_id, payload, active=True)

    try:
        ensure_tenant_api_key(store_id)
    except Exception:
        pass

    tenant = get_tenant(store_id, include_inactive=True) or {}
    return {
        "success": True,
        "created": created,
        "store_id": store_id,
        "store_name": tenant.get("store_name") or store_name,
        "store_url": tenant.get("store_url") or store_url,
        "platform": "woocommerce",
        "message": (
            "Store connected."
            if not created
            else "Store created and connected. You can create a merchant account next."
        ),
        "login_url": f"/app/{store_id}/login",
    }

