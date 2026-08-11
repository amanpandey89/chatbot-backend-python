"""Natural-language → WooCommerce PLP URL (filters + navigation)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from src.services.catalog_filter import (
    _norm,
    parse_query_constraints,
    filter_products_for_query,
)


_COLOR_MAP = {
    "black": "black",
    "svart": "black",
    "white": "white",
    "vit": "white",
    "blue": "blue",
    "blå": "blue",
    "bla": "blue",
    "red": "red",
    "röd": "red",
    "rod": "red",
    "green": "green",
    "grön": "green",
    "gron": "green",
    "gold": "gold",
    "guld": "gold",
    "silver": "silver",
    "pink": "pink",
    "rosa": "pink",
    "purple": "purple",
    "lila": "purple",
    "yellow": "yellow",
    "gul": "yellow",
    "orange": "orange",
    "graphite": "graphite",
    "midnight": "midnight",
    "starlight": "starlight",
    "sierra": "sierra",
    "alpine": "alpine",
    "titanium": "titanium",
}

_STORAGE_RE = re.compile(
    r"\b(\d+)\s*(?:gb|tb)\b",
    re.I,
)

_BROWSE_WORDS = (
    "show",
    "browse",
    "filter",
    "find all",
    "list",
    "open",
    "take me",
    "go to",
    "visa",
    "filtrera",
    "hitta",
    "sök",
    "sok",
    "visa alla",
)

_RECOMMEND_WORDS = (
    "recommend",
    "suggest",
    "best",
    "top pick",
    "rekommendera",
    "föreslå",
    "foresla",
)


def _slug(text: str) -> str:
    s = _norm(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def extract_plp_filters(message: str, products: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Extract structured shop filters from a natural-language message."""
    base = parse_query_constraints(message)
    text = base.get("raw") or _norm(message)

    colors = []
    for token, slug in _COLOR_MAP.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            if slug not in colors:
                colors.append(slug)

    storage = []
    for m in _STORAGE_RE.findall(text):
        # normalize 1 tb → 1024gb style label kept as "1tb" / "256gb"
        pass
    for m in _STORAGE_RE.finditer(text):
        num = m.group(1)
        unit = "tb" if "tb" in m.group(0).lower() else "gb"
        label = f"{num}{unit}"
        if label not in storage:
            storage.append(label)

    search_parts = []
    if base.get("models"):
        search_parts.append(base["models"][0])
    elif base.get("wants_phone"):
        search_parts.append("phone")
    if storage:
        search_parts.append(storage[0])
    if colors:
        search_parts.append(colors[0])
    if base.get("wants_accessory"):
        search_parts.append("accessories")

    # Map attribute taxonomy guesses from catalog attribute keys
    attr_filters: Dict[str, str] = {}
    if products and (colors or storage):
        attr_filters.update(
            _map_attributes_from_catalog(products, colors=colors, storage=storage)
        )

    category_slug = _guess_category_slug(products or [], base)

    return {
        **base,
        "colors": colors,
        "storage": storage,
        "search_parts": search_parts,
        "attr_filters": attr_filters,
        "category_slug": category_slug,
        "budget": base.get("budget"),
    }


def _map_attributes_from_catalog(
    products: List[dict], *, colors: List[str], storage: List[str]
) -> Dict[str, str]:
    """
    Best-effort map to WooCommerce layered-nav query args:
    filter_pa_color=black, filter_pa_storage=256gb, etc.
    """
    out: Dict[str, str] = {}
    color_tax = None
    storage_tax = None

    for p in products[:80]:
        attrs = p.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        for key in attrs.keys():
            k = _norm(str(key))
            tax = "pa_" + _slug(k).replace("pa-", "")
            if color_tax is None and any(
                x in k for x in ("color", "colour", "färg", "farg")
            ):
                color_tax = tax
            if storage_tax is None and any(
                x in k for x in ("storage", "capacity", "minne", "gb", "size")
            ):
                storage_tax = tax

    if colors and color_tax:
        out[f"filter_{color_tax}"] = colors[0]
        out[f"query_type_{color_tax}"] = "or"
    if storage and storage_tax:
        out[f"filter_{storage_tax}"] = storage[0]
        out[f"query_type_{storage_tax}"] = "or"
    return out


