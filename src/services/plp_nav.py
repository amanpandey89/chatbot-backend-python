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
    extract_search_keywords,
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
    keywords = extract_search_keywords(message)
    # Prefer concrete product keywords over phone/accessory heuristics alone
    for k in keywords:
        if k not in search_parts:
            search_parts.append(k)
    if not search_parts:
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
    elif colors:
        for c in colors:
            if c not in search_parts:
                search_parts.append(c)

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


def _storefront_base(store_url: str) -> str:
    """Always return an absolute https storefront origin."""
    raw = (store_url or "").strip()
    if not raw:
        return ""
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.strip().strip("/")
    if not host:
        return ""
    return f"https://{host}"


def _is_shopify_host(store_url: str) -> bool:
    host = _storefront_base(store_url).replace("https://", "").lower()
    return host.endswith(".myshopify.com") or "shopify" in (store_url or "").lower()


async def fetch_shopify_collections(store_url: str) -> List[dict]:
    """Public collections.json when the storefront is not password-gated."""
    base = _storefront_base(store_url)
    if not base:
        return []
    cache_key = f"shopify:{base}"
    now = time.time()
    cached = _CATEGORY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    items: List[dict] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(
                f"{base}/collections.json",
                params={"limit": 250},
                headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            )
            if resp.status_code < 400:
                data = resp.json()
                for c in data.get("collections") or []:
                    handle = (c.get("handle") or "").lower()
                    if not handle:
                        continue
                    items.append(
                        {
                            "id": c.get("id"),
                            "slug": handle,
                            "name": c.get("title") or handle,
                            "parent": 0,
                            "count": c.get("products_count") or 0,
                            "link": f"{base}/collections/{handle}",
                        }
                    )
    except Exception as e:
        print(f"── Shopify collections fetch failed: {e}")

    _CATEGORY_CACHE[cache_key] = (now + _CATEGORY_TTL, items)
    return items


def resolve_shopify_collection(
    collections: List[dict], filters: Dict[str, Any]
) -> Tuple[str, str]:
    """Match a collection handle from keywords (e.g. jeans → /collections/jeans)."""
    if not collections:
        return "", ""
    by_slug = {c["slug"]: c for c in collections if c.get("slug")}
    keywords = list(filters.get("search_parts") or []) + list(
        (filters.get("constraints") or {}).get("keywords") or []
    )
    text = " ".join(keywords + [filters.get("raw") or ""]).lower()

    # Prefer non-"all" collections whose handle/title appears in the query
    ranked = []
    for c in collections:
        slug = c.get("slug") or ""
        if slug in ("all", "frontpage"):
            continue
        name = _norm(c.get("name") or "")
        score = 0
        if slug and slug in text.replace(" ", "-"):
            score += 3
        if slug and slug.replace("-", " ") in text:
            score += 3
        for part in slug.split("-"):
            if len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", text):
                score += 1
        for tok in name.split():
            if len(tok) > 2 and re.search(rf"\b{re.escape(tok)}\b", text):
                score += 1
        if score:
            ranked.append((score, len(slug), c))
    if ranked:
        ranked.sort(key=lambda x: (-x[0], x[1]))
        best = ranked[0][2]
        return best.get("link") or "", best.get("slug") or ""

    if "all" in by_slug:
        return by_slug["all"].get("link") or "", "all"
    return "", ""


def build_shopify_plp_url(
    store_url: str, filters: Dict[str, Any], collections: Optional[List[dict]] = None
) -> Tuple[str, str]:
    """
    Shopify listing URLs:
      - matched collection: /collections/{handle}?q=...
      - else search: /search?q=...
      - else all products: /collections/all
    Always absolute https.
    """
    base = _storefront_base(store_url)
    if not base:
        return "", ""

    collections = collections or []
    cat_url, cat_slug = resolve_shopify_collection(collections, filters)
    search = " ".join(filters.get("search_parts") or []).strip()
    # Clean search — avoid dumping the shop domain into q=
    host = base.replace("https://", "")
    if search.lower() == host.lower():
        search = ""

    if cat_url and cat_slug and cat_slug != "all":
        url = cat_url
        if search:
            url = _append_query(url, {"q": search})
        return url, cat_slug

    if search:
        return _append_query(f"{base}/search", {"q": search, "type": "product"}), "search"

    # Full catalog listing page used by this theme
    return f"{base}/collections/all", "all"


