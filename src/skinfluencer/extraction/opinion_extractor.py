from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field


PROMPT_VERSION = "transcript-product-comments-v8-natural-summary"
SUMMARY_STYLE_VERSION = "conversational-tr-v4"
MAX_DISPLAY_SUMMARY_POINTS = 4
DEFAULT_DATA_ROOT = Path("data")


# ---------------------------------------------------------------------------
# STRUCTURED OUTPUT SCHEMA
# ---------------------------------------------------------------------------

class OpinionPoint(BaseModel):
    """One grounded product-specific claim and its exact transcript evidence."""

    claim: str = Field(
        description=(
            "One natural, user-facing Turkish sentence about the influencer's product-specific "
            "view or experience. Paraphrase the meaning instead of copying transcript wording, "
            "but preserve every qualifier, limitation and sentiment. It must be fully supported "
            "by evidence_text. Write in a warm, clear third-person style such as 'Ürünü çok "
            "seviyor' or 'Hassas ciltlerde seyrek başlanmasını öneriyor'. Avoid repetitive "
            "reporting phrases such as 'söylüyor', 'belirtiyor' and 'ifade ediyor'. Do not "
            "mention the product name unless needed for clarity. Use grammatically natural "
            "Turkish; avoid awkward literal constructions and repeated subjects."
        )
    )
    polarity: Literal["positive", "negative", "neutral"] = Field(
        description=(
            "positive for praise/recommendation/benefit; negative for criticism, "
            "drawback, warning, restriction or price complaint; neutral for factual usage advice."
        )
    )
    evidence_text: str = Field(
        description=(
            "An exact contiguous excerpt copied from the transcript that directly and fully "
            "supports this single claim. Preserve ASR errors and punctuation."
        )
    )