def _guess_category_slug(products: List[dict], constraints: Dict[str, Any]) -> str:
    models = constraints.get("models") or []
    counts: Dict[str, int] = {}
    for p in products:
        if models:
            name = _norm(str(p.get("name") or ""))
            if not any(m in name for m in models):
                continue
        for cat in p.get("categories") or []:
            slug = _slug(str(cat))
            if not slug or slug in ("uncategorized", "okategoriserad"):
                continue
            counts[slug] = counts.get(slug, 0) + 1
    if not counts:
        # brand fallback
        if models and models[0].startswith("iphone"):
            return "iphone"
        if any("galaxy" in (m or "") for m in models):
            return "samsung"
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def wants_plp_navigation(message: str) -> bool:
    """True when the shopper is asking to browse/filter a listing, not only get 3 picks."""
    text = _norm(message)
    if not text:
        return False
    if any(w in text for w in _RECOMMEND_WORDS) and not any(
        w in text for w in ("show", "browse", "filter", "visa", "filtrera")
    ):
        # "recommend iPhone 12" → cards; still may attach plp_url
        return False
    if any(w in text for w in _BROWSE_WORDS):
        return True
    # Specific filter combo without "recommend" → treat as browse
    filters = extract_plp_filters(message)
    specific = bool(filters.get("models") or filters.get("colors") or filters.get("storage"))
    if specific and not any(w in text for w in _RECOMMEND_WORDS):
        return True
    return False


def build_plp_url(
    store_url: str,
    message: str,
    products: Optional[List[dict]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Build a WooCommerce listing URL with search + common filter query args.
    Returns (url, filters_payload).
    """
    base = (store_url or "").rstrip("/")
    if not base:
        return "", {}

    filters = extract_plp_filters(message, products)
    matched, constraints = filter_products_for_query(products or [], message, limit=50)
    filters["match_count"] = len(matched)
    filters["constraints"] = {
        "models": constraints.get("models") or [],
        "budget": constraints.get("budget"),
    }

    params: Dict[str, Any] = {"post_type": "product"}
    search = " ".join(filters.get("search_parts") or []).strip()
    if search:
        params["s"] = search

    budget = filters.get("budget")
    if budget and budget > 0:
        params["max_price"] = int(budget)
        params["min_price"] = 0

    # WooCommerce product attribute layered nav (when catalog exposes attrs)
    for k, v in (filters.get("attr_filters") or {}).items():
        params[k] = v

    # Prefer category landing when we have a confident slug and a model/brand
    path = "/"
    cat = filters.get("category_slug") or ""
    if cat and (filters.get("models") or filters.get("wants_phone")):
        path = f"/product-category/{cat}/"

    query = urlencode(params, doseq=True)
    url = f"{base}{path}"
    if query:
        url = f"{url}?{query}"

    label_bits = []
    if filters.get("models"):
        label_bits.append(filters["models"][0])
    if filters.get("storage"):
        label_bits.append(filters["storage"][0].upper())
    if filters.get("colors"):
        label_bits.append(filters["colors"][0])
    if budget:
        label_bits.append(f"under {int(budget)}")
    filters["label"] = " · ".join(label_bits) if label_bits else (search or "products")
    filters["url"] = url
    return url, filters


def plp_message(filters: Dict[str, Any], *, navigating: bool = True) -> str:
    label = filters.get("label") or "matching products"
    count = filters.get("match_count")
    count_bit = f" ({count} in catalog)" if isinstance(count, int) and count > 0 else ""
    if navigating:
        return (
            f"Opening the product list for **{label}**{count_bit}. "
            "Filters are applied on the shop page."
        )
    return f"You can also browse all **{label}**{count_bit} on the shop page."
