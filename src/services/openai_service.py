# src/services/openai_service.py
import json
import os
from typing import cast
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from src.services.store import Session

# Initialize client — reads OPENAI_API_KEY from .env automatically
client = AsyncOpenAI()


def build_product_summary(products: list, answers: dict) -> str:
    """
    Sort products by relevance to user answers before sending to AI.
    This ensures the most relevant products are in the top 25.
    """

    # Extract budget number from answers if available
    # e.g. "under 50" → 50.0
    budget = None
    budget_text = answers.get("budget", "")
    if budget_text:
        import re

        numbers = re.findall(r"[\d.]+", budget_text)
        if numbers:
            budget = float(numbers[0])

    # Sort products — budget matches first
    def relevance_score(p):
        try:
            price = float(p.get("price") or p.get("regular_price") or 0)
        except (ValueError, TypeError):
            price = 0

        if budget and price > 0:
            # closer to budget = higher score
            diff = abs(price - budget)
            return diff
        return 999999  # no budget — no sorting

    sorted_products = sorted(products, key=relevance_score)

    lines = []
    for p in sorted_products:  # send top 40 instead of 25
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


def build_system_prompt(products: list, answers: dict) -> str:
    product_summary = build_product_summary(products, answers)

    answers_text = json.dumps(answers, indent=2)

    system_prompt = f"""You are an expert product recommendation assistant with deep knowledge of consumer electronics, phones, laptops, and devices.

    AVAILABLE PRODUCTS IN STORE:
    {product_summary}

    ANSWERS COLLECTED FROM USER SO FAR:
    {answers_text}

    YOUR BEHAVIOUR:
    1. Use your own knowledge about each product — specs, features, pros, cons, typical use cases — based on the product name and model. Do not rely on the product description provided.
    2. If you recognise a product name or model (e.g. iPhone 13, Samsung Galaxy S21) use your training knowledge to explain why it fits the user.
    3. Be flexible — not every question needs budget, purpose, and preference before recommending:
    - "best phones" or "top phones" → recommend immediately from your knowledge, no questions needed
    - "best phone under £300" → recommend immediately, budget already given
    - "Samsung phones" → recommend all Samsung options immediately
    - "cheap phones" → recommend lowest priced options immediately
    - "I need a phone for gaming" → recommend immediately based on use case
    - Only ask clarifying questions when the request is genuinely vague and you cannot make a good recommendation without more info
    4. When you have enough context to recommend — do it immediately. Never ask unnecessary questions.
    5. When giving recommendations respond ONLY with this exact JSON format and nothing else:
    {{
    "type": "recommendations",
    "message": "Based on your needs, here are my top picks:",
    "products": [
        {{ "id": 123, "reason": "one sentence using your knowledge of this product and why it fits" }},
        {{ "id": 456, "reason": "one sentence using your knowledge of this product and why it fits" }},
        {{ "id": 789, "reason": "one sentence using your knowledge of this product and why it fits" }}
    ]
    }}
    6. ALWAYS recommend exactly 3 products. Never fewer.
    7. If fewer than 3 products match perfectly, pick the closest alternatives — always return 3.
    8. Rank by best fit first — consider budget, use case, and preference if given, otherwise rank by overall value.
    9. When asking a question respond with plain conversational text only — no JSON.
    10. Keep all responses short, warm, and helpful.
    11. Only recommend products that exist in the AVAILABLE PRODUCTS list above — never invent product IDs.
    12. Use product IDs exactly as shown — do not make up IDs.
    13. CRITICAL: Your entire response when recommending must be ONLY the JSON object. Start with {{ and end with }}. Nothing before or after."""

    return system_prompt


async def get_recommendation(session: Session, products: list) -> str:

    system_prompt = build_system_prompt(products, session["answers"])

    messages = cast(
        list[ChatCompletionMessageParam],
        [{"role": "system", "content": system_prompt}, *session["messages"]],
    )

    response = await client.chat.completions.create(
        model="gpt-4o-mini", max_tokens=1024, temperature=0.7, messages=messages
    )

    return response.choices[0].message.content or ""
