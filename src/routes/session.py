import uuid
import httpx
import html
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, create_session, register_tenant

router = APIRouter()


class SessionRequest(BaseModel):
    store_id: str


@router.post("/session")
async def create_new_session(body: SessionRequest):
    tenant = get_tenant(body.store_id)

    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )

    if "currency_symbol" not in tenant:
        try:
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

    session_id = str(uuid.uuid4())
    create_session(session_id, body.store_id)

    return {
        "success": True,
        "session_id": session_id,
        "store_name": tenant.get("store_name", "My Store"),
        "currency_symbol": tenant.get("currency_symbol", "₹"),
        "greeting": f"Hi! I am your shopping assistant for {tenant.get('store_name', 'My Store')}. What are you looking for today?",
    }
