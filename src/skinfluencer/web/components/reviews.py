from __future__ import annotations
import json, sqlite3
from typing import Any
from shiny import ui
from ..queries import influencer_display_name

SENTIMENT_LABELS={"positive":"Olumlu","negative":"Olumsuz","mixed":"Karışık","neutral":"Nötr","unclear":"Belirsiz"}
SENTIMENT_CLASSES={"positive":"sentiment-positive","negative":"sentiment-negative","mixed":"sentiment-mixed","neutral":"sentiment-neutral","unclear":"sentiment-neutral"}

def safe_json_list(value: str | None) -> list[Any]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    return parsed if isinstance(parsed, list) else []


def format_date(value: str | None) -> str:
    if not value:
        return "Tarih bilinmiyor"

    parts = value.split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"

    return value


def sentiment_pill(sentiment: str) -> Any:
    return ui.span(
        SENTIMENT_LABELS.get(
            sentiment,
            sentiment.title(),
        ),
        class_=(
            "meta-pill "
            + SENTIMENT_CLASSES.get(
                sentiment,
                "sentiment-neutral",
            )
        ),
    )


def comment_block(row: sqlite3.Row) -> Any:
    creator_name = influencer_display_name(
        str(row["influencer_slug"]),
        str(row["influencer_name"]),
    )
    evidence_texts = safe_json_list(
        row["evidence_texts_json"]
    )

    evidence_details = None
    if evidence_texts:
        evidence_details = ui.tags.details(
            ui.tags.summary("Transcript kanıtını göster"),
            ui.tags.ul(
                *[
                    ui.tags.li(text)
                    for text in evidence_texts
                ]
            ),
            class_="evidence-details",
        )

    return ui.div(
        ui.div(
            ui.div(
                f"{creator_name} incelemesi",
                class_="comment-label",
            ),
            sentiment_pill(str(row["sentiment"])),
            class_="comment-heading",
        ),
        ui.div(
            row["display_summary"],
            class_="comment-box",
        ),
        ui.div(
            f"{format_date(row['upload_date'])} · "
            f"Güven: {float(row['confidence']):.0%}",
            class_="comment-meta",
        ),
        ui.div(
            row["video_title"],
            class_="comment-meta",
        ),
        evidence_details,
        ui.div(
            ui.tags.a(
                "Videoyu aç ↗",
                href=row["video_url"],
                target="_blank",
                rel="noopener noreferrer",
                class_="source-link",
            ),
            class_="source-links",
        ),
        class_="comment-block",
    )

def review_entry(row: sqlite3.Row) -> Any:
    creator_name = influencer_display_name(
        str(row["influencer_slug"]),
        str(row["influencer_name"]),
    )
    evidence_texts = safe_json_list(
        row["evidence_texts_json"]
    )

    evidence_details = None
    if evidence_texts:
        evidence_details = ui.tags.details(
            ui.tags.summary("Transcript kanıtını göster"),
            ui.tags.ul(
                *[
                    ui.tags.li(str(text))
                    for text in evidence_texts
                ]
            ),
            class_="evidence-details",
        )

    return ui.div(
        ui.div(
            ui.div(
                f"{creator_name} incelemesi",
                class_="review-entry-label",
            ),
            sentiment_pill(str(row["sentiment"])),
            class_="review-entry-top",
        ),
        ui.div(
            str(row["display_summary"]),
            class_="review-summary",
        ),
        ui.div(
            f"{format_date(row['upload_date'])} · "
            f"Güven: {float(row['confidence']):.0%}",
            class_="review-meta",
        ),
        ui.div(
            str(row["video_title"]),
            class_="review-meta",
        ),
        evidence_details,
        ui.tags.a(
            "Kaynak videoyu aç ↗",
            href=row["video_url"],
            target="_blank",
            rel="noopener noreferrer",
            class_="video-link",
        ),
        class_="review-entry",
    )
