from __future__ import annotations

import json
import sqlite3
from typing import Any
from shiny import App, Inputs, Outputs, Session, reactive, render, ui
from ..config.settings import DATABASE_PATH, STATIC_DIR
from .queries import (browse_products, database_ready, get_commerce_catalog, get_product_by_id, get_product_comments, get_purchase_links, get_social_posts, get_influencer_id, influencer_choices, influencer_display_name, product_matches_category, search_products_commerce)
from .components.product_card import (creator_platform_icons, payload_as_dict, platform_icon, product_card, product_creator_presence, product_visual, retailer_links)
from .components.influencer_selector import category_tabs_ui, influencer_selector_ui
from .analytics import track_event
from .i18n import CATEGORY_CODES, DEFAULT_LANGUAGE, category_tabs, normalize_language, t

DB_PATH = DATABASE_PATH
CUSTOM_CSS = """
:root {
    --rose-50: #fff7f8;
    --rose-100: #fdecef;
    --rose-200: #f8d9df;
    --rose-500: #d97084;
    --rose-600: #bd566b;
    --ink-900: #2f2930;
    --ink-700: #5f5660;
    --ink-500: #8a808a;
    --surface: rgba(255, 255, 255, 0.94);
    --border: #eee4e7;
    --shadow: 0 18px 45px rgba(91, 62, 70, 0.10);
}

body {
    background:
        radial-gradient(
            circle at top left,
            rgba(248, 217, 223, 0.72),
            transparent 32%
        ),
        linear-gradient(180deg, #fffafa 0%, #f8f7f8 100%);
    color: var(--ink-900);
    min-height: 100vh;
}

.app-shell {
    max-width: 1180px;
    margin: 0 auto;
    padding: 2.2rem 1rem 3.5rem;
}

.hero {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 2rem;
    padding: 2rem;
    margin-bottom: 1.35rem;
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 28px;
    background:
        linear-gradient(
            135deg,
            rgba(255, 255, 255, 0.97),
            rgba(255, 243, 246, 0.92)
        );
    box-shadow: var(--shadow);
}

.hero h1 {
    margin: 0;
    font-size: clamp(2rem, 4vw, 3.2rem);
    line-height: 1.04;
    letter-spacing: -0.045em;
}

.hero p {
    max-width: 720px;
    margin: 0.9rem 0 0;
    color: var(--ink-700);
    font-size: 1.06rem;
    line-height: 1.65;
}

.hero-icon {
    display: grid;
    place-items: center;
    min-width: 110px;
    height: 110px;
    border-radius: 30px;
    background: linear-gradient(145deg, #fff, var(--rose-100));
    box-shadow:
        inset 0 0 0 1px white,
        0 16px 30px rgba(189, 86, 107, 0.12);
    font-size: 3.1rem;
}

.search-panel {
    padding: 1.45rem;
    margin-bottom: 1.35rem;
    border: 1px solid var(--border);
    border-radius: 24px;
    background: var(--surface);
    box-shadow: 0 12px 35px rgba(76, 58, 64, 0.07);
    backdrop-filter: blur(10px);
}

.search-grid {
    display: grid;
    grid-template-columns:
        minmax(190px, 0.75fr)
        minmax(320px, 2fr)
        auto;
    align-items: end;
    gap: 1rem;
}

.form-label {
    color: var(--ink-700);
    font-weight: 700;
    margin-bottom: 0.48rem;
}

.form-control,
.selectize-input {
    min-height: 48px;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    background: white !important;
    box-shadow: none !important;
}

.selectize-input {
    display: flex;
    align-items: center;
    padding: 0.65rem 0.85rem !important;
}

.selectize-input.focus {
    border-color: var(--rose-500) !important;
    box-shadow:
        0 0 0 0.22rem rgba(217, 112, 132, 0.14) !important;
}

.selectize-dropdown {
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
    box-shadow: 0 16px 30px rgba(75, 57, 63, 0.12);
}

.selectize-dropdown .option {
    padding: 0.75rem 0.9rem;
}

.selectize-dropdown .active {
    background: var(--rose-100);
    color: var(--ink-900);
}

.search-button {
    min-height: 48px;
    padding: 0 1.4rem;
    border: 0;
    border-radius: 14px;
    background:
        linear-gradient(135deg, var(--rose-500), var(--rose-600));
    box-shadow: 0 10px 20px rgba(189, 86, 107, 0.22);
    font-weight: 700;
    white-space: nowrap;
}

.search-button:hover,
.search-button:focus {
    background: linear-gradient(135deg, #cf667b, #ad485e);
    transform: translateY(-1px);
}

.search-help {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin: 0.9rem 0 0;
    color: var(--ink-500);
    font-size: 0.88rem;
}

.status-card,
.result-card {
    border: 1px solid var(--border);
    border-radius: 22px;
    background: var(--surface);
    box-shadow: 0 12px 32px rgba(72, 55, 61, 0.07);
}

.status-card {
    padding: 2.2rem;
    text-align: center;
}

.status-icon {
    display: grid;
    place-items: center;
    width: 58px;
    height: 58px;
    margin: 0 auto 0.9rem;
    border-radius: 18px;
    background: var(--rose-100);
    font-size: 1.65rem;
}

.status-card h3 {
    margin-bottom: 0.45rem;
}

.status-card p {
    max-width: 650px;
    margin: 0 auto;
    color: var(--ink-700);
    line-height: 1.6;
}

.results-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    margin: 0.35rem 0 0.9rem;
}

.results-heading h3 {
    margin: 0;
    font-size: 1.25rem;
}

.results-heading p {
    margin: 0;
    color: var(--ink-500);
    font-size: 0.9rem;
}

.result-card {
    padding: 1.4rem;
    margin-bottom: 1rem;
    transition: transform 160ms ease, box-shadow 160ms ease;
}

.result-card:hover {
    transform: translateY(-2px);
    box-shadow: 0 16px 38px rgba(72, 55, 61, 0.10);
}

.result-topline {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
}

.product-brand {
    margin-bottom: 0.28rem;
    color: var(--rose-600);
    font-size: 1.00rem;
    font-weight: 800;
    letter-spacing: 0.035em;
    text-transform: uppercase;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.product-name {
    margin: 0;
    color: #655a62;
    font-size: 0.94rem;
    font-weight: 650;
    line-height: 1.42;
}

.score-pill {
    flex-shrink: 0;
    padding: 0.45rem 0.72rem;
    border-radius: 999px;
    background: #f3f0f1;
    color: var(--ink-700);
    font-size: 0.82rem;
    font-weight: 700;
}

.meta-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.55rem;
    margin: 1rem 0;
}

.meta-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.42rem 0.65rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: #fff;
    color: var(--ink-700);
    font-size: 0.83rem;
}

.sentiment-positive {
    background: #e6f5ec;
    color: #23653d;
}

.sentiment-negative {
    background: #fbe9e9;
    color: #8d3030;
}

.sentiment-mixed {
    background: #fff2d9;
    color: #8a5b08;
}

.sentiment-neutral {
    background: #ececf2;
    color: #555466;
}

.comment-block {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
}

.comment-block:first-of-type {
    border-top: 0;
    padding-top: 0;
}

.comment-heading {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.75rem;
    align-items: center;
    margin-bottom: 0.45rem;
}

.comment-label {
    color: var(--ink-700);
    font-size: 0.84rem;
    font-weight: 800;
}

.comment-box {
    padding: 1rem 1.05rem;
    border-left: 4px solid var(--rose-500);
    border-radius: 0 14px 14px 0;
    background: var(--rose-50);
    color: #443d44;
    line-height: 1.65;
    white-space: normal;
}

.comment-meta {
    margin-top: 0.55rem;
    color: var(--ink-500);
    font-size: 0.84rem;
}

.source-links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65rem;
    margin-top: 0.75rem;
}

.source-link {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.5rem 0.72rem;
    border-radius: 10px;
    background: #f4f1f2;
    color: var(--rose-600);
    font-size: 0.85rem;
    font-weight: 700;
    text-decoration: none;
}

.source-link:hover {
    background: var(--rose-100);
    color: var(--rose-600);
}

.evidence-details {
    margin-top: 0.8rem;
}

.evidence-details summary {
    cursor: pointer;
    color: var(--ink-700);
    font-size: 0.86rem;
    font-weight: 700;
}

.evidence-details li {
    margin-top: 0.45rem;
    color: var(--ink-700);
    line-height: 1.5;
}

@media (max-width: 820px) {
    .app-shell {
        padding-top: 1rem;
    }

    .hero {
        padding: 1.5rem;
    }

    .hero-icon {
        display: none;
    }

    .search-grid {
        grid-template-columns: 1fr;
    }

    .search-button {
        width: 100%;
    }

    .result-topline,
    .results-heading {
        align-items: flex-start;
        flex-direction: column;
    }
}
"""


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------


