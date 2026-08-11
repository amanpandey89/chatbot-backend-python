"""Natural-language → WooCommerce PLP URL (filters + navigation)."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

from src.services.catalog_filter import (
    _norm,
    parse_query_constraints,
    filter_products_for_query,
)
from src.services.woocommerce import _BROWSER_UA


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

_STORAGE_RE = re.compile(r"\b(\d+)\s*(?:gb|tb)\b", re.I)

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

# store_url -> (expires_at, categories)
_CATEGORY_CACHE: Dict[str, Tuple[float, List[dict]]] = {}
_CATEGORY_TTL = 600


def _slug(text: str) -> str:
    s = _norm(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def extract_plp_filters(message: str, products: Optional[List[dict]] = None) -> Dict[str, Any]:
    """Extract structured shop filters from a natural-language message."""
    base = parse_query_constraints(message)
    text = base.get("raw") or _norm(message)

    colors: List[str] = []
    for token, slug in _COLOR_MAP.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            if slug not in colors:
                colors.append(slug)

    storage: List[str] = []
    for m in _STORAGE_RE.finditer(text):
        num = m.group(1)
        unit = "tb" if "tb" in m.group(0).lower() else "gb"
        label = f"{num}{unit}"
        if label not in storage:
            storage.append(label)

    search_parts: List[str] = []
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

    attr_filters: Dict[str, str] = {}
    if products and (colors or storage):
        attr_filters.update(
            _map_attributes_from_catalog(products, colors=colors, storage=storage)
        )

    return {
        **base,
        "colors": colors,
        "storage": storage,
        "search_parts": search_parts,
        "attr_filters": attr_filters,
        "budget": base.get("budget"),
    }


def _map_attributes_from_catalog(
    products: List[dict], *, colors: List[str], storage: List[str]
) -> Dict[str, str]:
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


async def fetch_product_categories(store_url: str) -> List[dict]:
    """
    Public WP taxonomy terms for product categories (includes custom permalinks
    like /begagnad-iphone/iphone-12/).
    """
    base = (store_url or "").rstrip("/")
    if not base:
        return []

    now = time.time()
    cached = _CATEGORY_CACHE.get(base)
    if cached and cached[0] > now:
        return cached[1]

    items: List[dict] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            page = 1
            while page <= 5:
                resp = await client.get(
                    f"{base}/wp-json/wp/v2/product_cat",
                    params={"per_page": 100, "page": page},
                    headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
                )
                if resp.status_code >= 400:
                    break
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for c in batch:
                    items.append(
                        {
                            "id": c.get("id"),
                            "slug": (c.get("slug") or "").lower(),
                            "name": c.get("name") or "",
                            "parent": c.get("parent") or 0,
                            "count": c.get("count") or 0,
                            "link": (c.get("link") or "").rstrip("/") + "/",
                        }
                    )
                if len(batch) < 100:
                    break
                page += 1
    except Exception as e:
        print(f"── PLP category fetch failed: {e}")

    _CATEGORY_CACHE[base] = (now + _CATEGORY_TTL, items)
    return items


def _model_slug_candidates(model: str) -> List[str]:
    """iphone 12 pro max → [iphone-12-pro-max, iphone-12-pro-max-phone, ...]"""
    base = _slug(model)
    if not base:
        return []
    out = [base, f"{base}-phone", f"{base}-iphone"]
    # Also without variant words for parent matching later
    return out


def resolve_category_link(
    categories: List[dict], filters: Dict[str, Any]
) -> Tuple[str, str]:
    """
    Pick the best product-category archive URL for this query.
    Prefers model child pages (…/begagnad-iphone/iphone-12/) over generic shop.
    Returns (url, matched_slug).
    """
    if not categories:
        return "", ""

    by_slug = {c["slug"]: c for c in categories if c.get("slug")}
    models = filters.get("models") or []

    # 1) Exact / near-exact model category
    for model in models:
        for cand in _model_slug_candidates(model):
            if cand in by_slug and by_slug[cand].get("link"):
                return by_slug[cand]["link"], cand
        # fuzzy: slug contains model slug tokens
        mslug = _slug(model)
        best = None
        for c in categories:
            slug = c.get("slug") or ""
            if "repair" in slug:
                continue
            if mslug and mslug in slug and c.get("link"):
                # prefer longer/more specific slug
                if best is None or len(slug) > len(best["slug"]):
                    best = c
        if best:
            return best["link"], best["slug"]

    # 2) Brand / family parent archives used by this store
    text = filters.get("raw") or ""
    family_map = [
        (("iphone", "mobil"), "begagnad-iphone"),
        (("ipad",), "begagnade-ipad"),
        (("accessor", "tillbehör", "tillbehor", "case", "charger"), "accessories"),
    ]
    for keys, slug in family_map:
        if any(k in text for k in keys) or (
            models and any(any(k in m for k in keys) for m in models)
        ):
            if slug in by_slug and by_slug[slug].get("link"):
                return by_slug[slug]["link"], slug

    if filters.get("wants_accessory") and "accessories" in by_slug:
        return by_slug["accessories"]["link"], "accessories"

    if filters.get("wants_phone") and "begagnad-iphone" in by_slug:
        return by_slug["begagnad-iphone"]["link"], "begagnad-iphone"

    return "", ""


def _append_query(url: str, params: Dict[str, Any]) -> str:
    if not url:
        return ""
    if not params:
        return url
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update({k: str(v) for k, v in params.items() if v is not None and v != ""})
    return urlunparse(parsed._replace(query=urlencode(existing, doseq=True)))


def wants_plp_navigation(message: str) -> bool:
    """True when the shopper is asking to browse/filter a listing, not only get 3 picks."""
    text = _norm(message)
    if not text:
        return False
    if any(w in text for w in _RECOMMEND_WORDS) and not any(
        w in text for w in ("show", "browse", "filter", "visa", "filtrera")
    ):
        return False
    if any(w in text for w in _BROWSE_WORDS):
        return True
    filters = extract_plp_filters(message)
    specific = bool(filters.get("models") or filters.get("colors") or filters.get("storage"))
    if specific and not any(w in text for w in _RECOMMEND_WORDS):
        return True
    return False


async def build_plp_url(
    store_url: str,
    message: str,
    products: Optional[List[dict]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Build the real store listing URL using live product_cat permalinks
    (e.g. /begagnad-iphone/iphone-12/), plus optional filter query args.
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

    categories = await fetch_product_categories(base)
    cat_url, cat_slug = resolve_category_link(categories, filters)
    filters["category_slug"] = cat_slug
    filters["category_url"] = cat_url

    params: Dict[str, Any] = {}
    budget = filters.get("budget")
    if budget and budget > 0:
        params["max_price"] = int(budget)
        params["min_price"] = 0

    for k, v in (filters.get("attr_filters") or {}).items():
        params[k] = v

    # Only add search when we lack a solid category landing (keeps PLP clean)
    if not cat_url:
        params["post_type"] = "product"
        search = " ".join(filters.get("search_parts") or []).strip()
        if search:
            params["s"] = search
        url = f"{base}/"
        url = _append_query(url, params)
    else:
        # On a category PLP, avoid redundant `s=` which can break theme filters
        url = _append_query(cat_url, params)

    label_bits = []
    if filters.get("models"):
        label_bits.append(filters["models"][0])
    if filters.get("storage"):
        label_bits.append(filters["storage"][0].upper())
    if filters.get("colors"):
        label_bits.append(filters["colors"][0])
    if budget:
        label_bits.append(f"under {int(budget)}")
    filters["label"] = " · ".join(label_bits) if label_bits else (
        cat_slug or "products"
    )
    filters["url"] = url
    return url, filters


def plp_message(filters: Dict[str, Any], *, navigating: bool = True) -> str:
    label = filters.get("label") or "matching products"
    count = filters.get("match_count")
    count_bit = f" ({count} in catalog)" if isinstance(count, int) and count > 0 else ""
    path = ""
    url = filters.get("url") or filters.get("category_url") or ""
    if url:
        try:
            path = urlparse(url).path or ""
        except Exception:
            path = ""
    path_bit = f" → `{path}`" if path else ""
    if navigating:
        return (
            f"I found the product list for **{label}**{count_bit}{path_bit}. "
            "Tap below to open it with your filters."
        )
    return f"You can also browse all **{label}**{count_bit} on the shop page{path_bit}."
