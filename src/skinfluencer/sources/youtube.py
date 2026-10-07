from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

"""Download and store YouTube metadata, descriptions, and transcripts.

Important design choices:
- The complete yt-dlp description is stored unchanged in each metadata JSON.
- Metadata is saved before transcript retrieval, so a transcript failure does not
  cause the source description to be lost.
- Existing videos are skipped only when both transcript and metadata files are
  present and the metadata JSON contains the expected description fields.
- The transcript TXT contains only transcript text; metadata stays in JSON.
"""

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

CHANNEL_URL = "https://www.youtube.com/@NaturallySerein/shorts"

OUTPUT_DIR = Path("data/naturally_serein")
TRANSCRIPT_DIR = OUTPUT_DIR / "transcripts"
METADATA_DIR = OUTPUT_DIR / "metadata"

DAYS_BACK = 548
MAX_CHANNEL_ENTRIES = 300
REQUEST_DELAY_SECONDS = 5.0
SKIP_EXISTING = True
METADATA_SCHEMA_VERSION = 2

LANGUAGE_PRIORITY = ["tr", "tr-TR", "en", "en-US", "en-GB"]

# ---------------------------------------------------------------------------
# CONTENT FILTERS
# ---------------------------------------------------------------------------

INCLUDE_KEYWORDS = [
    "ürün", "ürünler", "deniyorum", "inceleme", "review", "favori",
    "favoriler", "almaya değer", "öneri", "öneriler", "alternatif",
    "alternatifler", "cilt bakımı", "cilt bakım", "skincare", "serum",
    "nemlendirici", "temizleyici", "tonik", "essence", "ampul",
    "ampoule", "peeling", "maske", "güneş kremi", "güneş koruyucu",
    "sunscreen", "spf", "retinol", "retinal", "retinoid", "niacinamide",
    "niasinamid", "peptit", "peptide", "hyaluronic", "hyalüronik",
    "salisilik", "salicylic", "glikolik", "glycolic", "vitamin c",
    "c vitamini", "akne", "sivilce", "leke", "cilt bariyeri", "makyaj",
    "makeup", "fondöten", "foundation", "kapatıcı", "concealer", "allık",
    "blush", "bronzer", "kontür", "maskara", "rimel", "ruj", "lipstick",
    "lip tint", "tint", "far paleti", "eyeshadow", "eyeliner", "pudra",
    "primer", "highlighter", "dasique", "etude", "etude house",
    "colorgram", "romand", "rom&nd", "beauty of joseon", "cosrx", "anua",
    "round lab", "skin1004", "laneige", "nars", "mac", "rare beauty",
    "sephora", "maybelline", "loreal", "l'oréal", "nyx", "clinique",
    "estee lauder", "estée lauder", "the ordinary", "cerave",
    "la roche-posay", "bioderma", "kiehl", "kiehl's",
]

EXCLUDE_KEYWORDS = [
    "vlog", "seyahat", "travel", "tatil", "gezi", "otel", "hotel", "uçak",
    "havaalanı", "kayak tatili", "ski trip", "kitzbühel", "günlük vlog",
    "weekend vlog", "hafta sonu vlog", "ev turu", "house tour", "room tour",
    "alışveriş vlog", "doğum günü vlog", "birthday vlog", "taşınma vlog",
    "moving vlog", "ne yedim", "what i eat", "spor rutini", "workout",
]

# ---------------------------------------------------------------------------
# TEXT UTILITIES
# ---------------------------------------------------------------------------


def normalize_for_matching(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text).casefold()
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(text: str, max_length: int = 140) -> str:
    text = html.unescape(text)
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return (text or "untitled_video")[:max_length].strip()