CUSTOM_CSS += """
body {
    background:
        radial-gradient(circle at 6% 0%, rgba(255, 213, 223, 0.54), transparent 28rem),
        linear-gradient(180deg, #fffdfd 0%, #f8f6f7 100%);
}

.commerce-shell {
    width: min(1400px, calc(100% - 2rem));
    margin: 0 auto;
    padding: 1rem 0 4rem;
}

.topbar {
    position: sticky;
    top: 0.65rem;
    z-index: 50;
    display: grid;
    grid-template-columns: minmax(190px, 0.52fr) minmax(340px, 1.75fr) auto;
    align-items: center;
    gap: 1rem;
    padding: 0.85rem;
    border: 1px solid rgba(238, 229, 232, 0.95);
    border-radius: 22px;
    background: rgba(255, 255, 255, 0.94);
    box-shadow: 0 18px 45px rgba(67, 46, 55, 0.12);
    backdrop-filter: blur(16px);
}

.brand-lockup {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    min-width: 0;
    padding: 0 0.45rem;
}

.brand-home-button {
    border: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
}

.brand-home-button:focus-visible {
    outline: 2px solid #a9304d;
    outline-offset: 4px;
    border-radius: 12px;
}

.brand-mark {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border-radius: 14px;
    background: linear-gradient(145deg, #e35f7a, #a9304d);
    box-shadow: 0 10px 22px rgba(201, 67, 98, 0.25);
    color: white;
    font-size: 1.2rem;
}

.brand-name {
    margin: 0;
    font-family: "Playfair Display", Didot, Georgia, serif;
    font-size: 1.65rem;
    font-weight: 650;
    letter-spacing: -0.025em;
    white-space: nowrap;
}

.demo-chip {
    display: inline-flex;
    margin-left: 0.3rem;
    padding: 0.2rem 0.46rem;
    border-radius: 999px;
    background: #ffeaf0;
    color: #a9304d;
    font-family: Inter, sans-serif;
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    vertical-align: middle;
}

.top-search {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.55rem;
    min-width: 0;
}

.top-search .form-group,
.top-search .shiny-input-container {
    width: 100%;
    margin: 0;
}

.top-search label {
    display: none;
}

.top-search .selectize-control {
    margin: 0;
}

.top-search .selectize-input {
    min-height: 48px;
    padding: 0.65rem 0.95rem !important;
    border-radius: 14px !important;
}

.search-submit {
    border: 0 !important;
    background: linear-gradient(135deg, #e35f7a, #a9304d) !important;
    color: #fff !important;
    box-shadow: 0 10px 22px rgba(201, 67, 98, 0.21);
    font-weight: 800;
}

.search-submit {
    min-height: 48px;
    padding: 0 1.15rem;
    border-radius: 14px !important;
}

.category-strip {
    display: flex;
    gap: 0.55rem;
    margin: 1rem 0 0;
    padding: 0.8rem;
    overflow-x: auto;
    border: 1px solid #eee5e8;
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.9);
    box-shadow: 0 8px 24px rgba(67, 46, 55, 0.08);
}

.category-tab {
    flex: 0 0 auto;
    padding: 0.65rem 0.9rem;
    border: 1px solid transparent;
    border-radius: 999px;
    background: transparent;
    color: #655a62;
    font-weight: 750;
    white-space: nowrap;
}

.category-tab:hover,
.category-tab.active {
    background: #ffeaf0;
    color: #a9304d;
}

.filter-row {
    display: grid;
    gap: 0.9rem;
    margin: 1rem 0 1.3rem;
    padding: 0.9rem 1rem;
    border: 1px solid #eee5e8;
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.86);
}

.filter-copy {
    color: #655a62;
    font-size: 0.9rem;
    line-height: 1.5;
}

.influencer-prompt {
    color: #3f3944;
    font-size: 0.82rem;
    font-weight: 850;
}

.influencer-choices {
    display: flex;
    gap: 0.65rem;
    overflow-x: auto;
    padding-bottom: 0.12rem;
}

.influencer-choice {
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.55rem;
    padding: 0.48rem 0.72rem 0.48rem 0.5rem;
    border: 1px solid #eadfe3;
    border-radius: 999px;
    background: white;
    color: #655a62;
    font-weight: 800;
}

.influencer-choice:hover,
.influencer-choice.active {
    border-color: #e35f7a;
    background: #fff0f4;
    color: #a9304d;
}

.influencer-avatar {
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    background: linear-gradient(145deg, #ffe1e9, #f1dce3);
    color: #a9304d;
    font-family: Georgia, serif;
    font-size: 0.74rem;
    font-weight: 850;
}

.influencer-choice.active .influencer-avatar {
    background: linear-gradient(145deg, #e35f7a, #a9304d);
    color: white;
}

.influencer-choice-name {
    white-space: nowrap;
}

.section-heading {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 1rem;
    margin: 1.2rem 0 1rem;
}

.section-heading h2 {
    margin: 0;
    font-size: clamp(1.45rem, 2.5vw, 2rem);
}

.section-heading p {
    max-width: 680px;
    margin: 0.35rem 0 0;
    color: #655a62;
}

.result-count {
    padding: 0.45rem 0.7rem;
    border-radius: 999px;
    background: #f0edef;
    color: #655a62;
    font-size: 0.82rem;
    font-weight: 800;
}

.product-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1rem;
}

.product-card {
    display: flex;
    min-height: 100%;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid #eee5e8;
    border-radius: 20px;
    background: white;
    box-shadow: 0 8px 24px rgba(67, 46, 55, 0.08);
    transition: 160ms ease;
}

.product-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 18px 45px rgba(67, 46, 55, 0.12);
}

.product-visual {
    position: relative;
    display: grid;
    place-items: center;
    min-height: 188px;
    overflow: hidden;
    background:
        radial-gradient(circle at 22% 18%, rgba(255,255,255,0.92), transparent 34%),
        linear-gradient(145deg, #ffeaf0, #f5eff2 68%, #fff);
}

.product-image {
    display: block;
    width: 100%;
    height: 188px;
    padding: 0.85rem;
    object-fit: contain;
    object-position: center;
    background: #fff;
}

.product-monogram {
    display: grid;
    place-items: center;
    width: 92px;
    height: 112px;
    border-radius: 28px 28px 20px 20px;
    background: rgba(255,255,255,0.75);
    box-shadow: 0 18px 35px rgba(169, 48, 77, 0.13);
    color: #a9304d;
    font-family: Georgia, serif;
    font-size: 1.55rem;
    font-weight: 800;
}

.card-badge {
    position: absolute;
    z-index: 2;
    top: 0.8rem;
    left: 0.8rem;
    padding: 0.38rem 0.58rem;
    border-radius: 999px;
    background: rgba(255,255,255,0.9);
    color: #a9304d;
    font-size: 0.72rem;
    font-weight: 850;
}

.product-card-body {
    display: flex;
    flex: 1;
    flex-direction: column;
    padding: 0.95rem 1rem 0;
}

.product-name {
    margin-bottom: 0.9rem;
}

.product-category {
    display: none;
}

.product-details {
    margin: auto -1rem 0;
    border-top: 1px solid #eee5e8;
    background: #fff;
}

.product-details > summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
    padding: 0.76rem 1rem;
    cursor: pointer;
    list-style: none;
    color: #7d3148;
    font-size: 0.76rem;
    font-weight: 850;
    user-select: none;
}

.product-details > summary::-webkit-details-marker {
    display: none;
}

.product-details > summary:hover {
    background: #fff8fa;
}

.details-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 999px;
    background: #f6f0f2;
    color: #a9304d;
    transition: transform 160ms ease;
}

.product-details[open] .details-chevron {
    transform: rotate(180deg);
}

.product-details-content {
    padding: 0.05rem 1rem 0.95rem;
    border-top: 1px solid #f3ecef;
    background: #fffdfd;
}

.detail-section {
    padding-top: 0.78rem;
}

.detail-section + .detail-section,
.detail-section + .retailer-section,
.retailer-section + .detail-section {
    margin-top: 0.7rem;
    border-top: 1px solid #eee5e8;
}

.detail-section-title,
.retailer-links-title {
    margin-bottom: 0.38rem;
    color: #7a6e75;
    font-size: 0.68rem;
    font-weight: 850;
    letter-spacing: 0.035em;
    text-transform: uppercase;
}

.creator-actions {
    display: grid;
    gap: 0;
}

.creator-button {
    position: relative;
    width: 100%;
    padding: 0.58rem 1.45rem 0.58rem 0;
    border: 0;
    border-bottom: 1px solid #f0e8eb;
    border-radius: 0;
    background: transparent;
    color: #5f4f57;
    font-size: 0.76rem;
    font-weight: 800;
    text-align: left;
    transition: color 140ms ease, padding-left 140ms ease;
}

.creator-button:last-child {
    border-bottom: 0;
}

.creator-button::after {
    content: "→";
    position: absolute;
    right: 0.1rem;
    top: 50%;
    transform: translateY(-50%);
    color: #b27a8a;
    font-size: 0.9rem;
}

.creator-button:hover {
    padding-left: 0.18rem;
    background: transparent;
    color: #a9304d;
}

.social-button {
    color: #62558f;
}

.social-button::after {
    color: #8c82bd;
}

.social-button:hover {
    background: transparent;
    color: #51458f;
}

.retailer-section {
    padding-top: 0.85rem;
}

.retailer-links {
    display: grid;
    gap: 0;
}

.retailer-link {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 36px;
    padding: 0.42rem 0;
    border: 0;
    border-bottom: 1px solid #f0e8eb;
    border-radius: 0;
    background: transparent;
    color: #6a535b;
    font-size: 0.71rem;
    font-weight: 850;
    text-align: left;
    text-decoration: none;
    gap: 0.45rem;
}

.retailer-link:last-child {
    border-bottom: 0;
}

.retailer-link:hover {
    background: transparent;
    color: #a9304d;
}

.retailer-disclaimer {
    margin-top: 0.45rem;
    color: #988d94;
    font-size: 0.66rem;
    line-height: 1.35;
}

.stock-pill {
    padding: 0.14rem 0.38rem;
    border-radius: 999px;
    font-size: 0.62rem;
    font-weight: 850;
}

.stock-in_stock {
    background: #e8f7ee;
    color: #267a48;
}

.stock-out_of_stock {
    background: #f3eff1;
    color: #7f747a;
}

.stock-limited_stock {
    background: #fff3d9;
    color: #8a6414;
}

.empty-card {
    padding: 2.5rem 1.2rem;
    border: 1px solid #eee5e8;
    border-radius: 22px;
    background: rgba(255,255,255,0.92);
    text-align: center;
}

.empty-icon {
    display: grid;
    place-items: center;
    width: 62px;
    height: 62px;
    margin: 0 auto 0.9rem;
    border-radius: 20px;
    background: #ffeaf0;
    font-size: 1.7rem;
}

.review-entry {
    margin-top: 0.85rem;
    padding: 1rem;
    border: 1px solid #eee5e8;
    border-radius: 15px;
    background: #fcf9fa;
}

.social-entry {
    margin-top: 0.85rem;
    padding: 1rem;
    border: 1px solid #ded8f5;
    border-radius: 15px;
    background: #faf9ff;
}

.social-entry-label {
    color: #51458f;
    font-size: 0.86rem;
    font-weight: 850;
}

.social-context-label {
    display: inline-flex;
    margin-top: 0.65rem;
    padding: 0.22rem 0.5rem;
    border-radius: 999px;
    background: #efedff;
    color: #51458f;
    font-size: 0.72rem;
    font-weight: 800;
}

.social-context-opinion {
    margin: 0 0 0.55rem;
    color: #5d5562;
    font-size: 0.86rem;
    line-height: 1.5;
}

.social-entry-copy {
    margin: 0.7rem 0 0.35rem;
    color: #3f3944;
    line-height: 1.55;
}

.social-disclaimer {
    margin: 0 0 0.65rem;
    color: #82798a;
    font-size: 0.78rem;
    line-height: 1.45;
}

.social-video-link {
    background: #efedff;
    color: #51458f;
}

.review-entry-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.65rem;
}

.review-entry-label {
    color: #655a62;
    font-size: 0.82rem;
    font-weight: 850;
}

.review-summary {
    padding: 0.85rem 0.95rem;
    border-left: 4px solid #e35f7a;
    border-radius: 0 12px 12px 0;
    background: #fff7f9;
    line-height: 1.65;
}

.review-meta {
    margin-top: 0.58rem;
    color: #8f838b;
    font-size: 0.79rem;
}

.video-link {
    display: inline-flex;
    margin-top: 0.75rem;
    padding: 0.5rem 0.7rem;
    border-radius: 10px;
    background: #f0edef;
    color: #a9304d;
    font-size: 0.82rem;
    font-weight: 850;
    text-decoration: none;
}

@media (max-width: 1120px) {
    .product-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
}

@media (max-width: 900px) {
    .topbar {
        position: static;
        grid-template-columns: 1fr auto;
    }

    .top-search {
        grid-column: 1 / -1;
        grid-row: 2;
    }

    .product-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

@media (max-width: 650px) {
    .commerce-shell {
        width: min(100% - 1rem, 1400px);
    }

    .topbar {
        grid-template-columns: minmax(0, 1fr) auto;
    }

    .demo-chip {
        display: none;
    }

    .top-search {
        grid-template-columns: 1fr;
    }

    .section-heading {
        align-items: stretch;
        flex-direction: column;
    }

    .product-grid {
        grid-template-columns: 1fr;
    }
}
"""


