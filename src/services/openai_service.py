# src/services/openai_service.py
import json
import os
import re
from typing import Dict, Optional, cast
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from src.services.store import Session, get_tenant
from src.services.user_context import preference_score

_clients: Dict[str, AsyncOpenAI] = {}


def resolve_openai_api_key(
    tenant: Optional[dict] = None, store_id: Optional[str] = None
) -> str:
    """Prefer per-tenant OpenAI key; fall back to platform .env only if unset."""
    sid = store_id or (tenant or {}).get("store_id")
    raw = ""
    if tenant:
        raw = (tenant.get("openai_api_key") or "").strip()
        # Ignore masked values from include_secrets=False
        if raw.endswith("...") or raw == "***":
            raw = ""
    if not raw and sid:
        full = get_tenant(sid, include_inactive=True, include_secrets=True) or {}
        raw = (full.get("openai_api_key") or "").strip()
    if not raw:
        raw = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not raw:
        raise RuntimeError(
            "No OpenAI API key for this store. Add the merchant's OpenAI key "
            "in Admin → store detail, merchant Settings, or WordPress AI Assistant settings."
        )
    return raw


def get_openai_client(
    api_key: Optional[str] = None,
    tenant: Optional[dict] = None,
    store_id: Optional[str] = None,
) -> AsyncOpenAI:
    key = api_key or resolve_openai_api_key(tenant=tenant, store_id=store_id)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=key)
        _clients[key] = client
    return client


def build_product_summary(
    products: list, answers: dict, user_context: Optional[dict] = None
) -> str:
    """
    Sort products by preference + budget relevance before sending to AI.
    """

    budget = None
    budget_text = (answers or {}).get("budget", "") or ""
    prefs = (user_context or {}).get("preferences") or {}
    if not budget_text:
        budget_text = prefs.get("budget_hint") or ""

    if budget_text:
        numbers = re.findall(r"[\d.]+", budget_text)
        if numbers:
            budget = float(numbers[0])

    def relevance_score(p):
        score = preference_score(p, user_context)

        try:
            price = float(p.get("price") or p.get("regular_price") or 0)
        except (ValueError, TypeError):
            price = 0

        if budget and price > 0:
            score += abs(price - budget) / max(budget, 1.0)

        return score

    sorted_products = sorted(products, key=relevance_score)

    lines = []
    for p in sorted_products:
        categories = ", ".join(p["categories"])
        tags = ", ".join(p["tags"])
        attributes = json.dumps(p["attributes"])
        price = p.get("price") or p.get("regular_price") or "price not set"
        currency = p.get("currency", "")

        line = (
            f"ID:{p['id']} | "
            f"Name:{p['name']} | "
            f"Price:{currency}{price} | "
            f"Categories:{categories} | "
            f"Tags:{tags} | "
            f"Attrs:{attributes}"
        )
        lines.append(line)

    return "\n".join(lines)


