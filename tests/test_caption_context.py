import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skinfluencer.extraction.caption_context_extractor import (
    validate_product_contexts,
)
from skinfluencer.storage.social_products import (
    SOCIAL_SCHEMA,
    context_candidates,
    context_candidates_sha256,
    import_context_payload,
    import_payload,
)


def test_caption_context_preserves_multiple_skin_type_contexts():
    caption = (
        "Güneş kremi önerisi KURU CİLT 4. "
        "Shiseido Expert Sun Protector Lotion SPF50+ "
        "KARMA CİLT 3. "
        "Shiseido Expert Sun Protector Lotion SPF50+"
    )

    candidates = [
        {
            "candidate_key": "candidate-1",
            "brand": "Shiseido",
            "product_name": "Expert Sun Protector Lotion SPF50+",
            "category": "skincare",
            "product_evidence_text": (
                "Shiseido Expert Sun Protector Lotion SPF50+"
            ),
        }
    ]

    contexts = [
        {
            "product_index": 0,
            "relationship": "recommendation",
            "topics": ["güneş kremi önerileri"],
            "audience_contexts": [
                "kuru cilt",
                "karma cilt",
            ],
            "opinion_summary": None,
            "opinion_scope": None,
            "evidence_texts": [
                "Güneş kremi önerisi",
                "KURU CİLT",
                "KARMA CİLT",
            ],
            "opinion_evidence_texts": [],
            "confidence": 0.97,
        }
    ]

    result = validate_product_contexts(
        contexts,
        caption,
        candidates,
    )

    assert result[0]["status"] == "approved"
    assert result[0]["relationship"] == "recommendation"
    assert result[0]["audience_contexts"] == [
        "kuru cilt",
        "karma cilt",
    ]
    assert "güneş kremi önerileri" in result[0]["summary"].lower()


def test_evidence_allows_typographic_case_and_whitespace_variants():
    caption = (
        "Güneş kremi önerisi   KARMA CİLT 1. "
        "Shiseido Expert Sun Protector Lotion SPF50+"
    )

    candidates = [
        {
            "candidate_key": "candidate-1",
            "brand": "Shiseido",
            "product_name": "Expert Sun Protector Lotion SPF50+",
            "category": "skincare",
            "product_evidence_text": (
                "Shiseido Expert Sun Protector Lotion SPF50+"
            ),
        }
    ]

    contexts = [
        {
            "product_index": 0,
            "relationship": "recommendation",
            "topics": ["güneş kremi önerileri"],
            "audience_contexts": ["karma cilt"],
            "opinion_summary": None,
            "opinion_scope": None,
            "evidence_texts": [
                "güneş kremi önerisi",
                "KARMA   CİLT",
            ],
            "opinion_evidence_texts": [],
            "confidence": 0.95,
        }
    ]

    result = validate_product_contexts(
        contexts,
        caption,
        candidates,
    )

    assert result[0]["status"] == "approved"
    assert result[0]["relationship"] == "recommendation"


def test_unsupported_context_is_not_surfaced():
    caption = "Shiseido Expert Sun Protector Lotion SPF50+"

    candidates = [
        {
            "candidate_key": "candidate-1",
            "brand": "Shiseido",
            "product_name": "Expert Sun Protector Lotion SPF50+",
            "category": "skincare",
            "product_evidence_text": caption,
        }
    ]

    contexts = [
        {
            "product_index": 0,
            "relationship": "recommendation",
            "topics": ["güneş kremi önerileri"],
            "audience_contexts": ["karma cilt"],
            "opinion_summary": None,
            "opinion_scope": None,
            "evidence_texts": [
                "bu metin caption içinde yok",
            ],
            "opinion_evidence_texts": [],
            "confidence": 0.99,
        }
    ]

    result = validate_product_contexts(
        contexts,
        caption,
        candidates,
    )

    assert result[0]["status"] == "review"
    assert result[0]["relationship"] == "mention_only"
    assert result[0]["summary"] is None


def test_context_cache_imports_into_social_view():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")

    schema = (
        ROOT / "src/skinfluencer/storage/schema.sql"
    ).read_text(encoding="utf-8")

    connection.executescript(schema)

    caption = (
        "Güneş kremi önerisi KARMA CİLT 1. "
        "Shiseido Expert Sun Protector Lotion SPF50+"
    )

    product_payload = {
        "extraction_status": "success",
        "platform": "tiktok",
        "influencer_slug": "yagmurvardar",
        "external_post_id": "7500000000000000001",
        "caption": caption,
        "original_url": (
            "https://www.tiktok.com/"
            "@yagmurvardar_/video/7500000000000000001"
        ),
        "published_at": "2026-09-01",
        "caption_sha256": __import__("hashlib")
        .sha256(caption.encode())
        .hexdigest(),
        "prompt_version": "test-products",
        "model": "test-model",
        "extracted_at": "2026-09-07T00:00:00+00:00",
        "products": [
            {
                "brand": "Shiseido",
                "product_name": (
                    "Expert Sun Protector Lotion SPF50+"
                ),
                "category": "skincare",
                "evidence_text": (
                    "Shiseido Expert Sun Protector Lotion SPF50+"
                ),
                "confidence": 1.0,
                "status": "approved",
                "status_reason": (
                    "exact_evidence_supports_product"
                ),
            }
        ],
    }

    import_payload(
        connection,
        product_payload,
        "product.json",
    )

    candidates = context_candidates(product_payload)

    context_payload = {
        "extraction_status": "success",
        "platform": "tiktok",
        "influencer_slug": "yagmurvardar",
        "external_post_id": product_payload[
            "external_post_id"
        ],
        "caption_sha256": product_payload[
            "caption_sha256"
        ],
        "product_candidates_sha256": (
            context_candidates_sha256(candidates)
        ),
        "prompt_version": "social-caption-context-v1",
        "model": "test-model",
        "extracted_at": "2026-09-07T00:01:00+00:00",
        "contexts": [
            {
                "candidate_key": candidates[0][
                    "candidate_key"
                ],
                "brand": "Shiseido",
                "product_name": (
                    "Expert Sun Protector Lotion SPF50+"
                ),
                "relationship": "recommendation",
                "topics": [
                    "güneş kremi önerileri"
                ],
                "audience_contexts": [
                    "karma cilt"
                ],
                "summary": (
                    "Karma cilt için güneş kremi "
                    "önerileri arasında listeliyor."
                ),
                "opinion_summary": None,
                "evidence_texts": [
                    "Güneş kremi önerisi",
                    "KARMA CİLT",
                ],
                "confidence": 0.96,
                "status": "approved",
                "status_reason": (
                    "caption_context_supported"
                ),
            }
        ],
    }

    imported = import_context_payload(
        connection,
        context_payload,
        product_payload,
        "context.json",
    )

    assert imported == 1

    row = connection.execute(
        "SELECT * FROM approved_social_product_links"
    ).fetchone()

    assert row["caption_relationship"] == "recommendation"
    assert json.loads(
        row["caption_audience_contexts_json"]
    ) == ["karma cilt"]
    assert "Karma cilt" in row["caption_summary"]