# ---------------------------------------------------------------------------
# PRODUCT CARD + INFLUENCER DETAIL REFINEMENT
# ---------------------------------------------------------------------------

CUSTOM_CSS += """
/* The product itself is the visual anchor. Cards stay quiet and informational. */
.product-grid {
    gap: 1.15rem;
}

.product-card {
    border: 1px solid #e8e1e4;
    border-radius: 16px;
    background: #fff;
    box-shadow: none;
    transition: border-color 140ms ease, box-shadow 140ms ease;
}

.product-card:hover {
    transform: none;
    border-color: #d9cdd2;
    box-shadow: 0 10px 28px rgba(67, 46, 55, 0.055);
}

.product-visual {
    min-height: 210px;
    background: #fff;
    border-bottom: 1px solid #f0eaed;
}

.product-image {
    height: 210px;
    padding: 1.05rem;
}

.product-monogram {
    width: 82px;
    height: 98px;
    border-radius: 18px;
    background: #faf6f7;
    box-shadow: none;
    color: #9a5367;
}

.product-card-body {
    padding: 1rem 1rem 0.45rem;
}

.product-brand {
    margin-bottom: 0.22rem;
    color: #7d3148;
    font-size: 0.82rem;
    font-weight: 800;
    letter-spacing: 0.015em;
    text-transform: none;
}

.product-name {
    min-height: 2.7em;
    margin: 0;
    color: #352f33;
    font-size: 0.98rem;
    font-weight: 680;
    line-height: 1.38;
}

.creator-count {
    margin: 0.72rem 0 0.55rem;
    color: #847980;
    font-size: 0.78rem;
}

.creator-list {
    margin: 0 -1rem;
    border-top: 1px solid #f0eaed;
}

.creator-row {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    padding: 0.72rem 1rem;
    border: 0;
    border-bottom: 1px solid #f3edef;
    border-radius: 0;
    background: #fff;
    color: #3f373c;
    text-align: left;
}

.creator-row:last-child {
    border-bottom: 0;
}

.creator-row:hover {
    background: #fcf9fa;
}

.creator-row:focus-visible,
.retailer-link:focus-visible,
.short-form-link:focus-visible {
    outline: 3px solid rgba(217, 112, 132, 0.26);
    outline-offset: -3px;
}

.creator-row-name {
    min-width: 0;
    overflow: hidden;
    color: #4a4046;
    font-size: 0.8rem;
    font-weight: 760;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.platform-presence-list {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    align-items: center;
    gap: 0.27rem;
}

.platform-presence {
    color: #776c72;
    font-size: 0.66rem;
    font-weight: 720;
    line-height: 1.25;
}

.platform-separator {
    color: #b8adb2;
    font-size: 0.62rem;
}

.platform-youtube {
    color: #a6414a;
}

.platform-tiktok {
    color: #343036;
}

.platform-instagram {
    color: #7d4d78;
}

.influencer-detail {
    max-width: 760px;
    margin: 0 auto;
}

.detail-product-context {
    padding-bottom: 1rem;
    border-bottom: 1px solid #ece5e8;
}

.detail-product-context .product-brand {
    margin-bottom: 0.22rem;
}

.detail-product-name {
    margin: 0;
    color: #332d31;
    font-size: 1.18rem;
    line-height: 1.35;
}

.detail-section-panel {
    padding: 1.15rem 0;
    border-bottom: 1px solid #ece5e8;
}

.detail-section-panel:last-child {
    border-bottom: 0;
}

.detail-section-heading {
    margin: 0 0 0.7rem;
    color: #352f33;
    font-size: 0.98rem;
    font-weight: 790;
}

.sentiment-group + .sentiment-group {
    margin-top: 0.9rem;
}

.sentiment-title {
    margin-bottom: 0.35rem;
    color: #6f6269;
    font-size: 0.78rem;
    font-weight: 780;
}

.sentiment-positive-title {
    color: #2f7049;
}

.sentiment-negative-title {
    color: #91414a;
}

.summary-list {
    margin: 0;
    padding-left: 1.15rem;
    color: #4c4449;
}

.summary-list li + li {
    margin-top: 0.38rem;
}

.summary-list li {
    line-height: 1.52;
}

.youtube-source-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin-top: 0.75rem;
}

.short-form-list {
    display: grid;
    gap: 0.75rem;
}

.short-form-entry-compact {
    padding: 0.82rem 0;
    border-top: 1px solid #f1ebed;
}

.short-form-entry-compact:first-child {
    padding-top: 0;
    border-top: 0;
}

.short-form-copy {
    margin: 0;
    color: #4c4449;
    line-height: 1.52;
}

.short-form-meta {
    margin-top: 0.38rem;
    color: #92878d;
    font-size: 0.72rem;
}

.short-form-links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin-top: 0.6rem;
}

.short-form-link,
.youtube-source-link {
    display: inline-flex;
    align-items: center;
    min-height: 34px;
    padding: 0.36rem 0.58rem;
    border: 1px solid #e5dce0;
    border-radius: 8px;
    background: #fff;
    color: #7d3148;
    font-size: 0.75rem;
    font-weight: 760;
    text-decoration: none;
}

.short-form-link:hover,
.youtube-source-link:hover {
    background: #fbf6f8;
    color: #7d3148;
}

.purchase-panel .retailer-links {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.55rem;
}

.purchase-panel .retailer-link {
    min-height: 44px;
    padding: 0.62rem 0.7rem;
    border: 1px solid #e6dde1;
    border-radius: 9px;
    background: #fff;
    color: #4d4147;
    font-size: 0.78rem;
}

.purchase-panel .retailer-link:hover {
    border-color: #d7c7ce;
    background: #fcf9fa;
    color: #7d3148;
}

.retailer-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.detail-empty-copy {
    margin: 0;
    color: #8a7f85;
    font-size: 0.84rem;
}

@media (max-width: 650px) {
    .product-visual,
    .product-image {
        min-height: 190px;
        height: 190px;
    }

    .creator-row {
        align-items: flex-start;
        grid-template-columns: 1fr;
    }

    .platform-presence-list {
        justify-content: flex-start;
    }

    .purchase-panel .retailer-links {
        grid-template-columns: 1fr;
    }
}

@media (prefers-reduced-motion: reduce) {
    .product-card,
    .creator-row,
    .search-button,
    .search-submit {
        transition: none !important;
    }
}
"""


