import uuid
import httpx
import html
import traceback
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, create_session, register_tenant
from src.services.user_context import normalize_user_context

router = APIRouter()


class SessionRequest(BaseModel):
    store_id: str
    user_context: Optional[Dict[str, Any]] = None


@router.post("/session")
async def create_new_session(body: SessionRequest):
    store_id = (body.store_id or "").strip()
    if not store_id:
        raise HTTPException(status_code=400, detail="store_id is required.")

    try:
        tenant = get_tenant(store_id)
    except Exception as e:
        print(f"/api/session get_tenant error: {e}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="Could not load store from database. Check server disk/DB.",
        ) from e

    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )

    # Best-effort currency lookup — never fail session creation
    if "currency_symbol" not in tenant:
        try:
            platform = (tenant.get("platform") or "woocommerce").lower()
            if platform == "shopify":
                from src.services.shopify_service import fetch_shop_currency_symbol

                symbol = await fetch_shop_currency_symbol(tenant)
                if symbol:
                    tenant["currency_symbol"] = symbol
                    _safe_update_tenant(store_id, tenant)
            else:
                store_url = tenant.get("store_url", "")
                consumer_key = tenant.get("consumer_key", "")
                consumer_secret = tenant.get("consumer_secret", "")

                if store_url and consumer_key and consumer_secret:
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        res = await client.get(
                            f"{store_url}/wp-json/wc/v3/data/currencies/current",
                            params={
                                "consumer_key": consumer_key,
                                "consumer_secret": consumer_secret,
                            },
                            timeout=5.0,
                        )
                        if res.status_code == 200:
                            data = res.json()
                            symbol = data.get("symbol", "₹")
                            tenant["currency_symbol"] = html.unescape(symbol)
                            _safe_update_tenant(store_id, tenant)
        except Exception as e:
            print(f"Error fetching currency from REST API: {e}")

    try:
        user_context = normalize_user_context(body.user_context)
    except Exception as e:
        print(f"/api/session user_context error: {e}")
        user_context = {"auth_state": "guest", "preferences": {}, "source": "none"}

    session_id = str(uuid.uuid4())
    try:
        create_session(session_id, store_id, user_context)
    except Exception as e:
        print(f"/api/session create_session error: {e}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail="Could not create chat session. Server database may be unavailable.",
        ) from e

    store_name = tenant.get("store_name") or store_id
    auth_state = (user_context or {}).get("auth_state") or "guest"

    greeting = (
        f"Hi! I am your shopping assistant for {store_name}. "
        "What are you looking for today?"
    )
    if auth_state == "logged_in":
        greeting = (
            f"Hi! Welcome back to {store_name}. "
            "What are you looking for today?"
        )

    return {
        "success": True,
        "session_id": session_id,
        "store_name": store_name,
        "currency_symbol": tenant.get("currency_symbol", "₹"),
        "greeting": greeting,
        "auth_state": auth_state,
    }


def _safe_update_tenant(store_id: str, tenant: dict) -> None:
    """Persist non-secret tenant field updates without wiping credentials."""
    try:
        payload = {
            k: v
            for k, v in tenant.items()
            if k
            not in (
                "store_id",
                "active",
                "created_at",
                "updated_at",
            )
        }
        register_tenant(store_id, payload, active=bool(tenant.get("active", True)))
    except Exception as e:
        print(f"Warning: tenant update skipped: {e}")
