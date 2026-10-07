from __future__ import annotations

INFLUENCERS = {
    "naturally_serein": {
        "display_name": "Çisem Çakır",
        "youtube_url": "https://www.youtube.com/@NaturallySerein/videos",
        "youtube_shorts_url": "https://www.youtube.com/@NaturallySerein/shorts",
        "tiktok_handle": "cisemcakir",
        "instagram_handle": "cisemcakir",
    },
    "yagmurvardar": {
        "display_name": "Yağmur Vardar",
        "youtube_url": "https://www.youtube.com/@yagmurvardar/videos",
        "youtube_shorts_url": "https://www.youtube.com/@yagmurvardar/shorts",
        "tiktok_handle": "yagmurvardar_",
        "instagram_handle": "yagmurvardar_",
    },
}


def influencer_slugs() -> list[str]:
    return list(INFLUENCERS)
