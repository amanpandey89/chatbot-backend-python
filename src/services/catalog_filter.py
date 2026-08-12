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

# Generation only when it sits next to the brand (not "12 månaders garanti")
_IPHONE_GEN_RE = re.compile(
    r"iphone[\s\-]*?(se|[0-9]{1,2})(?![0-9])",
    re.I,
)
_GALAXY_GEN_RE = re.compile(
    r"(?:galaxy|samsung)[\s\-]*s?\s*([0-9]{1,2})(?![0-9])",
    re.I,
)
_PIXEL_GEN_RE = re.compile(
    r"pixel[\s\-]*([0-9]{1,2})(?![0-9])",
    re.I,
)

_BUDGET_RE = re.compile(
    r"(?:under|below|max|upto|up\s*to|less\s*than|<|budget)\s*"
    r"(?:sek|kr|£|\$|€|rs\.?|inr)?\s*"
    r"([\d][\d\s.,]{0,12})",
    re.I,
)

_STOPWORDS = {
    "show",
    "me",
    "a",
    "an",
    "the",
    "all",
    "please",
    "find",
    "list",
    "open",
    "browse",
    "for",
    "with",
    "and",
    "or",
    "of",
    "to",
    "my",
    "some",
    "any",
    "looking",
    "want",
    "need",
    "i",
    "im",
    "get",
    "buy",
    "shop",
    "store",
    "product",
    "products",
    "item",
    "items",
    "can",
    "you",
    "your",
    "have",
    "has",
    "are",
    "is",
    "in",
    "on",
    "at",
    "from",
    "that",
    "this",
    "those",
    "these",
    "recommend",
    "suggest",
    "best",
    "top",
    "under",
    "below",
    "max",
    "budget",
    "color",
    "colour",
    "size",
}

# Canonical keyword expansions for apparel / common retail
_KEYWORD_EXPAND = {
    "jean": ("jean", "jeans", "denim"),
    "jeans": ("jean", "jeans", "denim"),
    "denim": ("jean", "jeans", "denim"),
    "men": ("men", "mens", "men's", "male", "man", "gentleman"),
    "mens": ("men", "mens", "men's", "male", "man"),
    "man": ("men", "mens", "men's", "male", "man"),
    "male": ("men", "mens", "men's", "male", "man"),
    "women": ("women", "womens", "women's", "female", "lady", "ladies", "woman"),
    "womens": ("women", "womens", "women's", "female", "lady", "ladies", "woman"),
    "woman": ("women", "womens", "women's", "female", "lady", "ladies", "woman"),
    "female": ("women", "womens", "women's", "female", "lady", "ladies", "woman"),
    "dress": ("dress", "dresses", "gown"),
    "dresses": ("dress", "dresses", "gown"),
    "shirt": ("shirt", "shirts", "tee", "t-shirt", "tshirt"),
    "shirts": ("shirt", "shirts", "tee", "t-shirt", "tshirt"),
    "jacket": ("jacket", "jackets", "coat"),
    "jackets": ("jacket", "jackets", "coat"),
    "pant": ("pant", "pants", "trousers"),
    "pants": ("pant", "pants", "trousers"),
    "shoe": ("shoe", "shoes", "sneaker", "sneakers"),
    "shoes": ("shoe", "shoes", "sneaker", "sneakers"),
}

