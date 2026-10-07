from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    from ddgs import DDGS
except ImportError:  # optional at import time; CLI reports degraded mode
    DDGS = None


DEFAULT_DATABASE = Path("data/database/skinfluencer.sqlite")
DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "storage" / "schema.sql"
DEFAULT_CONFIG = Path("config/official_domains.json")
DEFAULT_OUTPUT = Path("data/catalog/purchase_links_verified.json")
DEFAULT_MODEL = (
    os.getenv("OPENAI_LINK_MODEL")
    or os.getenv("OPENAI_MODEL")
    or "gpt-5.6-luna"
)
VALIDATOR_VERSION = 14
DEFAULT_DDGS_RESULTS = 8
DEFAULT_DDGS_REGION = "tr-tr"
DEFAULT_MAX_OPENAI_CALLS = 8
DEFAULT_MIN_SEARCH_QUALITY = 40
DEFAULT_MIN_OPENAI_QUALITY = 70
DETERMINISTIC_RETAILER_THRESHOLD = 0.74
DETERMINISTIC_OFFICIAL_THRESHOLD = 0.79
DETERMINISTIC_GENERIC_RETAILER_THRESHOLD = 0.80
GENERIC_RETAILER_FETCH_TIMEOUT = 8
GENERIC_RETAILER_MAX_HTML_BYTES = 400_000
_GENERIC_PAGE_VALIDATION_CACHE: dict[tuple[str, str, str], tuple[bool, float, str]] = {}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "merchant_slug": {"type": "string"},
                    "url": {"type": "string"},
                    "page_title": {"type": "string"},
                    "exact_match": {"type": "boolean"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reason": {"type": "string"},
                },
                "required": [
                    "merchant_slug",
                    "url",
                    "page_title",
                    "exact_match",
                    "confidence",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
        "no_match_reason": {"type": "string"},
    },
    "required": ["candidates", "no_match_reason"],
    "additionalProperties": False,
}

STOPWORDS = {
    "and", "the", "for", "with", "ve", "ile", "bir", "of",
    "ml", "oz", "gr", "gram",
}

# Only domains that are not useful direct purchase destinations are blocked.
# Trendyol is intentionally trusted in v14.
GENERIC_RETAILER_BLOCKED_DOMAINS = {
    "amazon.com", "amazon.com.tr", "ebay.com", "etsy.com",
    "hepsiburada.com", "n11.com", "pazarama.com", "aliexpress.com",
    "temu.com", "shein.com",
    "enucuzgo.com.tr", "skinsort.com", "incidecoder.com", "cosdna.com",
    "makeupalley.com",
    "pinterest.com", "instagram.com", "facebook.com", "youtube.com",
    "tiktok.com", "reddit.com",
}

# Extra trusted retailer that is available even when it is not listed in config.
BUILTIN_TRUSTED_RETAILERS = (
    {
        "slug": "trendyol",
        "display_name": "Trendyol",
        "domains": ["trendyol.com"],
    },
)

PRODUCT_PATH_MARKERS = (
    "/products/", "/product/", "/urun/", "/p/", "-p-",
)

HARD_REJECTED_PATH_PREFIXES = (
    "/arama", "/search", "/category", "/categories", "/brand", "/brands",
    "/blog", "/blogs", "/article", "/articles", "/pages",
)

NON_BEAUTY_TERMS = {
    "ayakkabi", "sandalet", "terlik", "bot", "cizme", "sneaker",
    "elbise", "gomlek", "pantolon", "etek", "ceket", "kazak", "canta",
    "kemer", "kolye", "kupe", "yuzuk", "bileklik", "gozluk",
    "sunglasses", "kot",
}

VAGUE_PRODUCT_PHRASES = (
    "benzer bir urun", "benzeri bir urun", "gibi bir urun", "bu urun", "su urun",
)

# Safe bilingual product-type normalization used only for family identity/matching.
PRODUCT_TYPE_SYNONYMS = {
    "allik": "blush", "fondoten": "foundation", "kapatici": "concealer",
    "krem": "cream", "kremi": "cream", "maskara": "mascara",
    "ruj": "lipstick", "tonik": "toner", "esans": "essence",
    "esansi": "essence",
}

DESCRIPTIVE_TOKENS = {
    "iceren", "icerikli", "etkili", "urun", "urunler", "urunleri", "yeni",
    "numara", "numarasi", "numaralari", "numaralarini", "rengi", "renkleri",
}

PLURAL_OR_RANGE_PATTERNS = (
    re.compile(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b"),
    re.compile(r"\b(?:kapaticilarin|fondotenlerin|alliklarin|rujlarin|urunleri)\b"),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deterministic_stage_completed(record: dict[str, Any] | None) -> bool:
    """Whether free deterministic search already finished for this product."""
    return bool(
        record is not None
        and record.get("status") == "deterministic_unresolved"
        and int(record.get("validator_version") or 0) >= VALIDATOR_VERSION
    )


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    translated = value.translate(
        str.maketrans(
            {
                "ı": "i", "İ": "i", "ş": "s", "Ş": "s",
                "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
                "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
            }
        )
    )
    decomposed = unicodedata.normalize("NFKD", translated)
    ascii_like = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    ascii_like = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_like)
    return " ".join(ascii_like.casefold().split())


def clean_brand_for_search(value: str | None) -> str:
    """Remove social-handle syntax without mutating the stored catalogue brand."""
    return re.sub(r"^\s*@+", "", str(value or "")).strip()


def product_family_signature(product_name: str | None) -> str:
    """Build a conservative family key for case/translation/shade duplicates.

    Only explicit size and numeric shade markers are removed. Standalone words such as
    ``color`` are retained so genuinely different products are not collapsed.
    """
    value = normalize_text(product_name)
    value = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:ml|gr|g|gram|oz)\b", " ", value)
    value = re.sub(
        r"\b(?:no|numara|numarasi|numarali|shade)\s*#?\d+(?:[.,]\d+)?\b",
        " ",
        value,
    )
    value = re.sub(
        r"\b#?\d+(?:[.,]\d+)?\s*(?:numara|numarasi|numarali|shade)\b",
        " ",
        value,
    )
    tokens: list[str] = []
    for token in value.split():
        if token in {"numara", "numarasi", "numarali"}:
            continue
        tokens.append(PRODUCT_TYPE_SYNONYMS.get(token, token))
    return " ".join(tokens)


def product_family_key(brand: str, product_name: str) -> str:
    return (
        f"{normalize_text(clean_brand_for_search(brand))}::"
        f"{product_family_signature(product_name)}"
    )


def _catalog_title_score(product: dict[str, Any]) -> float:
    """Choose the cleanest search identity inside a conservative family."""
    raw_name = str(product.get("product_name") or "").strip()
    normalized = normalize_text(raw_name)
    score = 10.0
    if 2 <= len(normalized.split()) <= 12:
        score += 3.0
    if any(phrase in normalized for phrase in VAGUE_PRODUCT_PHRASES):
        score -= 8.0
    if any(pattern.search(normalized) for pattern in PLURAL_OR_RANGE_PATTERNS):
        score -= 8.0
    if "..." in raw_name:
        score -= 8.0
    score += min(4.0, float(product.get("review_count") or 0))
    score += min(2.0, float(product.get("social_post_count") or 0))
    return score

def search_quality_score(product: dict[str, Any]) -> int:
    """Simple 3-level cost gate, not a ranking heuristic.

    0   = unusable extraction: do not search.
    50  = searchable for free with DDGS, but too caption-like/vague for paid fallback.
    100 = normal retail-like product identity: DDGS first, then OpenAI if needed.

    Ordinary beauty words (cream, serum, SPF, vitamin, ceramide, etc.) never lower
    quality. Only obvious extraction-language artifacts trigger FREE_ONLY.
    """
    brand = normalize_text(str(product.get("search_brand") or product.get("brand") or ""))
    name = normalize_text(str(product.get("search_product_name") or product.get("product_name") or ""))
    raw_name = str(product.get("search_product_name") or product.get("product_name") or "")

    if not brand or not name:
        return 0
    if any(term in name for term in VAGUE_PRODUCT_PHRASES) or "..." in raw_name:
        return 0
    if any(pattern.search(name) for pattern in PLURAL_OR_RANGE_PATTERNS):
        return 0
    if brand == name and len(name.split()) <= 3:
        return 0

    # Natural-language extraction residue such as
    # "patch pdrn ve retinol içeren" may still be worth a free lookup,
    # but should not consume paid OpenAI search until canonicalized.
    name_tokens = set(name.split())
    if name_tokens & {"iceren", "icerikli", "etkili", "urunler", "urunleri"}:
        return 50

    return 100