class ProductCommentResult(BaseModel):
    """Transcript result for one product candidate supplied by the caller."""

    candidate_id: str = Field(
        description="Candidate ID copied exactly from the supplied product catalog."
    )
    mention_status: Literal[
        "reviewed",
        "mentioned_without_opinion",
        "not_mentioned",
    ] = Field(
        description=(
            "reviewed: the influencer expresses a product-specific opinion; "
            "mentioned_without_opinion: the product is named but no meaningful opinion is given; "
            "not_mentioned: there is insufficient transcript evidence that the product appears."
        )
    )
    raw_product_mentions: list[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Up to three exact contiguous excerpts copied from the transcript showing how "
            "the product name was spoken or mistranscribed. Empty when not mentioned."
        ),
    )
    opinion_points: list[OpinionPoint] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Grounded product-specific claims. Each claim must have its own exact transcript evidence. "
            "Empty unless mention_status is reviewed."
        ),
    )
    overall_sentiment: Literal[
        "positive",
        "negative",
        "mixed",
        "neutral",
        "unclear",
        "not_applicable",
    ] = Field(
        description=(
            "Overall product-specific sentiment across the complete discussion. "
            "Use mixed whenever both meaningful praise and criticism/limitation are present. "
            "Use not_applicable unless mention_status is reviewed."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that this candidate was matched to the correct transcript passage.",
    )
    notes: str | None = Field(
        description="Brief uncertainty note; null when no clarification is necessary."
    )


class TranscriptCommentExtraction(BaseModel):
    results: list[ProductCommentResult] = Field(
        description="One result for every supplied candidate ID."
    )


SYSTEM_PROMPT = """You match a known beauty-product catalog to an automatic YouTube transcript.

The caller supplies:
1. a fixed product candidate list extracted from the video's own description;
2. the video's complete automatic transcript.

The description-grounded brand and product wording is already supplied and fixed for this
run. It is not necessarily a retailer-verified canonical catalog. Do NOT discover, rename,
correct, complete, merge or invent products. Your task is only to determine what the
influencer says in the transcript about each supplied candidate.

AUTOMATIC TRANSCRIPT REALITY

The transcript may severely distort brand and product names. Match conservatively using:
- phonetic similarity;
- recognizable brand or product fragments;
- shade, number, SPF, concentration or variant;
- product category;
- order of products in the supplied description list and transcript;
- nearby discussion and product characteristics.

Do not force a match when the evidence is weak.

STRICT OUTPUT RULES

1. Return exactly one result for every supplied candidate_id.
2. Never return a candidate_id that was not supplied.
3. Do not add products outside the candidate list.
4. mention_status:
   - reviewed: the influencer gives a meaningful product-specific evaluation,
     experience, recommendation, criticism, comparison, warning or usage opinion;
   - mentioned_without_opinion: identifiable, but no meaningful opinion is given;
   - not_mentioned: insufficient transcript evidence.
5. raw_product_mentions must be copied EXACTLY and contiguously from the transcript.
6. For reviewed products, return opinion_points. Every point must contain:
   - one natural, user-facing Turkish sentence;
   - one polarity;
   - one exact contiguous evidence_text copied from the transcript.
   Never insert "...", an ellipsis character, a replacement word or corrected spelling into
   evidence_text unless those exact characters occur in the transcript. Never join two
   non-contiguous transcript passages into one evidence_text. When two passages support two
   claims, return two separate opinion_points with one exact excerpt each.
7. Write claims in a warm and conversational third-person style suitable for direct display
   in an application. Paraphrase the influencer's meaning; do not simply copy or lightly
   punctuate the transcript sentence. Prefer wording such as "Ürünü çok seviyor", "Tekrar
   satın almayı düşünüyor" or "Hassas ciltlerde dikkatli kullanılmasını öneriyor". Avoid
   repetitive phrases such as "söylüyor", "belirtiyor", "ifade ediyor" and "bahsediyor".
   The exact original wording belongs only in evidence_text. Use fluent, grammatically natural
   Turkish rather than mirroring malformed ASR syntax. Avoid repeating the product or brand at
   the start of consecutive claims when a pronoun or omission is clear.
8. One opinion point must contain only one claim. Split positive and negative ideas into
   separate points. Do not write a claim containing contrast conjunctions such as "ama",
   "ancak", "fakat", "bununla birlikte" or "yine de". Never place a claim in the output
   unless its own evidence_text directly supports the entire claim.
9. Preserve scope and uncertainty exactly. If the influencer says "bende", "bence",
   "sanırım", "olabilir" or speaks only about a specific skin/hair type, the claim must not
   become a universal fact. Use wording such as "Kendi deneyiminde...", "...buluyor" or
   "...olabileceğini düşünüyor" when needed. Do not add causal explanations, ingredients,
   durations, suitability claims or recommendations that are absent from the evidence.
   Preserve concrete states and unambiguous quantities exactly: for example, do not change
   "ürün bitti" into "ürün bitmek üzere" or vice versa. Automatic transcripts may merge a
   spoken range into one number (for example, "4-5" may appear as "45"). Never silently guess
   or copy an implausible/ambiguous numeric value into a claim. Omit the exact number, express
   only a qualitative judgment when surrounding evidence explicitly supports it, or omit that
   point. The evidence_text itself must still remain an exact transcript substring. Never infer
   purchase, repurchase, avoidance or reluctance from praise, criticism, price or ingredient
   disclosure alone. Include purchase intent only when the evidence explicitly states that the
   influencer bought, will buy, will not buy or is considering buying the product again.
10. Order opinion_points by user value: overall verdict first, then the most important benefits,
   drawbacks, suitability warnings, repurchase intent and practical usage advice. Return at most
   six material points; omit repetitions and minor filler.
11. Capture material criticism even when the overall review is favorable, including price,
    irritation, dryness, limitations and suitability warnings.
12. Polarity rules:
   - positive: praise, benefit, recommendation, repurchase intent;
   - negative: criticism, drawback, warning, restriction, irritation, dryness or price complaint;
   - neutral: factual usage frequency or application advice without praise/criticism.
   A general statement that a skincare step or product category is optional or non-essential
   is neutral, not negative, unless the influencer explicitly criticizes this specific product.
   Such a general routine comment must not by itself turn a positive product review into mixed.
13. Set overall_sentiment from the complete product discussion. Use mixed whenever the
   influencer expresses both meaningful praise and criticism, limitation, warning or
   disappointment, even if one side is brief. Before returning, verify that overall_sentiment
   agrees with the polarities in opinion_points: mixed requires at least one positive and one
   negative point, each with its own exact contiguous evidence. If both sides cannot be returned
   with exact evidence, do not label the result mixed.
14. If mention_status is not reviewed, opinion_points must be empty and overall_sentiment
    must be not_applicable.
15. Never use web knowledge, the description, or general ingredient knowledge as transcript evidence.
16. Preserve automatic-transcript errors in raw_product_mentions and evidence_text.
"""


# ---------------------------------------------------------------------------
# GENERAL UTILITIES
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_text(serialized)


def normalize_for_dedupe(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.casefold().split()).strip(" :;,.-–—")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_parsed_output(response: Any) -> TranscriptCommentExtraction:
    """Extract the Pydantic object returned by responses.parse()."""
    for output in response.output:
        if getattr(output, "type", None) != "message":
            continue

        for item in getattr(output, "content", []):
            if getattr(item, "type", None) != "output_text":
                continue

            parsed = getattr(item, "parsed", None)
            if parsed is None:
                continue

            if isinstance(parsed, TranscriptCommentExtraction):
                return parsed

            return TranscriptCommentExtraction.model_validate(parsed)

    raise RuntimeError("OpenAI yanıtında parse edilmiş structured output bulunamadı.")


def usage_to_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    if hasattr(usage, "to_dict"):
        return usage.to_dict()

    if hasattr(usage, "model_dump"):
        return usage.model_dump()

    return None


# ---------------------------------------------------------------------------
# INPUT LOADING
# ---------------------------------------------------------------------------

def load_description_products(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))

    if payload.get("extraction_status") != "success":
        raise ValueError(
            f"Description extraction başarılı değil: {path.name}"
        )

    if not isinstance(payload.get("products"), list):
        raise ValueError(
            f"Description extraction içinde products listesi yok: {path.name}"
        )

    return payload


