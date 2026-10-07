from __future__ import annotations

import json
import sqlite3
from typing import Any

from shiny import ui

from ..queries import influencer_display_name
from .reviews import format_date


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read optional columns so the app also works before the schema migration."""
    return row[key] if key in row.keys() else default


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def social_post_entry(row: sqlite3.Row) -> Any:
    creator_name = influencer_display_name(
        str(row["influencer_slug"]),
        str(row["influencer_name"]),
    )
    platform = str(row["platform"])
    platform_label = "TikTok" if platform == "tiktok" else "Instagram"
    content_label = "TikTok videosunun" if platform == "tiktok" else "Reel'in"
    link_label = "TikTok videosunu izle ↗" if platform == "tiktok" else "Reel'i izle ↗"

    context_status = str(_row_value(row, "caption_context_status", "") or "")
    relationship = str(_row_value(row, "caption_relationship", "") or "")
    summary = str(_row_value(row, "caption_summary", "") or "").strip()
    opinion_summary = str(_row_value(row, "caption_opinion_summary", "") or "").strip()
    context_confidence = _row_value(row, "caption_context_confidence")
    context_evidence = _json_list(_row_value(row, "caption_context_evidence_json"))

    has_context = (
        context_status == "approved"
        and relationship not in {"", "mention_only"}
        and bool(summary)
    )

    relationship_labels = {
        "recommendation": "Caption önerisi",
        "positive_opinion": "Caption yorumu",
        "negative_opinion": "Caption uyarısı",
        "comparison": "Caption karşılaştırması",
        "routine": "Caption rutini",
        "mixed": "Caption bağlamı",
    }

    body: list[Any] = [
        ui.div(
            f"{creator_name} · {platform_label}",
            class_="social-entry-label",
        )
    ]

    if has_context:
        label = relationship_labels.get(relationship)
        if label:
            body.append(ui.div(label, class_="social-context-label"))
        body.append(ui.p(summary, class_="social-entry-copy"))
        if opinion_summary and opinion_summary != summary:
            body.append(ui.p(opinion_summary, class_="social-context-opinion"))
        body.append(
            ui.p(
                "Bu özet yalnızca gönderi açıklamasındaki metinden çıkarılmıştır; "
                "video konuşma dökümü değildir.",
                class_="social-disclaimer",
            )
        )
        evidence_items = context_evidence or [str(row["evidence_text"])]
        body.append(
            ui.tags.details(
                ui.tags.summary("Caption kanıtını göster"),
                *[ui.p(item) for item in evidence_items],
                class_="evidence-details",
            )
        )
    else:
        body.extend(
            [
                ui.p(
                    f"Bu ürün bu {content_label} açıklamasında yer alıyor.",
                    class_="social-entry-copy",
                ),
                ui.p(
                    "Açıklamada bulunması, ürünün tavsiye edildiği veya "
                    "beğenildiği anlamına gelmez.",
                    class_="social-disclaimer",
                ),
                ui.tags.details(
                    ui.tags.summary("Açıklamadaki eşleşmeyi göster"),
                    ui.p(str(row["evidence_text"])),
                    class_="evidence-details",
                ),
            ]
        )

    confidence_text = f"Eşleşme güveni: {float(row['confidence']):.0%}"
    if has_context and context_confidence is not None:
        confidence_text += f" · Context güveni: {float(context_confidence):.0%}"

    platform_links: list[Any] = [
        ui.tags.a(
            link_label,
            href=str(row["original_url"]),
            target="_blank",
            rel="noopener noreferrer nofollow",
            class_="video-link social-video-link",
        )
    ]

    instagram_url = str(
        _row_value(row, "instagram_url", "") or ""
    ).strip()

    if platform == "tiktok" and instagram_url:
        platform_links.append(
            ui.tags.a(
                "Instagram Reel'i izle ↗",
                href=instagram_url,
                target="_blank",
                rel="noopener noreferrer nofollow",
                class_="video-link social-video-link",
            )
        )

    body.extend(
        [
            ui.div(
                f"{format_date(str(row['published_at']))} · {confidence_text}",
                class_="review-meta",
            ),
            ui.div(
                *platform_links,
                class_="social-platform-links",
            ),
        ]
    )

    return ui.div(*body, class_="social-entry")