def build_system_prompt(
    products: list,
    answers: dict,
    tenant: Optional[dict] = None,
    order_lookup: Optional[dict] = None,
    user_context: Optional[dict] = None,
    training_block: str = "",
) -> str:
    product_summary = build_product_summary(products, answers, user_context)

    answers_text = json.dumps(answers, indent=2)
    user_context_text = json.dumps(user_context or {}, indent=2)
    tenant = tenant or {}
    store_name = tenant.get("store_name", "our store")
    store_url = (tenant.get("store_url") or "").rstrip("/")
    platform = (tenant.get("platform") or "woocommerce").lower()
    if platform == "shopify" and store_url and not store_url.startswith("http"):
        orders_url = f"https://{store_url}/account"
    elif store_url:
        orders_url = f"{store_url}/my-account/orders/"
    else:
        orders_url = "your account orders page"

    if order_lookup:
        order_lookup_text = json.dumps(order_lookup, indent=2)
    else:
        order_lookup_text = (
            "No order looked up yet (customer has not provided an order number)."
        )

    auth_state = (user_context or {}).get("auth_state", "guest")
    personalization_note = (
        "Customer is LOGGED IN — prefer account / purchase history signals."
        if auth_state == "logged_in"
        else "Customer is a GUEST — prefer cookie / browsing preference signals."
    )

    training_section = ""
    if (training_block or "").strip():
        training_section = f"""

    MERCHANT AI TRAINING (highest priority for store policies, FAQs, tone, and rules):
    {training_block.strip()}
    - Prefer these trained facts over generic assumptions.
    - For FAQs/policies, answer from this training when relevant.
    - Keep the brand tone if provided.
    - When RETRIEVED STORE KNOWLEDGE is present, treat it as the latest indexed store content.
"""

    system_prompt = f"""You are the official shopping assistant for {store_name}, an online ecommerce store.
    You help customers with product recommendations AND store support (orders, tracking, returns, accessories).

    STORE DETAILS:
    - Store name: {store_name}
    - Store URL: {store_url or "not available"}
    - Orders / tracking page: {orders_url}
{training_section}
    AVAILABLE PRODUCTS IN STORE:
    {product_summary}

    ANSWERS COLLECTED FROM USER SO FAR:
    {answers_text}

    USER PREFERENCE PROFILE (from WordPress plugin — trust this for personalization):
    {user_context_text}
    PERSONALIZATION MODE: {personalization_note}

    LIVE ORDER LOOKUP RESULT (from store order API — trust this as source of truth):
    {order_lookup_text}

    YOUR BEHAVIOUR:

    A) ECOMMERCE SUPPORT (orders, tracking, returns, account help):
    1. NEVER say you cannot help with orders, tracking, returns, shipping, or payments. You ARE this store's assistant.
    2. "Track my order" / order status:
       - If LIVE ORDER LOOKUP RESULT has found=true: tell the customer their REAL order status in chat.
         Include: order number, status_label (e.g. Processing), total with currency, date, and item names.
         Do NOT invent tracking numbers or carrier details that are not in the lookup result.
         Do NOT only say "visit the orders page" — you must state the status in this chat.
         You may optionally also share {orders_url} as an extra link.
       - If found=false with error not_found: say that order number was not found and ask them to double-check it.
       - If found=false with error email_mismatch: ask them to confirm the email used at checkout.
       - If found=false with error lookup_failed: apologise briefly and ask them to try again shortly, and share {orders_url}.
       - If no order has been looked up yet: warmly confirm you can help and ask for order number and checkout email.
    3. "I want to return an item" / refunds:
       - Confirm you can help with returns.
       - Ask for order number and which item they want to return.
       - If LIVE ORDER LOOKUP RESULT has found=true, acknowledge that order and its items while explaining the return steps.
       - Use MERCHANT AI TRAINING return/shipping policies when present; otherwise briefly explain a typical return flow.
    4. Shipping / delivery questions: give clear, helpful guidance; use LIVE ORDER LOOKUP RESULT and training when available.
    5. Keep support replies as plain conversational text only — no JSON.

    B) PRODUCT RECOMMENDATIONS:
    1. Match the customer's latest request STRICTLY:
       - If they ask for a specific model (e.g. "iPhone 12"), ONLY recommend products whose name includes that model.
       - If none match, say so in plain text and offer the closest in-stock alternatives — do NOT pretend they are the requested model.
       - If they ask for a phone, do NOT recommend accessories (cases, glass, chargers).
       - If they ask for accessories, do NOT recommend phones.
       - If they give a budget ("under 80000"), only recommend products with Price <= that budget.
    2. Reply in the same language the customer used (Swedish or English).
    3. Use USER PREFERENCE PROFILE when recommending:
       - Guest (cookies): lean on viewed_categories, viewed_product_ids, cart_product_ids, search_terms, brand_affinity.
       - Logged in (account): lean on last_order_categories and account signals first, then browsing cookies.
       - Mention personalization lightly in the reason when relevant.
       - Never invent preferences that are not in the profile.
    4. Use product names/models from AVAILABLE PRODUCTS — do not invent specs that contradict the listing.
    5. Be flexible on clarifying questions — recommend when the request is clear enough.
    6. When giving recommendations respond ONLY with this exact JSON format and nothing else:
    {{
    "type": "recommendations",
    "message": "short message that reflects the request (e.g. iPhone 12 options):",
    "products": [
        {{ "id": 123, "reason": "one sentence why this exact product fits" }},
        {{ "id": 456, "reason": "one sentence why this exact product fits" }},
        {{ "id": 789, "reason": "one sentence why this exact product fits" }}
    ]
    }}
    7. Recommend up to 3 products. If fewer than 3 match the request, return only the matches (1 or 2 is OK).
    8. Never pad with unrelated models (e.g. do not return iPhone 15 when the user asked for iPhone 12).
    9. Rank by best fit first — model match, then budget, then preference profile.
    10. Only recommend products that exist in the AVAILABLE PRODUCTS list above — never invent product IDs.
    11. Use product IDs exactly as shown — do not make up IDs.
    12. CRITICAL: Your entire response when recommending must be ONLY the JSON object. Start with {{ and end with }}. Nothing before or after.
    13. If AVAILABLE PRODUCTS is empty or none fit, respond with plain text (no JSON) explaining that and ask a short clarifying question.

    GENERAL:
    - Keep all responses short, warm, and helpful.
    - When asking a question or giving support help, respond with plain conversational text only — no JSON.
    - Prefer helping inside this chat first; share the orders page link as a useful option, not a brush-off.
    - FORMATTING (important for chat readability):
      - Use real line breaks between sentences/sections.
      - You may use light Markdown: **bold**, bullet lists with "- ", and numbered lists.
      - Put each numbered step on its own line, like:
        1. First step
        2. Second step
        3. Third step
      - For product overviews, use short bullets instead of one long paragraph.
      - For order status, prefer a short structured layout, e.g.:
        I found your order #119!

        Status: Processing
        Item: Brand Buttons
        Total: ₹9.99

        Let me know if you need anything else.
      - Do not write multi-step instructions as one long paragraph."""

    return system_prompt