def build_candidates(
    description_payload: dict[str, Any],
    *,
    include_review_products: bool,
) -> list[dict[str, Any]]:
    """Create stable, deduplicated product candidates for the transcript model."""
    allowed_statuses = {"approved"}
    if include_review_products:
        allowed_statuses.add("review")

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for product in description_payload.get("products", []):
        source_status = product.get("status")
        if source_status not in allowed_statuses:
            continue

        brand = product.get("brand")
        product_name = str(product.get("product_name") or "").strip()
        if not product_name:
            continue

        dedupe_key = (
            normalize_for_dedupe(brand),
            normalize_for_dedupe(product_name),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        candidate_id = f"P{len(candidates) + 1:03d}"

        candidates.append(
            {
                "candidate_id": candidate_id,
                "canonical_brand": brand,
                "canonical_product_name": product_name,
                "category": product.get("category"),
                "description_evidence_text": product.get("evidence_text"),
                "description_confidence": product.get("confidence"),
                "description_status": source_status,
            }
        )

    return candidates


def find_metadata_path(
    metadata_dir: Path,
    *,
    expected_filename: str,
    video_id: str,
) -> Path | None:
    direct = metadata_dir / expected_filename
    if direct.exists():
        return direct

    for path in metadata_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if payload.get("video_id") == video_id:
            return path

    return None


def find_transcript_path(
    transcript_dir: Path,
    *,
    expected_stem: str,
    video_id: str,
) -> Path | None:
    direct = transcript_dir / f"{expected_stem}.txt"
    if direct.exists():
        return direct

    matches = sorted(transcript_dir.glob(f"*__{video_id}.txt"))
    if matches:
        return matches[0]

    for path in transcript_dir.glob("*.txt"):
        if video_id in path.stem:
            return path

    return None


def collect_description_files(
    description_dir: Path,
    *,
    video_id: str | None,
    limit: int | None,
    allowed_video_ids: set[str] | None = None,
) -> list[Path]:
    ignored_names = {
        "summary.json",
        "failures.json",
        "description_products_all.json",
    }

    paths = sorted(
        path
        for path in description_dir.glob("*.json")
        if path.name not in ignored_names
    )

    if video_id or allowed_video_ids is not None:
        selected: list[Path] = []

        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            current_video_id = str(payload.get("video_id") or "").strip()
            if not current_video_id:
                continue
            if video_id and current_video_id != video_id:
                continue
            if (
                allowed_video_ids is not None
                and current_video_id not in allowed_video_ids
            ):
                continue

            selected.append(path)

        paths = selected

    if limit is not None:
        paths = paths[:limit]

    return paths


def extract_video_ids(payload: Any) -> set[str]:
    """Recursively collect video_id values from a downloader report payload."""
    video_ids: set[str] = set()

    if isinstance(payload, dict):
        video_id = payload.get("video_id")
        if isinstance(video_id, str) and video_id.strip():
            video_ids.add(video_id.strip())

        for value in payload.values():
            video_ids.update(extract_video_ids(value))

    elif isinstance(payload, list):
        for value in payload:
            video_ids.update(extract_video_ids(value))

    return video_ids


def load_report_video_ids(report_dir: Path) -> set[str]:
    """Load the exact long/Shorts membership recorded by downloader reports."""
    if not report_dir.exists():
        raise FileNotFoundError(f"Rapor klasörü bulunamadı: {report_dir}")

    video_ids: set[str] = set()
    readable_report_count = 0

    for path in sorted(report_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        readable_report_count += 1
        video_ids.update(extract_video_ids(payload))

    if readable_report_count == 0:
        raise RuntimeError(
            f"Okunabilir JSON raporu bulunamadı: {report_dir}"
        )
    if not video_ids:
        raise RuntimeError(
            f"Raporlarda video_id bulunamadı: {report_dir}"
        )

    return video_ids


def resolve_model(
    cli_model: str | None,
    description_payload: dict[str, Any],
) -> str:
    """Prefer CLI, then environment, then the model used for description extraction."""
    model = (
        cli_model
        or os.getenv("OPENAI_TRANSCRIPT_MODEL")
        or os.getenv("OPENAI_MODEL")
        or description_payload.get("model")
    )

    if not model:
        raise RuntimeError(
            "Model belirlenemedi. --model ver veya .env içine "
            "OPENAI_TRANSCRIPT_MODEL ekle."
        )

    return str(model)


# ---------------------------------------------------------------------------
# PROMPT AND VALIDATION
# ---------------------------------------------------------------------------

def build_user_input(
    *,
    title: str,
    candidates: list[dict[str, Any]],
    transcript: str,
) -> str:
    compact_candidates = [
        {
            "candidate_id": item["candidate_id"],
            "canonical_brand": item["canonical_brand"],
            "canonical_product_name": item["canonical_product_name"],
            "category": item["category"],
            "description_evidence_text": item["description_evidence_text"],
        }
        for item in candidates
    ]

    return (
        "VIDEO TITLE\n"
        "===========\n"
        f"{title}\n\n"
        "FIXED PRODUCT CANDIDATES FROM THE VIDEO DESCRIPTION\n"
        "===================================================\n"
        f"{json.dumps(compact_candidates, ensure_ascii=False, indent=2)}\n\n"
        "COMPLETE AUTOMATIC TRANSCRIPT\n"
        "=============================\n"
        f"{transcript}"
    )


def exact_excerpt_validation(
    transcript: str,
    excerpts: list[str],
) -> tuple[bool, list[dict[str, Any]]]:
    validations: list[dict[str, Any]] = []
    all_exact = True

    for excerpt in excerpts:
        index = transcript.find(excerpt)
        exact = index >= 0
        all_exact = all_exact and exact

        validations.append(
            {
                "text": excerpt,
                "exact": exact,
                "start_character": index if exact else None,
                "end_character": index + len(excerpt) if exact else None,
            }
        )

    return all_exact, validations


def default_not_mentioned_result(candidate_id: str) -> ProductCommentResult:
    return ProductCommentResult(
        candidate_id=candidate_id,
        mention_status="not_mentioned",
        raw_product_mentions=[],
        opinion_points=[],
        overall_sentiment="not_applicable",
        confidence=0.0,
        notes="Model bu candidate_id için sonuç döndürmedi; programatik olarak eklendi.",
    )


def normalize_model_results(
    extraction: TranscriptCommentExtraction,
    candidates: list[dict[str, Any]],
) -> tuple[
    dict[str, ProductCommentResult],
    list[dict[str, Any]],
]:
    """Reject unknown/duplicate IDs and fill any missing candidate records."""
    valid_ids = {item["candidate_id"] for item in candidates}
    results_by_id: dict[str, ProductCommentResult] = {}
    rejected_model_results: list[dict[str, Any]] = []

    for result in extraction.results:
        if result.candidate_id not in valid_ids:
            rejected_model_results.append(
                {
                    "reason": "unknown_candidate_id",
                    "model_result": result.model_dump(),
                }
            )
            continue

        if result.candidate_id in results_by_id:
            rejected_model_results.append(
                {
                    "reason": "duplicate_candidate_id",
                    "model_result": result.model_dump(),
                }
            )
            continue

        results_by_id[result.candidate_id] = result

    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        if candidate_id not in results_by_id:
            results_by_id[candidate_id] = default_not_mentioned_result(
                candidate_id
            )

    return results_by_id, rejected_model_results


def ensure_sentence(text: str) -> str:
    """Normalize a model claim into one clean display sentence."""
    sentence = " ".join(text.strip().split())
    if not sentence:
        return ""

    sentence = sentence.rstrip(" .;:")
    return sentence + "."


def starts_with_contrast(text: str) -> bool:
    normalized = text.casefold().lstrip()
    return normalized.startswith(
        (
            "ancak ",
            "ama ",
            "fakat ",
            "bununla birlikte ",
            "öte yandan ",
            "yine de ",
            "buna rağmen ",
        )
    )


def select_display_points(
    opinion_points: list[OpinionPoint],
    *,
    max_points: int = MAX_DISPLAY_SUMMARY_POINTS,
) -> list[tuple[int, OpinionPoint]]:
    """Select concise points while preserving both sides of a mixed review."""
    indexed = [
        (index, point)
        for index, point in enumerate(opinion_points)
        if point.claim.strip()
    ]
    if len(indexed) <= max_points:
        return indexed

    selected = indexed[:max_points]
    selected_polarities = {point.polarity for _, point in selected}
    all_polarities = {point.polarity for _, point in indexed}

    # A mixed review must surface at least one positive and one negative point.
    for required in ("negative", "positive"):
        if required not in all_polarities or required in selected_polarities:
            continue

        replacement = next(
            (
                candidate
                for candidate in indexed[max_points:]
                if candidate[1].polarity == required
            ),
            None,
        )
        if replacement is None:
            continue

        replace_position = next(
            (
                position
                for position in range(len(selected) - 1, -1, -1)
                if selected[position][1].polarity == "neutral"
            ),
            len(selected) - 1,
        )
        selected[replace_position] = replacement
        selected_polarities = {point.polarity for _, point in selected}

    return sorted(selected, key=lambda item: item[0])


def build_conversational_summary(
    opinion_points: list[OpinionPoint],
) -> tuple[str | None, str | None, str, list[int]]:
    """Create audit and display summaries only from grounded atomic claims."""
    valid_points = [point for point in opinion_points if point.claim.strip()]
    if not valid_points:
        return None, None, "not_applicable", []

    grounded_summary = " ".join(
        ensure_sentence(point.claim)
        for point in valid_points
        if ensure_sentence(point.claim)
    ) or None

    selected = select_display_points(valid_points)
    display_sentences: list[str] = []
    previous_opinion_polarity: str | None = None

    for _, point in selected:
        sentence = ensure_sentence(point.claim)
        if not sentence:
            continue

        if (
            point.polarity == "negative"
            and previous_opinion_polarity == "positive"
            and not starts_with_contrast(sentence)
        ):
            sentence = "Ancak " + sentence[0].lower() + sentence[1:]
        elif (
            point.polarity == "positive"
            and previous_opinion_polarity == "negative"
            and not starts_with_contrast(sentence)
        ):
            sentence = "Buna rağmen " + sentence[0].lower() + sentence[1:]

        display_sentences.append(sentence)
        if point.polarity in {"positive", "negative"}:
            previous_opinion_polarity = point.polarity

    display_summary = " ".join(display_sentences) or grounded_summary

    polarities = {point.polarity for point in valid_points}
    has_positive = "positive" in polarities
    has_negative = "negative" in polarities

    if has_positive and has_negative:
        sentiment = "mixed"
    elif has_positive:
        sentiment = "positive"
    elif has_negative:
        sentiment = "negative"
    elif polarities == {"neutral"}:
        sentiment = "neutral"
    else:
        sentiment = "unclear"

    summary_point_indexes = [index for index, _ in selected]
    return grounded_summary, display_summary, sentiment, summary_point_indexes

def validate_product_comment(
    candidate: dict[str, Any],
    result: ProductCommentResult,
    transcript: str,
) -> dict[str, Any]:
    """Validate each model item independently.

    A single malformed/non-exact opinion point must not demote an otherwise
    well-grounded product review. Only exact transcript excerpts are retained
    for the user-facing summary and database record.
    """
    raw_validations: list[dict[str, Any]] = []
    valid_raw_mentions: list[str] = []
    discarded_raw_mentions: list[dict[str, Any]] = []

    for raw_mention in result.raw_product_mentions:
        index = transcript.find(raw_mention)
        exact = index >= 0
        validation = {
            "text": raw_mention,
            "exact": exact,
            "start_character": index if exact else None,
            "end_character": index + len(raw_mention) if exact else None,
        }
        raw_validations.append(validation)

        if exact:
            valid_raw_mentions.append(raw_mention)
        else:
            discarded_raw_mentions.append(
                {
                    "text": raw_mention,
                    "reason": "not_exact_transcript_substring",
                }
            )

    valid_opinion_objects: list[OpinionPoint] = []
    valid_opinion_payloads: list[dict[str, Any]] = []
    discarded_opinion_points: list[dict[str, Any]] = []

    contrast_pattern = re.compile(
        r"\b(ama|ancak|fakat|bununla birlikte|yine de|öte yandan)\b",
        flags=re.IGNORECASE,
    )

    for point in result.opinion_points:
        claim = " ".join(point.claim.strip().split())
        index = transcript.find(point.evidence_text)
        evidence_exact = index >= 0

        payload = {
            "claim": claim,
            "polarity": point.polarity,
            "evidence_text": point.evidence_text,
            "evidence_exact": evidence_exact,
            "start_character": index if evidence_exact else None,
            "end_character": (
                index + len(point.evidence_text)
                if evidence_exact
                else None
            ),
        }

        discard_reasons: list[str] = []

        if not claim:
            discard_reasons.append("empty_claim")
        if not evidence_exact:
            discard_reasons.append("evidence_not_exact_transcript_substring")

        # This is a quality warning rather than a hard rejection. The evidence
        # remains grounded, but the point may combine two views. Keeping it is
        # safer than losing the whole product review.
        if contrast_pattern.search(claim):
            payload["quality_warning"] = "claim_may_contain_multiple_views"

        if discard_reasons:
            discarded_opinion_points.append(
                {
                    **payload,
                    "discard_reasons": discard_reasons,
                }
            )
            continue

        valid_opinion_objects.append(point)
        valid_opinion_payloads.append(payload)

    (
        grounded_summary,
        display_summary,
        derived_sentiment,
        summary_point_indexes,
    ) = build_conversational_summary(valid_opinion_objects)

    if result.mention_status != "reviewed":
        sentiment = "not_applicable"
    elif derived_sentiment in {
        "positive",
        "negative",
        "mixed",
        "neutral",
    }:
        # The public sentiment is derived only from opinion points whose
        # evidence survived exact-substring validation.
        sentiment = derived_sentiment
    else:
        sentiment = result.overall_sentiment

    evidence_texts = [
        point["evidence_text"]
        for point in valid_opinion_payloads
    ]

    critical_validation_issues: list[str] = []
    quality_warnings: list[str] = []

    if (
        result.mention_status == "reviewed"
        and result.overall_sentiment == "mixed"
        and derived_sentiment != "mixed"
    ):
        critical_validation_issues.append(
            "model_mixed_but_grounded_points_not_mixed"
        )

    if discarded_raw_mentions:
        quality_warnings.append(
            "some_raw_product_mentions_were_discarded"
        )
    if discarded_opinion_points:
        quality_warnings.append(
            "some_opinion_points_were_discarded"
        )
    if any(
        point.get("quality_warning")
        for point in valid_opinion_payloads
    ):
        quality_warnings.append(
            "one_or_more_claims_may_combine_multiple_views"
        )

    if result.mention_status == "not_mentioned":
        if valid_raw_mentions:
            critical_validation_issues.append(
                "not_mentioned_but_exact_raw_mentions_present"
            )
        if valid_opinion_payloads:
            critical_validation_issues.append(
                "not_mentioned_but_exact_opinion_points_present"
            )

    elif result.mention_status == "mentioned_without_opinion":
        if not valid_raw_mentions:
            critical_validation_issues.append(
                "mentioned_without_opinion_but_no_exact_raw_mention"
            )
        if valid_opinion_payloads:
            critical_validation_issues.append(
                "mentioned_without_opinion_but_exact_opinion_points_present"
            )

    elif result.mention_status == "reviewed":
        if not valid_raw_mentions:
            critical_validation_issues.append(
                "reviewed_but_no_exact_raw_mention"
            )
        if not valid_opinion_payloads:
            critical_validation_issues.append(
                "reviewed_but_no_exact_opinion_point"
            )

    source_status = candidate.get("description_status")

    if result.mention_status == "not_mentioned":
        final_status = "rejected"
        status_reason = "not_mentioned_in_transcript"

    elif result.mention_status == "mentioned_without_opinion":
        final_status = "rejected"
        status_reason = "mentioned_without_meaningful_opinion"

    elif (
        source_status == "approved"
        and result.confidence >= 0.75
        and bool(valid_raw_mentions)
        and bool(valid_opinion_payloads)
        and not critical_validation_issues
    ):
        final_status = "approved"
        if discarded_raw_mentions or discarded_opinion_points:
            status_reason = (
                "validated_with_non_exact_model_items_discarded"
            )
        else:
            status_reason = (
                "description_product_and_grounded_transcript_comment_validated"
            )

    else:
        final_status = "review"
        if source_status != "approved":
            status_reason = "description_product_not_approved"
        elif result.confidence < 0.75:
            status_reason = "transcript_match_confidence_below_0_75"
        elif critical_validation_issues:
            status_reason = "insufficient_exact_grounded_content"
        else:
            status_reason = "manual_review_required"

    return {
        **candidate,
        "mention_status": result.mention_status,

        # Only exact transcript excerpts are exposed to the app/database.
        "raw_product_mentions": valid_raw_mentions,
        "opinion_points": valid_opinion_payloads,
        "evidence_texts": evidence_texts,

        # Full audit trail is preserved separately.
        "model_raw_product_mentions": result.raw_product_mentions,
        "model_opinion_points": [
            point.model_dump()
            for point in result.opinion_points
        ],
        "discarded_raw_product_mentions": discarded_raw_mentions,
        "discarded_opinion_points": discarded_opinion_points,

        "summary": display_summary,
        "display_summary": display_summary,
        "grounded_summary": grounded_summary,
        "summary_style_version": SUMMARY_STYLE_VERSION,
        "summary_point_indexes": summary_point_indexes,
        "sentiment": sentiment,
        "derived_sentiment": derived_sentiment,
        "model_overall_sentiment": result.overall_sentiment,
        "confidence": result.confidence,
        "notes": result.notes,
        "raw_mention_validation": raw_validations,
        "validation_issues": critical_validation_issues,
        "quality_warnings": quality_warnings,
        "status": final_status,
        "status_reason": status_reason,
    }


# ---------------------------------------------------------------------------
# API EXTRACTION
# ---------------------------------------------------------------------------

def extract_transcript_comments(
    client: OpenAI,
    *,
    model: str,
    title: str,
    candidates: list[dict[str, Any]],
    transcript: str,
) -> tuple[TranscriptCommentExtraction, Any]:
    response = client.responses.parse(
        model=model,
        store=False,
        input=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": build_user_input(
                    title=title,
                    candidates=candidates,
                    transcript=transcript,
                ),
            },
        ],
        text_format=TranscriptCommentExtraction,
    )

    return get_parsed_output(response), response


