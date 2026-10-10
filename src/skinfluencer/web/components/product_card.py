from __future__ import annotations

import json
import re
from typing import Any

from shiny import ui
from htmltools import HTML

from ..i18n import normalize_language, t
from ..queries import (
    get_product_comments,
    get_purchase_links,
    get_social_posts,
    influencer_display_name,
    product_image_url,
)

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


def js_set_input(input_name: str, payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"Shiny.setInputValue('{input_name}', {serialized}, "
        "{priority: 'event'});"
    )


def js_track_outbound(
    *,
    product_id: int,
    influencer_id: int | None,
    influencer_slug: str,
    merchant: str,
) -> str:
    """Emit creator-attributed outbound intent before following a retailer link."""
    base_payload = json.dumps(
        {
            "product_id": int(product_id),
            "influencer_id": influencer_id,
            "influencer_slug": influencer_slug,
            "merchant": merchant,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"const sfEvent={base_payload};"
        "sfEvent.timestamp=new Date().toISOString();"
        "Shiny.setInputValue('retailer_click',sfEvent,{priority:'event'});"
    )


def payload_as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def product_initials(brand: str) -> str:
    words = [word for word in re.split(r"\s+", brand.strip()) if word]
    initials = "".join(word[0] for word in words[:2]).upper()
    return initials or "SK"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
        if isinstance(row, dict):
            return row.get(key, default)
    except (KeyError, TypeError, AttributeError):
        pass
    return default


def platform_icon(platform: str, *, title: str | None = None) -> Any:
    """Small dependency-free SVG platform icon with tooltip + accessible label."""
    platform = platform.casefold()
    label = title or PLATFORM_LABELS.get(platform, platform.title())

    svg_markup = {
        "youtube": (
            '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<rect x="2" y="5.5" width="20" height="13" rx="4" fill="currentColor"></rect>'
            '<polygon points="10,9 16,12 10,15" fill="white"></polygon>'
            '</svg>'
        ),
        "instagram": (
            '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<rect x="4" y="4" width="16" height="16" rx="5" '
            'fill="none" stroke="currentColor" stroke-width="1.9"></rect>'
            '<circle cx="12" cy="12" r="3.6" fill="none" '
            'stroke="currentColor" stroke-width="1.9"></circle>'
            '<circle cx="17.2" cy="6.9" r="1.05" fill="currentColor"></circle>'
            '</svg>'
        ),
        "tiktok": (
            '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<path d="M14 3v9.8a4.25 4.25 0 1 1-2-3.6V6.1c1.7 1.45 3.3 2.18 '
            '5.45 2.32V6.2C15.95 6.08 14.85 5.35 14 4.2V3z" fill="currentColor"></path>'
            '</svg>'
        ),
    }.get(platform)

    if svg_markup is None:
        svg_markup = (
            '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            '<circle cx="12" cy="12" r="8" fill="currentColor"></circle>'
            '</svg>'
        )

    return ui.span(
        HTML(svg_markup),
        ui.span(label, class_="visually-hidden"),
        class_=f"platform-icon platform-icon-{platform}",
        title=label,
        aria_label=label,
    )


def _selected_purchase_rows(rows: list[Any]) -> list[Any]:
    retailer_rows = [row for row in rows if row["link_type"] == "retailer"]
    official_rows = [row for row in rows if row["link_type"] == "official_brand"]
    return retailer_rows or official_rows[:1]


def retailer_links(
    product: dict[str, Any],
    rows: list[Any] | None = None,
    *,
    influencer_slug: str,
    influencer_id: int | None = None,
    lang: str = "tr",
) -> Any:
    """Render purchase choices only after a creator has been selected."""
    lang = normalize_language(lang)
    rows = rows if rows is not None else get_purchase_links(int(product["product_id"]))
    selected_rows = _selected_purchase_rows(rows)
    if not selected_rows:
        return ui.div(
            ui.p(
                t("no_purchase_link", lang),
                class_="detail-empty-copy",
            ),
            class_="purchase-panel",
        )

    links: list[Any] = []
    seen_merchants: set[str] = set()

    for row in selected_rows:
        merchant_slug = str(row["merchant_slug"])
        variant = row.get('variant_label') if isinstance(row, dict) else None
        merchant_key = f"{merchant_slug}:{variant or ''}"
        if merchant_key in seen_merchants:
            continue
        seen_merchants.add(merchant_key)

        label = (
            t("official_site", lang, brand=product["brand"])
            if row["link_type"] == "official_brand"
            else str(row["merchant_name"])
        )

        if variant:
            label += f" · Shade {variant}"

        stock_status = str(row["stock_status"] or "unknown")
        stock_key = {"in_stock": "in_stock", "out_of_stock": "out_of_stock", "limited_stock": "limited_stock"}.get(stock_status)
        stock_label = t(stock_key, lang) if stock_key else None
        children: list[Any] = [ui.span(label, class_="retailer-name")]
        if stock_label:
            children.append(
                ui.span(
                    stock_label,
                    class_=f"stock-pill stock-{stock_status}",
                )
            )

        links.append(
            ui.tags.a(
                *children,
                href=str(row["url"]),
                target="_blank",
                rel="noopener noreferrer nofollow",
                class_="retailer-link",
                onclick=js_track_outbound(
                    product_id=int(product["product_id"]),
                    influencer_id=influencer_id,
                    influencer_slug=influencer_slug,
                    merchant=merchant_slug,
                ),
                title=t("open_product_page", lang, label=label),
            )
        )

    return ui.div(
        ui.div(*links, class_="retailer-links"),
        ui.p(
            t("stock_may_change", lang),
            class_="retailer-disclaimer",
        ),
        class_="purchase-panel",
    )


def product_creator_presence(
    product_id: int,
    influencer_slug: str = "all",
) -> list[dict[str, Any]]:
    """Return one creator row with platform-presence flags for a product."""
    comments = get_product_comments(product_id, influencer_slug)
    social_posts = get_social_posts(product_id, influencer_slug)
    creators: dict[str, dict[str, Any]] = {}

    def ensure_creator(slug: str, raw_name: str) -> dict[str, Any]:
        if slug not in creators:
            creators[slug] = {
                "slug": slug,
                "name": influencer_display_name(slug, raw_name),
                "youtube": False,
                "tiktok": False,
                "instagram": False,
            }
        return creators[slug]

    for comment in comments:
        slug = str(comment["influencer_slug"])
        ensure_creator(slug, str(comment["influencer_name"]))["youtube"] = True

    for post in social_posts:
        slug = str(post["influencer_slug"])
        creator = ensure_creator(slug, str(post["influencer_name"]))
        platform = str(post["platform"] or "").casefold()
        if platform == "tiktok":
            creator["tiktok"] = True
        elif platform == "instagram":
            creator["instagram"] = True

        # Current Instagram architecture stores the Reel as an alternate link
        # attached to the canonical TikTok post, not as a second social post.
        instagram_url = str(_row_value(post, "instagram_url", "") or "").strip()
        if instagram_url:
            creator["instagram"] = True

    return sorted(
        creators.values(),
        key=lambda creator: str(creator["name"]).casefold(),
    )


def creator_platform_icons(creator: dict[str, Any]) -> Any:
    icons = [
        platform_icon(platform)
        for platform in ("youtube", "tiktok", "instagram")
        if bool(creator.get(platform))
    ]
    return ui.div(*icons, class_="platform-presence-list")


def product_visual(product: dict[str, Any], *, detail: bool = False) -> Any:
    """Render verified product image with a monogram fallback for broken/missing URLs."""
    product_id = int(product["product_id"])
    image_url = product_image_url(product_id)
    monogram_class = "product-monogram detail-product-monogram" if detail else "product-monogram"
    fallback = ui.div(
        product_initials(str(product["brand"])),
        class_=monogram_class + " product-image-fallback",
        aria_hidden="true",
    )

    if not image_url:
        return fallback

    image_class = "product-image detail-product-image" if detail else "product-image"
    return ui.div(
        fallback,
        ui.tags.img(
            src=image_url,
            alt=f"{product['brand']} {product['product_name']}",
            class_=image_class,
            loading="eager" if detail else "lazy",
            onload="this.previousElementSibling.style.display='none';",
            onerror="this.style.display='none';this.previousElementSibling.style.display='grid';",
        ),
        class_="product-image-stack",
    )


def product_card(
    product: dict[str, Any],
    *,
    active_influencer: str,
    lang: str = "tr",
) -> Any:
    """Catalog card: product identity, review count and creator entry points."""
    lang = normalize_language(lang)
    product_id = int(product["product_id"])
    creators = product_creator_presence(product_id, active_influencer)

    creator_count = len(creators)
    count_label = (
        t("review_count_singular", lang)
        if creator_count == 1
        else t("review_count_plural", lang, count=creator_count)
    )

    creator_rows = [
        ui.tags.button(
            ui.span(str(creator["name"]), class_="creator-row-name"),
            creator_platform_icons(creator),
            ui.span("›", class_="creator-row-chevron", aria_hidden="true"),
            type="button",
            class_="creator-row",
            onclick=js_set_input(
                "product_detail_request",
                {
                    "product_id": product_id,
                    "influencer_slug": creator["slug"],
                },
            ),
            aria_label=(
                t(
                    "open_influencer_review",
                    lang,
                    creator=creator["name"],
                    brand=product["brand"],
                    product=product["product_name"],
                )
            ),
        )
        for creator in creators
    ]

    return ui.tags.article(
        ui.div(
            product_visual(product),
            class_="product-visual",
        ),
        ui.div(
            ui.div(
                ui.div(str(product["brand"]), class_="product-brand"),
                ui.div(str(product["product_name"]), class_="product-name"),
                class_="product-identity",
            ),
            ui.p(count_label, class_="creator-count"),
            ui.p(
                t("choose_influencer", lang),
                class_="creator-prompt-copy",
            ),
            ui.div(*creator_rows, class_="creator-list"),
            class_="product-card-body",
        ),
        class_="product-card",
    )