def clean_caption_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\n", " ")
    text = re.sub(
        r"\[\s*(müzik|music|alkış|applause|gülüşmeler|laughter|kahkaha|"
        r"ses|sound|sessizlik|silence)\s*\]",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("♪", " ").replace("♫", " ")
    return re.sub(r"\s+", " ", text).strip()


def merge_transcript_snippets(snippets: Any) -> str:
    cleaned_parts: list[str] = []
    previous_text = ""

    for snippet in snippets:
        current_text = clean_caption_text(snippet.text)
        if not current_text or current_text == previous_text:
            continue

        if previous_text and len(previous_text) > 20 and current_text.startswith(previous_text):
            current_text = current_text[len(previous_text):].strip()

        if current_text:
            cleaned_parts.append(current_text)
            previous_text = current_text

    text = " ".join(cleaned_parts)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return re.sub(r"[ \t]+", " ", text).strip()

# ---------------------------------------------------------------------------
# DATE UTILITIES
# ---------------------------------------------------------------------------


def parse_upload_date(upload_date: str | None) -> datetime | None:
    if not upload_date:
        return None
    try:
        return datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        return None


def format_date_for_display(upload_date: str | None) -> str:
    parsed = parse_upload_date(upload_date)
    return parsed.strftime("%Y-%m-%d") if parsed else (upload_date or "")

# ---------------------------------------------------------------------------
# VIDEO FILTERING
# ---------------------------------------------------------------------------


def classify_video(title: str, description: str) -> dict[str, Any]:
    normalized_title = normalize_for_matching(title)
    normalized_description = normalize_for_matching(description)

    matched_exclude = [
        keyword for keyword in EXCLUDE_KEYWORDS
        if normalize_for_matching(keyword) in normalized_title
    ]
    matched_include_title = [
        keyword for keyword in INCLUDE_KEYWORDS
        if normalize_for_matching(keyword) in normalized_title
    ]
    matched_include_description = [
        keyword for keyword in INCLUDE_KEYWORDS
        if normalize_for_matching(keyword) in normalized_description
    ]

    if matched_exclude:
        return {
            "is_relevant": False,
            "reason": "excluded_keyword",
            "matched_include_keywords": matched_include_title + matched_include_description,
            "matched_exclude_keywords": matched_exclude,
        }
    if matched_include_title:
        return {
            "is_relevant": True,
            "reason": "include_keyword_in_title",
            "matched_include_keywords": matched_include_title,
            "matched_exclude_keywords": [],
        }
    if matched_include_description:
        return {
            "is_relevant": True,
            "reason": "include_keyword_in_description",
            "matched_include_keywords": matched_include_description,
            "matched_exclude_keywords": [],
        }
    return {
        "is_relevant": False,
        "reason": "no_beauty_keyword_found",
        "matched_include_keywords": [],
        "matched_exclude_keywords": [],
    }

# ---------------------------------------------------------------------------
# YOUTUBE METADATA
# ---------------------------------------------------------------------------


def get_channel_entries(channel_url: str, max_entries: int) -> list[dict[str, Any]]:
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
        "playlistend": max_entries,
    }
    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        channel_info = ydl.extract_info(channel_url, download=False)

    if not channel_info:
        return []

    results: list[dict[str, Any]] = []
    for entry in channel_info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        video_id = entry["id"]
        url = entry.get("url") or entry.get("webpage_url") or ""
        results.append({
            "video_id": video_id,
            "flat_title": entry.get("title") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return results


def get_detailed_metadata(video_id: str) -> dict[str, Any]:
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(video_url, download=False)

    if not info:
        return {}

    # Do not clean, truncate, or normalize this field. It is the source text.
    description = info.get("description") or ""

    return {
        "video_id": video_id,
        "title": info.get("title") or "",
        "channel": info.get("channel") or "",
        "channel_id": info.get("channel_id") or "",
        "channel_url": info.get("channel_url") or "",
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
        "duration_seconds": info.get("duration"),
        "description": description,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "availability": info.get("availability"),
        "live_status": info.get("live_status"),
        "was_live": info.get("was_live"),
        "url": video_url,
    }

# ---------------------------------------------------------------------------
# TRANSCRIPT
# ---------------------------------------------------------------------------


def fetch_best_transcript(video_id: str):
    api = YouTubeTranscriptApi()
    try:
        return api.fetch(
            video_id,
            languages=LANGUAGE_PRIORITY,
            preserve_formatting=False,
        )
    except NoTranscriptFound:
        available = list(api.list(video_id))
        if not available:
            raise

        print(
            "    Türkçe transcript bulunamadı. Mevcut diller:",
            ", ".join(f"{item.language} ({item.language_code})" for item in available),
        )
        manual = [item for item in available if not item.is_generated]
        selected = manual[0] if manual else available[0]
        print(f"    Seçilen transcript: {selected.language} ({selected.language_code})")
        return selected.fetch()

# ---------------------------------------------------------------------------
# FILE MANAGEMENT
# ---------------------------------------------------------------------------


def get_output_stem(video_id: str, title: str) -> str:
    return f"{safe_filename(title)}__{video_id}"


def get_video_paths(video_id: str, title: str) -> tuple[Path, Path]:
    stem = get_output_stem(video_id, title)
    return TRANSCRIPT_DIR / f"{stem}.txt", METADATA_DIR / f"{stem}.json"


def description_digest(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def existing_video_files_complete(video_id: str, title: str) -> bool:
    txt_path, json_path = get_video_paths(video_id, title)
    if not txt_path.exists() or not json_path.exists():
        return False

    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return (
        payload.get("video_id") == video_id
        and "description" in payload
        and payload.get("description_retrieved") is True
        and payload.get("transcript_status") == "available"
    )


def transcript_metadata(transcript_object: Any) -> dict[str, Any]:
    return {
        "transcript_language": getattr(transcript_object, "language", ""),
        "transcript_language_code": getattr(transcript_object, "language_code", ""),
        "is_generated_transcript": getattr(transcript_object, "is_generated", None),
    }


def save_metadata_file(
    metadata: dict[str, Any],
    classification: dict[str, Any],
    *,
    transcript_status: str,
    transcript_text: str | None = None,
    transcript_object: Any | None = None,
    transcript_error: str | None = None,
) -> Path:
    video_id = metadata["video_id"]
    title = metadata.get("title") or video_id
    txt_path, json_path = get_video_paths(video_id, title)
    description = metadata.get("description") or ""

    payload: dict[str, Any] = {
        **metadata,
        "metadata_schema_version": METADATA_SCHEMA_VERSION,
        "metadata_retrieved_at": datetime.now().isoformat(),
        "formatted_upload_date": format_date_for_display(metadata.get("upload_date")),
        "classification": classification,
        "description_retrieved": True,
        "description_character_count": len(description),
        "description_line_count": len(description.splitlines()),
        "description_sha256": description_digest(description),
        "transcript_status": transcript_status,
        "transcript_file": str(txt_path),
        "json_file": str(json_path),
    }

    if transcript_object is not None:
        payload.update(transcript_metadata(transcript_object))
    if transcript_text is not None:
        payload["transcript_character_count"] = len(transcript_text)
        payload["transcript_word_count"] = len(transcript_text.split())
    if transcript_error:
        payload["transcript_error"] = transcript_error

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return json_path


def save_transcript_file(metadata: dict[str, Any], transcript_text: str) -> Path:
    video_id = metadata["video_id"]
    title = metadata.get("title") or video_id
    txt_path, _ = get_video_paths(video_id, title)
    txt_path.write_text(transcript_text.rstrip() + "\n", encoding="utf-8")
    return txt_path


def write_json_report(filename: str, data: Any) -> Path:
    path = OUTPUT_DIR / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------


def main(*, dateafter: str | None = None, datebefore: str | None = None) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    if dateafter:
        cutoff_date = datetime.strptime(dateafter, "%Y-%m-%d")
    else:
        cutoff_date = now - timedelta(days=DAYS_BACK)
    before_date = datetime.strptime(datebefore, "%Y-%m-%d") if datebefore else None

    print("=" * 88)
    print("SKINFLUENCER TRANSCRIPT DOWNLOADER")
    print("=" * 88)
    print(f"Kanal: {CHANNEL_URL}")
    print(f"Bugün: {now:%Y-%m-%d}")
    print(f"Kesim tarihi: {cutoff_date:%Y-%m-%d}")
    if before_date is not None:
        print(f"Üst tarih: {before_date:%Y-%m-%d}")
    print()

    channel_entries = get_channel_entries(CHANNEL_URL, MAX_CHANNEL_ENTRIES)
    if not channel_entries:
        raise RuntimeError("Kanaldan video listesi alınamadı.")

    print(f"Kanal listesinden {len(channel_entries)} video ID'si alındı.\n")

    downloaded: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    excluded_by_date: list[dict[str, Any]] = []
    excluded_by_content: list[dict[str, Any]] = []
    metadata_failures: list[dict[str, Any]] = []
    transcript_failures: list[dict[str, Any]] = []
    consecutive_old_videos = 0

    for index, entry in enumerate(channel_entries, start=1):
        video_id = entry["video_id"]
        print("-" * 88)
        print(f"[{index}/{len(channel_entries)}] Video inceleniyor: {video_id}")

        try:
            metadata = get_detailed_metadata(video_id)
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
            print(f"    METADATA HATASI: {reason}")
            metadata_failures.append({"video_id": video_id, "url": entry["url"], "reason": reason})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if not metadata:
            reason = "Metadata boş döndü."
            print(f"    METADATA HATASI: {reason}")
            metadata_failures.append({"video_id": video_id, "url": entry["url"], "reason": reason})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        title = metadata.get("title") or entry.get("flat_title") or video_id
        description = metadata.get("description") or ""
        upload_date = parse_upload_date(metadata.get("upload_date"))

        print(f"    Başlık: {title}")
        print(f"    Yayın tarihi: {format_date_for_display(metadata.get('upload_date'))}")
        print(f"    Açıklama uzunluğu: {len(description):,} karakter")

        if upload_date is None:
            reason = "Yayın tarihi belirlenemedi."
            print(f"    ELENDİ: {reason}")
            excluded_by_date.append({**metadata, "reason": reason})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        # Compare calendar dates, not midnight to current clock time.
        if before_date is not None and upload_date.date() > before_date.date():
            reason = f"Üst tarih sınırından yeni: {upload_date:%Y-%m-%d}"
            print(f"    ELENDİ: {reason}")
            excluded_by_date.append({**metadata, "reason": reason})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if upload_date.date() < cutoff_date.date():
            consecutive_old_videos += 1
            reason = f"Kesim tarihinden eski: {upload_date:%Y-%m-%d}"
            print(f"    ELENDİ: {reason}")
            excluded_by_date.append({**metadata, "reason": reason})
            if consecutive_old_videos >= 10:
                print("Arka arkaya 10 eski video bulundu; tarama durduruluyor.")
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        consecutive_old_videos = 0

        if metadata.get("live_status") in {"is_live", "is_upcoming"}:
            reason = f"Canlı/yaklaşan yayın: {metadata.get('live_status')}"
            print(f"    ELENDİ: {reason}")
            excluded_by_content.append({**metadata, "classification": {"is_relevant": False, "reason": reason}})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        classification = classify_video(title, description)
        if not classification["is_relevant"]:
            print(f"    İÇERİK FİLTRESİYLE ELENDİ: {classification['reason']}")
            excluded_by_content.append({**metadata, "classification": classification})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"    İlgili içerik: {classification['reason']}")

        if SKIP_EXISTING and existing_video_files_complete(video_id, title):
            print("    ATLANDI: Transcript ve açıklamalı metadata zaten eksiksiz.")
            skipped_existing.append({**metadata, "classification": classification, "reason": "already_complete"})
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        # Save the full source metadata immediately, before transcript retrieval.
        json_path = save_metadata_file(
            metadata,
            classification,
            transcript_status="pending",
        )
        print(f"    Metadata kaydedildi: {json_path}")

        try:
            transcript = fetch_best_transcript(video_id)
            transcript_text = merge_transcript_snippets(transcript)
            if len(transcript_text) < 100:
                raise ValueError("Temiz transcript 100 karakterden kısa.")

            txt_path = save_transcript_file(metadata, transcript_text)
            json_path = save_metadata_file(
                metadata,
                classification,
                transcript_status="available",
                transcript_text=transcript_text,
                transcript_object=transcript,
            )

            print(f"    TXT:  {txt_path}")
            print(f"    JSON: {json_path}")
            print(f"    Kelime sayısı: {len(transcript_text.split()):,}")
            downloaded.append({
                **metadata,
                "classification": classification,
                "transcript_word_count": len(transcript_text.split()),
                "txt_file": str(txt_path),
                "json_file": str(json_path),
            })

        except TranscriptsDisabled:
            reason = "Bu video için transcriptler kapalı."
        except NoTranscriptFound:
            reason = "Kullanılabilir transcript bulunamadı."
        except VideoUnavailable:
            reason = "Video kullanılamıyor."
        except AgeRestricted:
            reason = "Video yaş kısıtlamalı."
        except CouldNotRetrieveTranscript as error:
            reason = f"Transcript alınamadı: {error}"
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
        else:
            reason = ""

        if reason:
            print(f"    TRANSCRIPT BAŞARISIZ: {reason}")
            save_metadata_file(
                metadata,
                classification,
                transcript_status="failed",
                transcript_error=reason,
            )
            transcript_failures.append({**metadata, "reason": reason})

        time.sleep(REQUEST_DELAY_SECONDS)

    summary = {
        "channel_url": CHANNEL_URL,
        "run_datetime": now.isoformat(),
        "cutoff_date": cutoff_date.strftime("%Y-%m-%d"),
        "days_back": DAYS_BACK if not dateafter else None,
        "dateafter": dateafter,
        "datebefore": datebefore,
        "channel_entries_found": len(channel_entries),
        "downloaded_count": len(downloaded),
        "skipped_existing_count": len(skipped_existing),
        "excluded_by_date_count": len(excluded_by_date),
        "excluded_by_content_count": len(excluded_by_content),
        "metadata_failure_count": len(metadata_failures),
        "transcript_failure_count": len(transcript_failures),
    }

    report_paths = [
        write_json_report("summary.json", summary),
        write_json_report("downloaded_videos.json", downloaded),
        write_json_report("skipped_existing.json", skipped_existing),
        write_json_report("excluded_by_content.json", excluded_by_content),
        write_json_report("excluded_by_date.json", excluded_by_date),
        write_json_report("failures.json", {
            "metadata_failures": metadata_failures,
            "transcript_failures": transcript_failures,
        }),
    ]

    print("\n" + "=" * 88)
    print("İŞLEM TAMAMLANDI")
    print("=" * 88)
    print(f"Yeni indirilen:              {len(downloaded)}")
    print(f"Eksiksiz olduğu için atlanan:{len(skipped_existing):>6}")
    print(f"Tarih nedeniyle elenen:     {len(excluded_by_date):>6}")
    print(f"İçerik nedeniyle elenen:    {len(excluded_by_content):>6}")
    print(f"Metadata hatası:            {len(metadata_failures):>6}")
    print(f"Transcript hatası:          {len(transcript_failures):>6}")
    print("\nRaporlar:")
    for path in report_paths:
        print(f"  {path}")


if __name__ == "__main__":
    main()


def collect(
    influencer: str,
    *,
    shorts: bool = False,
    dateafter: str | None = None,
    datebefore: str | None = None,
) -> None:
    from ..config.influencers import INFLUENCERS
    from ..config.settings import DATA_ROOT
    global CHANNEL_URL, OUTPUT_DIR, TRANSCRIPT_DIR, METADATA_DIR
    if influencer not in INFLUENCERS:
        raise ValueError(f"Unknown influencer: {influencer}")
    cfg = INFLUENCERS[influencer]
    CHANNEL_URL = cfg["youtube_shorts_url"] if shorts else cfg["youtube_url"]
    OUTPUT_DIR = DATA_ROOT / influencer
    TRANSCRIPT_DIR = OUTPUT_DIR / "transcripts"
    METADATA_DIR = OUTPUT_DIR / "metadata"
    main(dateafter=dateafter, datebefore=datebefore)
