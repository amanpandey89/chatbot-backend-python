"""Normalize visitor preference context from WordPress (and future platforms)."""

from typing import Any, Dict, List, Optional


def _as_str_list(value: Any, limit: int = 20) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _as_id_list(value: Any, limit: int = 20) -> List[int]:
    if not value:
        return []
    if not isinstance(value, list):
        value = [value]
    out: List[int] = []
    for item in value:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if num not in out:
            out.append(num)
        if len(out) >= limit:
            break
    return out


def normalize_user_context(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not raw or not isinstance(raw, dict):
        return {
            "auth_state": "guest",
            "visitor_id": None,
            "customer_id": None,
            "preferences": {},
            "source": "none",
        }

    prefs_in = raw.get("preferences") or {}
    if not isinstance(prefs_in, dict):
        prefs_in = {}

    auth_state = raw.get("auth_state") or "guest"
    if auth_state not in ("guest", "logged_in"):
        auth_state = "guest"

    source = raw.get("source") or ("account" if auth_state == "logged_in" else "cookies")
    if source not in ("cookies", "account", "merged", "none"):
        source = "cookies"

    preferences = {
        "viewed_product_ids": _as_id_list(prefs_in.get("viewed_product_ids")),
        "viewed_categories": _as_str_list(prefs_in.get("viewed_categories")),
        "cart_product_ids": _as_id_list(prefs_in.get("cart_product_ids")),
        "search_terms": _as_str_list(prefs_in.get("search_terms"), limit=10),
        "budget_hint": str(prefs_in.get("budget_hint") or "").strip()[:80],
        "brand_affinity": _as_str_list(prefs_in.get("brand_affinity"), limit=10),
        "last_order_categories": _as_str_list(prefs_in.get("last_order_categories")),
    }

    customer_id = raw.get("customer_id")
    if customer_id is not None:
        customer_id = str(customer_id).strip() or None

    visitor_id = raw.get("visitor_id")
    if visitor_id is not None:
        visitor_id = str(visitor_id).strip() or None

    return {
        "auth_state": auth_state,
        "visitor_id": visitor_id,
        "customer_id": customer_id,
        "preferences": preferences,
        "source": source,
    }


def merge_user_context(
    existing: Optional[Dict[str, Any]], incoming: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    base = normalize_user_context(existing)
    new = normalize_user_context(incoming)

    if new.get("source") == "none" and not any(new.get("preferences", {}).values()):
        return base

    prefs = dict(base.get("preferences") or {})
    new_prefs = new.get("preferences") or {}
    for key, value in new_prefs.items():
        if value:
            prefs[key] = value

    auth_state = new.get("auth_state") or base.get("auth_state") or "guest"
    source = new.get("source") or base.get("source") or "none"
    if auth_state == "logged_in" and base.get("source") in ("cookies", "merged"):
        source = "merged"

    return {
        "auth_state": auth_state,
        "visitor_id": new.get("visitor_id") or base.get("visitor_id"),
        "customer_id": new.get("customer_id") or base.get("customer_id"),
        "preferences": prefs,
        "source": source,
    }


def has_personalization_signals(user_context: Optional[dict]) -> bool:
    if not user_context:
        return False
    prefs = user_context.get("preferences") or {}
    return any(
        [
            prefs.get("viewed_product_ids"),
            prefs.get("viewed_categories"),
            prefs.get("cart_product_ids"),
            prefs.get("search_terms"),
            prefs.get("brand_affinity"),
            prefs.get("last_order_categories"),
            prefs.get("budget_hint"),
        ]
    )


def preference_score(product: dict, user_context: Optional[dict]) -> float:
    """Lower score = more relevant (used for sorting)."""
    if not user_context:
        return 0.0

    prefs = user_context.get("preferences") or {}
    score = 0.0

    pid = product.get("id")
    categories = [c.lower() for c in (product.get("categories") or [])]
    name = (product.get("name") or "").lower()
    tags = [t.lower() for t in (product.get("tags") or [])]

    viewed_ids = set(prefs.get("viewed_product_ids") or [])
    cart_ids = set(prefs.get("cart_product_ids") or [])
    viewed_cats = {c.lower() for c in (prefs.get("viewed_categories") or [])}
    order_cats = {c.lower() for c in (prefs.get("last_order_categories") or [])}
    brands = {b.lower() for b in (prefs.get("brand_affinity") or [])}
    searches = [s.lower() for s in (prefs.get("search_terms") or [])]

    if pid in cart_ids:
        score -= 50
    if pid in viewed_ids:
        score -= 30
    if viewed_cats and any(c in viewed_cats for c in categories):
        score -= 20
    if order_cats and any(c in order_cats for c in categories):
        score -= 25
    if brands and any(b in name or any(b in t for t in tags) for b in brands):
        score -= 15
    for term in searches:
        if term and (term in name or any(term in c for c in categories)):
            score -= 10

    budget_text = prefs.get("budget_hint") or ""
    if budget_text:
        import re

        numbers = re.findall(r"[\d.]+", budget_text)
        if numbers:
            try:
                budget = float(numbers[0])
                price = float(product.get("price") or product.get("regular_price") or 0)
                if price > 0:
                    score += abs(price - budget) / max(budget, 1.0)
            except (TypeError, ValueError):
                pass

    return score


def _reason_for_product(product: dict, user_context: dict) -> str:
    prefs = user_context.get("preferences") or {}
    pid = product.get("id")
    categories = [c.lower() for c in (product.get("categories") or [])]
    name = (product.get("name") or "").lower()

    if pid in set(prefs.get("cart_product_ids") or []):
        return "Already in your cart interest — a strong match for you."
    if pid in set(prefs.get("viewed_product_ids") or []):
        return "You viewed this recently — here it is again as a top pick."
    order_cats = {c.lower() for c in (prefs.get("last_order_categories") or [])}
    if order_cats and any(c in order_cats for c in categories):
        return "Matches categories from your past orders."
    viewed_cats = {c.lower() for c in (prefs.get("viewed_categories") or [])}
    if viewed_cats and any(c in viewed_cats for c in categories):
        matched = next(c for c in (product.get("categories") or []) if c.lower() in viewed_cats)
        return f"Based on your interest in {matched}."
    brands = {b.lower() for b in (prefs.get("brand_affinity") or [])}
    for b in brands:
        if b in name:
            return f"Fits your preference for {b.title()}."
    searches = [s.lower() for s in (prefs.get("search_terms") or [])]
    for term in searches:
        if term and term in name:
            return f"Related to your search for “{term}”."
    return "Picked for you based on your browsing activity."


def top_personalized_products(
    products: list, user_context: Optional[dict], limit: int = 3
) -> List[dict]:
    """Return top personalized products when preference signals exist."""
    if not products or not has_personalization_signals(user_context):
        return []

    ranked = sorted(products, key=lambda p: preference_score(p, user_context))
    out: List[dict] = []
    for product in ranked:
        if preference_score(product, user_context) >= 0:
            continue
        out.append(
            {
                **product,
                "reason": _reason_for_product(product, user_context or {}),
            }
        )
        if len(out) >= limit:
            break
    return out
