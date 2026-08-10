import json
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, get_session, add_message, set_user_context
from src.services.catalog import fetch_products, lookup_order_status
from src.services.openai_service import get_recommendation
from src.services.user_context import merge_user_context
from src.services.woocommerce import WooConfigError

router = APIRouter()


class ChatRequest(BaseModel):
    store_id: str
    session_id: str
    message: str
    user_context: Optional[Dict[str, Any]] = None


def _config_http_detail(exc: Exception) -> Optional[str]:
    if isinstance(exc, WooConfigError):
        return str(exc)
    raw = str(exc).strip().strip("'\"")
    if raw in ("consumer_key", "consumer_secret", "store_url"):
        return (
            "Store configuration incomplete: WooCommerce API keys or store URL are missing. "
            "Open Merchant Dashboard → Settings → Store connection and save them."
        )
    return None


@router.post("/chat")
async def chat(body: ChatRequest):

    tenant = get_tenant(body.store_id)
    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )
    tenant = {**tenant, "store_id": body.store_id}

    session = get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    if body.user_context:
        merged = merge_user_context(session.get("user_context"), body.user_context)
        set_user_context(body.session_id, merged)

    add_message(body.session_id, "user", body.message)

    try:
        products = await fetch_products(tenant)

        fresh_session = get_session(body.session_id)
        if not fresh_session:
            raise HTTPException(status_code=404, detail="Session not found or expired.")

        order_lookup = await lookup_order_status(tenant, fresh_session["messages"])

        ai_response = await get_recommendation(
            fresh_session, products, tenant, order_lookup
        )

        add_message(body.session_id, "assistant", ai_response)

        cleaned = ai_response.strip()
        cleaned = cleaned.removeprefix("```json").removeprefix("```")
        cleaned = cleaned.removesuffix("```").strip()

        if "{" in cleaned and "}" in cleaned:
            start = cleaned.index("{")
            end = cleaned.rindex("}") + 1
            cleaned = cleaned[start:end]

        try:
            parsed = json.loads(cleaned)
            if parsed.get("type") == "recommendations":
                enriched = []
                for item in parsed["products"]:
                    product = next((p for p in products if p["id"] == item["id"]), None)
                    if product:
                        enriched.append({**product, "reason": item["reason"]})

                return {
                    "success": True,
                    "response": {
                        "type": "recommendations",
                        "message": parsed["message"],
                        "products": enriched,
                    },
                }

        except json.JSONDecodeError:
            pass

        return {
            "success": True,
            "response": {"type": "question", "message": ai_response},
        }

    except HTTPException:
        raise
    except WooConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        detail = _config_http_detail(e)
        if detail:
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    except Exception as e:
        import traceback

        traceback.print_exc()
        detail = _config_http_detail(e)
        if detail:
            raise HTTPException(status_code=400, detail=detail)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
