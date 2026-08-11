"""Deterministic catalog filtering so recommendations match the user query."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


_ACCESSORY_WORDS = (
    "case",
    "cover",
    "glass",
    "skyddsglas",
    "skärmskydd",
    "screen protector",
    "charger",
    "cable",
    "laddare",
    "headset",
    "earphone",
    "earbud",
    "airpods",
    "folier",
    "fodral",
    "battery",
    "batteri",
    "adapter",
    "mount",
    "holder",
    "stand",
    "accessory",
    "accessories",
    "tillbehör",
)

_PHONE_WORDS = (
    "phone",
    "phones",
    "iphone",
    "samsung",
    "galaxy",
    "pixel",
    "xiaomi",
    "oneplus",
    "huawei",
    "mobil",
    "mobiler",
    "smartphone",
    "handset",
)

_MODEL_RE = re.compile(
    r"\b("
    r"iphone\s*(?:se|[0-9]{1,2})\s*(?:pro\s*max|pro|plus|mini)?"
    r"|galaxy\s*s?\s*[0-9]{1,2}\s*(?:ultra|plus|\+)?"
    r"|pixel\s*[0-9]{1,2}\s*(?:pro\s*xl|pro|a)?"
    r")\b",
    re.I,
)

_BUDGET_RE = re.compile(
    r"(?:under|below|max|upto|up\s*to|less\s*than|<|budget)\s*"
    r"(?:sek|kr|£|\$|€|rs\.?|inr)?\s*"
    r"([\d][\d\s.,]{0,12})",
    re.I,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _parse_price(raw: Any) -> float:
    try:
        return float(str(raw or "0").replace(",", "").replace(" ", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _looks_accessory(product: dict) -> bool:
    blob = _norm(
        " ".join(
            [
                str(product.get("name") or ""),
                " ".join(product.get("categories") or []),
                " ".join(product.get("tags") or []),
            ]
        )
    )
    return any(w in blob for w in _ACCESSORY_WORDS)


def _looks_phone(product: dict) -> bool:
    blob = _norm(
        " ".join(
            [
                str(product.get("name") or ""),
                " ".join(product.get("categories") or []),
                " ".join(product.get("tags") or []),
            ]
        )
    )
    if _looks_accessory(product):
        return False
    if any(w in blob for w in _PHONE_WORDS):
        return True
    # Many phone listings are just the model name
    return bool(_MODEL_RE.search(blob))


def parse_query_constraints(message: str) -> Dict[str, Any]:
    text = _norm(message)
    models = [_norm(m) for m in _MODEL_RE.findall(text)]
    # Prefer longest model string (iphone 12 pro over iphone 12)
    models = sorted(set(models), key=len, reverse=True)

    budget = None
    m = _BUDGET_RE.search(text)
    if m:
        num = re.sub(r"[^\d.]", "", m.group(1).replace(",", ""))
        if num:
            try:
                budget = float(num)
            except ValueError:
                budget = None
    if budget is None:
        # "80 Thousand" / "80k"
        m2 = re.search(r"\b(\d{1,3})\s*(?:thousand|k)\b", text, re.I)
        if m2:
            budget = float(m2.group(1)) * 1000

    wants_accessory = any(
        w in text
        for w in (
            "accessor",
            "tillbehör",
            "case",
            "cover",
            "glass",
            "skyddsglas",
            "charger",
            "laddare",
            "cable",
        )
    )
    wants_phone = (not wants_accessory) and (
        any(w in text for w in _PHONE_WORDS)
        or bool(models)
        or "recommend" in text
        or "suggest" in text
    )

    return {
        "models": models,
        "budget": budget,
        "wants_phone": wants_phone,
        "wants_accessory": wants_accessory,
        "raw": text,
    }


def filter_products_for_query(
    products: List[dict], message: str, *, limit: int = 40
) -> Tuple[List[dict], Dict[str, Any]]:
    """
    Narrow catalog before the LLM chooses IDs.
    Returns (filtered_products, constraints).
    """
    constraints = parse_query_constraints(message)
    pool = list(products or [])

    if constraints["wants_phone"] and not constraints["wants_accessory"]:
        phones = [p for p in pool if _looks_phone(p)]
        if phones:
            pool = phones
    elif constraints["wants_accessory"]:
        accessories = [p for p in pool if _looks_accessory(p)]
        if accessories:
            pool = accessories

    models = constraints["models"]
    if models:
        exact = []
        for p in pool:
            name = _norm(str(p.get("name") or ""))
            if any(model in name for model in models):
                exact.append(p)
        if exact:
            pool = exact
        else:
            # Soften: keep phones that share brand token (iphone / galaxy / pixel)
            brands = []
            for model in models:
                if model.startswith("iphone"):
                    brands.append("iphone")
                elif "galaxy" in model:
                    brands.append("galaxy")
                elif model.startswith("pixel"):
                    brands.append("pixel")
            if brands:
                soft = [
                    p
                    for p in pool
                    if any(b in _norm(str(p.get("name") or "")) for b in brands)
                ]
                if soft:
                    pool = soft

    budget = constraints["budget"]
    if budget and budget > 0:
        under = []
        for p in pool:
            price = _parse_price(p.get("price") or p.get("regular_price"))
            if price <= 0 or price <= budget:
                under.append(p)
        if under:
            pool = under

    def rank(p: dict) -> tuple:
        name = _norm(str(p.get("name") or ""))
        model_hit = 0
        if models:
            model_hit = 0 if any(m in name for m in models) else 1
        price = _parse_price(p.get("price") or p.get("regular_price"))
        budget_pen = 0.0
        if budget and price > 0:
            budget_pen = abs(price - budget) / max(budget, 1.0)
        return (model_hit, budget_pen, name)

    pool = sorted(pool, key=rank)
    return pool[:limit], constraints


def enforce_recommendation_ids(
    products: List[dict],
    recommended: List[dict],
    constraints: Optional[Dict[str, Any]] = None,
) -> List[dict]:
    """Drop AI picks that violate hard filters; keep order."""
    by_id = {p.get("id"): p for p in products}
    constraints = constraints or {}
    models = constraints.get("models") or []
    budget = constraints.get("budget")
    out = []
    for item in recommended or []:
        pid = item.get("id")
        product = by_id.get(pid)
        if not product:
            continue
        name = _norm(str(product.get("name") or ""))
        if models and not any(m in name for m in models):
            # If we had exact model matches in catalog, skip mismatches
            continue
        if budget:
            price = _parse_price(product.get("price") or product.get("regular_price"))
            if price > budget > 0:
                continue
        if constraints.get("wants_phone") and not constraints.get("wants_accessory"):
            if _looks_accessory(product):
                continue
        out.append({**product, "reason": item.get("reason")})
    return out
