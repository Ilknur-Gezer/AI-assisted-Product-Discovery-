from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skinfluencer.config.influencers import influencer_slugs


def normalize_caption(text: str | None) -> str:
    text = str(text or "").strip().casefold()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = text.replace("#", " ")
    text = re.sub(r"@\w+", " ", text)
    # TikTok reply wrapper: "Replying to @name ...".  The mention has already
    # been removed above, so only the wrapper words remain here.
    text = re.sub(r"^\s*replying\s+to\s+", " ", text)
    text = re.sub(r"[^\wçğıöşü]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def extract_caption(payload: dict) -> str:
    for key in ("description", "caption", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_date(payload: dict) -> datetime | None:
    upload_date = str(payload.get("upload_date") or "").strip()
    if re.fullmatch(r"\d{8}", upload_date):
        try:
            return datetime.strptime(upload_date, "%Y%m%d")
        except ValueError:
            pass

    published = str(payload.get("published_at") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
        try:
            return datetime.strptime(published, "%Y-%m-%d")
        except ValueError:
            pass

    timestamp = payload.get("timestamp")
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp)
        except (ValueError, OSError):
            pass
    return None


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def date_distance_days(
    instagram_date: datetime | None,
    tiktok_date: datetime | None,
) -> int | None:
    if instagram_date is None or tiktok_date is None:
        return None
    return abs((instagram_date.date() - tiktok_date.date()).days)


def load_posts(folder: Path, platform: str) -> list[dict]:
    posts: list[dict] = []
    for path in sorted(folder.glob("*.info.json")):
        # TikTok playlist metadata is not a post.  Current collector no longer
        # creates it, but this guard keeps older archives safe.
        if platform == "tiktok" and not path.name.removesuffix(".info.json").isdigit():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"WARNING: okunamadı: {path} ({exc})")
            continue

        caption = extract_caption(payload)
        normalized = normalize_caption(caption)
        if not normalized:
            continue

        posts.append(
            {
                "platform": platform,
                "path": str(path),
                "id": str(payload.get("id") or path.name.replace(".info.json", "")),
                "url": str(
                    payload.get("webpage_url")
                    or payload.get("original_url")
                    or ""
                ),
                "caption": caption,
                "normalized_caption": normalized,
                "date": extract_date(payload),
            }
        )
    return posts


def find_best_match(
    instagram_post: dict,
    tiktok_posts: list[dict],
    *,
    fuzzy_threshold: float,
    max_date_days: int,
) -> dict | None:
    ig_caption = instagram_post["normalized_caption"]
    ig_date = instagram_post["date"]
    candidates: list[dict] = []

    for tiktok_post in tiktok_posts:
        score = similarity(ig_caption, tiktok_post["normalized_caption"])
        date_days = date_distance_days(ig_date, tiktok_post["date"])

        # Exact normalized caption is strong enough even when the creator
        # cross-posts days or weeks later.
        if score == 1.0:
            match_type = "exact"
        elif score >= fuzzy_threshold:
            # Near matches remain date-constrained to reduce false positives.
            if date_days is not None and date_days > max_date_days:
                continue
            match_type = "near"
        else:
            continue

        candidates.append(
            {
                "match_type": match_type,
                "similarity": score,
                "date_distance_days": date_days,
                "tiktok": tiktok_post,
            }
        )

    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            -item["similarity"],
            item["date_distance_days"]
            if item["date_distance_days"] is not None
            else 999,
        )
    )
    return candidates[0]


def match_influencer(
    influencer: str,
    *,
    data_root: Path = Path("data"),
    threshold: float = 0.86,
    max_date_days: int = 3,
    output: Path | None = None,
    verbose: bool = True,
) -> dict:
    instagram_posts = load_posts(data_root / influencer / "instagram" / "raw", "instagram")
    tiktok_posts = load_posts(data_root / influencer / "tiktok" / "raw", "tiktok")

    if verbose:
        print(f"Instagram post: {len(instagram_posts)}")
        print(f"TikTok post:    {len(tiktok_posts)}\n")

    matches: list[dict] = []
    unmatched: list[dict] = []
    exact_count = 0
    near_count = 0

    for instagram_post in instagram_posts:
        match = find_best_match(
            instagram_post,
            tiktok_posts,
            fuzzy_threshold=threshold,
            max_date_days=max_date_days,
        )
        if match is None:
            unmatched.append(
                {
                    "instagram_id": instagram_post["id"],
                    "instagram_url": instagram_post["url"],
                    "caption": instagram_post["caption"],
                }
            )
            if verbose:
                print(f"NO MATCH  IG={instagram_post['id']} {instagram_post['url']}")
            continue

        if match["match_type"] == "exact":
            exact_count += 1
        else:
            near_count += 1

        tiktok = match["tiktok"]
        result = {
            "match_type": match["match_type"],
            "similarity": round(match["similarity"], 4),
            "date_distance_days": match["date_distance_days"],
            "instagram_id": instagram_post["id"],
            "instagram_url": instagram_post["url"],
            "instagram_caption": instagram_post["caption"],
            "tiktok_id": tiktok["id"],
            "tiktok_url": tiktok["url"],
            "tiktok_caption": tiktok["caption"],
        }
        matches.append(result)
        if verbose:
            print(
                f"{match['match_type'].upper():5} "
                f"{match['similarity']:.3f}  "
                f"IG={instagram_post['id']} <-> TT={tiktok['id']} "
                f"dateΔ={match['date_distance_days']}"
            )

    payload = {
        "influencer": influencer,
        "exact_matches": exact_count,
        "near_matches": near_count,
        "matches": matches,
        "unmatched": unmatched,
    }
    output_path = output or data_root / influencer / "instagram" / "tiktok_matches.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if verbose:
        print("\n" + "=" * 72)
        print(f"Instagram total: {len(instagram_posts)}")
        print(f"Exact match:     {exact_count}")
        print(f"Near match:      {near_count}")
        print(f"No match:        {len(unmatched)}")
        rate = (len(matches) / len(instagram_posts) * 100) if instagram_posts else 0.0
        print(f"Match rate:      {rate:.1f}%")
        print(f"\nJSON: {output_path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--influencer", required=True, choices=sorted(influencer_slugs()))
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--threshold", type=float, default=0.86)
    parser.add_argument("--max-date-days", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    match_influencer(
        args.influencer,
        data_root=args.data_root,
        threshold=args.threshold,
        max_date_days=args.max_date_days,
        output=args.output,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