async def get_recommendation(
    session: Session,
    products: list,
    tenant: Optional[dict] = None,
    order_lookup: Optional[dict] = None,
) -> str:

    training_block = ""
    store_id = (tenant or {}).get("store_id") or session.get("store_id")
    if store_id:
        try:
            from src.services.training import build_training_prompt_block
            from src.knowledge.retrieve import search, format_retrieval_block

            training_block = build_training_prompt_block(store_id)

            # RAG: retrieve top chunks for the latest user message
            user_msgs = [
                m.get("content") or ""
                for m in (session.get("messages") or [])
                if m.get("role") == "user"
            ]
            query = (user_msgs[-1] if user_msgs else "").strip()
            if query:
                hits = await search(store_id, query, api_key=resolve_openai_api_key(tenant, store_id))
                rag_block = format_retrieval_block(hits)
                if rag_block:
                    training_block = (
                        f"{training_block}\n\n{rag_block}".strip()
                        if training_block
                        else rag_block
                    )
        except Exception as e:
            print(f"Training/RAG prompt skipped: {e}")

    system_prompt = build_system_prompt(
        products,
        session.get("answers") or {},
        tenant,
        order_lookup,
        session.get("user_context") or {},
        training_block=training_block,
    )

    messages = cast(
        list[ChatCompletionMessageParam],
        [{"role": "system", "content": system_prompt}, *session["messages"]],
    )

    client = get_openai_client(tenant=tenant, store_id=store_id)
    response = await client.chat.completions.create(
        model="gpt-4o-mini", max_tokens=1024, temperature=0.7, messages=messages
    )

    return response.choices[0].message.content or ""