def _family_representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        members,
        key=lambda item: (
            _catalog_title_score(item),
            int(item.get("review_count") or 0) + int(item.get("social_post_count") or 0),
            -len(str(item.get("product_name") or "")),
            -int(item.get("product_id") or 0),
        ),
    )


def prepare_product_families(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse conservative duplicate/variant families into one search queue item.

    The database is not modified here. Family members keep their original product IDs,
    while ranking signals are aggregated so duplicate extraction does not fragment
    importance. A verified result can later be copied to each safe family member.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        grouped[product_family_key(product["brand"], product["product_name"])].append(product)

    families: list[dict[str, Any]] = []
    for family_key, members in grouped.items():
        representative = _family_representative(members)
        family = dict(representative)
        family["family_key"] = family_key
        family["family_members"] = [dict(item) for item in members]
        family["family_size"] = len(members)
        family["search_brand"] = clean_brand_for_search(representative["brand"])
        family["search_product_name"] = representative["product_name"]
        family["review_count"] = sum(int(item.get("review_count") or 0) for item in members)
        family["social_post_count"] = sum(
            int(item.get("social_post_count") or 0) for item in members
        )
        family["total_content_count"] = (
            family["review_count"] + family["social_post_count"]
        )
        family["influencer_count"] = max(
            (int(item.get("influencer_count") or 0) for item in members),
            default=0,
        )
        family["search_quality"] = search_quality_score(family)
        # Review evidence is stronger than a social caption mention, while both remain
        # useful. Quality breaks the many low-frequency ties before alphabetical order.
        family["priority_score"] = (
            100 * family["influencer_count"]
            + 16 * family["review_count"]
            + 8 * family["social_post_count"]
        )
        families.append(family)

    families.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            -int(item["review_count"]),
            -int(item["social_post_count"]),
            normalize_text(str(item["search_brand"])),
            normalize_text(str(item["search_product_name"])),
        )
    )
    return families


def product_search_eligibility(
    product: dict[str, Any],
    *,
    min_quality: int = DEFAULT_MIN_SEARCH_QUALITY,
) -> tuple[bool, str]:
    """Hard gate only: reject non-beauty noise or clearly unusable extraction text."""
    brand = normalize_text(str(product.get("search_brand") or product.get("brand") or ""))
    name = normalize_text(str(product.get("search_product_name") or product.get("product_name") or ""))
    category = normalize_text(str(product.get("category") or ""))
    if not brand or not name:
        return False, "eksik_marka_veya_urun_adi"

    combined_tokens = set(f"{brand} {name} {category}".split())
    for term in sorted(NON_BEAUTY_TERMS):
        if term in combined_tokens:
            return False, f"beauty_disi:{term}"
    if any(phrase in name for phrase in VAGUE_PRODUCT_PHRASES):
        return False, "belirsiz_urun_adi"
    if "..." in str(product.get("search_product_name") or product.get("product_name") or ""):
        return False, "belirsiz_urun_adi:ellipsis"
    if any(pattern.search(name) for pattern in PLURAL_OR_RANGE_PATTERNS):
        return False, "belirsiz_urun_adi:plural_or_range"
    if brand == name and len(name.split()) <= 3:
        return False, "marka_ve_urun_adi_ayni"
    if search_quality_score(product) < min_quality:
        return False, "dusuk_search_quality"
    return True, "searchable"

def _clone_link_for_product(
    record: dict[str, Any],
    product: dict[str, Any],
    *,
    method: str,
) -> dict[str, Any]:
    cloned = dict(record)
    cloned["brand"] = str(product["brand"])
    cloned["product_name"] = str(product["product_name"])
    cloned["verification_method"] = method
    cloned.setdefault("source_product_key", record_key(record))
    return cloned


