import uuid
import httpx
import html
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, create_session, register_tenant
from src.services.user_context import normalize_user_context, top_personalized_products
from src.services.catalog import fetch_products

router = APIRouter()


class SessionRequest(BaseModel):
    store_id: str
    user_context: Optional[Dict[str, Any]] = None


@router.post("/session")
async def create_new_session(body: SessionRequest):
    tenant = get_tenant(body.store_id)

    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )

    if "currency_symbol" not in tenant:
        try:
            platform = (tenant.get("platform") or "woocommerce").lower()
            if platform == "shopify":
                from src.services.shopify_service import fetch_shop_currency_symbol

                symbol = await fetch_shop_currency_symbol(tenant)
                if symbol:
                    tenant["currency_symbol"] = symbol
                    register_tenant(body.store_id, tenant)
            else:
                store_url = tenant.get("store_url", "")
                consumer_key = tenant.get("consumer_key", "")
                consumer_secret = tenant.get("consumer_secret", "")

                if store_url and consumer_key and consumer_secret:
                    async with httpx.AsyncClient() as client:
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
                            register_tenant(body.store_id, tenant)
        except Exception as e:
            print(f"Error fetching currency from REST API: {e}")

    user_context = normalize_user_context(body.user_context)
    session_id = str(uuid.uuid4())
    create_session(session_id, body.store_id, user_context)

    auth_state = user_context.get("auth_state", "guest")
    store_name = tenant.get("store_name", "My Store")
    if auth_state == "logged_in":
        greeting = (
            f"Hi! Welcome back to {store_name}. "
            "I can personalize picks from your account activity. What are you looking for today?"
        )
    elif user_context.get("source") not in (None, "none"):
        greeting = (
            f"Hi! I am your shopping assistant for {store_name}. "
            "I can suggest products based on what you've been browsing. What are you looking for today?"
        )
    else:
        greeting = (
            f"Hi! I am your shopping assistant for {store_name}. "
            "What are you looking for today?"
        )

    # Proactive personalized recommendations when preference signals exist
    recommendations = None
    try:
        products = await fetch_products(tenant)
        personalized = top_personalized_products(products, user_context, limit=3)
        if personalized:
            recommendations = {
                "type": "recommendations",
                "message": "Based on what you've been browsing, here are some picks for you:",
                "products": personalized,
            }
    except Exception as e:
        print(f"Proactive recommendations skipped: {e}")

    payload = {
        "success": True,
        "session_id": session_id,
        "store_name": store_name,
        "currency_symbol": tenant.get("currency_symbol", "₹"),
        "greeting": greeting,
        "auth_state": auth_state,
    }
    if recommendations:
        payload["recommendations"] = recommendations
    return payload
