from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image


DEFAULT_DATABASE = Path("data/database/skinfluencer.sqlite")
DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "storage" / "schema.sql"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "product_images"
DEFAULT_TIMEOUT = 18.0
DEFAULT_HTTP_RETRIES = 3
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1200
MIN_IMAGE_DIMENSION = 120
DEFAULT_OPENAI_MODEL = (
    os.getenv("OPENAI_IMAGE_MODEL")
    or os.getenv("OPENAI_MODEL")
    or "gpt-5.6-luna"
)
OPENAI_BLOCKED_IMAGE_DOMAINS = [
    "pinterest.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "yandex.com",
    "yandex.com.tr",
    "whatsinskincare.com",
    "incidecoder.com",
    "cosdna.com",
]


OPENAI_PAGE_DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "page_title": {"type": "string"},
                    "site_name": {"type": "string"},
                    "exact_match": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["url", "page_title", "site_name", "exact_match", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
        "no_match_reason": {"type": "string"},
    },
    "required": ["candidates", "no_match_reason"],
    "additionalProperties": False,
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0 Safari/537.36 Skinfluencer/1.0"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_domain(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().removeprefix("www.")


def ensure_schema(database: Path, schema_path: Path) -> None:
    if not database.exists():
        raise FileNotFoundError(f"SQLite veritabanı bulunamadı: {database}")
    if not schema_path.exists():
        raise FileNotFoundError(f"SQL şema dosyası bulunamadı: {schema_path}")
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def load_candidates(
    database: Path,
    *,
    product_id: int | None,
    force: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        params: list[Any] = []
        filters = ["pl.verification_status = 'verified'"]
        if product_id is not None:
            filters.append("p.id = ?")
            params.append(product_id)
        if not force:
            filters.append(
                "NOT EXISTS ("
                "SELECT 1 FROM product_images pi "
                "WHERE pi.product_id = p.id AND pi.verification_status = 'verified'"
                ")"
            )
        where_sql = " AND ".join(filters)
        rows = connection.execute(
            f"""
            SELECT
                p.id AS product_id,
                p.brand,
                p.product_name,
                pl.id AS purchase_link_id,
                pl.merchant_name,
                pl.merchant_slug,
                pl.link_type,
                pl.url,
                pl.domain,
                pl.match_confidence,
                pl.stock_status,
                (
                    SELECT COUNT(*)
                    FROM product_mentions pm
                    WHERE pm.product_id = p.id AND pm.status = 'approved'
                ) AS review_count,
                (
                    SELECT COUNT(*)
                    FROM social_post_products spp
                    WHERE spp.product_id = p.id AND spp.status = 'approved'
                ) AS social_post_count
            FROM products p
            JOIN purchase_links pl ON pl.product_id = p.id
            WHERE {where_sql}
            ORDER BY
                review_count DESC,
                social_post_count DESC,
                p.id,
                CASE WHEN pl.link_type = 'official_brand' THEN 0 ELSE 1 END,
                CASE lower(pl.merchant_slug)
                    WHEN 'sephora' THEN 1
                    WHEN 'gratis' THEN 2
                    WHEN 'watsons' THEN 3
                    WHEN 'rossmann' THEN 4
                    WHEN 'korendy' THEN 5
                    WHEN 'boyner' THEN 6
                    WHEN 'trendyol' THEN 7
                    ELSE 20
                END,
                COALESCE(pl.match_confidence, 0) DESC,
                pl.id
            """,
            params,
        ).fetchall()

        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            pid = int(row["product_id"])
            item = grouped.setdefault(
                pid,
                {
                    "product_id": pid,
                    "brand": str(row["brand"]),
                    "product_name": str(row["product_name"]),
                    "review_count": int(row["review_count"] or 0),
                    "social_post_count": int(row["social_post_count"] or 0),
                    "links": [],
                },
            )
            item["links"].append(
                {
                    "purchase_link_id": int(row["purchase_link_id"]),
                    "merchant_name": str(row["merchant_name"]),
                    "merchant_slug": str(row["merchant_slug"]),
                    "link_type": str(row["link_type"]),
                    "url": str(row["url"]),
                    "domain": str(row["domain"]),
                    "match_confidence": row["match_confidence"],
                    "stock_status": str(row["stock_status"]),
                }
            )

        products = list(grouped.values())
        if limit is not None:
            products = products[:limit]
        return products
    finally:
        connection.close()


def parse_json_ld(soup: BeautifulSoup) -> list[Any]:
    payloads: list[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = tag.string or tag.get_text(" ", strip=True)
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return payloads


def iter_json_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def is_product_node(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, str):
        return node_type.casefold() == "product"
    if isinstance(node_type, list):
        return any(str(item).casefold() == "product" for item in node_type)
    return False


def first_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            result = first_url(item)
            if result:
                return result
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "thumbnailUrl"):
            result = first_url(value.get(key))
            if result:
                return result
    return ""


def normalize_availability(value: Any) -> str:
    text = str(value or "").casefold().replace("_", "").replace("-", "")
    compact = "".join(ch for ch in text if ch.isalnum())
    if not compact:
        return "unknown"
    if "outofstock" in compact or "soldout" in compact or "discontinued" in compact:
        return "out_of_stock"
    if "limitedavailability" in compact or "limitedstock" in compact or "lowstock" in compact:
        return "limited_stock"
    if "instock" in compact:
        return "in_stock"
    return "unknown"


def extract_stock_status(soup: BeautifulSoup, json_ld_payloads: list[Any]) -> str:
    for payload in json_ld_payloads:
        for node in iter_json_nodes(payload):
            if not is_product_node(node):
                continue
            offers = node.get("offers")
            offer_nodes = list(iter_json_nodes(offers)) if offers is not None else []
            for offer in offer_nodes:
                status = normalize_availability(offer.get("availability"))
                if status != "unknown":
                    return status

    selectors = [
        ("meta", {"property": "product:availability"}),
        ("meta", {"property": "og:availability"}),
        ("meta", {"name": "availability"}),
        ("link", {"itemprop": "availability"}),
        ("meta", {"itemprop": "availability"}),
    ]
    for tag_name, attrs in selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag is None:
            continue
        value = tag.get("content") or tag.get("href") or tag.get_text(" ", strip=True)
        status = normalize_availability(value)
        if status != "unknown":
            return status
    return "unknown"




_IMAGE_EXT_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)(?:$|[?#])", re.I)
_BG_URL_RE = re.compile(r"url\((?:['\"])?([^)'\"]+)(?:['\"])?\)", re.I)
_SCRIPT_IMAGE_RE = re.compile(
    r"https?:\\?/\\?/[^\"'\\s<>]+?\.(?:avif|jpe?g|png|webp)(?:\\?[^\"'\\s<>]*)?",
    re.I,
)

_NEGATIVE_IMAGE_TERMS = {
    "logo", "icon", "sprite", "favicon", "avatar", "flag", "payment", "footer",
    "header", "menu", "newsletter", "social", "facebook", "instagram", "youtube",
    "tiktok", "badge", "rating", "star", "placeholder", "loader", "spinner",
    "ingredient", "ingredients", "glycerin", "compo", "composition", "ecobiology",
    "routine", "article", "blog", "campaign", "promo", "promotion", "banner",
    "face", "neck", "hand", "model", "lifestyle", "before-after", "before_after",
    "skin-schema", "skin_schema", "technology", "technology-icon", "dermatologist",
    "instruction", "instructions", "advice", "usage", "application", "clinical",
    "study", "efficacy", "efficiency", "woman", "women", "girl", "portrait",
    "visuel", "visual", "editorial", "story", "how-to", "how_to",
}

_POSITIVE_CONTAINER_TERMS = {
    "product", "product-image", "product_image", "product-media", "product_media",
    "product-gallery", "product_gallery", "gallery", "carousel", "pdp", "packshot",
    "primary", "main-image", "main_image", "hero-product", "hero_product",
}


def _normalize_search_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _meaningful_tokens(*values: str) -> list[str]:
    stop = {
        "the", "and", "with", "for", "from", "spf", "cream", "serum", "lotion",
        "gel", "mask", "toner", "essence", "foundation", "cushion", "cleanser",
        "bakim", "krem", "urun", "product", "ml", "gr", "g", "oz",
    }
    tokens: list[str] = []
    for value in values:
        for token in _normalize_search_text(value).split():
            if len(token) >= 3 and token not in stop and token not in tokens:
                tokens.append(token)
    return tokens


def _parse_srcset(value: str) -> list[tuple[str, float]]:
    """Return srcset URLs with a small quality hint from width/density descriptors."""
    out: list[tuple[str, float]] = []
    if not value:
        return out
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        bits = item.rsplit(None, 1)
        url = bits[0].strip()
        hint = 0.0
        if len(bits) == 2:
            descriptor = bits[1].strip().lower()
            try:
                if descriptor.endswith("w"):
                    width = float(descriptor[:-1])
                    hint = min(width / 250.0, 8.0)
                elif descriptor.endswith("x"):
                    density = float(descriptor[:-1])
                    hint = min(density * 2.0, 6.0)
            except ValueError:
                pass
        out.append((url, hint))
    return out


def _tag_context(tag: Any) -> str:
    parts: list[str] = []
    current = tag
    depth = 0
    while current is not None and depth < 4:
        if getattr(current, "attrs", None):
            for key in ("id", "class", "alt", "title", "aria-label", "data-testid"):
                value = current.attrs.get(key)
                if isinstance(value, list):
                    parts.extend(str(x) for x in value)
                elif value:
                    parts.append(str(value))
        current = getattr(current, "parent", None)
        depth += 1
    return " ".join(parts)


def _image_relevance_score(
    *,
    url: str,
    source_type: str,
    context: str,
    brand: str,
    product_name: str,
    quality_hint: float = 0.0,
) -> float:
    source_scores = {
        "json_ld": 80.0,
        "itemprop_image": 72.0,
        "product_img": 68.0,
        "picture_srcset": 60.0,
        "img_srcset": 58.0,
        "lazy_img": 55.0,
        "image_src": 52.0,
        "og_image": 45.0,
        "twitter_image": 40.0,
        "background_image": 30.0,
        "generic_img": 20.0,
        "embedded_script": 18.0,
        "openai_image_search": 78.0,
        "openai_image_thumbnail": 58.0,
    }
    score = source_scores.get(source_type, 15.0) + quality_hint
    combined = _normalize_search_text(f"{url} {context}")
    url_norm = _normalize_search_text(url)

    brand_tokens = _meaningful_tokens(brand)
    product_tokens = _meaningful_tokens(product_name)
    all_tokens = list(dict.fromkeys(brand_tokens + product_tokens))
    matches = sum(1 for token in all_tokens if token in combined)
    score += min(matches * 7.0, 35.0)

    product_phrase = _normalize_search_text(product_name)
    if product_phrase and product_phrase in combined:
        score += 28.0
    brand_phrase = _normalize_search_text(brand)
    if brand_phrase and brand_phrase in combined:
        score += 10.0

    if any(term.replace("-", " ") in combined for term in _POSITIVE_CONTAINER_TERMS):
        score += 14.0
    if "/products/" in url.casefold() or "/product/" in url.casefold():
        score += 10.0
    if "media/catalog/product" in url.casefold():
        score += 14.0

    for term in _NEGATIVE_IMAGE_TERMS:
        term_norm = term.replace("-", " ").replace("_", " ")
        if term_norm in combined:
            score -= 35.0

    # Tiny thumbnails and tracking assets often advertise dimensions in the URL.
    if re.search(r"(?:^|[^0-9])(1x1|16x16|24x24|32x32|48x48|64x64)(?:[^0-9]|$)", url_norm):
        score -= 50.0
    return score


def _find_product_anchor_index(soup: BeautifulSoup, product_name: str) -> tuple[dict[int, int], int | None]:
    """Map tags to document order and locate the product title heading when possible."""
    tags = list(soup.find_all(True))
    tag_positions = {id(tag): idx for idx, tag in enumerate(tags)}
    target = _normalize_search_text(product_name)
    target_tokens = set(_meaningful_tokens(product_name))

    best: tuple[int, int] | None = None
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = _normalize_search_text(tag.get_text(" ", strip=True))
        if not text:
            continue
        score = 0
        if target and target in text:
            score += 100
        overlap = len(target_tokens.intersection(text.split()))
        score += overlap * 12
        if tag.name == "h1":
            score += 10
        if score <= 0:
            continue
        pos = tag_positions.get(id(tag), 0)
        if best is None or score > best[0]:
            best = (score, pos)
    return tag_positions, (best[1] if best else None)


def _dom_proximity_bonus(tag: Any, tag_positions: dict[int, int], anchor_index: int | None) -> float:
    if tag is None or anchor_index is None:
        return 0.0
    pos = tag_positions.get(id(tag))
    if pos is None:
        return 0.0
    distance = abs(pos - anchor_index)
    if distance <= 20:
        return 32.0
    if distance <= 50:
        return 20.0
    if distance <= 100:
        return 8.0
    if pos > anchor_index + 180:
        return -18.0
    return 0.0


def _resolution_preference_score(image: Image.Image) -> float | None:
    """Prefer crisp ecommerce images and reject thumbnail-sized candidates.

    The short side is the main quality gate because product packshots are often
    portrait-oriented. Returning ``None`` means the candidate is too small to
    be used at all.
    """
    short_side = min(int(image.width), int(image.height))
    area = int(image.width) * int(image.height)

    # Hard reject thumbnails. These were frequently winning because clean white
    # backgrounds produced a high packshot score despite poor display quality.
    if short_side < 300:
        return None

    if short_side < 500:
        score = -12.0
    elif short_side < 800:
        score = 2.0
    elif short_side < 1000:
        score = 18.0
    else:
        score = 28.0

    # Break ties between otherwise similar variants of the same product image.
    if area >= 1_500_000:
        score += 8.0
    elif area >= 1_000_000:
        score += 6.0
    elif area >= 640_000:
        score += 3.0

    return score


def _packshot_visual_score(image: Image.Image) -> float:
    """Lightweight visual heuristic: favor clean/neutral product-packshot backgrounds.

    This intentionally does not try to identify people. It only scores border/background
    uniformity and brightness, which is useful for ecommerce packshots.
    """
    sample = image.convert("RGB")
    sample.thumbnail((96, 96), Image.Resampling.BILINEAR)
    w, h = sample.size
    if w < 8 or h < 8:
        return 0.0

    px = sample.load()
    border = []
    thickness = max(1, min(w, h) // 10)
    for y in range(h):
        for x in range(w):
            if x < thickness or y < thickness or x >= w - thickness or y >= h - thickness:
                border.append(px[x, y])

    if not border:
        return 0.0

    bright_neutral = 0
    neutral = 0
    channel_values = [[], [], []]
    for r, g, b in border:
        brightness = (r + g + b) / 3.0
        chroma = max(r, g, b) - min(r, g, b)
        if chroma <= 24:
            neutral += 1
        if brightness >= 210 and chroma <= 28:
            bright_neutral += 1
        channel_values[0].append(r)
        channel_values[1].append(g)
        channel_values[2].append(b)

    n = len(border)
    bright_ratio = bright_neutral / n
    neutral_ratio = neutral / n

    def std(values: list[int]) -> float:
        mean = sum(values) / len(values)
        return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5

    border_std = sum(std(values) for values in channel_values) / 3.0

    score = bright_ratio * 42.0 + neutral_ratio * 10.0
    if bright_ratio >= 0.55:
        score += 18.0
    elif bright_ratio >= 0.30:
        score += 8.0
    if border_std <= 22:
        score += 10.0
    elif border_std >= 65:
        score -= 10.0

    ratio = max(image.width / image.height, image.height / image.width)
    if ratio <= 2.2:
        score += 3.0
    return score


def extract_ranked_image_candidates(
    soup: BeautifulSoup,
    json_ld_payloads: list[Any],
    page_url: str,
    *,
    brand: str = "",
    product_name: str = "",
) -> list[dict[str, Any]]:
    """Collect and score image candidates from structured, responsive and lazy markup."""
    candidates: dict[str, dict[str, Any]] = {}
    tag_positions, product_anchor_index = _find_product_anchor_index(soup, product_name)

    def add(
        value: Any,
        source_type: str,
        *,
        context: str = "",
        quality_hint: float = 0.0,
        tag: Any = None,
    ) -> None:
        url = first_url(value)
        if not url:
            return
        url = url.replace(r"\/", "/").strip().strip('"\'')
        resolved = urljoin(page_url, url).strip()
        parsed = urlsplit(resolved)
        if parsed.scheme not in {"http", "https"}:
            return
        score = _image_relevance_score(
            url=resolved,
            source_type=source_type,
            context=context,
            brand=brand,
            product_name=product_name,
            quality_hint=quality_hint,
        ) + _dom_proximity_bonus(tag, tag_positions, product_anchor_index)
        previous = candidates.get(resolved)
        if previous is None or score > float(previous["score"]):
            candidates[resolved] = {
                "url": resolved,
                "source_type": source_type,
                "score": score,
                "context": context,
            }

    # 1) Product JSON-LD.
    for payload in json_ld_payloads:
        for node in iter_json_nodes(payload):
            if not is_product_node(node):
                continue
            node_context = " ".join(str(node.get(k) or "") for k in ("name", "sku", "brand"))
            value = node.get("image")
            if isinstance(value, list):
                for item in value:
                    add(item, "json_ld", context=node_context)
            else:
                add(value, "json_ld", context=node_context)

    # 2) Explicit product/itemprop and social metadata.
    for tag in soup.find_all(attrs={"itemprop": "image"}):
        add(
            tag.get("content") or tag.get("href") or tag.get("src"),
            "itemprop_image",
            context=_tag_context(tag),
            tag=tag,
        )

    for tag in soup.find_all("link", rel=lambda value: value and "image_src" in str(value)):
        add(tag.get("href"), "image_src", context=_tag_context(tag), tag=tag)

    for attrs, source_type in [
        ({"property": "og:image"}, "og_image"),
        ({"property": "og:image:url"}, "og_image"),
        ({"name": "twitter:image"}, "twitter_image"),
        ({"property": "twitter:image"}, "twitter_image"),
    ]:
        for tag in soup.find_all("meta", attrs=attrs):
            add(tag.get("content"), source_type, context=_tag_context(tag), tag=tag)

    # 3) Every responsive/lazy image. Drupal, Shopify and many beauty sites use these.
    img_attrs = (
        "data-zoom-image", "data-large-image", "data-large_image", "data-original",
        "data-lazy-src", "data-lazy", "data-src", "data-image", "data-image-url", "src",
    )
    srcset_attrs = ("data-srcset", "data-lazy-srcset", "srcset")

    for tag in soup.find_all("img"):
        context = _tag_context(tag)
        classes = " ".join(str(x) for x in (tag.get("class") or []))
        identifier = f"{tag.get('id') or ''} {classes}".casefold()
        productish = any(term.replace("-", " ") in _normalize_search_text(identifier) for term in _POSITIVE_CONTAINER_TERMS)
        direct_source = "product_img" if productish else "generic_img"

        for attr in img_attrs:
            value = tag.get(attr)
            if value:
                source = "lazy_img" if attr.startswith("data-") and attr != "data-zoom-image" else direct_source
                add(value, source, context=context, tag=tag)
        for attr in srcset_attrs:
            for src_url, hint in _parse_srcset(str(tag.get(attr) or "")):
                add(src_url, "img_srcset", context=context, quality_hint=hint, tag=tag)

    for tag in soup.find_all("source"):
        context = _tag_context(tag)
        for attr in ("data-srcset", "data-lazy-srcset", "srcset"):
            for src_url, hint in _parse_srcset(str(tag.get(attr) or "")):
                add(src_url, "picture_srcset", context=context, quality_hint=hint, tag=tag)

    # 4) CSS background images on product-ish containers.
    for tag in soup.find_all(style=True):
        context = _tag_context(tag)
        context_norm = _normalize_search_text(context)
        if not any(term.replace("-", " ") in context_norm for term in _POSITIVE_CONTAINER_TERMS):
            continue
        for match in _BG_URL_RE.finditer(str(tag.get("style") or "")):
            add(match.group(1), "background_image", context=context, tag=tag)

    # 5) A cautious fallback for JS-rendered galleries that serialize full image URLs.
    # Only consider scripts mentioning the brand/product and only real image extensions.
    search_tokens = _meaningful_tokens(brand, product_name)
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text or len(text) > 2_000_000:
            continue
        norm = _normalize_search_text(text[:200_000])
        if search_tokens and not any(token in norm for token in search_tokens):
            continue
        script_text = text.replace(r"\/", "/")
        for match in _SCRIPT_IMAGE_RE.finditer(script_text):
            add(match.group(0), "embedded_script", context="embedded product gallery")

    ranked = sorted(
        candidates.values(),
        key=lambda item: (-float(item["score"]), item["url"]),
    )
    return ranked

def extract_image_candidates(
    soup: BeautifulSoup,
    json_ld_payloads: list[Any],
    page_url: str,
    *,
    brand: str = "",
    product_name: str = "",
) -> list[tuple[str, str]]:
    """Backward-compatible tuple view of the ranked image candidates."""
    return [
        (str(item["url"]), str(item["source_type"]))
        for item in extract_ranked_image_candidates(
            soup,
            json_ld_payloads,
            page_url,
            brand=brand,
            product_name=product_name,
        )
    ]


def extract_image_candidate(
    soup: BeautifulSoup,
    json_ld_payloads: list[Any],
    page_url: str,
    *,
    brand: str = "",
    product_name: str = "",
) -> tuple[str, str]:
    """Backward-compatible single-candidate helper."""
    candidates = extract_image_candidates(
        soup,
        json_ld_payloads,
        page_url,
        brand=brand,
        product_name=product_name,
    )
    return candidates[0] if candidates else ("", "")


def _response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        payload = response.model_dump()
        return payload if isinstance(payload, dict) else {}
    return response if isinstance(response, dict) else {}


def _openai_usage(response: Any) -> dict[str, int]:
    payload = _response_payload(response)
    usage = payload.get("usage") or {}
    output = payload.get("output") or []
    return {
        "api_requests": 1,
        "web_search_calls": sum(
            1 for item in output
            if isinstance(item, dict) and item.get("type") == "web_search_call"
        ),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _add_usage(total: dict[str, int], current: dict[str, int]) -> None:
    for key, value in current.items():
        total[key] = int(total.get(key, 0)) + int(value)


def _extract_openai_image_results(response: Any) -> list[dict[str, str]]:
    """Parse raw image_result items returned by Responses API web search."""
    payload = _response_payload(response)
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        for result in item.get("results") or []:
            if not isinstance(result, dict) or result.get("type") != "image_result":
                continue
            image_url = str(result.get("image_url") or "").strip()
            if not image_url or image_url in seen:
                continue
            parsed = urlsplit(image_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            seen.add(image_url)
            found.append(
                {
                    "image_url": image_url,
                    "thumbnail_url": str(result.get("thumbnail_url") or "").strip(),
                    "source_website_url": str(result.get("source_website_url") or "").strip(),
                    "caption": str(result.get("caption") or "").strip(),
                }
            )
    return found


def _openai_identity_score(
    result: dict[str, str],
    *,
    brand: str,
    product_name: str,
) -> float | None:
    """Conservative identity gate before downloading a search-result image."""
    source_url = result.get("source_website_url", "")
    caption = result.get("caption", "")
    image_url = result.get("image_url", "")
    combined = _normalize_search_text(f"{caption} {source_url} {image_url}")

    brand_tokens = _meaningful_tokens(brand)
    product_tokens = _meaningful_tokens(product_name)
    brand_hits = sum(token in combined for token in brand_tokens)
    product_hits = sum(token in combined for token in product_tokens)
    brand_cov = brand_hits / len(brand_tokens) if brand_tokens else 0.0
    product_cov = product_hits / len(product_tokens) if product_tokens else 0.0

    brand_compact = _normalize_search_text(brand).replace(" ", "")
    source_domain = normalized_domain(source_url)
    domain_compact = _normalize_search_text(source_domain).replace(" ", "")
    brand_domain_match = bool(brand_compact) and brand_compact in domain_compact

    # Generic names (e.g. "eyeliner") are unsafe unless brand identity is also present.
    if len(product_tokens) <= 2:
        if product_cov < 0.50 or (brand_cov < 0.50 and not brand_domain_match):
            return None
    else:
        if product_cov < 0.35:
            return None
        if brand_cov < 0.50 and not brand_domain_match and product_cov < 0.75:
            return None

    score = product_cov * 70.0 + brand_cov * 25.0
    if brand_domain_match:
        score += 18.0

    source_norm = source_domain.casefold()
    trusted = (
        "sephora", "gratis", "watsons", "rossmann", "korendy", "boyner",
        "trendyol", "douglas", "ulta", "lookfantastic", "cultbeauty",
        "yesstyle", "stylevana", "oliveyoung", "koreanskincare",
    )
    if any(token in source_norm for token in trusted):
        score += 10.0
    return score


def search_openai_product_images(
    client: Any,
    *,
    model: str,
    brand: str,
    product_name: str,
    max_results: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Use OpenAI web image search only as fallback discovery."""
    prompt = f"""Find clear ecommerce product photos for the exact beauty product below.

Brand: {brand}
Product: {product_name}

Requirements:
- Exact product identity is essential. Respect shade, number, SPF, concentration, variant, and edition when present.
- Prefer official brand pages and established beauty retailers.
- Prefer clean packshot/product-gallery images over lifestyle photos, models, swatches, ingredient diagrams, logos, banners, or category pages.
- Do not substitute a similar product or another shade/variant.
- Search the web for image results; do not invent URLs."""

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        tools=[
            {
                "type": "web_search",
                "search_content_types": ["image", "text"],
                "image_settings": {
                    "max_results": max(1, int(max_results)),
                    "caption": True,
                },
                "filters": {
                    "blocked_domains": OPENAI_BLOCKED_IMAGE_DOMAINS,
                },
            }
        ],
        tool_choice="required",
        include=["web_search_call.results"],
        input=prompt,
    )

    candidates: list[dict[str, Any]] = []
    for result in _extract_openai_image_results(response):
        identity_score = _openai_identity_score(
            result,
            brand=brand,
            product_name=product_name,
        )
        if identity_score is None:
            continue
        metadata_score = _image_relevance_score(
            url=result["image_url"],
            source_type="openai_image_search",
            context=f"{result.get('caption', '')} {result.get('source_website_url', '')}",
            brand=brand,
            product_name=product_name,
        )
        candidates.append(
            {
                **result,
                "source_type": "openai_image_search",
                "identity_score": identity_score,
                "score": metadata_score + identity_score,
            }
        )

    candidates.sort(key=lambda x: (-float(x["score"]), x["image_url"]))
    return candidates, _openai_usage(response)



def _response_json_object(response: Any) -> dict[str, Any]:
    text = str(getattr(response, "output_text", "") or "").strip()
    if not text:
        payload = _response_payload(response)
        for item in payload.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict) or content.get("type") != "output_text":
                    continue
                text = str(content.get("text") or "").strip()
                if text:
                    break
            if text:
                break
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def discover_openai_product_pages(
    client: Any,
    *,
    model: str,
    brand: str,
    product_name: str,
    existing_urls: list[str],
    max_results: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Discover alternative exact product pages; OpenAI only finds pages, not images."""
    existing = "\n".join(f"- {url}" for url in existing_urls[:12]) or "- none"
    prompt = f"""Find alternative product-detail pages for this exact beauty product.

Brand: {brand}
Product: {product_name}

Already tried / avoid returning these exact URLs:
{existing}

Requirements:
- Return only exact product-detail pages, never category/search/home pages.
- Exact shade, number, SPF, concentration, size/variant or edition must match when specified.
- Prefer the official brand or established beauty retailers, but prioritize pages likely to be publicly fetchable without login.
- It is fine to use retailers in other countries if the exact product is the same.
- Do not substitute a similar product, another shade, or another formulation.
- Return up to {max(1, int(max_results))} strong candidates.
"""

    response = client.responses.create(
        model=model,
        reasoning={"effort": "low"},
        tools=[{"type": "web_search", "search_context_size": "medium"}],
        tool_choice="required",
        include=["web_search_call.action.sources"],
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "product_page_discovery",
                "strict": True,
                "schema": OPENAI_PAGE_DISCOVERY_SCHEMA,
            }
        },
    )

    payload = _response_json_object(response)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set(existing_urls)
    for item in payload.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen:
            continue
        if not bool(item.get("exact_match")):
            continue
        confidence = float(item.get("confidence") or 0.0)
        if confidence < 0.72:
            continue
        seen.add(url)
        candidates.append(
            {
                "url": url,
                "page_title": str(item.get("page_title") or ""),
                "site_name": str(item.get("site_name") or ""),
                "confidence": confidence,
                "reason": str(item.get("reason") or ""),
            }
        )
    candidates.sort(key=lambda x: (-float(x["confidence"]), x["url"]))
    return candidates[: max(1, int(max_results))], _openai_usage(response)


def _page_identity_score(
    soup: BeautifulSoup,
    page_url: str,
    *,
    brand: str,
    product_name: str,
) -> float | None:
    """Light local identity gate for OpenAI-discovered product pages."""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(
        tag.get_text(" ", strip=True)
        for tag in soup.find_all(["h1", "h2"], limit=8)
    )
    combined = _normalize_search_text(f"{title} {headings} {page_url}")
    brand_tokens = _meaningful_tokens(brand)
    product_tokens = _meaningful_tokens(product_name)
    brand_cov = (
        sum(token in combined for token in brand_tokens) / len(brand_tokens)
        if brand_tokens else 0.0
    )
    product_cov = (
        sum(token in combined for token in product_tokens) / len(product_tokens)
        if product_tokens else 0.0
    )

    brand_compact = _normalize_search_text(brand).replace(" ", "")
    domain_compact = _normalize_search_text(normalized_domain(page_url)).replace(" ", "")
    brand_domain = bool(brand_compact) and brand_compact in domain_compact

    if len(product_tokens) <= 2:
        if product_cov < 0.50 or (brand_cov < 0.50 and not brand_domain):
            return None
    else:
        if product_cov < 0.35:
            return None
        if brand_cov < 0.35 and not brand_domain and product_cov < 0.70:
            return None

    score = product_cov * 70.0 + brand_cov * 25.0
    if brand_domain:
        score += 12.0
    return score


def try_openai_page_fallback(
    *,
    client: Any,
    session: requests.Session,
    database: Path,
    output_dir: Path,
    cache_local: bool,
    timeout: float,
    model: str,
    max_results: int,
    max_images_per_page: int,
    product: dict[str, Any],
    debug: bool,
) -> tuple[bool, int, int, dict[str, int], str]:
    """Discover alternative exact PDPs, then reuse deterministic image extraction."""
    existing_urls = [str(link.get("url") or "") for link in product.get("links") or []]
    pages, usage = discover_openai_product_pages(
        client,
        model=model,
        brand=str(product["brand"]),
        product_name=str(product["product_name"]),
        existing_urls=existing_urls,
        max_results=max_results,
    )
    if not pages:
        return False, 0, 0, usage, "OpenAI page discovery: alternatif exact product page bulunamadı"

    downloaded: list[dict[str, Any]] = []
    image_downloads = 0
    page_requests = 0
    failures: list[str] = []

    if debug:
        for page in pages:
            print(
                f"      OPENAI PAGE conf={float(page['confidence']):.0%} "
                f"{page['site_name']} · {page['url']}"
            )

    for page in pages:
        page_url = str(page["url"])
        try:
            html = fetch_page(session, page_url, timeout)
            page_requests += 1
            soup = BeautifulSoup(html, "html.parser")
            identity = _page_identity_score(
                soup,
                page_url,
                brand=str(product["brand"]),
                product_name=str(product["product_name"]),
            )
            if identity is None:
                failures.append(f"{normalized_domain(page_url)}: page identity gate")
                continue
            json_ld_payloads = parse_json_ld(soup)
            ranked = extract_ranked_image_candidates(
                soup,
                json_ld_payloads,
                page_url,
                brand=str(product["brand"]),
                product_name=str(product["product_name"]),
            )
            if not ranked:
                failures.append(f"{normalized_domain(page_url)}: image metadata yok")
                continue

            for candidate in ranked[: max(1, int(max_images_per_page))]:
                image_url = str(candidate["url"])
                source_type = str(candidate["source_type"])
                try:
                    image, mime_type = download_image(
                        session,
                        image_url,
                        timeout,
                        referer=page_url,
                    )
                    ratio = max(image.width / image.height, image.height / image.width)
                    if ratio > 4.5:
                        raise ValueError(f"aşırı panoramik: {image.width}x{image.height}")
                    image_downloads += 1
                    visual_score = _packshot_visual_score(image)
                    resolution_score = _resolution_preference_score(image)
                    if resolution_score is None:
                        raise ValueError(
                            f"görsel çözünürlüğü çok düşük: {image.width}x{image.height}"
                        )
                    final_score = (
                        float(candidate["score"])
                        + visual_score
                        + resolution_score
                        + float(identity) * 0.35
                        + float(page["confidence"]) * 20.0
                    )
                    downloaded.append(
                        {
                            "page": page,
                            "page_url": page_url,
                            "candidate": candidate,
                            "image": image,
                            "mime_type": mime_type,
                            "final_score": final_score,
                        }
                    )
                    if debug:
                        print(
                            f"      PAGE IMAGE final={final_score:.1f} "
                            f"identity={identity:.1f} packshot={visual_score:.1f} "
                            f"resolution={resolution_score:+.1f} "
                            f"{image.width}x{image.height} {image_url}"
                        )
                except Exception as image_error:
                    failures.append(
                        f"{normalized_domain(page_url)} {source_type}: "
                        f"{type(image_error).__name__}: {image_error}"
                    )
        except Exception as page_error:
            failures.append(
                f"{normalized_domain(page_url)}: {type(page_error).__name__}: {page_error}"
            )

    if not downloaded:
        tail = "; ".join(failures[-3:])
        return False, image_downloads, page_requests, usage, (
            "OpenAI page discovery bulundu ama doğrulanabilir görsel indirilemedi"
            + (f" · {tail}" if tail else "")
        )

    best = max(downloaded, key=lambda item: float(item["final_score"]))
    page_url = str(best["page_url"])
    candidate = best["candidate"]
    image = best["image"]
    image_url = str(candidate["url"])
    source_type = str(candidate["source_type"])
    width, height = int(image.width), int(image.height)
    local_path = ""
    digest: str | None = None
    stored_mime_type = str(best["mime_type"] or "") or None

    if cache_local:
        destination = output_dir / f"{product['product_id']}.webp"
        width, height, digest = save_webp(image, destination)
        local_path = destination.as_posix()
        stored_mime_type = "image/webp"

    upsert_image(
        database,
        product_id=int(product["product_id"]),
        image_url=image_url,
        source_page_url=page_url,
        source_type=source_type,
        local_path=local_path,
        mime_type=stored_mime_type,
        width=width,
        height=height,
        file_sha256=digest,
    )
    message = (
        f"{best['page'].get('site_name') or normalized_domain(page_url)} · "
        f"{source_type} · {width}x{height} · score={float(best['final_score']):.1f}"
    )
    return True, image_downloads, page_requests, usage, message


def try_openai_image_fallback(
    *,
    client: Any,
    session: requests.Session,
    database: Path,
    output_dir: Path,
    cache_local: bool,
    timeout: float,
    model: str,
    max_results: int,
    product: dict[str, Any],
    debug: bool,
) -> tuple[bool, int, dict[str, int], str]:
    """Search, validate, download and store one fallback product image."""
    candidates, usage = search_openai_product_images(
        client,
        model=model,
        brand=str(product["brand"]),
        product_name=str(product["product_name"]),
        max_results=max_results,
    )
    if not candidates:
        return False, 0, usage, "OpenAI image search: uygun exact image sonucu yok"

    downloaded: list[dict[str, Any]] = []
    download_count = 0
    failures: list[str] = []

    for candidate in candidates:
        source_page = str(candidate.get("source_website_url") or "").strip()
        attempts = [(str(candidate["image_url"]), "openai_image_search")]
        thumbnail = str(candidate.get("thumbnail_url") or "").strip()
        if thumbnail and thumbnail != candidate["image_url"]:
            attempts.append((thumbnail, "openai_image_thumbnail"))

        for candidate_url, source_type in attempts:
            try:
                image, mime_type = download_image(
                    session,
                    candidate_url,
                    timeout,
                    referer=source_page or None,
                )
                ratio = max(image.width / image.height, image.height / image.width)
                if ratio > 4.5:
                    raise ValueError(f"aşırı panoramik: {image.width}x{image.height}")
                download_count += 1
                visual_score = _packshot_visual_score(image)
                resolution_score = _resolution_preference_score(image)
                if resolution_score is None:
                    raise ValueError(
                        f"görsel çözünürlüğü çok düşük: {image.width}x{image.height}"
                    )
                final_score = float(candidate["score"]) + visual_score + resolution_score
                if source_type == "openai_image_thumbnail":
                    final_score -= 18.0
                downloaded.append(
                    {
                        "candidate": candidate,
                        "actual_url": candidate_url,
                        "source_type": source_type,
                        "image": image,
                        "mime_type": mime_type,
                        "final_score": final_score,
                    }
                )
                if debug:
                    print(
                        f"      OPENAI IMAGE final={final_score:.1f} "
                        f"identity={float(candidate['identity_score']):.1f} "
                        f"packshot={visual_score:.1f} resolution={resolution_score:+.1f} "
                        f"{image.width}x{image.height} "
                        f"{candidate_url}"
                    )
                break
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

    if not downloaded:
        reason = "; ".join(failures[-3:]) or "image download başarısız"
        return False, download_count, usage, f"OpenAI image search: {reason}"

    best = max(downloaded, key=lambda x: float(x["final_score"]))
    candidate = best["candidate"]
    image = best["image"]
    actual_url = str(best["actual_url"])
    source_page = str(candidate.get("source_website_url") or actual_url)
    width, height = int(image.width), int(image.height)
    local_path = ""
    digest: str | None = None
    stored_mime_type = str(best["mime_type"] or "") or None

    if cache_local:
        destination = output_dir / f"{product['product_id']}.webp"
        width, height, digest = save_webp(image, destination)
        local_path = destination.as_posix()
        stored_mime_type = "image/webp"

    upsert_image(
        database,
        product_id=int(product["product_id"]),
        image_url=actual_url,
        source_page_url=source_page,
        source_type=str(best["source_type"]),
        local_path=local_path,
        mime_type=stored_mime_type,
        width=width,
        height=height,
        file_sha256=digest,
    )
    return True, download_count, usage, (
        f"OpenAI image search · {width}x{height} · score={float(best['final_score']):.1f}"
    )


def fetch_page(
    session: requests.Session,
    url: str,
    timeout: float,
    *,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> str:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = session.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            response.raise_for_status()
            content_type = str(response.headers.get("content-type") or "").casefold()
            looks_like_html = "<html" in response.text[:2000].casefold() or "<!doctype html" in response.text[:2000].casefold()
            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
                and not looks_like_html
            ):
                raise ValueError(f"HTML olmayan içerik: {content_type or 'unknown'}")
            return response.text
        except Exception as error:
            last_error = error
            if attempt + 1 < max(1, retries):
                time.sleep(0.6 * (attempt + 1))
    assert last_error is not None
    raise last_error


def download_image(
    session: requests.Session,
    image_url: str,
    timeout: float,
    *,
    referer: str | None = None,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> tuple[Image.Image, str]:
    parsed = urlsplit(image_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Desteklenmeyen image URL şeması")

    request_headers = {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    if referer:
        request_headers["Referer"] = referer

    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with session.get(
                image_url,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
                headers=request_headers,
            ) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("content-type") or "").casefold()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(f"Image olmayan content-type: {content_type}")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError("Görsel 15 MB sınırını aşıyor")
                    chunks.append(chunk)

            payload = b"".join(chunks)
            image = Image.open(io.BytesIO(payload))
            image.load()
            if image.width < MIN_IMAGE_DIMENSION or image.height < MIN_IMAGE_DIMENSION:
                raise ValueError(
                    f"Görsel çok küçük: {image.width}x{image.height}"
                )
            return image, content_type
        except Exception as error:
            last_error = error
            if attempt + 1 < max(1, retries):
                time.sleep(0.6 * (attempt + 1))

    assert last_error is not None
    raise last_error


def save_webp(image: Image.Image, destination: Path) -> tuple[int, int, str]:
    if image.mode not in {"RGB", "RGBA"}:
        if "A" in image.getbands():
            image = image.convert("RGBA")
        else:
            image = image.convert("RGB")

    if image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION:
        image.thumbnail(
            (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
            Image.Resampling.LANCZOS,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=88, method=6)
    payload = buffer.getvalue()
    destination.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return image.width, image.height, digest



ALLOWED_DB_SOURCE_TYPES = {"og_image", "json_ld", "twitter_image", "manual"}

def db_source_type(source_type: str) -> str:
    """Map richer extractor source labels onto the existing SQLite CHECK constraint."""
    return source_type if source_type in ALLOWED_DB_SOURCE_TYPES else "manual"

def upsert_image(
    database: Path,
    *,
    product_id: int,
    image_url: str,
    source_page_url: str,
    source_type: str,
    local_path: str = "",
    mime_type: str | None = None,
    width: int | None = None,
    height: int | None = None,
    file_sha256: str | None = None,
) -> None:
    now = utc_now_iso()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO product_images (
                    product_id,
                    image_url,
                    source_page_url,
                    source_domain,
                    source_type,
                    local_path,
                    mime_type,
                    width,
                    height,
                    file_sha256,
                    verification_status,
                    checked_at,
                    fetched_at,
                    source_file,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(product_id) DO UPDATE SET
                    image_url = excluded.image_url,
                    source_page_url = excluded.source_page_url,
                    source_domain = excluded.source_domain,
                    source_type = excluded.source_type,
                    local_path = excluded.local_path,
                    mime_type = excluded.mime_type,
                    width = excluded.width,
                    height = excluded.height,
                    file_sha256 = excluded.file_sha256,
                    verification_status = 'verified',
                    checked_at = excluded.checked_at,
                    fetched_at = excluded.fetched_at,
                    source_file = excluded.source_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    product_id,
                    image_url,
                    source_page_url,
                    normalized_domain(source_page_url),
                    db_source_type(source_type),
                    local_path,
                    mime_type,
                    width,
                    height,
                    file_sha256,
                    now,
                    now,
                    str(Path(__file__).name),
                ),
            )
    finally:
        connection.close()


def update_stock_status(
    database: Path,
    *,
    purchase_link_id: int,
    stock_status: str,
) -> bool:
    if stock_status not in {"in_stock", "out_of_stock", "limited_stock"}:
        return False
    now = utc_now_iso()
    connection = sqlite3.connect(database)
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE purchase_links
                SET stock_status = ?,
                    stock_checked_at = ?,
                    checked_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND verification_status = 'verified'
                """,
                (stock_status, now, now, purchase_link_id),
            )
            return cursor.rowcount > 0
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Verified purchase/product pages üzerinden ürün görseli ve açık "
            "structured stock metadata'sı toplar; deterministic yöntem başarısızsa OpenAI image search fallback kullanır."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--cache-local",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Seçilen görseli static/product_images altında WebP olarak cache'ler. "
            "Coverage run için varsayılan açık; kapatmak için --no-cache-local."
        ),
    )
    parser.add_argument("--product-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--max-links-per-product", type=int, default=8)
    parser.add_argument(
        "--openai-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Deterministic product-page extraction başarısızsa OpenAI ile alternatif exact product page bul.",
    )
    parser.add_argument(
        "--openai-only",
        action="store_true",
        help="Verified product-page scraping'i atla; eksik görsellerde doğrudan OpenAI product-page discovery kullan.",
    )
    parser.add_argument("--openai-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument(
        "--openai-page-results",
        type=int,
        default=6,
        help="Her fallback aramasında istenecek alternatif exact product-page sayısı.",
    )
    parser.add_argument(
        "--max-openai-calls",
        type=int,
        default=0,
        help="0=sınırsız; aksi halde bu run içinde en fazla fallback API çağrısı.",
    )
    parser.add_argument("--openai-max-retries", type=int, default=2)
    parser.add_argument(
        "--debug-candidates",
        action="store_true",
        help="Her sayfada en yüksek skorlu image adaylarını gösterir.",
    )
    parser.add_argument(
        "--max-images-per-page",
        type=int,
        default=15,
        help="Her verified ürün sayfasında indirip karşılaştırılabilecek image adayı.",
    )
    args = parser.parse_args()

    ensure_schema(args.database, args.schema)
    products = load_candidates(
        args.database,
        product_id=args.product_id,
        force=args.force,
        limit=args.limit,
    )

    print("=" * 88)
    print("PRODUCT IMAGE ENRICHER")
    print("=" * 88)
    print(f"İşlenecek ürün: {len(products)}")
    print("Görsel modu:    verified product page → ranked image → DB + local WebP cache")
    print(f"Local cache:    {'açık: ' + str(args.output_dir) if args.cache_local else 'kapalı'}")
    print("Kaynak sırası: official_brand → trusted retailer priority → confidence")
    print("Ürün sırası:   en çok incelenen/sosyalde geçen önce")
    print("Image sırası:  metadata/DOM relevance + indirilen adaylarda packshot skoru")
    print(f"OpenAI fallback: {'açık' if args.openai_fallback else 'kapalı'} · product-page discovery" + (" · direct mode" if args.openai_only else ""))

    if args.dry_run:
        print("\nDRY RUN — HTTP indirme ve DB yazımı yapılmayacak.")
        for product in products:
            print(
                f"  {product['product_id']:>5}  {product['brand']} — "
                f"{product['product_name']}  ({len(product['links'])} verified link)"
            )
        return

    if args.openai_only:
        args.openai_fallback = True

    openai_client = None
    if args.openai_fallback:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        if os.getenv("OPENAI_API_KEY"):
            try:
                from openai import OpenAI
                openai_client = OpenAI(max_retries=max(0, int(args.openai_max_retries)))
            except ImportError:
                print("UYARI: openai paketi bulunamadı; OpenAI fallback kapatıldı.")
        else:
            print("UYARI: OPENAI_API_KEY bulunamadı; OpenAI fallback kapatıldı.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )

    success = 0
    failed = 0
    stock_updates = 0
    http_pages = 0
    image_downloads = 0
    openai_calls = 0
    openai_success = 0
    openai_usage_total = {
        "api_requests": 0,
        "web_search_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }

    for index, product in enumerate(products, start=1):
        print(
            f"[{index}/{len(products)}] {product['brand']} — "
            f"{product['product_name']}"
        )
        image_saved = False
        errors: list[str] = []

        links_to_try = (
            []
            if args.openai_only
            else product["links"][: max(1, args.max_links_per_product)]
        )
        for link in links_to_try:
            page_url = str(link["url"])
            try:
                html = fetch_page(session, page_url, args.timeout)
                http_pages += 1
                soup = BeautifulSoup(html, "html.parser")
                json_ld_payloads = parse_json_ld(soup)

                stock_status = extract_stock_status(soup, json_ld_payloads)
                if update_stock_status(
                    args.database,
                    purchase_link_id=int(link["purchase_link_id"]),
                    stock_status=stock_status,
                ):
                    stock_updates += 1

                ranked_candidates = extract_ranked_image_candidates(
                    soup,
                    json_ld_payloads,
                    page_url,
                    brand=str(product["brand"]),
                    product_name=str(product["product_name"]),
                )
                if not ranked_candidates:
                    errors.append(f"{link['merchant_slug']}: image metadata yok")
                    continue

                if args.debug_candidates:
                    for candidate in ranked_candidates[:10]:
                        print(
                            f"      CANDIDATE score={candidate['score']:.1f} "
                            f"source={candidate['source_type']} {candidate['url']}"
                        )

                candidate_errors: list[str] = []
                downloaded_candidates: list[dict[str, Any]] = []
                for candidate in ranked_candidates[: max(1, args.max_images_per_page)]:
                    image_url = str(candidate["url"])
                    source_type = str(candidate["source_type"])
                    try:
                        image, mime_type = download_image(
                            session,
                            image_url,
                            args.timeout,
                            referer=page_url,
                        )
                        ratio = max(image.width / image.height, image.height / image.width)
                        if ratio > 4.5:
                            raise ValueError(
                                f"Ürün kartı için aşırı panoramik görsel: "
                                f"{image.width}x{image.height}"
                            )
                        image_downloads += 1
                        visual_score = _packshot_visual_score(image)
                        resolution_score = _resolution_preference_score(image)
                        if resolution_score is None:
                            raise ValueError(
                                f"görsel çözünürlüğü çok düşük: {image.width}x{image.height}"
                            )
                        final_score = (
                            float(candidate["score"])
                            + visual_score
                            + resolution_score
                        )
                        downloaded_candidates.append(
                            {
                                "candidate": candidate,
                                "image": image,
                                "mime_type": mime_type,
                                "visual_score": visual_score,
                                "resolution_score": resolution_score,
                                "final_score": final_score,
                            }
                        )
                        if args.debug_candidates:
                            print(
                                f"      DOWNLOADED final={final_score:.1f} "
                                f"meta={float(candidate['score']):.1f} "
                                f"packshot={visual_score:.1f} "
                                f"resolution={resolution_score:+.1f} "
                                f"{image.width}x{image.height} {image_url}"
                            )
                    except Exception as image_error:
                        candidate_errors.append(
                            f"{source_type}: {type(image_error).__name__}: {image_error}"
                        )

                if downloaded_candidates:
                    best = max(downloaded_candidates, key=lambda item: float(item["final_score"]))
                    candidate = best["candidate"]
                    image = best["image"]
                    image_url = str(candidate["url"])
                    source_type = str(candidate["source_type"])
                    # Remote-image-first: the original verified CDN URL is the
                    # canonical display source. Local storage is only an optional
                    # cache for sites that later block hotlinking or change URLs.
                    width, height = int(image.width), int(image.height)
                    local_path = ""
                    digest: str | None = None
                    stored_mime_type = str(best["mime_type"] or "") or None

                    if args.cache_local:
                        destination = args.output_dir / f"{product['product_id']}.webp"
                        width, height, digest = save_webp(image, destination)
                        local_path = destination.as_posix()
                        stored_mime_type = "image/webp"

                    upsert_image(
                        args.database,
                        product_id=int(product["product_id"]),
                        image_url=image_url,
                        source_page_url=page_url,
                        source_type=source_type,
                        local_path=local_path,
                        mime_type=stored_mime_type,
                        width=width,
                        height=height,
                        file_sha256=digest,
                    )
                    stored_source_type = db_source_type(source_type)
                    source_label = (
                        source_type
                        if stored_source_type == source_type
                        else f"{source_type} (DB: {stored_source_type})"
                    )
                    storage_label = "remote" + (" + local cache" if args.cache_local else "")
                    print(
                        f"    OK: {link['merchant_name']} · {source_label} · "
                        f"{width}x{height} · {storage_label} · "
                        f"score={float(best['final_score']):.1f}"
                    )
                    if args.debug_candidates:
                        print(f"      IMAGE URL: {image_url}")
                    if stock_status != "unknown":
                        print(f"    STOCK: {stock_status}")
                    image_saved = True
                    success += 1

                if image_saved:
                    break
                errors.append(
                    f"{link['merchant_slug']}: "
                    + "; ".join(candidate_errors[-3:])
                )
            except Exception as error:
                errors.append(
                    f"{link['merchant_slug']}: {type(error).__name__}: {error}"
                )

        if (
            not image_saved
            and openai_client is not None
            and args.openai_fallback
            and (args.max_openai_calls <= 0 or openai_calls < args.max_openai_calls)
        ):
            print("    → deterministic kaynaklar başarısız; OpenAI product-page discovery fallback")
            try:
                openai_calls += 1
                ok, dl_count, page_count, usage, message = try_openai_page_fallback(
                    client=openai_client,
                    session=session,
                    database=args.database,
                    output_dir=args.output_dir,
                    cache_local=args.cache_local,
                    timeout=args.timeout,
                    model=args.openai_model,
                    max_results=args.openai_page_results,
                    max_images_per_page=args.max_images_per_page,
                    product=product,
                    debug=args.debug_candidates,
                )
                image_downloads += dl_count
                http_pages += page_count
                _add_usage(openai_usage_total, usage)
                if ok:
                    image_saved = True
                    success += 1
                    openai_success += 1
                    print(f"    OK FALLBACK: {message}")
                else:
                    errors.append(message)
            except Exception as exc:
                errors.append(f"OpenAI fallback: {type(exc).__name__}: {exc}")

        if not image_saved:
            failed += 1
            print("    GÖRSEL YOK")
            for reason in errors[-3:]:
                print(f"      - {reason}")

        if args.delay > 0 and index < len(products):
            time.sleep(args.delay)

    print()
    print("=" * 88)
    print("İŞLEM TAMAMLANDI")
    print("=" * 88)
    print(f"Görsel kaydedilen ürün: {success}")
    print(f"Görsel bulunamayan:     {failed}")
    print(f"HTML page request:      {http_pages}")
    print(f"Image download:         {image_downloads}")
    print(f"Stock status update:    {stock_updates}")
    print(f"OpenAI fallback call:   {openai_calls}")
    print(f"OpenAI fallback success:{openai_success}")
    if openai_calls:
        print(f"OpenAI web-search call: {openai_usage_total['web_search_calls']}")
        print(f"OpenAI input tokens:    {openai_usage_total['input_tokens']}")
        print(f"OpenAI output tokens:   {openai_usage_total['output_tokens']}")
        print(f"OpenAI total tokens:    {openai_usage_total['total_tokens']}")


if __name__ == "__main__":
    main()