def family_link_cache(
    links: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    cached: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in links:
        if record.get("verification_status") != "verified":
            continue
        key = product_family_key(
            str(record.get("brand") or ""),
            str(record.get("product_name") or ""),
        )
        cached[key].append(record)
    return cached


def propagate_family_links(
    source_links: list[dict[str, Any]],
    members: list[dict[str, Any]],
    *,
    method: str,
) -> list[dict[str, Any]]:
    propagated: list[dict[str, Any]] = []
    for member in members:
        for record in source_links:
            propagated.append(_clone_link_for_product(record, member, method=method))
    return dedupe_links(propagated)


def normalize_database_identity(value: str | None) -> str:
    """Match build_sqlite_database.py's product identity normalization."""
    if not value:
        return ""
    translated = value.translate(
        str.maketrans(
            {
                "ı": "i",
                "İ": "i",
                "ş": "s",
                "Ş": "s",
                "ğ": "g",
                "Ğ": "g",
                "ü": "u",
                "Ü": "u",
                "ö": "o",
                "Ö": "o",
                "ç": "c",
                "Ç": "c",
            }
        )
    )
    decomposed = unicodedata.normalize("NFKD", translated)
    ascii_like = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(ascii_like.casefold().split()).strip()


def normalized_domain(value: str) -> str:
    return value.casefold().strip().rstrip(".").removeprefix("www.")


def domain_matches(hostname: str, expected_domain: str) -> bool:
    host = normalized_domain(hostname)
    expected = normalized_domain(expected_domain)
    return host == expected or host.endswith(f".{expected}")


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    hostname = normalized_domain(parsed.hostname or "")
    if parsed.scheme != "https" or not hostname:
        return ""

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if domain_matches(hostname, "korendy.com.tr") and "/products/" in path:
        product_tail = path.split("/products/", 1)[1].split("/", 1)[0]
        path = f"/products/{product_tail}"

    query = "" if path not in {"", "/"} else parsed.query
    return urlunsplit(("https", hostname, path.rstrip("/") or "/", query, ""))


def is_product_page_path(url: str, merchant_slug: str) -> bool:
    """Simple product-detail URL rule; no HTML fetching.

    /collections/.../products/... is valid because Shopify often nests real product
    pages under collections. Plain collection/search/category/blog pages are rejected.
    Official brand sites may use a clean product slug without a standard marker.
    """
    path = urlsplit(url).path.casefold().rstrip("/")
    if not path or path == "/":
        return False
    if any(path == p or path.startswith(f"{p}/") for p in HARD_REJECTED_PATH_PREFIXES):
        return False

    has_product_marker = any(marker in path for marker in PRODUCT_PATH_MARKERS)
    if path.startswith("/collections/") or path.startswith("/collection/"):
        return "/products/" in path or "/product/" in path

    required = {
        "gratis": "-p-",
        "watsons": "/p/",
        "rossmann": "-p-",
        "korendy": "/products/",
        "sephora": "/p/",
        "trendyol": "-p-",
    }.get(merchant_slug)
    if required:
        return required in path
    if merchant_slug == "official_brand":
        return has_product_marker or len([x for x in path.split("/") if x]) >= 1
    return has_product_marker

def meaningful_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in normalize_text(value).split():
        token = PRODUCT_TYPE_SYNONYMS.get(token, token)
        if len(token) < 2 or token in STOPWORDS or token.isdigit():
            continue
        tokens.add(token)
    return tokens

def title_product_match_score(
    brand: str,
    product_name: str,
    source_title: str,
    url: str,
) -> float:
    """Simple token-overlap score kept for backwards-compatible tests/logging."""
    haystack = meaningful_tokens(f"{source_title} {url}")
    brand_tokens = meaningful_tokens(brand)
    product_tokens = meaningful_tokens(product_name)
    brand_overlap = len(brand_tokens & haystack) / len(brand_tokens) if brand_tokens else 0.0
    product_overlap = len(product_tokens & haystack) / len(product_tokens) if product_tokens else 0.0
    return 0.30 * brand_overlap + 0.70 * product_overlap

def generic_product_page_path(url: str) -> bool:
    return is_product_page_path(url, "generic_retailer")

def generic_domain_blocked(hostname: str, blocked_domains: list[str]) -> bool:
    # A configured trusted retailer always wins over a generic block classification.
    candidates = set(blocked_domains) | GENERIC_RETAILER_BLOCKED_DOMAINS
    return any(domain_matches(hostname, item) for item in candidates)

def _html_visible_text(raw_html: str) -> str:
    """Legacy compatibility helper; v14 no longer fetches/parses retailer HTML."""
    return html.unescape(re.sub(r"<[^>]+>", " ", raw_html))

def generic_commerce_page_valid(
    *,
    url: str,
    brand: str,
    product_name: str,
    search_evidence: str,
) -> tuple[bool, float, str]:
    """No-fetch generic validation: product URL + brand/product token overlap only."""
    if not generic_product_page_path(url):
        return False, 0.0, "generic_not_product_path"
    evidence = meaningful_tokens(f"{search_evidence} {url}")
    brand_tokens = meaningful_tokens(brand)
    product_tokens = meaningful_tokens(product_name)
    brand_overlap = len(brand_tokens & evidence) / len(brand_tokens) if brand_tokens else 0.0
    product_overlap = len(product_tokens & evidence) / len(product_tokens) if product_tokens else 0.0
    valid = brand_overlap >= 0.50 and product_overlap >= 0.65
    confidence = min(0.94, 0.55 + 0.15 * brand_overlap + 0.24 * product_overlap)
    return valid, confidence if valid else product_overlap, (
        f"generic_token_match:brand={brand_overlap:.2f},product={product_overlap:.2f}"
        if valid else
        f"generic_token_miss:brand={brand_overlap:.2f},product={product_overlap:.2f}"
    )

def generic_merchant_name(hostname: str) -> str:
    """Return the merchant from the registrable/base domain, not a shop subdomain.

    Examples:
      shop.advancedderm.com -> Advancedderm
      shop.delivered.co.kr  -> Delivered
      www.lookfantastic.com -> Lookfantastic
    """
    host = normalized_domain(hostname)
    parts = [part for part in host.split(".") if part]
    if not parts:
        return hostname

    # Ignore common storefront/language/mobile subdomains.
    while len(parts) > 2 and parts[0] in {
        "www", "shop", "store", "m", "mobile", "en", "tr", "us", "uk"
    }:
        parts.pop(0)

    # Handle common country-code second-level domains such as
    # delivered.co.kr, example.com.tr, example.co.uk.
    if (
        len(parts) >= 3
        and len(parts[-1]) == 2
        and parts[-2] in {"co", "com", "net", "org"}
    ):
        label = parts[-3]
    elif len(parts) >= 2:
        label = parts[-2]
    else:
        label = parts[0]

    label = label.replace("-", " ").replace("_", " ").strip()
    return label.title() or hostname


def ddgs_search(
    query: str,
    *,
    max_results: int,
    region: str,
) -> list[dict[str, str]]:
    """Run token-free search and normalize results."""
    if DDGS is None:
        raise RuntimeError("ddgs paketi kurulu değil: pip install -U ddgs")
    raw_results = DDGS(timeout=20).text(
        query=query,
        region=region,
        safesearch="moderate",
        max_results=min(max(max_results, 1), 20),
        backend="auto",
    )
    normalized: list[dict[str, str]] = []
    for item in raw_results or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("href") or item.get("url") or "").strip()
        if not url:
            continue
        normalized.append(
            {
                "url": url,
                "title": html.unescape(str(item.get("title") or "")).strip(),
                "description": html.unescape(
                    str(item.get("body") or item.get("description") or "")
                ).strip(),
            }
        )
    return normalized


