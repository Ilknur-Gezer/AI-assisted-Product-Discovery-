"""Shared structured caption-product extraction helpers for social platforms."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..storage.social_products import validate_products


class Product(BaseModel):
    brand: str | None
    product_name: str
    category: Literal[
        "skincare",
        "makeup",
        "haircare",
        "bodycare",
        "fragrance",
        "other_beauty",
    ]
    evidence_text: str
    confidence: float = Field(ge=0, le=1)


class Extraction(BaseModel):
    products: list[Product]


def parse_caption_with_openai(
    *,
    caption: str,
    model: str,
    max_retries: int,
    system_prompt: str,
):
    """Run one structured OpenAI extraction and deterministic validation."""
    from openai import OpenAI

    client = OpenAI(max_retries=max_retries)
    response = client.responses.parse(
        model=model,
        store=False,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": caption},
        ],
        text_format=Extraction,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise ValueError("Model geçerli structured output döndürmedi.")

    products = validate_products(
        [item.model_dump() for item in parsed.products],
        caption,
    )
    usage = response.usage.model_dump() if response.usage is not None else None
    return products, usage, response.id
