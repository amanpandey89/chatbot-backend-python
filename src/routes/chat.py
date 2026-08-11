import json
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.services.store import get_tenant, get_session, add_message, set_user_context
from src.services.catalog import fetch_products, lookup_order_status
from src.services.openai_service import get_recommendation
from src.services.user_context import merge_user_context
from src.services.woocommerce import WooConfigError, WooAuthError
from src.services.catalog_filter import (
    filter_products_for_query,
    enforce_recommendation_ids,
    parse_query_constraints,
)
from src.services.plp_nav import (
    wants_plp_navigation,
    build_plp_url,
    plp_message,
)


router = APIRouter()


class ChatRequest(BaseModel):
    store_id: str
    session_id: str
    message: str
    user_context: Optional[Dict[str, Any]] = None


def _config_http_detail(exc: Exception) -> Optional[str]:
    if isinstance(exc, WooConfigError):
        return str(exc)
    msg = str(exc)
    low = msg.lower()
    if "401" in low or "cannot_view" in low or "cannot list" in low:
        return (
            "WooCommerce API keys were rejected (cannot list products). "
            "In WooCommerce → Settings → Advanced → REST API, create a Read key "
            "(or Read/Write), paste the new key and secret into Merchant Dashboard → "
            "Settings → Store connection, and use the exact https store URL."
        )
    raw = msg.strip().strip("'\"")
    if raw in ("consumer_key", "consumer_secret", "store_url"):
        return (
            "Store configuration incomplete: WooCommerce API keys or store URL are missing. "
            "Open Merchant Dashboard → Settings → Store connection and save them."
        )
    return None


def _public_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "label": filters.get("label") or "",
        "models": filters.get("models") or [],
        "colors": filters.get("colors") or [],
        "storage": filters.get("storage") or [],
        "budget": filters.get("budget"),
        "category_slug": filters.get("category_slug") or "",
        "search_parts": filters.get("search_parts") or [],
        "match_count": filters.get("match_count"),
        "attr_filters": filters.get("attr_filters") or {},
    }


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
        products: list = []
        catalog_error = ""
        order_lookup = None
        try:
            products = await fetch_products(tenant)
        except WooConfigError as e:
            catalog_error = str(e)
            print(f"── Chat catalog unavailable: {e}")

        wants_products = _looks_like_product_request(body.message)
        store_url = (tenant.get("store_url") or "").rstrip("/")

        # ── PLP navigation (browse / filter intent) ───────────────────────
        if wants_plp_navigation(body.message) and store_url:
            plp_url, filters = build_plp_url(store_url, body.message, products)
            if plp_url:
                msg = plp_message(filters, navigating=True)
                add_message(body.session_id, "assistant", msg)
                sample = []
                if products:
                    matched, _ = filter_products_for_query(
                        products, body.message, limit=3
                    )
                    sample = matched
                return {
                    "success": True,
                    "response": {
                        "type": "navigate",
                        "message": msg,
                        "url": plp_url,
                        "auto_navigate": True,
                        "filters": _public_filters(filters),
                        "products": sample,
                    },
                }

        if wants_products and not products:
            msg = catalog_error or (
                "I could not load products from your WooCommerce store yet. "
                "Open Merchant Dashboard → Settings → Store connection, save a working "
                "Consumer Key/Secret for this store URL, then try again."
            )
            add_message(body.session_id, "assistant", msg)
            return {
                "success": True,
                "response": {"type": "question", "message": msg},
            }

        constraints = {}
        catalog_for_ai = products
        plp_url = ""
        plp_filters: Dict[str, Any] = {}
        if products and wants_products:
            catalog_for_ai, constraints = filter_products_for_query(
                products, body.message, limit=40
            )
            if store_url:
                plp_url, plp_filters = build_plp_url(
                    store_url, body.message, products
                )
            if not catalog_for_ai:
                constraints = parse_query_constraints(body.message)
                models = constraints.get("models") or []
                budget = constraints.get("budget")
                bits = []
                if models:
                    bits.append(" / ".join(models))
                if budget:
                    bits.append(f"under {int(budget)}")
                label = " ".join(bits) if bits else "that request"
                msg = (
                    f"I could not find in-stock products matching {label} in the live catalog. "
                    "Try another model, a wider budget, or ask me to show similar phones."
                )
                add_message(body.session_id, "assistant", msg)
                # Still offer PLP browse if we can build a URL
                if plp_url:
                    return {
                        "success": True,
                        "response": {
                            "type": "navigate",
                            "message": msg + " You can still browse the shop filters.",
                            "url": plp_url,
                            "auto_navigate": False,
                            "filters": _public_filters(plp_filters),
                            "products": [],
                        },
                    }
                return {
                    "success": True,
                    "response": {"type": "question", "message": msg},
                }

        fresh_session = get_session(body.session_id)
        if not fresh_session:
            raise HTTPException(status_code=404, detail="Session not found or expired.")

        try:
            order_lookup = await lookup_order_status(tenant, fresh_session["messages"])
        except WooConfigError as e:
            print(f"── Chat order lookup skipped: {e}")
            order_lookup = None

        ai_response = await get_recommendation(
            fresh_session, catalog_for_ai, tenant, order_lookup
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
                enriched = enforce_recommendation_ids(
                    catalog_for_ai,
                    parsed.get("products") or [],
                    constraints,
                )
                if not enriched and catalog_for_ai:
                    enriched = [
                        {
                            **p,
                            "reason": "Matches your request from the store catalog.",
                        }
                        for p in catalog_for_ai[:3]
                    ]

                if enriched:
                    resp = {
                        "type": "recommendations",
                        "message": parsed.get("message")
                        or "Based on your needs, here are my top picks:",
                        "products": enriched,
                    }
                    if plp_url:
                        resp["plp_url"] = plp_url
                        resp["filters"] = _public_filters(plp_filters)
                        resp["plp_message"] = plp_message(
                            plp_filters, navigating=False
                        )
                    return {"success": True, "response": resp}
                fallback = (
                    catalog_error
                    or "I could not match products from the live catalog. Please try again."
                )
                return {
                    "success": True,
                    "response": {"type": "question", "message": fallback},
                }

        except json.JSONDecodeError:
            pass

        return {
            "success": True,
            "response": {"type": "question", "message": ai_response},
        }

    except HTTPException:
        raise
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


def _looks_like_product_request(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    keys = (
        "recommend",
        "phone",
        "product",
        "buy",
        "best",
        "under",
        "accessory",
        "accessories",
        "iphone",
        "samsung",
        "find",
        "suggest",
        "looking for",
        "show me",
        "show",
        "browse",
        "filter",
        "visa",
        "filtrera",
        "gb",
        "black",
        "white",
    )
    return any(k in text for k in keys)
