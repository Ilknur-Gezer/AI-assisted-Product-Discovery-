from __future__ import annotations
from typing import Any
from shiny import ui
from ..queries import influencer_choices, influencer_display_name
from ..i18n import category_tabs, normalize_language, t
from .product_card import js_set_input

INITIAL_INFLUENCERS = influencer_choices()

def category_tabs_ui(active_category: str, lang: str = "tr") -> Any:
    lang = normalize_language(lang)
    tabs = category_tabs(lang)
    return ui.div(
        *[
            ui.tags.button(
                label,
                type="button",
                class_=(
                    "category-tab active"
                    if code == active_category
                    else "category-tab"
                ),
                onclick=js_set_input(
                    "category_select",
                    {"category": code},
                ),
            )
            for code, label in tabs.items()
        ],
        class_="category-strip",
    )


def influencer_selector_ui(active_influencer: str, lang: str = "tr") -> Any:
    lang = normalize_language(lang)
    items = [("all", t("all", lang), "✦")]
    items.extend(
        (
            slug,
            influencer_display_name(slug, name),
            "".join(part[0] for part in name.split()[:2]).upper(),
        )
        for slug, name in INITIAL_INFLUENCERS.items()
        if slug != "all"
    )

    return ui.div(
        *[
            ui.tags.button(
                ui.span(initials, class_="influencer-avatar"),
                ui.span(name, class_="influencer-choice-name"),
                type="button",
                class_=(
                    "influencer-choice active"
                    if slug == active_influencer
                    else "influencer-choice"
                ),
                onclick=js_set_input(
                    "influencer_select",
                    {"influencer_slug": slug},
                ),
            )
            for slug, name, initials in items
        ],
        class_="influencer-choices",
    )
