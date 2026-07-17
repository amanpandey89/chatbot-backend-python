import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, get_session, add_message
from src.services.woocommerce import fetch_products, lookup_order_status
from src.services.openai_service import get_recommendation

router = APIRouter()


class ChatRequest(BaseModel):
    store_id: str
    session_id: str
    message: str


@router.post("/chat")
async def chat(body: ChatRequest):

    tenant = get_tenant(body.store_id)
    if not tenant:
        raise HTTPException(
            status_code=404, detail="Store not found. Please register first."
        )

    session = get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    add_message(body.session_id, "user", body.message)

    try:
        # Fetch all products from WooCommerce
        products = await fetch_products(tenant)

        fresh_session = get_session(body.session_id)
        if not fresh_session:
            raise HTTPException(status_code=404, detail="Session not found or expired.")

        # Look up live order status when the user shared an order number
        order_lookup = await lookup_order_status(tenant, fresh_session["messages"])

        # ── Send products (+ optional order status) to OpenAI ─────────────
        ai_response = await get_recommendation(
            fresh_session, products, tenant, order_lookup
        )

        add_message(body.session_id, "assistant", ai_response)

        # ── Extract JSON from response ────────────────────────────────────
        # Handle cases where AI embeds JSON inside text
        cleaned = ai_response.strip()

        # Remove markdown code blocks if present
        cleaned = cleaned.removeprefix("```json").removeprefix("```")
        cleaned = cleaned.removesuffix("```").strip()

        # If response contains JSON embedded in text — extract it
        # Find the first { and last } and take everything between
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

    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