async def build_plp_url(
    store_url: str,
    message: str,
    products: Optional[List[dict]] = None,
    *,
    platform: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """
    Build the real store listing URL.
    WooCommerce: product_cat permalinks (e.g. /begagnad-iphone/iphone-12/).
    Shopify: /search?q=... or /collections/{handle} or /collections/all.
    """
    base_raw = (store_url or "").rstrip("/")
    if not base_raw:
        return "", {}

    platform = (platform or "").lower()
    if platform == "wordpress":
        platform = "woocommerce"
    if not platform:
        platform = "shopify" if _is_shopify_host(base_raw) else "woocommerce"

    filters = extract_plp_filters(message, products)
    matched, constraints = filter_products_for_query(products or [], message, limit=50)
    filters["match_count"] = len(matched)
    filters["constraints"] = {
        "models": constraints.get("models") or [],
        "budget": constraints.get("budget"),
        "keywords": constraints.get("keywords") or [],
        "gender": constraints.get("gender") or "",
    }
    # Prefer keyword-rich search_parts from catalog filter
    kw = constraints.get("keywords") or []
    if kw:
        merged = []
        for part in list(kw) + list(filters.get("search_parts") or []):
            if part and part not in merged:
                merged.append(part)
        filters["search_parts"] = merged

    if platform == "shopify":
        collections = await fetch_shopify_collections(base_raw)
        url, cat_slug = build_shopify_plp_url(base_raw, filters, collections)
        filters["category_slug"] = cat_slug
        filters["category_url"] = url
        filters["platform"] = "shopify"
    else:
        base = base_raw if base_raw.startswith("http") else f"https://{base_raw}"
        categories = await fetch_product_categories(base)
        cat_url, cat_slug = resolve_category_link(categories, filters)
        filters["category_slug"] = cat_slug
        filters["category_url"] = cat_url
        filters["platform"] = "woocommerce"

        params: Dict[str, Any] = {}
        budget = filters.get("budget")
        if budget and budget > 0:
            params["max_price"] = int(budget)
            params["min_price"] = 0

        for k, v in (filters.get("attr_filters") or {}).items():
            params[k] = v

        if not cat_url:
            params["post_type"] = "product"
            search = " ".join(filters.get("search_parts") or []).strip()
            if search:
                params["s"] = search
            url = _append_query(f"{base}/", params)
        else:
            url = _append_query(cat_url, params)

    label_bits = []
    if filters.get("models"):
        # Title-case model for display: iphone 12 → iPhone 12
        model = filters["models"][0]
        if model.startswith("iphone"):
            label_bits.append("iPhone" + model[6:])
        else:
            label_bits.append(model)
    elif filters.get("search_parts"):
        # Avoid dumping duplicate noise; keep first few unique shopper terms
        seen = set()
        for part in filters["search_parts"]:
            key = _norm(str(part))
            if not key or key in seen:
                continue
            if key in ("iphone", "phone", "product", "products"):
                continue
            seen.add(key)
            label_bits.append(str(part))
            if len(label_bits) >= 4:
                break
    if filters.get("storage"):
        bit = filters["storage"][0].upper()
        if bit.lower() not in {_norm(x) for x in label_bits}:
            label_bits.append(bit)
    if filters.get("colors"):
        bit = filters["colors"][0]
        if bit.lower() not in {_norm(x) for x in label_bits}:
            label_bits.append(bit)
    budget = filters.get("budget")
    if budget:
        label_bits.append(f"under {int(budget)}")
    filters["label"] = " · ".join(label_bits) if label_bits else "products"
    filters["url"] = url
    return url, filters


def plp_message(filters: Dict[str, Any], *, navigating: bool = True) -> str:
    label = filters.get("label") or "matching products"
    count = filters.get("match_count")
    if isinstance(count, int) and count > 0:
        count_bit = f" ({count} found)"
    else:
        count_bit = ""
    if navigating:
        return (
            f"I found products for **{label}**{count_bit}. "
            "Here are a few matches — use the button below to open the full filtered list in the store."
        )
    return (
        f"You can also browse all **{label}**{count_bit} on the shop page "
        "with the button below."
    )
