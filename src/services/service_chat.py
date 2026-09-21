"""Service / lead chatbot (LiveStoreFix-style) — not product shopping."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

SERVICE_PLATFORMS = frozenset(
    {"service", "lead", "support", "livestorefix", "agency"}
)

DEFAULT_CTAS: List[Dict[str, str]] = [
    {
        "label": "Book Free Rescue Session",
        "url": "https://livestorefix.com/",
        "style": "primary",
        "id": "free_rescue",
    },
    {
        "label": "Emergency Fix — $399",
        "url": "https://livestorefix.com/",
        "style": "danger",
        "id": "emergency",
    },
    {
        "label": "Long-Term Support",
        "url": "https://livestorefix.com/",
        "style": "secondary",
        "id": "long_term",
    },
    {
        "label": "Call (315) 715-8494",
        "url": "tel:+13157158494",
        "style": "secondary",
        "id": "phone",
    },
    {
        "label": "Email us",
        "url": "mailto:info@livestorefix.com",
        "style": "secondary",
        "id": "email",
    },
]

DEFAULT_QUICK_REPLIES = [
    "My store is down right now",
    "Checkout / payments broken",
    "Book a free rescue session",
    "Do you fix Shopify?",
    "Long-term support",
]

LIVESTOREFIX_KNOWLEDGE = """
LiveStoreFix (https://livestorefix.com/) — emergency Shopify, Magento/Adobe Commerce, and WooCommerce store support (EvinceDev).

OFFERS:
1) Free Rescue Session — starts with a free 20-minute qualification call. Ideal when the issue can be scheduled. Expert assigned in about 3 days. Includes live diagnosis/fix (~100 minutes) and a written fix summary within 3 days.
2) Emergency Support — $399 paid. Senior architect assigned within ~2 hours. No tickets/queue. For store-down / urgent revenue loss.
3) Long-Term Support — ongoing development, maintenance, SLA-backed response for growing businesses, franchises, and agencies (ERP/CRM/multi-location).

PLATFORMS: Shopify (incl. Plus), Magento 2 / Adobe Commerce, WooCommerce. Also automation, CRM sync, webhooks, integrations.

TYPICAL ISSUES: checkout/payment failures, plugin/app conflicts, slow admin/storefront, confirmation emails not sending, inventory/ERP sync, Magento indexing/cache, theme/layout breaks, shipping rates wrong, discount codes failing.

CONTACT: phone (315) 715-8494, email info@livestorefix.com.

PROCESS (free path): Qualification call → Live diagnosis & fix → Written summary (root cause, changes, what to watch).
""".strip()


def is_service_platform(platform: Optional[str]) -> bool:
    return (platform or "").strip().lower() in SERVICE_PLATFORMS


def service_ctas(tenant: Optional[dict] = None) -> List[Dict[str, str]]:
    tenant = tenant or {}
    raw = tenant.get("service_ctas")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return [
                    {
                        "label": str(a.get("label") or "Open"),
                        "url": str(a.get("url") or ""),
                        "style": str(a.get("style") or "primary"),
                        "id": str(a.get("id") or ""),
                    }
                    for a in parsed
                    if isinstance(a, dict) and a.get("url")
                ]
        except Exception:
            pass
    if isinstance(raw, list) and raw:
        return raw  # type: ignore[return-value]

    ctas = [dict(c) for c in DEFAULT_CTAS]
    booking = (tenant.get("booking_url") or tenant.get("free_rescue_url") or "").strip()
    emergency = (tenant.get("emergency_url") or "").strip()
    longterm = (tenant.get("longterm_url") or "").strip()
    phone = (tenant.get("contact_phone") or "").strip()
    email = (tenant.get("contact_email") or "").strip()
    site = (tenant.get("store_url") or "").rstrip("/")
    if site and not site.startswith("http"):
        site = f"https://{site}"

    for c in ctas:
        if c["id"] == "free_rescue" and booking:
            c["url"] = booking
        elif c["id"] == "emergency" and emergency:
            c["url"] = emergency
        elif c["id"] == "long_term" and longterm:
            c["url"] = longterm
        elif c["id"] == "phone" and phone:
            digits = re.sub(r"[^\d+]", "", phone)
            c["url"] = f"tel:{digits}" if digits else c["url"]
            c["label"] = f"Call {phone}"
        elif c["id"] == "email" and email:
            c["url"] = f"mailto:{email}"
            c["label"] = f"Email {email}"
        elif site and c["url"].startswith("https://livestorefix.com"):
            # Prefer tenant site homepage when custom CTAs not set
            if c["id"] in ("free_rescue", "emergency", "long_term"):
                c["url"] = site
    return ctas


def service_quick_replies(tenant: Optional[dict] = None) -> List[str]:
    tenant = tenant or {}
    raw = tenant.get("quick_replies")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return [str(x) for x in parsed if str(x).strip()]
        except Exception:
            pass
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw if str(x).strip()]
    return list(DEFAULT_QUICK_REPLIES)


def service_greeting(tenant: Optional[dict] = None) -> str:
    tenant = tenant or {}
    name = tenant.get("store_name") or "LiveStoreFix"
    return (
        f"Hi! Welcome to {name}. "
        "We fix broken Shopify, Magento, and WooCommerce stores — live. "
        "Is your store down right now, or can we schedule a free rescue session?"
    )


def build_service_system_prompt(
    tenant: Optional[dict] = None, training_block: str = ""
) -> str:
    tenant = tenant or {}
    store_name = tenant.get("store_name") or "LiveStoreFix"
    store_url = (tenant.get("store_url") or "https://livestorefix.com").rstrip("/")
    if store_url and not store_url.startswith("http"):
        store_url = f"https://{store_url}"
    ctas = service_ctas(tenant)
    ctas_text = json.dumps(ctas, indent=2)
    phone = tenant.get("contact_phone") or "(315) 715-8494"
    email = tenant.get("contact_email") or "info@livestorefix.com"

    training_section = ""
    if (training_block or "").strip():
        training_section = f"""

    EXTRA TRAINED KNOWLEDGE (prefer when relevant):
    {training_block.strip()}
    """

    return f"""You are the website assistant for {store_name} ({store_url}).