# ---------------------------------------------------------------------------
# CATALOG -> PRODUCT DETAIL EXPERIENCE
# ---------------------------------------------------------------------------

CUSTOM_CSS += """
.visually-hidden {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    padding: 0 !important;
    margin: -1px !important;
    overflow: hidden !important;
    clip: rect(0, 0, 0, 0) !important;
    white-space: nowrap !important;
    border: 0 !important;
}

.topbar {
    grid-template-columns: minmax(190px, 0.52fr) minmax(340px, 1.75fr);
}

.product-image-stack {
    position: relative;
    display: grid;
    place-items: center;
    width: 100%;
    height: 100%;
}

.product-image-stack .product-image-fallback {
    position: absolute;
    inset: 50% auto auto 50%;
    transform: translate(-50%, -50%);
    z-index: 0;
}

.product-image-stack .product-image {
    position: relative;
    z-index: 1;
}

.product-open-trigger,
.product-identity-trigger {
    width: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    text-align: left;
}

.product-open-trigger {
    cursor: pointer;
}

.product-identity-trigger {
    display: block;
    cursor: pointer;
}

.product-open-trigger:focus-visible,
.product-identity-trigger:focus-visible,
.creator-row:focus-visible,
.detail-creator-button:focus-visible,
.detail-back-button:focus-visible,
.platform-source-link:focus-visible,
.retailer-link:focus-visible {
    outline: 3px solid rgba(217, 112, 132, 0.28);
    outline-offset: 2px;
}

.creator-count {
    margin-bottom: 0.2rem;
}

.creator-prompt-copy {
    margin: 0 0 0.62rem;
    color: #9a8d94;
    font-size: 0.7rem;
    line-height: 1.35;
}

.creator-row {
    grid-template-columns: minmax(0, 1fr) auto 14px;
}

.creator-row-chevron {
    color: #b39fa7;
    font-size: 1.15rem;
    line-height: 1;
}

.platform-presence-list {
    gap: 0.38rem;
}

.platform-icon {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.05rem;
    height: 1.05rem;
    color: #776c72;
    flex: 0 0 auto;
}

.platform-icon svg {
    display: block;
    width: 100%;
    height: 100%;
}

.platform-icon-youtube {
    color: #c84d56;
}

.platform-icon-tiktok {
    color: #302c31;
}

.platform-icon-instagram {
    color: #8b557e;
}

.detail-product-page {
    width: min(1080px, 100%);
    margin: 0 auto;
    padding: 1.15rem 0 3rem;
}

.detail-back-button {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0 0 1rem;
    padding: 0.42rem 0;
    border: 0;
    background: transparent;
    color: #746970;
    font-size: 0.82rem;
    font-weight: 720;
}

.detail-back-button:hover {
    color: #7d3148;
}

.detail-hero {
    display: grid;
    grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
    gap: clamp(1.5rem, 4vw, 3.4rem);
    align-items: center;
    padding: 1.5rem;
    border: 1px solid #e8e1e4;
    border-radius: 18px;
    background: #fff;
}

.detail-media {
    display: grid;
    place-items: center;
    min-height: 360px;
    background: #fff;
}

.detail-product-image {
    width: 100%;
    height: 350px;
    padding: 1rem;
    object-fit: contain;
}

.detail-product-monogram {
    width: 130px;
    height: 150px;
    font-size: 2rem;
}

.detail-brand {
    margin-bottom: 0.35rem;
    color: #7d3148;
    font-size: 0.9rem;
    font-weight: 820;
}

.detail-title {
    max-width: 620px;
    margin: 0;
    color: #2f2930;
    font-size: clamp(1.65rem, 4vw, 2.55rem);
    font-weight: 690;
    line-height: 1.12;
    letter-spacing: -0.025em;
}

.detail-review-count {
    margin: 0.85rem 0 0.2rem;
    color: #5f5660;
    font-size: 0.9rem;
    font-weight: 720;
}

.detail-review-prompt {
    margin: 0;
    color: #958990;
    font-size: 0.78rem;
}

.detail-creator-switcher {
    display: grid;
    gap: 0.5rem;
    margin-top: 1rem;
}

.detail-creator-button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.7rem;
    width: 100%;
    padding: 0.72rem 0.82rem;
    border: 1px solid #e7dfe2;
    border-radius: 10px;
    background: #fff;
    color: #4a4046;
    text-align: left;
}

.detail-creator-button:hover {
    border-color: #d8c8cf;
    background: #fcf9fa;
}

.detail-creator-button.active {
    border-color: #d97084;
    background: #fff7f9;
    color: #7d3148;
}

.detail-creator-name {
    font-size: 0.85rem;
    font-weight: 780;
}

.detail-content {
    width: min(780px, 100%);
    margin: 1.7rem auto 0;
}

.detail-selected-heading {
    padding: 0 0 0.9rem;
    border-bottom: 1px solid #ece5e8;
}

.detail-selected-heading h2 {
    margin: 0;
    color: #332d31;
    font-size: 1.35rem;
    font-weight: 760;
}

.detail-selected-heading p {
    margin: 0.28rem 0 0;
    color: #8c8087;
    font-size: 0.82rem;
}

.detail-section-heading-row {
    display: flex;
    align-items: center;
    gap: 0.52rem;
    margin-bottom: 0.75rem;
}

.detail-section-heading-row .detail-section-heading {
    margin: 0;
}

.platform-source-links,
.short-form-links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.75rem;
}

.platform-source-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.35rem;
    height: 2.35rem;
    border: 1px solid #e4dadd;
    border-radius: 50%;
    background: #fff;
    text-decoration: none;
}

.platform-source-link:hover {
    border-color: #d4c1c8;
    background: #fbf7f8;
}

.platform-source-link .platform-icon {
    width: 1.12rem;
    height: 1.12rem;
}

@media (max-width: 760px) {
    .detail-hero {
        grid-template-columns: 1fr;
        gap: 1rem;
        padding: 1rem;
    }

    .detail-media {
        min-height: 250px;
    }

    .detail-product-image {
        height: 250px;
    }

    .creator-row {
        grid-template-columns: minmax(0, 1fr) auto 14px;
        align-items: center;
    }
}


.language-switch {
    display: inline-flex;
    justify-self: end;
    gap: 0.25rem;
    padding: 0.25rem;
    border: 1px solid #eee5e8;
    border-radius: 999px;
    background: #fff;
}
.language-button {
    min-width: 42px;
    padding: 0.45rem 0.65rem;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: #655a62;
    font-size: 0.78rem;
    font-weight: 850;
}
.language-button.active {
    background: #ffeaf0;
    color: #a9304d;
}
@media (max-width: 650px) {
    .topbar {
        grid-template-columns: 1fr;
    }

    .creator-row {
        grid-template-columns: minmax(0, 1fr) auto 14px;
    }

    .platform-presence-list {
        justify-content: flex-end;
    }
}
"""


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
        if isinstance(row, dict):
            return row.get(key, default)
    except (KeyError, TypeError, AttributeError):
        pass
    return default