_FEMALE_MARKERS = (
    "women",
    "womens",
    "woman",
    "female",
    "lady",
    "ladies",
    "dress",
    "dresses",
    "gown",
    "kimono",
    "skirt",
    "blouse",
)
_MALE_MARKERS = ("men", "mens", "man", "male", "gentleman", "boys")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _parse_price(raw: Any) -> float:
    try:
        return float(str(raw or "0").replace(",", "").replace(" ", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def _product_blob(product: dict) -> str:
    return _norm(
        " ".join(
            [
                str(product.get("name") or ""),
                str(product.get("description") or "")[:400],
                " ".join(str(x) for x in (product.get("categories") or [])),
                " ".join(str(x) for x in (product.get("tags") or [])),
                str(product.get("product_type") or ""),
            ]
        )
    )


def _looks_accessory(product: dict) -> bool:
    blob = _product_blob(product)
    return any(w in blob for w in _ACCESSORY_WORDS)


def _looks_phone(product: dict) -> bool:
    blob = _product_blob(product)
    if _looks_accessory(product):
        return False
    if any(w in blob for w in _PHONE_WORDS):
        return True
    return bool(_MODEL_RE.search(blob))


def extract_search_keywords(message: str) -> List[str]:
    """Meaningful product keywords from a shopper message (not stopwords)."""
    text = _norm(message)
    text = text.replace("'s", "s").replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", text)
    out: List[str] = []
    for t in tokens:
        if t in _STOPWORDS or len(t) < 2:
            continue
        # Keep short model numbers (12, 14, 15); drop long numeric noise
        if t.isdigit() and len(t) > 3:
            continue
        if t not in out:
            out.append(t)
    return out


def _keyword_variants(keyword: str) -> Tuple[str, ...]:
    return _KEYWORD_EXPAND.get(keyword, (keyword,))


def _blob_has_keyword(blob: str, keyword: str) -> bool:
    return any(re.search(rf"\b{re.escape(v)}\b", blob) for v in _keyword_variants(keyword))


def _first_gen(regex: re.Pattern, text: str) -> str:
    m = regex.search(text or "")
    return (m.group(1) or "").lower() if m else ""


def parse_requested_phone_model(message: str) -> Dict[str, str]:
    """iphone 12 / galaxy s21 / pixel 8 → {brand, gen} from the shopper query only."""
    text = _norm(message)
    gen = _first_gen(_IPHONE_GEN_RE, text)
    if gen:
        return {"brand": "iphone", "gen": gen}
    gen = _first_gen(_GALAXY_GEN_RE, text)
    if gen:
        return {"brand": "galaxy", "gen": gen}
    gen = _first_gen(_PIXEL_GEN_RE, text)
    if gen:
        return {"brand": "pixel", "gen": gen}
    return {}


def _product_model_text(product: dict) -> str:
    """Title/sku/taxonomy/url only — never long description (mentions other models)."""
    attrs = product.get("attributes") or {}
    attr_bits: List[str] = []
    if isinstance(attrs, dict):
        for opts in attrs.values():
            if isinstance(opts, list):
                attr_bits.extend(str(x) for x in opts)
            elif opts:
                attr_bits.append(str(opts))
    return _norm(
        " ".join(
            [
                str(product.get("name") or ""),
                str(product.get("sku") or ""),
                " ".join(str(x) for x in (product.get("categories") or [])),
                " ".join(str(x) for x in (product.get("tags") or [])),
                " ".join(attr_bits),
                str(product.get("product_url") or ""),
            ]
        )
    )


def product_phone_generation(product: dict) -> Dict[str, str]:
    """Primary phone generation from the listing title, then sku/url."""
    name = str(product.get("name") or "")
    gen = _first_gen(_IPHONE_GEN_RE, name)
    if gen:
        return {"brand": "iphone", "gen": gen}
    gen = _first_gen(_GALAXY_GEN_RE, name)
    if gen:
        return {"brand": "galaxy", "gen": gen}
    gen = _first_gen(_PIXEL_GEN_RE, name)
    if gen:
        return {"brand": "pixel", "gen": gen}

    extra = _product_model_text(product)
    gen = _first_gen(_IPHONE_GEN_RE, extra)
    if gen:
        return {"brand": "iphone", "gen": gen}
    gen = _first_gen(_GALAXY_GEN_RE, extra)
    if gen:
        return {"brand": "galaxy", "gen": gen}
    gen = _first_gen(_PIXEL_GEN_RE, extra)
    if gen:
        return {"brand": "pixel", "gen": gen}
    return {}


def _model_matches_product(requested: Dict[str, str], product: dict) -> bool:
    if not requested or not requested.get("gen"):
        return False
    found = product_phone_generation(product)
    if not found:
        return False
    return found.get("brand") == requested.get("brand") and found.get("gen") == requested.get(
        "gen"
    )


def _model_matches_name(model: str, name: str) -> bool:
    """Back-compat helper: compare a query model string to a product title."""
    requested = parse_requested_phone_model(model)
    if not requested:
        return False
    return _model_matches_product(requested, {"name": name})


def parse_query_constraints(message: str) -> Dict[str, Any]:
    text = _norm(message)
    models = [_norm(m) for m in _MODEL_RE.findall(text)]
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
        any(w in text for w in _PHONE_WORDS) or bool(models)
    )

    keywords = extract_search_keywords(message)
    requested = parse_requested_phone_model(message)
    # When a phone model is present, drop brand/generation tokens from free-text keywords
    if models or requested:
        drop = {"iphone", "galaxy", "pixel", "samsung", "phone", "phones", "mobil"}
        for model in models:
            drop.update(model.split())
        if requested.get("gen"):
            drop.add(requested["gen"])
        keywords = [k for k in keywords if k not in drop]

    gender = ""
    if any(k in ("men", "mens", "man", "male") for k in keywords):
        gender = "men"
    elif any(k in ("women", "womens", "woman", "female", "lady", "ladies") for k in keywords):
        gender = "women"

    return {
        "models": models,
        "requested_phone": requested,
        "budget": budget,
        "wants_phone": wants_phone,
        "wants_accessory": wants_accessory,
        "keywords": keywords,
        "gender": gender,
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
    requested = constraints.get("requested_phone") or parse_requested_phone_model(
        constraints.get("raw") or message
    )
    model_locked = False
    if requested.get("gen"):
        exact = [p for p in pool if _model_matches_product(requested, p)]
        # Hard lock: never fall back to other iPhone generations
        pool = exact
        model_locked = True
    elif models:
        exact = [
            p
            for p in pool
            if any(
                _model_matches_name(model, str(p.get("name") or "")) for model in models
            )
        ]
        pool = exact
        model_locked = True

    keywords = constraints.get("keywords") or []
    type_keys = [
        k
        for k in keywords
        if k
        not in (
            "blue",
            "black",
            "white",
            "red",
            "green",
            "pink",
            "yellow",
            "purple",
            "orange",
            "gold",
            "silver",
            "men",
            "mens",
            "man",
            "male",
            "women",
            "womens",
            "woman",
            "female",
            "lady",
            "ladies",
        )
    ]

    # Do not re-broaden a locked phone-model pool with generic type keywords
    if type_keys and not model_locked:
        typed = [
            p for p in pool if any(_blob_has_keyword(_product_blob(p), k) for k in type_keys)
        ]
        if typed:
            pool = typed

    gender = constraints.get("gender") or ""
    if gender == "men":
        explicit = [
            p for p in pool if any(m in _product_blob(p) for m in _MALE_MARKERS)
        ]
        if explicit:
            pool = explicit
        else:
            non_female = [
                p
                for p in pool
                if not any(m in _product_blob(p) for m in _FEMALE_MARKERS)
            ]
            if non_female:
                pool = non_female
    elif gender == "women":
        womens = [
            p for p in pool if any(m in _product_blob(p) for m in _FEMALE_MARKERS)
        ]
        if womens:
            pool = womens

    _COLOR_SYNONYMS = {
        "black": ("black", "svart", "space grey", "space gray", "graphite", "midnight"),
        "white": ("white", "vit", "starlight", "silver"),
        "blue": ("blue", "blå", "bla", "navy", "sierra"),
        "red": ("red", "röd", "rod", "product red"),
        "green": ("green", "grön", "gron", "alpine"),
        "pink": ("pink", "rosa"),
        "yellow": ("yellow", "gul"),
        "purple": ("purple", "lila"),
        "orange": ("orange",),
        "gold": ("gold", "guld"),
        "silver": ("silver",),
    }
    colors = []
    raw = constraints.get("raw") or ""
    for token, syns in _COLOR_SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(s)}\b", raw) for s in syns):
            colors.append(token)
    constraints["colors"] = colors
    if colors:
        wanted = []
        for c in colors:
            wanted.extend(_COLOR_SYNONYMS.get(c, (c,)))
        colored = [
            p
            for p in pool
            if any(re.search(rf"\b{re.escape(s)}\b", _product_model_text(p)) for s in wanted)
        ]
        if colored:
            pool = colored

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
        blob = _product_blob(p)
        name = _norm(str(p.get("name") or ""))
        model_hit = 0
        if requested.get("gen"):
            model_hit = 0 if _model_matches_product(requested, p) else 1
        elif models:
            model_hit = 0 if any(_model_matches_name(m, name) for m in models) else 1
        kw_miss = 0
        for k in keywords:
            if not _blob_has_keyword(blob, k) and k not in colors:
                kw_miss += 1
        color_miss = 0
        if colors and not any(c in blob for c in colors):
            color_miss = 1
        gender_pen = 0
        if gender == "men" and any(m in blob for m in _FEMALE_MARKERS):
            gender_pen = 2
        if gender == "women" and any(m in blob for m in _MALE_MARKERS) and not any(
            m in blob for m in _FEMALE_MARKERS
        ):
            gender_pen = 1
        price = _parse_price(p.get("price") or p.get("regular_price"))
        budget_pen = 0.0
        if budget and price > 0:
            budget_pen = abs(price - budget) / max(budget, 1.0)
        return (model_hit, gender_pen, color_miss, kw_miss, budget_pen, name)

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
    requested = constraints.get("requested_phone") or {}
    budget = constraints.get("budget")
    keywords = constraints.get("keywords") or []
    type_keys = [
        k
        for k in keywords
        if k
        not in (
            "blue",
            "black",
            "white",
            "red",
            "green",
            "pink",
            "yellow",
            "purple",
            "orange",
            "gold",
            "silver",
            "men",
            "mens",
            "man",
            "male",
            "women",
            "womens",
            "woman",
            "female",
            "lady",
            "ladies",
        )
    ]
    gender = constraints.get("gender") or ""
    out = []
    for item in recommended or []:
        pid = item.get("id")
        product = by_id.get(pid)
        if not product:
            continue
        name = _norm(str(product.get("name") or ""))
        blob = _product_blob(product)
        if requested.get("gen"):
            if not _model_matches_product(requested, product):
                continue
        elif models and not any(_model_matches_name(m, name) for m in models):
            continue
        if budget:
            price = _parse_price(product.get("price") or product.get("regular_price"))
            if price > budget > 0:
                continue
        if constraints.get("wants_phone") and not constraints.get("wants_accessory"):
            if _looks_accessory(product):
                continue
        if type_keys and not models and not any(
            _blob_has_keyword(blob, k) for k in type_keys
        ):
            continue
        if gender == "men" and any(
            m in blob for m in ("dress", "dresses", "kimono", "blouse", "skirt")
        ):
            continue
        out.append({**product, "reason": item.get("reason")})
    return out