You help eCommerce merchants choose the right support path. You are NOT a product shopping bot.
Never recommend products, never invent product IDs, never talk about Add to cart or shop filters.

BUSINESS FACTS:
{LIVESTOREFIX_KNOWLEDGE}

Contact: {phone} · {email}

AVAILABLE CTA BUTTONS (use these exact urls/labels when suggesting next steps):
{ctas_text}
{training_section}

CONVERSATION GOALS:
1. Identify platform: Shopify, Magento / Adobe Commerce, or WooCommerce (ask if unclear).
2. Identify the issue in plain language (checkout, payments, speed, emails, sync, apps/plugins, etc.).
3. Gauge urgency:
   - Store down / losing sales NOW → Emergency $399
   - Can wait / wants free help → Free Rescue Session
   - Ongoing help → Long-Term Support
4. Answer FAQs briefly using the business facts.
5. Collect useful lead details when the visitor is ready: name, email, store URL, platform, short issue summary.
6. Always steer toward a clear next step (CTA buttons).

RESPONSE FORMAT:
- For normal chat / FAQ: plain conversational text (light Markdown OK: **bold**, bullets, numbered steps). Keep it short and calm.
- When you want the widget to show action buttons, respond ONLY with this JSON (nothing else):
{{
  "type": "actions",
  "message": "short helpful message",
  "actions": [
    {{ "id": "emergency", "label": "Emergency Fix — $399", "url": "https://...", "style": "danger" }}
  ],
  "lead": {{
    "platform": "shopify|magento|woocommerce|unknown",
    "urgency": "emergency|scheduled|long_term|unknown",
    "issue_summary": "one line",
    "store_url": "",
    "name": "",
    "email": ""
  }}
}}
- Prefer 1–3 actions max. Use urls from AVAILABLE CTA BUTTONS.
- If the visitor only needs information, plain text is fine (no JSON).
- Never pretend you already fixed their store — you qualify and route them to human experts.
- Reply in the same language the visitor uses.
"""


def parse_service_ai_response(raw: str, tenant: Optional[dict] = None) -> Dict[str, Any]:
    """Normalize AI output into widget-friendly response dict."""
    text = (raw or "").strip()
    if not text:
        return {
            "type": "question",
            "message": "How can I help with your store today?",
            "actions": service_ctas(tenant)[:3],
        }

    cleaned = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if "{" in cleaned and "}" in cleaned:
        try:
            start = cleaned.index("{")
            end = cleaned.rindex("}") + 1
            parsed = json.loads(cleaned[start:end])
            if isinstance(parsed, dict) and parsed.get("type") == "actions":
                actions = parsed.get("actions") or []
                if not isinstance(actions, list) or not actions:
                    actions = service_ctas(tenant)[:3]
                # Fill missing urls from defaults by id
                by_id = {c.get("id"): c for c in service_ctas(tenant) if c.get("id")}
                normalized = []
                for a in actions[:4]:
                    if not isinstance(a, dict):
                        continue
                    aid = str(a.get("id") or "")
                    base = by_id.get(aid) or {}
                    url = (a.get("url") or base.get("url") or "").strip()
                    label = (a.get("label") or base.get("label") or "Continue").strip()
                    if not url:
                        continue
                    normalized.append(
                        {
                            "id": aid,
                            "label": label,
                            "url": url,
                            "style": str(a.get("style") or base.get("style") or "primary"),
                        }
                    )
                if not normalized:
                    normalized = service_ctas(tenant)[:3]
                return {
                    "type": "actions",
                    "message": str(parsed.get("message") or text),
                    "actions": normalized,
                    "lead": parsed.get("lead")
                    if isinstance(parsed.get("lead"), dict)
                    else {},
                }
        except Exception:
            pass

    return {"type": "question", "message": text}