def _format_date(value: Any, lang: str = "tr") -> str:
    raw = str(value or "").strip()
    parts = raw.split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return raw or t("unknown_date", lang)


def _unique_texts(rows: list[Any], key: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for row in rows:
        value = str(_row_value(row, key, "") or "").strip()
        if not value:
            continue
        normalized = " ".join(value.casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(value)
    return values


def _opinion_points(comments: list[Any]) -> list[dict[str, str]]:
    """Collect grounded atomic YouTube opinion claims from SQLite JSON."""
    points: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for comment in comments:
        raw = _row_value(
            comment,
            "localized_opinion_points_json",
            _row_value(comment, "opinion_points_json", "[]"),
        )
        try:
            parsed = json.loads(str(raw or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = []

        if not isinstance(parsed, list):
            continue

        for item in parsed:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()
            polarity = str(item.get("polarity") or "neutral").strip().casefold()
            evidence = str(item.get("evidence_text") or "").strip()
            if not claim:
                continue
            if polarity not in {"positive", "negative", "neutral"}:
                polarity = "neutral"

            key = (polarity, " ".join(claim.casefold().split()))
            if key in seen:
                continue
            seen.add(key)
            points.append(
                {
                    "claim": claim,
                    "polarity": polarity,
                    "evidence_text": evidence,
                }
            )

    return points


def _section_heading(title: str, *platforms: str) -> Any:
    return ui.div(
        *[platform_icon(platform) for platform in platforms],
        ui.h4(title, class_="detail-section-heading"),
        class_="detail-section-heading-row",
    )


def youtube_review_summary(comments: list[Any], lang: str = "tr") -> Any:
    lang = normalize_language(lang)
    if not comments:
        return ui.div(
            _section_heading(t("review_summary", lang), "youtube"),
            ui.p(
                t("no_long_review", lang),
                class_="detail-empty-copy",
            ),
            class_="detail-section-panel",
        )

    points = _opinion_points(comments)
    group_nodes: list[Any] = []

    for polarity, label, title_class in (
        ("positive", t("positive", lang), "sentiment-positive-title"),
        ("negative", t("negative", lang), "sentiment-negative-title"),
        ("neutral", t("usage_notes", lang), ""),
    ):
        claims = [point["claim"] for point in points if point["polarity"] == polarity]
        if not claims:
            continue
        group_nodes.append(
            ui.div(
                ui.div(label, class_=("sentiment-title " + title_class).strip()),
                ui.tags.ul(
                    *[ui.tags.li(claim) for claim in claims],
                    class_="summary-list",
                ),
                class_="sentiment-group",
            )
        )

    # Compatibility fallback for rows created before opinion_points_json existed.
    if not group_nodes:
        summaries = _unique_texts(comments, "localized_display_summary")
        if summaries:
            group_nodes.append(
                ui.div(
                    ui.div(t("summary", lang), class_="sentiment-title"),
                    ui.tags.ul(
                        *[ui.tags.li(summary) for summary in summaries],
                        class_="summary-list",
                    ),
                    class_="sentiment-group",
                )
            )

    source_links: list[Any] = []
    seen_urls: set[str] = set()
    for row in comments:
        url = str(_row_value(row, "video_url", "") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        video_title = str(_row_value(row, "video_title", "") or "").strip()
        source_links.append(
            ui.tags.a(
                platform_icon("youtube"),
                href=url,
                target="_blank",
                rel="noopener noreferrer",
                class_="platform-source-link",
                title=t("open_source_video", lang),
                aria_label=(
                    f"{t('open_source_video', lang)}: {video_title}" if video_title else t("open_source_video", lang)
                ),
            )
        )

    children: list[Any] = [_section_heading(t("review_summary", lang), "youtube"), *group_nodes]
    if source_links:
        children.append(ui.div(*source_links, class_="platform-source-links"))

    return ui.div(*children, class_="detail-section-panel")


def short_form_context(posts: list[Any], lang: str = "tr") -> Any:
    lang = normalize_language(lang)
    if not posts:
        return ui.div(
            _section_heading(t("short_form_context", lang), "tiktok", "instagram"),
            ui.p(
                t("no_short_form", lang),
                class_="detail-empty-copy",
            ),
            class_="detail-section-panel",
        )

    entries: list[Any] = []
    for post in posts:
        context_status = str(_row_value(post, "caption_context_status", "") or "")
        relationship = str(_row_value(post, "caption_relationship", "") or "")
        summary = str(_row_value(post, "localized_caption_summary", _row_value(post, "caption_summary", "")) or "").strip()
        opinion = str(_row_value(post, "localized_caption_opinion_summary", _row_value(post, "caption_opinion_summary", "")) or "").strip()

        has_context = (
            context_status == "approved"
            and relationship not in {"", "mention_only"}
            and bool(summary)
        )
        copy = (opinion or summary) if has_context else t("product_in_caption", lang)

        links: list[Any] = []
        tiktok_url = str(_row_value(post, "original_url", "") or "").strip()
        if tiktok_url:
            links.append(
                ui.tags.a(
                    platform_icon("tiktok"),
                    href=tiktok_url,
                    target="_blank",
                    rel="noopener noreferrer nofollow",
                    class_="platform-source-link",
                    title=t("open_short_video", lang),
                    aria_label=t("open_tiktok", lang),
                )
            )

        instagram_url = str(_row_value(post, "instagram_url", "") or "").strip()
        if instagram_url:
            links.append(
                ui.tags.a(
                    platform_icon("instagram"),
                    href=instagram_url,
                    target="_blank",
                    rel="noopener noreferrer nofollow",
                    class_="platform-source-link",
                    title=t("open_instagram", lang),
                    aria_label=t("open_instagram", lang),
                )
            )

        entries.append(
            ui.div(
                ui.p(copy, class_="short-form-copy"),
                ui.div(
                    _format_date(_row_value(post, "published_at"), lang),
                    class_="short-form-meta",
                ),
                ui.div(*links, class_="short-form-links") if links else None,
                class_="short-form-entry-compact",
            )
        )

    return ui.div(
        _section_heading(t("short_form_context", lang), "tiktok", "instagram"),
        ui.div(*entries, class_="short-form-list"),
        class_="detail-section-panel",
    )


def product_detail_page(product: dict[str, Any], influencer_slug: str, lang: str = "tr") -> Any:
    lang = normalize_language(lang)
    product_id = int(product["product_id"])
    creators = product_creator_presence(product_id, "all")
    creator_slugs = {str(creator["slug"]) for creator in creators}
    if influencer_slug not in creator_slugs and creators:
        influencer_slug = str(creators[0]["slug"])

    selected_creator = next(
        (creator for creator in creators if str(creator["slug"]) == influencer_slug),
        None,
    )

    comments = get_product_comments(product_id, influencer_slug, lang)
    posts = get_social_posts(product_id, influencer_slug, lang)
    raw_name = ""
    if selected_creator:
        raw_name = str(selected_creator["name"])
    elif comments:
        raw_name = str(comments[0]["influencer_name"])
    elif posts:
        raw_name = str(posts[0]["influencer_name"])
    influencer_name = influencer_display_name(influencer_slug, raw_name or influencer_slug)

    purchase_rows = get_purchase_links(product_id)
    influencer_id = get_influencer_id(influencer_slug)
    return ui.tags.main(
        ui.tags.button(
            t("back_to_products", lang),
            type="button",
            class_="detail-back-button",
            onclick="Shiny.setInputValue('detail_back',Date.now(),{priority:'event'});",
        ),
        ui.tags.section(
            ui.div(product_visual(product, detail=True), class_="detail-media"),
            ui.div(
                ui.div(str(product["brand"]), class_="detail-brand"),
                ui.h1(str(product["product_name"]), class_="detail-title"),
                ui.p(
                    t("youtube_review", lang, creator=influencer_name),
                    class_="detail-review-count",
                ),
            ),
            class_="detail-hero",
        ),
        ui.div(
            ui.div(
                ui.h2(influencer_name),
                ui.p(t("selected_influencer_subtitle", lang)),
                class_="detail-selected-heading",
            ),
            youtube_review_summary(comments, lang),
            short_form_context(posts, lang),
            ui.div(
                ui.h4(t("purchase_options", lang), class_="detail-section-heading"),
                retailer_links(
                    product,
                    purchase_rows,
                    influencer_slug=influencer_slug,
                    influencer_id=influencer_id,
                    lang=lang,
                ),
                class_="detail-section-panel",
            ),
            class_="detail-content",
        ),
        class_="detail-product-page",
    )


INITIAL_INFLUENCERS = influencer_choices()

app_ui = ui.page_fluid(
    ui.tags.head(
        ui.tags.meta(
            name="viewport",
            content="width=device-width, initial-scale=1",
        ),
        ui.tags.style(CUSTOM_CSS),
    ),
    ui.div(
        ui.output_ui("topbar"),
        ui.output_ui("catalog_controls"),
        ui.output_ui("database_message"),
        ui.output_ui("catalog_content"),
        class_="commerce-shell",
    ),
)


# ---------------------------------------------------------------------------
# SERVER
# ---------------------------------------------------------------------------

def server(
    input: Inputs,
    output: Outputs,
    session: Session,
) -> None:
    active_language = reactive.Value(DEFAULT_LANGUAGE)
    active_category = reactive.Value("all")
    active_influencer = reactive.Value("all")
    submitted_query = reactive.Value("")
    selected_product_id = reactive.Value(None)
    selected_detail_influencer = reactive.Value("")

    def close_detail() -> None:
        selected_product_id.set(None)
        selected_detail_influencer.set("")

    @reactive.effect
    @reactive.event(input.language_select)
    def select_language() -> None:
        payload = payload_as_dict(input.language_select())
        active_language.set(normalize_language(payload.get("language")))

    @reactive.effect
    @reactive.event(input.influencer_select)
    def select_influencer() -> None:
        payload = payload_as_dict(input.influencer_select())
        influencer_slug = str(payload.get("influencer_slug") or "all")
        if influencer_slug not in INITIAL_INFLUENCERS:
            influencer_slug = "all"

        close_detail()
        active_influencer.set(influencer_slug)
        active_category.set("all")
        submitted_query.set("")

    @reactive.effect
    @reactive.event(input.search_button)
    def submit_search() -> None:
        close_detail()
        submitted_query.set(str(input.product_query() or "").strip())

    @reactive.effect
    @reactive.event(input.category_select)
    def select_category() -> None:
        payload = payload_as_dict(input.category_select())
        category = str(payload.get("category") or "all")
        if category not in CATEGORY_CODES:
            category = "all"

        close_detail()
        active_category.set(category)
        submitted_query.set("")

    @reactive.effect
    @reactive.event(input.product_detail_request)
    def open_product_detail() -> None:
        payload = payload_as_dict(input.product_detail_request())
        try:
            product_id = int(payload["product_id"])
        except (KeyError, TypeError, ValueError):
            return

        if get_product_by_id(product_id) is None:
            return

        creators = product_creator_presence(product_id, "all")
        if not creators:
            return

        available = {str(creator["slug"]) for creator in creators}
        requested = str(payload.get("influencer_slug") or "")
        if requested not in available:
            catalog_filter = active_influencer.get()
            requested = catalog_filter if catalog_filter in available else str(creators[0]["slug"])

        selected_product_id.set(product_id)
        selected_detail_influencer.set(requested)

    @reactive.effect
    @reactive.event(input.detail_back)
    def back_to_catalog() -> None:
        close_detail()

    @reactive.effect
    @reactive.event(input.home_request)
    def go_home() -> None:
        close_detail()
        active_influencer.set("all")
        active_category.set("all")
        submitted_query.set("")


    @reactive.effect
    @reactive.event(input.retailer_click)
    def record_retailer_click() -> None:
        payload = payload_as_dict(input.retailer_click())
        if not payload:
            return
        try:
            track_event("outbound_click", payload)
        except (ValueError, sqlite3.Error) as exc:
            # Analytics must never interrupt navigation or the Shiny session.
            print(f"[analytics] outbound click not persisted: {exc}")

    @render.ui
    def topbar() -> Any:
        lang = active_language.get()

        # Build search choices directly in the rendered UI. This avoids calling
        # ui.update_selectize() while the Shiny session/input binding is still
        # starting or being rebound.
        choices: dict[str, str] = {}
        if database_ready():
            catalog = [
                product
                for product in get_commerce_catalog(active_influencer.get())
                if product_matches_category(product, active_category.get())
            ]
            choices = {
                str(product["product_id"]): (
                    f"{product['brand']} — {product['product_name']}"
                )
                for product in catalog
            }

        def lang_button(code: str) -> Any:
            active = " active" if code == lang else ""
            return ui.tags.button(
                code.upper(),
                type="button",
                class_=f"language-button{active}",
                onclick=(
                    f"Shiny.setInputValue('language_select',{{language:'{code}'}},{{priority:'event'}});"
                ),
                aria_label=f"Switch language to {code.upper()}",
            )

        return ui.div(
            ui.tags.button(
                ui.div("S", class_="brand-mark"),
                ui.h1(
                    "Skinfluencer",
                    ui.span("Demo", class_="demo-chip"),
                    class_="brand-name",
                ),
                type="button",
                class_="brand-lockup brand-home-button",
                aria_label="Skinfluencer home",
                onclick="Shiny.setInputValue('home_request',Date.now(),{priority:'event'});",
            ),
            ui.div(
                ui.input_selectize(
                    "product_query",
                    "",
                    choices=choices,
                    selected=None,
                    multiple=False,
                    options={
                        "placeholder": t("search_placeholder", lang),
                        "create": True,
                        "createOnBlur": True,
                        "persist": False,
                        "maxOptions": 180,
                        "closeAfterSelect": True,
                    },
                ),
                ui.input_action_button(
                    "search_button",
                    t("search_button", lang),
                    class_="search-submit",
                ),
                class_="top-search",
            ),
            ui.div(lang_button("en"), lang_button("tr"), class_="language-switch"),
            class_="topbar",
        )

    @render.ui
    def catalog_controls() -> Any:
        if selected_product_id.get() is not None:
            return None

        return ui.div(
            category_tabs_ui(active_category.get(), active_language.get()),
            ui.div(
                ui.div(
                    ui.tags.strong(t("catalog_instruction_bold", active_language.get())),
                    t("catalog_instruction_rest", active_language.get()),
                    class_="filter-copy",
                ),
                ui.div(
                    t("filter_by_influencer", active_language.get()),
                    class_="influencer-prompt",
                ),
                influencer_selector_ui(active_influencer.get(), active_language.get()),
                class_="filter-row",
            ),
            class_="catalog-controls",
        )

    @render.ui
    def database_message() -> Any:
        if database_ready():
            return None
        return ui.div(
            ui.div("⚠️", class_="empty-icon"),
            ui.h3(t("database_missing", active_language.get())),
            ui.p(t("database_missing_copy", active_language.get(), path=DB_PATH)),
            class_="empty-card",
        )

    @render.ui
    def catalog_content() -> Any:
        lang = active_language.get()
        if not database_ready():
            return None

        product_id = selected_product_id.get()
        if product_id is not None:
            product = get_product_by_id(int(product_id))
            if product is None:
                return None
            return product_detail_page(product, selected_detail_influencer.get(), lang)

        influencer_slug = active_influencer.get()
        category = active_category.get()
        query = submitted_query.get()

        if query:
            products = search_products_commerce(query, influencer_slug, category, limit=24)
            if query.isdigit() and products:
                display_query = f"{products[0]['brand']} — {products[0]['product_name']}"
            else:
                display_query = query
            heading = t("search_results", lang, query=display_query)
            description = t("search_results_copy", lang)
        elif category == "all":
            products = browse_products(influencer_slug, "skincare", limit=12)
            if influencer_slug == "all":
                heading = t("top_skincare", lang)
                description = t("top_skincare_copy", lang)
            else:
                influencer_name = INITIAL_INFLUENCERS[influencer_slug]
                heading = t("influencer_top_products", lang, name=influencer_name)
                description = t("influencer_top_products_copy", lang, name=influencer_name)
        else:
            products = browse_products(influencer_slug, category, limit=24)
            heading = category_tabs(lang)[category]
            description = t("category_results_copy", lang)

        if not products:
            return ui.div(
                ui.div("🔎", class_="empty-icon"),
                ui.h3(t("product_not_found", lang)),
                ui.p(t("product_not_found_copy", lang)),
                class_="empty-card",
            )

        return ui.div(
            ui.div(
                ui.div(ui.h2(heading), ui.p(description)),
                class_="section-heading",
            ),
            ui.div(
                *[
                    product_card(product, active_influencer=influencer_slug, lang=lang)
                    for product in products
                ],
                class_="product-grid",
            ),
        )


app = App(app_ui, server, static_assets=STATIC_DIR)
