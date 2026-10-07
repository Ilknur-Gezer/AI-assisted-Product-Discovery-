from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Platform = Literal["youtube", "tiktok", "instagram"]

@dataclass(frozen=True)
class ContentRef:
    platform: Platform
    influencer: str
    content_id: str
    original_url: str