def deterministic_validate_results(
    *,
    results: list[dict[str, str]],
    brand: str,
    product_name: str,
    retailers: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Three-rule DDGS validator: URL path, token overlap, domain class."""
    known_brand_domains = configured_brand_domains(config, brand)
    blocked_domains = [normalized_domain(str(x)) for x in config.get("blocked_domains") or []]
    accepted: list[dict[str, Any]] = []

    for item in results:
        url = canonicalize_url(str(item.get("url") or ""))
        if not url:
            continue
        hostname = normalized_domain(urlsplit(url).hostname or "")
        evidence_text = " ".join([str(item.get("title") or ""), str(item.get("description") or ""), url])
        evidence_tokens = meaningful_tokens(evidence_text)
        brand_tokens = meaningful_tokens(brand)
        product_tokens = meaningful_tokens(product_name)
        brand_overlap = len(brand_tokens & evidence_tokens) / len(brand_tokens) if brand_tokens else 0.0
        product_overlap = len(product_tokens & evidence_tokens) / len(product_tokens) if product_tokens else 0.0

        matched_retailer = next((
            retailer for retailer in retailers
            if any(domain_matches(hostname, str(domain)) for domain in retailer.get("domains") or [])
        ), None)
        now = utc_now_iso()

        if matched_retailer is not None:
            slug = str(matched_retailer.get("slug") or "")
            if not is_product_page_path(url, slug):
                continue
            if brand_overlap < 0.34 or product_overlap < 0.55:
                continue
            accepted.append({
                "brand": brand,
                "product_name": product_name,
                "merchant_name": str(matched_retailer.get("display_name") or slug),
                "merchant_slug": slug,
                "link_type": "retailer",
                "url": url,
                "domain": hostname,
                "verification_status": "verified",
                "match_confidence": round(min(0.98, 0.58 + 0.16 * brand_overlap + 0.24 * product_overlap), 3),
                "source_title": str(item.get("title") or ""),
                "verified_at": now,
                "checked_at": now,
                "verification_method": "ddgs_simple",
                "openai_response_id": "",
            })
            continue

        if official_domain_plausible(brand, hostname, known_brand_domains):
            if not is_product_page_path(url, "official_brand"):
                continue
            # Domain identity supplies the brand evidence; product title still needs overlap.
            if product_overlap < 0.55:
                continue
            accepted.append({
                "brand": brand,
                "product_name": product_name,
                "merchant_name": brand,
                "merchant_slug": "official_brand",
                "link_type": "official_brand",
                "url": url,
                "domain": hostname,
                "verification_status": "verified",
                "match_confidence": round(min(0.97, 0.66 + 0.30 * product_overlap), 3),
                "source_title": str(item.get("title") or ""),
                "verified_at": now,
                "checked_at": now,
                "verification_method": "ddgs_simple",
                "openai_response_id": "",
            })
            continue

        if generic_domain_blocked(hostname, blocked_domains):
            continue
        if not generic_product_page_path(url):
            continue
        if brand_overlap < 0.50 or product_overlap < 0.65:
            continue
        accepted.append({
            "brand": brand,
            "product_name": product_name,
            "merchant_name": generic_merchant_name(hostname),
            "merchant_slug": "generic_retailer",
            "link_type": "retailer",
            "url": url,
            "domain": hostname,
            "verification_status": "verified",
            "match_confidence": round(min(0.94, 0.55 + 0.15 * brand_overlap + 0.24 * product_overlap), 3),
            "source_title": str(item.get("title") or ""),
            "verified_at": now,
            "checked_at": now,
            "verification_method": "ddgs_simple",
            "openai_response_id": "",
        })

    # Prefer trusted stores, then official brand page, then generic retailers.
    def rank(record: dict[str, Any]) -> tuple[int, float]:
        slug = str(record.get("merchant_slug") or "")
        if slug not in {"official_brand", "generic_retailer"}:
            tier = 0
        elif slug == "official_brand":
            tier = 1
        else:
            tier = 2
        return (tier, -float(record.get("match_confidence") or 0.0))

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for record in accepted:
        key = (str(record.get("merchant_slug") or ""), str(record.get("domain") or ""))
        previous = unique.get(key)
        if previous is None or float(record.get("match_confidence") or 0) > float(previous.get("match_confidence") or 0):
            unique[key] = record
    return sorted(unique.values(), key=rank)[:3]

def deterministic_search_results(
    *,
    brand: str,
    product_name: str,
    retailers: list[dict[str, Any]],
    config: dict[str, Any],
    max_results: int,
    region: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    trusted_names = " ".join(str(item.get("display_name") or "") for item in retailers)
    queries = [
        f'"{brand}" "{product_name}" {trusted_names}',
        f'"{brand}" "{product_name}"',
    ]
    raw_results: list[dict[str, str]] = []
    seen: set[str] = set()
    notes: list[str] = []
    for idx, query in enumerate(queries, start=1):
        try:
            batch = ddgs_search(query, max_results=max_results, region=region)
        except Exception as error:
            notes.append(f"DDGS query {idx} failed: {type(error).__name__}: {error}")
            continue
        for item in batch:
            url = str(item.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                raw_results.append(item)
        verified = deterministic_validate_results(
            results=raw_results,
            brand=brand,
            product_name=product_name,
            retailers=retailers,
            config=config,
        )
        if verified:
            return verified, notes
    return deterministic_validate_results(
        results=raw_results,
        brand=brand,
        product_name=product_name,
        retailers=retailers,
        config=config,
    ), notes

def response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        data = response.model_dump()
        return data if isinstance(data, dict) else {}
    return response if isinstance(response, dict) else {}


def response_sources(response: Any) -> dict[str, str]:
    """Return canonical source URL -> source title from search output."""
    sources: dict[str, str] = {}
    payload = response_payload(response)

    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue

        action = item.get("action")
        if isinstance(action, dict):
            for source in action.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                url = canonicalize_url(str(source.get("url") or ""))
                if url:
                    sources[url] = str(source.get("title") or "")

        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                citation = annotation.get("url_citation")
                if isinstance(citation, dict):
                    url_value = citation.get("url")
                    title_value = citation.get("title")
                else:
                    url_value = annotation.get("url")
                    title_value = annotation.get("title")
                url = canonicalize_url(str(url_value or ""))
                if url:
                    sources[url] = str(title_value or "")

    return sources


def response_usage(response: Any) -> dict[str, int]:
    payload = response_payload(response)
    usage = payload.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    web_search_calls = sum(
        1
        for item in payload.get("output") or []
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    )
    return {
        "api_requests": 1,
        "web_search_calls": web_search_calls,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_tokens": int(input_details.get("cached_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def add_usage(total: dict[str, int], current: dict[str, int]) -> None:
    for key, value in current.items():
        total[key] = total.get(key, 0) + int(value)


def parse_structured_result(response: Any) -> dict[str, Any]:
    text = str(getattr(response, "output_text", "") or "").strip()
    if not text:
        raise ValueError("Model boş çıktı döndürdü.")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Model çıktısı JSON nesnesi değil.")
    return parsed


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Domain ayar dosyası bulunamadı: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Domain ayar dosyası JSON nesnesi değil.")
    return payload


def retailer_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = config.get("preferred_retailers") or []
    retailers = [dict(item) for item in configured if isinstance(item, dict)]
    by_slug = {str(item.get("slug") or ""): item for item in retailers}
    for builtin in BUILTIN_TRUSTED_RETAILERS:
        slug = str(builtin["slug"])
        if slug not in by_slug:
            retailers.append(dict(builtin))
    if not retailers:
        raise ValueError("preferred_retailers ayarı bulunamadı.")
    return retailers

def all_retailer_domains(retailers: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            normalized_domain(str(domain))
            for retailer in retailers
            for domain in retailer.get("domains") or []
            if str(domain).strip()
        }
    )


def retailer_by_slug(retailers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(retailer.get("slug") or ""): retailer
        for retailer in retailers
        if retailer.get("slug")
    }


def configured_brand_domains(config: dict[str, Any], brand: str) -> list[str]:
    brand_domains = config.get("brand_domains") or {}
    if not isinstance(brand_domains, dict):
        return []

    wanted = normalize_text(brand)
    for configured_brand, domains in brand_domains.items():
        if normalize_text(str(configured_brand)) == wanted:
            return [
                normalized_domain(str(domain))
                for domain in domains or []
                if str(domain).strip()
            ]
    return []


def official_domain_plausible(
    brand: str,
    domain: str,
    configured_domains: list[str],
) -> bool:
    if any(domain_matches(domain, item) for item in configured_domains):
        return True
    brand_compact = normalize_text(brand).replace(" ", "")
    domain_compact = normalize_text(domain).replace(" ", "")
    return bool(brand_compact) and brand_compact in domain_compact


def search_with_schema(
    client: Any,
    *,
    model: str,
    prompt: str,
    allowed_domains: list[str] | None = None,
    search_context: str = "high",
    max_tool_calls: int = 2,
) -> tuple[dict[str, Any], dict[str, str], str, dict[str, int]]:
    tool: dict[str, Any] = {
        "type": "web_search",
        "search_context_size": search_context,
    }
    if allowed_domains:
        tool["filters"] = {"allowed_domains": allowed_domains}

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        tools=[tool],
        tool_choice="required",
        max_tool_calls=max(1, max_tool_calls),
        include=["web_search_call.action.sources"],
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "purchase_link_search",
                "strict": True,
                "schema": RESULT_SCHEMA,
            }
        },
    )
    return (
        parse_structured_result(response),
        response_sources(response),
        str(getattr(response, "id", "") or ""),
        response_usage(response),
    )


def validate_openai_candidates(
    *,
    result: dict[str, Any],
    sources: dict[str, str],
    config: dict[str, Any],
    retailers: list[dict[str, Any]],
    brand: str,
    product_name: str,
    response_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trust the LLM semantic exact-match decision; apply only hard sanity checks."""
    blocked_domains = [normalized_domain(str(x)) for x in config.get("blocked_domains") or []]
    known_brand_domains = configured_brand_domains(config, brand)
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []

    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if not candidate.get("exact_match"):
            rejected.append("model exact_match=false")
            continue
        confidence = float(candidate.get("confidence") or 0.0)
        if confidence < 0.65:
            rejected.append(f"model confidence düşük ({confidence:.2f})")
            continue
        url = canonicalize_url(str(candidate.get("url") or ""))
        if not url:
            rejected.append("geçersiz URL")
            continue
        if url not in sources:
            rejected.append("URL web-search kaynaklarında yok")
            continue
        hostname = normalized_domain(urlsplit(url).hostname or "")

        matched_retailer = next((
            retailer for retailer in retailers
            if any(domain_matches(hostname, str(domain)) for domain in retailer.get("domains") or [])
        ), None)

        if matched_retailer is not None:
            slug = str(matched_retailer.get("slug") or "")
            if not is_product_page_path(url, slug):
                rejected.append(f"{slug}: product-detail URL değil")
                continue
            merchant_name = str(matched_retailer.get("display_name") or slug)
            merchant_slug = slug
            link_type = "retailer"
        elif str(candidate.get("merchant_slug") or "") == "official_brand":
            if generic_domain_blocked(hostname, blocked_domains):
                rejected.append("official candidate blocked domain")
                continue
            if not official_domain_plausible(brand, hostname, known_brand_domains):
                rejected.append("official domain marka ile uyuşmuyor")
                continue
            if not is_product_page_path(url, "official_brand"):
                rejected.append("official URL product page değil")
                continue
            merchant_name = brand
            merchant_slug = "official_brand"
            link_type = "official_brand"
        else:
            if generic_domain_blocked(hostname, blocked_domains):
                rejected.append(f"generic blocked domain: {hostname}")
                continue
            if not generic_product_page_path(url):
                rejected.append("generic URL product-detail page değil")
                continue
            # Minimal sanity only: for an unknown shop, brand should at least appear
            # in the cited title/URL. We do not re-score the model's semantic decision.
            source_title = sources.get(url) or str(candidate.get("page_title") or "")
            evidence = meaningful_tokens(f"{source_title} {url}")
            brand_tokens = meaningful_tokens(brand)
            if brand_tokens and not (brand_tokens & evidence):
                rejected.append("generic page brand sinyali yok")
                continue
            merchant_name = generic_merchant_name(hostname)
            merchant_slug = "generic_retailer"
            link_type = "retailer"

        now = utc_now_iso()
        accepted.append({
            "brand": brand,
            "product_name": product_name,
            "merchant_name": merchant_name,
            "merchant_slug": merchant_slug,
            "link_type": link_type,
            "url": url,
            "domain": hostname,
            "verification_status": "verified",
            "match_confidence": round(confidence, 3),
            "source_title": sources.get(url) or str(candidate.get("page_title") or ""),
            "verified_at": now,
            "checked_at": now,
            "verification_method": "openai_exact",
            "openai_response_id": response_id,
        })

    def rank(record: dict[str, Any]) -> tuple[int, float]:
        slug = str(record.get("merchant_slug") or "")
        if slug not in {"official_brand", "generic_retailer"}:
            tier = 0
        elif slug == "official_brand":
            tier = 1
        else:
            tier = 2
        return (tier, -float(record.get("match_confidence") or 0.0))

    best: dict[tuple[str, str], dict[str, Any]] = {}
    for record in accepted:
        key = (str(record.get("merchant_slug") or ""), str(record.get("domain") or ""))
        previous = best.get(key)
        if previous is None or float(record.get("match_confidence") or 0) > float(previous.get("match_confidence") or 0):
            best[key] = record
    return sorted(best.values(), key=rank)[:3], rejected

def validate_retailer_candidates(
    *, result: dict[str, Any], sources: dict[str, str], retailers: list[dict[str, Any]],
    brand: str, product_name: str, response_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    # Legacy wrapper kept for imports/tests; configured retailer-only validation.
    config = {"preferred_retailers": retailers, "blocked_domains": [], "brand_domains": {}}
    records, rejected = validate_openai_candidates(
        result=result, sources=sources, config=config, retailers=retailers,
        brand=brand, product_name=product_name, response_id=response_id,
    )
    return [r for r in records if r.get("merchant_slug") not in {"official_brand", "generic_retailer"}], rejected

def validate_official_candidates(
    *, result: dict[str, Any], sources: dict[str, str], config: dict[str, Any],
    retailers: list[dict[str, Any]], brand: str, product_name: str, response_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    records, rejected = validate_openai_candidates(
        result=result, sources=sources, config=config, retailers=retailers,
        brand=brand, product_name=product_name, response_id=response_id,
    )
    return [r for r in records if r.get("merchant_slug") == "official_brand"], rejected

def retailer_prompt(
    brand: str,
    product_name: str,
    retailers: list[dict[str, Any]],
) -> str:
    retailer_text = ", ".join(
        f"{item.get('display_name')}={item.get('slug')}" for item in retailers
    )
    return (
        f"Find exact product-detail pages for {brand} — {product_name} on these "
        f"Turkish retailers: {retailer_text}. Return only the same product; reject "
        "search/category/brand pages, bundles, marketplaces and different variants. "
        "Sold-out exact pages are valid. merchant_slug must be one of the listed slugs. "
        "If none is exact, return no candidates."
    )


def official_prompt(brand: str, product_name: str) -> str:
    return (
        f"Find the exact product-detail page for {brand} — {product_name} on the "
        "brand manufacturer's own official website. Reject retailers, marketplaces, "
        "distributors, social pages, search/category pages, homepages, reviews and "
        "different products. Use merchant_slug='official_brand'. If exact official "
        "page cannot be established, return no candidates."
    )


def combined_prompt(
    brand: str,
    product_name: str,
    retailers: list[dict[str, Any]],
    official_domains: list[str],
) -> str:
    trusted = ", ".join(
        f"{item.get('display_name')} (merchant_slug={item.get('slug')})"
        for item in retailers
    )
    official_hint = (
        f" Known official domain(s): {', '.join(official_domains)}."
        if official_domains else ""
    )
    return (
        f"Find up to 3 exact purchase product-detail pages for {brand} — {product_name}. "
        f"Search in this order: (1) trusted stores {trusted}; "
        "(2) the brand's own official product page; "
        "(3) another legitimate direct online retailer if needed. "
        "Trendyol is allowed when the result is the exact product-detail page. "
        "Search/category/brand-listing/blog/home pages are invalid; a sold-out exact product page is valid. "
        "Different shades/variants are invalid unless the requested shade/variant is exactly the same. "
        "For trusted stores use their listed merchant_slug; for the manufacturer use merchant_slug='official_brand'; "
        "for another legitimate direct retailer use merchant_slug='generic_retailer'. "
        "Set exact_match=true only when the page is clearly the same product."
        f"{official_hint}"
    )

def load_existing_output(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 2,
            "generated_at": None,
            "links": [],
            "unresolved": [],
            "skipped": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Çıktı JSON nesnesi değil: {path}")
    payload.setdefault("links", [])
    payload.setdefault("unresolved", [])
    payload.setdefault("skipped", [])

    # Repair merchant names from older generic-retailer runs and remove generic
    # links that are now explicitly classified as marketplace/comparison/review
    # destinations. Those should never survive as purchase options.
    cleaned_links: list[dict[str, Any]] = []
    config_blocked: list[str] = []
    for record in payload.get("links") or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("merchant_slug") or "") == "generic_retailer":
            domain = normalized_domain(str(record.get("domain") or ""))
            if not domain:
                domain = normalized_domain(
                    urlsplit(str(record.get("url") or "")).hostname or ""
                )
            if domain and generic_domain_blocked(domain, config_blocked):
                continue
            if domain:
                record["merchant_name"] = generic_merchant_name(domain)
        cleaned_links.append(record)
    payload["links"] = cleaned_links

    return payload


def product_key(brand: str, product_name: str) -> str:
    return f"{normalize_text(brand)}::{normalize_text(product_name)}"


def record_key(record: dict[str, Any]) -> str:
    return product_key(
        str(record.get("brand") or ""),
        str(record.get("product_name") or ""),
    )


def link_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        record_key(record),
        str(record.get("merchant_slug") or ""),
        canonicalize_url(str(record.get("url") or "")),
    )


def learned_official_domains_from_links(
    records: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Reuse already-verified official brand domains as a free search prior."""
    learned: dict[str, set[str]] = {}
    for record in records:
        if str(record.get("verification_status") or "") != "verified":
            continue
        if str(record.get("link_type") or "") != "official_brand":
            continue
        brand_key = normalize_text(str(record.get("brand") or ""))
        domain = normalized_domain(str(record.get("domain") or ""))
        if not domain:
            url = str(record.get("url") or "")
            domain = normalized_domain(urlsplit(url).hostname or "")
        if brand_key and domain:
            learned.setdefault(brand_key, set()).add(domain)
    return {key: sorted(values) for key, values in learned.items()}


def dedupe_links(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        identity = link_identity(record)
        if not all(identity):
            continue
        previous = best.get(identity)
        if previous is None:
            best[identity] = record
            continue
        if float(record.get("match_confidence") or 0.0) >= float(
            previous.get("match_confidence") or 0.0
        ):
            merged = dict(previous)
            merged.update({k: v for k, v in record.items() if v is not None})
            best[identity] = merged
    return list(best.values())


def write_output(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["schema_version"] = 2
    payload["generated_at"] = utc_now_iso()
    payload["verified_link_count"] = len(payload.get("links") or [])
    payload["unresolved_product_count"] = len(payload.get("unresolved") or [])
    payload["skipped_product_count"] = len(payload.get("skipped") or [])
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_products(database: Path, product_id: int | None) -> list[dict[str, Any]]:
    """Load catalogue products with YouTube + social visibility signals.

    Review-only and social-only products are deliberately treated symmetrically:
    ranking is based first on influencer coverage and total content evidence, not
    on whether the evidence happened to come from a YouTube transcript.
    """
    if not database.exists():
        raise FileNotFoundError(f"SQLite veritabanı bulunamadı: {database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        where = "WHERE p.id = ?" if product_id is not None else ""
        params: tuple[Any, ...] = (product_id,) if product_id is not None else ()
        rows = connection.execute(
            f"""
            WITH product_stats AS (
                SELECT
                    p.id,
                    p.brand,
                    p.product_name,
                    p.category,
                    (
                        SELECT COUNT(*)
                        FROM product_mentions pm
                        WHERE pm.product_id = p.id
                          AND pm.status = 'approved'
                    ) AS review_count,
                    (
                        SELECT COUNT(*)
                        FROM social_post_products spp
                        WHERE spp.product_id = p.id
                          AND spp.status = 'approved'
                    ) AS social_post_count,
                    (
                        SELECT COUNT(DISTINCT influencer_id)
                        FROM (
                            SELECT pm_i.influencer_id AS influencer_id
                            FROM product_mentions pm_i
                            WHERE pm_i.product_id = p.id
                              AND pm_i.status = 'approved'
                            UNION ALL
                            SELECT sp_i.influencer_id AS influencer_id
                            FROM social_post_products spp_i
                            JOIN social_posts sp_i ON sp_i.id = spp_i.social_post_id
                            WHERE spp_i.product_id = p.id
                              AND spp_i.status = 'approved'
                        )
                    ) AS influencer_count
                FROM products p
                {where}
            )
            SELECT
                id, brand, product_name, category,
                review_count, social_post_count, influencer_count,
                (review_count + social_post_count) AS total_content_count
            FROM product_stats
            ORDER BY
                CASE WHEN (review_count + social_post_count) > 0 THEN 0 ELSE 1 END,
                influencer_count DESC,
                total_content_count DESC,
                social_post_count DESC,
                review_count DESC,
                brand COLLATE NOCASE,
                product_name COLLATE NOCASE
            """,
            params,
        ).fetchall()
        return [
            {
                "product_id": int(row["id"]),
                "brand": str(row["brand"]),
                "product_name": str(row["product_name"]),
                "category": str(row["category"] or ""),
                "review_count": int(row["review_count"] or 0),
                "social_post_count": int(row["social_post_count"] or 0),
                "influencer_count": int(row["influencer_count"] or 0),
                "total_content_count": int(row["total_content_count"] or 0),
                "social_only": (
                    int(row["review_count"] or 0) == 0
                    and int(row["social_post_count"] or 0) > 0
                ),
            }
            for row in rows
        ]
    finally:
        connection.close()


def load_database_verified_links(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchase_links'"
        ).fetchone()
        if table is None:
            return []
        rows = connection.execute(
            """
            SELECT
                p.brand,
                p.product_name,
                pl.merchant_name,
                pl.merchant_slug,
                pl.link_type,
                pl.url,
                pl.domain,
                pl.verification_status,
                pl.match_confidence,
                pl.source_title,
                pl.verified_at,
                pl.checked_at,
                pl.stock_status,
                pl.stock_checked_at
            FROM purchase_links pl
            JOIN products p ON p.id = pl.product_id
            WHERE pl.verification_status = 'verified'
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def sync_links_to_database(
    *,
    database: Path,
    schema_path: Path,
    output_path: Path,
    links: list[dict[str, Any]],
) -> int:
    """Incrementally upsert verified links. Existing rows are never bulk-deleted."""
    if not schema_path.exists():
        raise FileNotFoundError(f"SQL şema dosyası bulunamadı: {schema_path}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        with connection:
            # Remove generic links from domains that v13 explicitly classifies as
            # marketplace/comparison/review destinations. This cleans up rows
            # written by the more permissive v11/v12 validators.
            for blocked_domain in sorted(GENERIC_RETAILER_BLOCKED_DOMAINS):
                connection.execute(
                    """
                    DELETE FROM purchase_links
                    WHERE merchant_slug = 'generic_retailer'
                      AND (domain = ? OR domain LIKE ?)
                    """,
                    (blocked_domain, f"%.{blocked_domain}"),
                )

            imported = 0
            for record in links:
                if record.get("verification_status") != "verified":
                    continue
                product = connection.execute(
                    """
                    SELECT id FROM products
                    WHERE normalized_brand = ?
                      AND normalized_product_name = ?
                    """,
                    (
                        normalize_database_identity(str(record.get("brand") or "")),
                        normalize_database_identity(
                            str(record.get("product_name") or "")
                        ),
                    ),
                ).fetchone()
                if product is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO purchase_links (
                        product_id,
                        merchant_name,
                        merchant_slug,
                        link_type,
                        url,
                        domain,
                        verification_status,
                        match_confidence,
                        source_title,
                        verified_at,
                        checked_at,
                        stock_status,
                        stock_checked_at,
                        source_file,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(product_id, merchant_slug, url) DO UPDATE SET
                        merchant_name = excluded.merchant_name,
                        link_type = excluded.link_type,
                        domain = excluded.domain,
                        verification_status = excluded.verification_status,
                        match_confidence = excluded.match_confidence,
                        source_title = excluded.source_title,
                        verified_at = COALESCE(excluded.verified_at, purchase_links.verified_at),
                        checked_at = COALESCE(excluded.checked_at, purchase_links.checked_at),
                        stock_status = CASE
                            WHEN excluded.stock_status <> 'unknown'
                                THEN excluded.stock_status
                            ELSE purchase_links.stock_status
                        END,
                        stock_checked_at = COALESCE(
                            excluded.stock_checked_at,
                            purchase_links.stock_checked_at
                        ),
                        source_file = excluded.source_file,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        int(product["id"]),
                        record["merchant_name"],
                        record["merchant_slug"],
                        record["link_type"],
                        record["url"],
                        record["domain"],
                        record["verification_status"],
                        record.get("match_confidence"),
                        record.get("source_title"),
                        record.get("verified_at"),
                        record.get("checked_at"),
                        record.get("stock_status") or "unknown",
                        record.get("stock_checked_at"),
                        str(output_path),
                    ),
                )
                imported += 1
        return imported
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Purchase-link enrichment v14: canonical family priority, simple DDGS validation, "
            "then one capped OpenAI exact-page fallback."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--search-context",
        choices=("low", "medium", "high"),
        default="high",
        help="OpenAI fallback web-search context size.",
    )
    parser.add_argument("--product-id", type=int)
    parser.add_argument(
        "--social-only",
        action="store_true",
        help="Only target social-only products; canonical family context is still loaded.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum canonical product families to process in this run.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--retry-unresolved",
        action="store_true",
        help="Retry current-validator no_exact_link records.",
    )
    parser.add_argument(
        "--official-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Compatibility flag. v14 searches trusted retailers + official site + generic retailer in one OpenAI request."
        ),
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=2,
        help="Maximum web-search tool calls inside one OpenAI request.",
    )
    parser.add_argument(
        "--max-openai-calls",
        type=int,
        default=DEFAULT_MAX_OPENAI_CALLS,
        help=(
            "Hard cap on OpenAI requests for this run. DDGS/family-cache work does "
            "not count toward the cap. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--no-openai",
        action="store_true",
        help="Run only family-cache + DDGS/Python verification; spend zero OpenAI tokens.",
    )
    parser.add_argument("--ddgs-results", type=int, default=DEFAULT_DDGS_RESULTS)
    parser.add_argument("--ddgs-region", default=DEFAULT_DDGS_REGION)
    parser.add_argument(
        "--min-search-quality",
        type=int,
        default=DEFAULT_MIN_SEARCH_QUALITY,
        help="Compatibility option; v14 uses a binary searchable/not-searchable gate.",
    )
    parser.add_argument(
        "--min-openai-quality",
        type=int,
        default=DEFAULT_MIN_OPENAI_QUALITY,
        help=(
            "Compatibility option; v14 sends every searchable family to OpenAI when DDGS fails and budget remains."
        ),
    )
    parser.add_argument(
        "--include-not-searchable",
        action="store_true",
        help="Bypass search-quality filters for a manual controlled retry.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show canonical families, ranking and skips; make no network/API/DB writes.",
    )
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--no-db-sync",
        action="store_true",
        help="Write JSON but do not upsert verified links into SQLite.",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not args.dry_run and not args.no_openai and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY bulunamadı. Sıfır-token mod için --no-openai kullanın."
        )
    if not 0 <= args.min_search_quality <= 100:
        raise ValueError("--min-search-quality 0..100 arasında olmalı.")
    if not 0 <= args.min_openai_quality <= 100:
        raise ValueError("--min-openai-quality 0..100 arasında olmalı.")
    if args.max_openai_calls < 0:
        raise ValueError("--max-openai-calls negatif olamaz.")
    if not args.dry_run and DDGS is None:
        raise RuntimeError(
            "ddgs paketi kurulu değil. Ücretli aramaya free-first katmanı olmadan "
            "başlamamak için işlem durduruldu. `pip install -r requirements.txt` çalıştırın."
        )

    config = load_config(args.config)
    retailers = retailer_config(config)
    retailer_domains = all_retailer_domains(retailers)

    # Load the whole catalogue even for --product-id so a vague/shade variant can use
    # a stronger sibling as its canonical search identity.
    all_products = load_products(args.database, None)
    families = prepare_product_families(all_products)
    output = load_existing_output(args.output)
    json_links = [item for item in output.get("links") or [] if isinstance(item, dict)]
    database_links = load_database_verified_links(args.database)
    links = dedupe_links(database_links + json_links)
    unresolved = [
        item for item in output.get("unresolved") or [] if isinstance(item, dict)
    ]
    previous_unresolved = {record_key(item): item for item in unresolved}
    verified_keys = {record_key(item) for item in links}
    completed_no_match_keys = {
        record_key(item)
        for item in unresolved
        if item.get("status") == "no_exact_link"
        and int(item.get("validator_version") or 0) >= VALIDATOR_VERSION
    }

    cached_by_family = family_link_cache(links)
    family_cache_added: list[dict[str, Any]] = []
    candidate_families: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    raw_candidate_products = 0

    for family in families:
        members = list(family.get("family_members") or [])
        target_members: list[dict[str, Any]] = []
        for member in members:
            if args.product_id is not None and int(member["product_id"]) != args.product_id:
                continue
            if args.social_only and not member.get("social_only"):
                continue
            key = product_key(member["brand"], member["product_name"])
            if not args.force:
                if key in verified_keys:
                    continue
                if not args.retry_unresolved:
                    previous = previous_unresolved.get(key)
                    # In free-only batching, a family already searched deterministically
                    # under the current validator is complete for this stage. Excluding
                    # it here lets repeated --no-openai --limit N runs advance through
                    # the catalogue instead of selecting the same top-N families again.
                    if args.no_openai and deterministic_stage_completed(previous):
                        continue
                    if key in completed_no_match_keys:
                        continue
            target_members.append(member)

        if not target_members:
            continue
        raw_candidate_products += len(target_members)
        family = dict(family)
        family["target_members"] = target_members

        # Reuse a verified sibling URL before any web search. This is intentionally
        # restricted to the conservative family signature above.
        known_family_links = cached_by_family.get(str(family["family_key"]), [])
        if known_family_links and not args.force:
            propagated = propagate_family_links(
                known_family_links,
                target_members,
                method="family_cache",
            )
            family_cache_added.extend(propagated)
            for record in propagated:
                verified_keys.add(record_key(record))
            continue

        eligible, reason = product_search_eligibility(
            family,
            min_quality=args.min_search_quality,
        )
        if eligible or args.include_not_searchable:
            candidate_families.append(family)
            continue
        for member in target_members:
            skipped.append(
                {
                    "product_id": member["product_id"],
                    "brand": member["brand"],
                    "product_name": member["product_name"],
                    "category": member.get("category") or "",
                    "family_key": family["family_key"],
                    "canonical_search_brand": family["search_brand"],
                    "canonical_search_product_name": family["search_product_name"],
                    "search_quality": family["search_quality"],
                    "status": "not_searchable",
                    "reason": reason,
                    "filter_version": 2,
                }
            )

    if family_cache_added:
        links = dedupe_links(links + family_cache_added)
        cached_by_family = family_link_cache(links)

    # prepare_product_families already sorts by priority; this explicit sort makes the
    # cost policy obvious and stable after filtering.
    candidate_families.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            -int(item["review_count"]),
            -int(item["social_post_count"]),
            normalize_text(str(item["search_brand"])),
            normalize_text(str(item["search_product_name"])),
        )
    )
    selected = (
        candidate_families[: args.limit]
        if args.limit is not None
        else candidate_families
    )

    print("=" * 96)
    print("PURCHASE LINK ENRICHER v14 · SIMPLE URL/TOKEN VALIDATION + ONE OPENAI FALLBACK")
    print("=" * 96)
    print(f"Model:                    {args.model if not args.no_openai else 'disabled'}")
    print(f"DB verified cache:        {len(database_links)} link")
    print(f"JSON verified cache:      {len(json_links)} link")
    print(f"Linksiz aday ürün:        {raw_candidate_products}")
    print(f"Canonical family cache:   {len(family_cache_added)} ücretsiz link kaydı")
    print(f"Filtre ile atlanan ürün:  {len(skipped)}")
    print(f"Searchable family:        {len(candidate_families)}")
    print(f"Bu run family:            {len(selected)}")
    print(f"DDGS free-first:          {'AÇIK' if DDGS is not None else 'PAKET YOK / SKIP'}")
    print(f"OpenAI hard cap:          {0 if args.no_openai else args.max_openai_calls}")
    print("Official site search:     OpenAI ana aramasına dahil")
    print("Search gate:              binary searchable / not-searchable")
    print("OpenAI gate:              DDGS çözemeyen searchable family")
    print(f"Search context/tool cap:  {args.search_context}/{args.max_tool_calls}")
    print("Öncelik:                  influencer > review > social")

    if args.dry_run:
        print("\nDRY RUN — network/API/JSON/SQLite yazımı yapılmayacak.")
        if family_cache_added:
            print("\nFamily cache ile ücretsiz çözülebileceklerden örnek:")
            for item in family_cache_added[:12]:
                print(
                    f"  CACHE  {item['brand']} — {item['product_name']} -> "
                    f"{item['merchant_name']}"
                )
        if skipped:
            print("\nFiltrelenenlerden örnek:")
            for item in skipped[:18]:
                print(
                    f"  SKIP {item['product_id']:>5}  {item['brand']} — "
                    f"{item['product_name']}  [{item['reason']}]"
                )
        print("\nArama sırası:")
        for index, family in enumerate(selected, start=1):
            members = list(family.get("target_members") or [])
            canonicalized = any(
                normalize_text(str(member["product_name"]))
                != normalize_text(str(family["search_product_name"]))
                or normalize_text(clean_brand_for_search(str(member["brand"])))
                != normalize_text(str(family["search_brand"]))
                for member in members
            )
            print(
                f"  {index:>3}. score={family['priority_score']:.1f} "
                f"family={len(members)}/{family['family_size']}  FREE+AI  "
                f"{family['search_brand']} — {family['search_product_name']} "
                f"[YT={family['review_count']}, social={family['social_post_count']}, "
                f"infl={family['influencer_count']}]"
                + ("  [canonicalized]" if canonicalized or family['family_size'] > 1 else "")
            )
            if family["family_size"] > 1:
                aliases = "; ".join(
                    f"{member['brand']} — {member['product_name']}"
                    for member in family.get("family_members") or []
                )
                print(f"       aliases: {aliases}")
        return

    # Persist free family-cache propagation even if no network search is needed.
    if not selected:
        output["links"] = links
        output["skipped"] = skipped
        output["validator_version"] = VALIDATOR_VERSION
        output["search_strategy"] = "family_cache_then_simple_ddgs_then_one_openai"
        write_output(args.output, output)
        if not args.no_db_sync:
            count = sync_links_to_database(
                database=args.database,
                schema_path=args.schema,
                output_path=args.output,
                links=links,
            )
            print(f"SQLite incremental upsert: {count}")
        return

    client = None
    if not args.no_openai:
        from openai import OpenAI
        client = OpenAI(max_retries=args.max_retries)

    learned_official_domains = learned_official_domains_from_links(links)
    usage_total: dict[str, int] = {
        "api_requests": 0,
        "web_search_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    openai_calls = 0
    ddgs_families = 0
    ddgs_hits = 0
    family_hits = 0
    run_no_exact = 0
    run_deferred = 0
    run_errors = 0

    for index, family in enumerate(selected, start=1):
        brand = str(family["search_brand"])
        product_name = str(family["search_product_name"])
        members = list(family.get("target_members") or [])
        member_keys = {product_key(m["brand"], m["product_name"]) for m in members}
        print(
            f"[{index}/{len(selected)}] score={family['priority_score']:.1f} "
            f"{brand} — {product_name} "
            f"(family targets={len(members)})"
        )

        unresolved = [item for item in unresolved if record_key(item) not in member_keys]
        rejected_reasons: list[str] = []
        verified_search_identity: list[dict[str, Any]] = []
        openai_was_attempted = False
        deferred_for_budget = False

        try:
            # If this exact family already completed a DDGS-only pass under the current
            # validator, do not repeat free search; move directly to paid fallback.
            current_det_done = bool(members) and all(
                (
                    (prev := previous_unresolved.get(product_key(m["brand"], m["product_name"])))
                    is not None
                    and prev.get("status") in {"deterministic_unresolved", "deferred_openai_budget"}
                    and int(prev.get("validator_version") or 0) >= VALIDATOR_VERSION
                )
                for m in members
            )

            if not current_det_done:
                ddgs_families += 1
                verified_search_identity, ddgs_notes = deterministic_search_results(
                    brand=brand,
                    product_name=product_name,
                    retailers=retailers,
                    config=config,
                    max_results=args.ddgs_results,
                    region=args.ddgs_region,
                )
                rejected_reasons.extend(ddgs_notes)
                if verified_search_identity:
                    ddgs_hits += 1
                    print("    DDGS/PYTHON: exact link doğrulandı; OpenAI kullanılmadı")
            else:
                print("    DDGS: current-validator unresolved cache -> tekrar arama yok")

            known_domains = sorted(
                set(configured_brand_domains(config, brand))
                | set(learned_official_domains.get(normalize_text(brand), []))
            )

            # v14: one paid request only. FREE_ONLY identities are still tried by
            # DDGS but do not consume OpenAI calls until they are canonicalized.
            paid_eligible = int(family.get("search_quality") or 0) >= args.min_openai_quality
            if not verified_search_identity and not args.no_openai and paid_eligible:
                if openai_calls < args.max_openai_calls:
                    assert client is not None
                    openai_was_attempted = True
                    openai_calls += 1
                    result, sources, response_id, usage = search_with_schema(
                        client,
                        model=args.model,
                        prompt=combined_prompt(brand, product_name, retailers, known_domains),
                        allowed_domains=None,
                        search_context=args.search_context,
                        max_tool_calls=args.max_tool_calls,
                    )
                    add_usage(usage_total, usage)
                    verified_search_identity, rejected = validate_openai_candidates(
                        result=result,
                        sources=sources,
                        config=config,
                        retailers=retailers,
                        brand=brand,
                        product_name=product_name,
                        response_id=response_id,
                    )
                    rejected_reasons.extend(rejected)
                    print(f"    OPENAI: {openai_calls}/{args.max_openai_calls}")
                else:
                    deferred_for_budget = True
            elif not verified_search_identity and not args.no_openai and not paid_eligible:
                print("    OPENAI SKIP: FREE_ONLY ürün kimliği")

            if verified_search_identity:
                propagated = propagate_family_links(
                    verified_search_identity,
                    members,
                    method=(
                        "family_search_ddgs"
                        if any(
                            str(item.get("verification_method") or "").startswith("ddgs_")
                            for item in verified_search_identity
                        )
                        else "family_search_openai"
                    ),
                )
                links = dedupe_links(links + propagated)
                for record in propagated:
                    verified_keys.add(record_key(record))
                family_hits += 1
                merchants = ", ".join(
                    sorted({str(item["merchant_name"]) for item in propagated})
                )
                print(f"    DOĞRULANDI: {merchants} -> {len(members)} product row")
            else:
                if args.no_openai or not paid_eligible:
                    status = "deterministic_unresolved"
                elif deferred_for_budget or not openai_was_attempted:
                    status = "deferred_openai_budget"
                    run_deferred += len(members)
                else:
                    status = "no_exact_link"
                    run_no_exact += len(members)
                for member in members:
                    unresolved.append(
                        {
                            "brand": member["brand"],
                            "product_name": member["product_name"],
                            "status": status,
                            "validator_version": VALIDATOR_VERSION,
                            "checked_at": utc_now_iso(),
                            "family_key": family["family_key"],
                            "canonical_search_brand": brand,
                            "canonical_search_product_name": product_name,
                            "search_quality": family["search_quality"],
                            "priority_score": round(float(family["priority_score"]), 2),
                            "reason": "; ".join(rejected_reasons[-8:])
                            or (
                                "OpenAI fallback bütçe sınırına ulaşıldı."
                                if status == "deferred_openai_budget"
                                else "Kesin ürün sayfası bulunamadı."
                            ),
                        }
                    )
                print(f"    LİNK YOK: {status}")
        except Exception as error:
            run_errors += len(members)
            for member in members:
                unresolved.append(
                    {
                        "brand": member["brand"],
                        "product_name": member["product_name"],
                        "status": "error",
                        "validator_version": VALIDATOR_VERSION,
                        "checked_at": utc_now_iso(),
                        "family_key": family["family_key"],
                        "canonical_search_brand": brand,
                        "canonical_search_product_name": product_name,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
            print(f"    HATA: {type(error).__name__}: {error}")

        output["links"] = links
        output["unresolved"] = unresolved
        output["skipped"] = skipped
        output["model"] = None if args.no_openai else args.model
        output["validator_version"] = VALIDATOR_VERSION
        output["retailers"] = [
            str(item.get("display_name") or "") for item in retailers
        ]
        output["run_usage"] = usage_total
        output["openai_calls_this_run"] = openai_calls
        output["max_openai_calls"] = 0 if args.no_openai else args.max_openai_calls
        output["ddgs_families_this_run"] = ddgs_families
        output["ddgs_hits_this_run"] = ddgs_hits
        output["family_cache_links_this_run"] = len(family_cache_added)
        output["search_strategy"] = "family_cache_then_simple_ddgs_then_one_openai"
        write_output(args.output, output)
        if args.delay > 0 and index < len(selected):
            time.sleep(args.delay)

    if not args.no_db_sync:
        count = sync_links_to_database(
            database=args.database,
            schema_path=args.schema,
            output_path=args.output,
            links=links,
        )
    else:
        count = 0

    print()
    print("=" * 96)
    print("İŞLEM TAMAMLANDI")
    print("=" * 96)
    print(f"Family cache link row:    {len(family_cache_added)}")
    print(f"DDGS family / hit:        {ddgs_families} / {ddgs_hits}")
    print(f"Doğrulanan family:        {family_hits}")
    print(f"OpenAI request:           {openai_calls}/{0 if args.no_openai else args.max_openai_calls}")
    print(f"Bu run no_exact ürün:     {run_no_exact}")
    print(f"Bütçeye ertelenen ürün:   {run_deferred}")
    print(f"Bu run hata ürün:         {run_errors}")
    print(f"Filtre ile atlanan ürün:  {len(skipped)}")
    print(f"Toplam verified link:     {len(links)}")
    print(f"Web-search tool call:     {usage_total['web_search_calls']}")
    print(f"Input tokens:             {usage_total['input_tokens']}")
    print(f"Cached input tokens:      {usage_total['cached_tokens']}")
    print(f"Output tokens:            {usage_total['output_tokens']}")
    print(f"Reasoning tokens:         {usage_total['reasoning_tokens']}")
    print(f"Total tokens:             {usage_total['total_tokens']}")
    print(f"JSON:                     {args.output}")
    if not args.no_db_sync:
        print(f"SQLite upsert rows:        {count}")


if __name__ == "__main__":
    main()