def existing_output_is_current(
    output_path: Path,
    *,
    transcript_sha256: str,
    candidate_catalog_sha256: str,
    model: str,
    include_review_products: bool,
    content_type: str,
) -> bool:
    if not output_path.exists():
        return False

    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return (
        payload.get("extraction_status") == "success"
        and payload.get("prompt_version") == PROMPT_VERSION
        and payload.get("summary_style_version") == SUMMARY_STYLE_VERSION
        and payload.get("transcript_sha256") == transcript_sha256
        and payload.get("candidate_catalog_sha256")
        == candidate_catalog_sha256
        and payload.get("model") == model
        and payload.get("include_review_products")
        is include_review_products
        and payload.get("content_type") == content_type
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Description'dan çıkarılmış sabit ürün listesini kullanarak "
            "YouTube transcriptinden kanıtlı ürün yorumları ve uygulamada doğrudan "
            "gösterilebilecek doğal Türkçe özetler çıkarır."
        )
    )
    parser.add_argument("--influencer", required=True)
    parser.add_argument("--model")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument(
        "--output-subdir",
        help=(
            "Influencer klasörü altında kullanılacak çıktı klasörü. Verilmezse "
            "long ve Shorts çalışmaları otomatik olarak ayrı klasörlere yazılır."
        ),
    )
    parser.add_argument(
        "--content-type",
        choices=("long", "shorts", "all"),
        default="all",
        help=(
            "İşlenecek içerik türü. long/shorts seçimi downloader raporlarındaki "
            "video_id listelerine göre yapılır."
        ),
    )
    parser.add_argument("--video-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--include-review-products",
        action="store_true",
        help="Description extraction içindeki review ürünlerini de modele gönder.",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY bulunamadı. .env dosyasına veya terminal ortamına ekle."
        )

    influencer_dir = args.data_root / args.influencer
    metadata_dir = influencer_dir / "metadata"
    transcript_dir = influencer_dir / "transcripts"
    description_dir = influencer_dir / "description_products_llm"

    if args.output_subdir:
        output_subdir = args.output_subdir
    elif args.content_type == "all":
        output_subdir = "product_mentions_llm_final_v2"
    else:
        output_subdir = (
            f"product_mentions_llm_final_v2_{args.content_type}"
        )

    output_dir = influencer_dir / output_subdir

    for required_dir in (
        metadata_dir,
        transcript_dir,
        description_dir,
    ):
        if not required_dir.exists():
            raise FileNotFoundError(
                f"Gerekli klasör bulunamadı: {required_dir}"
            )

    allowed_video_ids: set[str] | None = None
    report_dir: Path | None = None
    if args.content_type != "all":
        report_dir = influencer_dir / f"reports_{args.content_type}"
        allowed_video_ids = load_report_video_ids(report_dir)

    description_paths = collect_description_files(
        description_dir,
        video_id=args.video_id,
        limit=args.limit,
        allowed_video_ids=allowed_video_ids,
    )

    if not description_paths:
        raise RuntimeError(
            "İşlenecek description product JSON dosyası bulunamadı."
        )

    client = OpenAI(max_retries=args.max_retries)
    output_dir.mkdir(parents=True, exist_ok=True)

    combined: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    processed_count = 0
    skipped_count = 0

    print("=" * 92)
    print("TRANSCRIPT PRODUCT COMMENT EXTRACTOR")
    print("=" * 92)
    print(f"Influencer:   {args.influencer}")
    print(f"Description:  {description_dir}")
    print(f"Transcript:   {transcript_dir}")
    print(f"Çıktı:        {output_dir}")
    print(f"İçerik türü:  {args.content_type}")
    if report_dir is not None and allowed_video_ids is not None:
        print(f"Rapor:        {report_dir}")
        print(f"Rapor ID:     {len(allowed_video_ids)}")
    print(f"Dosya sayısı: {len(description_paths)}")
    print()

    for index, description_path in enumerate(
        description_paths,
        start=1,
    ):
        try:
            description_payload = load_description_products(
                description_path
            )
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            failures.append(
                {
                    "description_file": str(description_path),
                    "reason": reason,
                }
            )
            print(
                f"[{index}/{len(description_paths)}] "
                f"{description_path.name}: HATA — {reason}"
            )
            continue

        video_id = str(
            description_payload.get("video_id")
            or description_path.stem
        )
        title = str(description_payload.get("title") or "")
        model = resolve_model(args.model, description_payload)

        candidates = build_candidates(
            description_payload,
            include_review_products=args.include_review_products,
        )

        if not candidates:
            print(
                f"[{index}/{len(description_paths)}] {video_id}: "
                "ATLANDI — uygun description ürünü yok"
            )
            continue

        metadata_path = find_metadata_path(
            metadata_dir,
            expected_filename=description_path.name,
            video_id=video_id,
        )
        if metadata_path is None:
            reason = "Eşleşen metadata JSON bulunamadı."
            failures.append(
                {
                    "video_id": video_id,
                    "description_file": str(description_path),
                    "reason": reason,
                }
            )
            print(
                f"[{index}/{len(description_paths)}] {video_id}: "
                f"HATA — {reason}"
            )
            continue

        transcript_path = find_transcript_path(
            transcript_dir,
            expected_stem=metadata_path.stem,
            video_id=video_id,
        )
        if transcript_path is None:
            reason = "Eşleşen transcript TXT bulunamadı."
            failures.append(
                {
                    "video_id": video_id,
                    "metadata_file": str(metadata_path),
                    "reason": reason,
                }
            )
            print(
                f"[{index}/{len(description_paths)}] {video_id}: "
                f"HATA — {reason}"
            )
            continue

        transcript = transcript_path.read_text(
            encoding="utf-8"
        ).strip()
        if len(transcript) < 100:
            reason = "Transcript 100 karakterden kısa."
            failures.append(
                {
                    "video_id": video_id,
                    "transcript_file": str(transcript_path),
                    "reason": reason,
                }
            )
            print(
                f"[{index}/{len(description_paths)}] {video_id}: "
                f"HATA — {reason}"
            )
            continue

        transcript_sha256 = sha256_text(transcript)
        candidate_catalog_sha256 = canonical_json_sha256(candidates)
        output_path = output_dir / description_path.name

        if (
            not args.force
            and existing_output_is_current(
                output_path,
                transcript_sha256=transcript_sha256,
                candidate_catalog_sha256=candidate_catalog_sha256,
                model=model,
                include_review_products=args.include_review_products,
                content_type=args.content_type,
            )
        ):
            existing = json.loads(
                output_path.read_text(encoding="utf-8")
            )
            combined.append(existing)
            skipped_count += 1
            print(
                f"[{index}/{len(description_paths)}] {video_id}: "
                f"ATLANDI ({existing.get('approved_count', 0)} approved)"
            )
            continue

        print(
            f"[{index}/{len(description_paths)}] {video_id}: "
            f"{len(candidates)} ürün — {title}"
        )

        try:
            extraction, response = extract_transcript_comments(
                client,
                model=model,
                title=title,
                candidates=candidates,
                transcript=transcript,
            )

            results_by_id, rejected_model_results = (
                normalize_model_results(
                    extraction,
                    candidates,
                )
            )

            product_mentions = [
                validate_product_comment(
                    candidate,
                    results_by_id[candidate["candidate_id"]],
                    transcript,
                )
                for candidate in candidates
            ]

            approved_count = sum(
                item["status"] == "approved"
                for item in product_mentions
            )
            review_count = sum(
                item["status"] == "review"
                for item in product_mentions
            )
            rejected_count = sum(
                item["status"] == "rejected"
                for item in product_mentions
            )

            result_payload = {
                "schema_version": 2,
                "prompt_version": PROMPT_VERSION,
                "summary_style_version": SUMMARY_STYLE_VERSION,
                "extraction_status": "success",
                "extracted_at": utc_now_iso(),
                "model": model,
                "response_id": getattr(response, "id", None),
                "include_review_products": (
                    args.include_review_products
                ),
                "content_type": args.content_type,
                "video_id": video_id,
                "title": title,
                "channel": description_payload.get("channel"),
                "upload_date": description_payload.get(
                    "upload_date"
                ),
                "url": description_payload.get("url"),
                "metadata_file": str(metadata_path),
                "transcript_file": str(transcript_path),
                "description_products_file": str(description_path),
                "transcript_sha256": transcript_sha256,
                "transcript_character_count": len(transcript),
                "candidate_catalog_sha256": (
                    candidate_catalog_sha256
                ),
                "candidate_count": len(candidates),
                "product_mentions": product_mentions,
                "approved_count": approved_count,
                "review_count": review_count,
                "rejected_count": rejected_count,
                "rejected_model_results": rejected_model_results,
                "usage": usage_to_dict(response),
            }

            write_json(output_path, result_payload)
            combined.append(result_payload)
            processed_count += 1

            print(
                "    "
                f"{approved_count} approved, "
                f"{review_count} review, "
                f"{rejected_count} rejected"
            )

        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            failure_payload = {
                "schema_version": 2,
                "prompt_version": PROMPT_VERSION,
                "summary_style_version": SUMMARY_STYLE_VERSION,
                "extraction_status": "failed",
                "extracted_at": utc_now_iso(),
                "model": model,
                "content_type": args.content_type,
                "video_id": video_id,
                "title": title,
                "description_products_file": str(
                    description_path
                ),
                "transcript_file": str(transcript_path),
                "transcript_sha256": transcript_sha256,
                "candidate_catalog_sha256": (
                    candidate_catalog_sha256
                ),
                "error": reason,
            }
            write_json(output_path, failure_payload)
            failures.append(failure_payload)
            print(f"    HATA: {reason}")

        if args.delay > 0 and index < len(description_paths):
            time.sleep(args.delay)

    combined.sort(
        key=lambda item: str(item.get("upload_date") or "")
    )

    approved_total = sum(
        item.get("approved_count", 0)
        for item in combined
    )
    review_total = sum(
        item.get("review_count", 0)
        for item in combined
    )
    rejected_total = sum(
        item.get("rejected_count", 0)
        for item in combined
    )

    combined_path = output_dir / "product_mentions_all.json"
    summary_path = output_dir / "summary.json"
    failures_path = output_dir / "failures.json"

    write_json(combined_path, combined)
    write_json(
        summary_path,
        {
            "influencer": args.influencer,
            "prompt_version": PROMPT_VERSION,
            "summary_style_version": SUMMARY_STYLE_VERSION,
            "content_type": args.content_type,
            "run_at": utc_now_iso(),
            "description_file_count": len(description_paths),
            "processed_now_count": processed_count,
            "skipped_current_count": skipped_count,
            "success_count": len(combined),
            "failure_count": len(failures),
            "approved_mention_count": approved_total,
            "review_mention_count": review_total,
            "rejected_mention_count": rejected_total,
            "include_review_products": (
                args.include_review_products
            ),
            "combined_output": str(combined_path),
        },
    )
    write_json(failures_path, failures)

    print()
    print("=" * 92)
    print("İŞLEM TAMAMLANDI")
    print("=" * 92)
    print(f"Başarılı video:       {len(combined)}")
    print(f"Bu çalışmada yeni:    {processed_count}")
    print(f"Atlanan güncel:       {skipped_count}")
    print(f"Approved yorum:       {approved_total}")
    print(f"Review yorum:         {review_total}")
    print(f"Rejected/no comment:  {rejected_total}")
    print(f"Hata:                 {len(failures)}")
    print(f"Birleşik çıktı:       {combined_path}")
    print(f"Özet:                 {summary_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nİşlem kullanıcı tarafından durduruldu.",
            file=sys.stderr,
        )
        raise SystemExit(130)
