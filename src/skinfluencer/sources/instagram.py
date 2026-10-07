from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.influencers import INFLUENCERS
from ..config.settings import DATA_ROOT


# src/skinfluencer/sources/instagram.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COOKIES_FILE = PROJECT_ROOT / ".secrets" / "instagram_yt_dlp_cookies.txt"
BOOKMARKLET_PREFIX = "skinfluencer_instagram_"

POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(reel|p)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)


def _parse_date_arg(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def _canonical_url(url: str) -> tuple[str, str] | None:
    match = POST_URL_RE.match(str(url or "").strip())
    if not match:
        return None

    kind, shortcode = match.groups()
    kind = "reel" if kind.casefold() == "reel" else "p"
    canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
    return shortcode, canonical


def _extract_urls(payload: Any, expected_handle: str) -> list[str]:
    if isinstance(payload, list):
        values = payload

    elif isinstance(payload, dict):
        handle = str(
            payload.get("profile_handle")
            or payload.get("instagram_handle")
            or ""
        ).strip().lstrip("@")

        if handle and handle.casefold() != expected_handle.casefold():
            raise ValueError(
                f"Discovery dosyası @{handle} için; beklenen @{expected_handle}."
            )

        values = payload.get("urls") or payload.get("items") or []

    else:
        raise ValueError("Instagram discovery JSON liste veya nesne olmalı.")

    urls: list[str] = []

    for item in values:
        if isinstance(item, str):
            raw_url = item
        elif isinstance(item, dict):
            raw_url = str(item.get("url") or item.get("permalink") or "")
        else:
            continue

        normalized = _canonical_url(raw_url)
        if normalized is not None:
            urls.append(normalized[1])

    return urls


def _candidate_files(handle: str) -> list[Path]:
    """Find bookmarklet discovery JSON files only in the project root."""
    safe_handle = re.sub(r"[^A-Za-z0-9_.-]+", "_", handle)
    pattern = f"{BOOKMARKLET_PREFIX}{safe_handle}_*.json"
    return sorted(PROJECT_ROOT.glob(pattern))


def _load_candidates(handle: str) -> tuple[list[str], list[Path]]:
    urls: list[str] = []
    source_files = _candidate_files(handle)

    for path in source_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            urls.extend(_extract_urls(payload, handle))
        except Exception as exc:
            raise RuntimeError(
                f"Instagram discovery dosyası okunamadı: {path}: {exc}"
            ) from exc

    # Stable de-duplication by shortcode.
    ordered: dict[str, str] = {}

    for url in urls:
        normalized = _canonical_url(url)
        if normalized is None:
            continue
        shortcode, canonical = normalized
        ordered.setdefault(shortcode, canonical)

    return list(ordered.values()), source_files


def _run_yt_dlp(
    url: str,
    auth_args: list[str],
) -> tuple[dict[str, Any] | None, str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--ignore-no-formats-error",
        *auth_args,
        url,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        error = (
            result.stderr
            or result.stdout
            or f"return code {result.returncode}"
        ).strip()
        return None, error

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"yt-dlp JSON decode error: {exc}"

    if not isinstance(payload, dict):
        return None, "yt-dlp JSON çıktısı nesne değil."

    return payload, ""



def _parse_gallery_date(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y%m%d")
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # gallery-dl commonly serializes Instagram dates as ISO-like strings.
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "".join(match.groups())

    if re.fullmatch(r"\d{8}", text):
        return text

    return None


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _gallery_payload_from_output(
    stdout: str,
    *,
    url: str,
) -> dict[str, Any] | None:
    normalized = _canonical_url(url)
    if normalized is None:
        return None

    shortcode, canonical = normalized

    documents: list[Any] = []
    text = stdout.strip()
    if not text:
        return None

    # gallery-dl may emit one JSON document or JSON Lines depending on config.
    try:
        documents.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                documents.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    candidates: list[dict[str, Any]] = []
    for document in documents:
        for item in _iter_dicts(document):
            item_code = str(
                item.get("post_shortcode")
                or item.get("shortcode")
                or ""
            ).strip()

            if item_code and item_code != shortcode:
                continue

            has_useful_metadata = any(
                item.get(key) is not None
                for key in ("description", "caption", "date", "username", "user")
            )
            if has_useful_metadata:
                candidates.append(item)

    if not candidates:
        return None

    # Prefer the richest dict. Carousel children generally repeat post-level
    # metadata, so one representative record is sufficient for Skinfluencer.
    def richness(item: dict[str, Any]) -> int:
        return sum(
            bool(item.get(key))
            for key in (
                "post_shortcode",
                "description",
                "date",
                "username",
                "user",
            )
        )

    item = max(candidates, key=richness)

    user = item.get("user")
    username = str(item.get("username") or "").strip().lstrip("@")
    if not username and isinstance(user, dict):
        username = str(user.get("username") or "").strip().lstrip("@")

    description = item.get("description")
    if description is None:
        description = item.get("caption")
    if isinstance(description, dict):
        description = description.get("text")
    if description is None:
        description = ""

    upload_date = _parse_gallery_date(item.get("date"))
    if upload_date is None:
        upload_date = _parse_gallery_date(item.get("timestamp"))

    payload: dict[str, Any] = {
        "id": shortcode,
        "webpage_url": canonical,
        "original_url": canonical,
        "description": str(description),
    }
    if username:
        payload["channel"] = username
    if upload_date:
        payload["upload_date"] = upload_date

    return payload


def _run_gallery_dl(
    url: str,
    auth_args: list[str],
) -> tuple[dict[str, Any] | None, str]:
    command = [
        "gallery-dl",
        "--simulate",
        "--resolve-json",
        *auth_args,
        url,
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None, "gallery-dl executable bulunamadı"

    if result.returncode != 0 and not result.stdout.strip():
        error = (
            result.stderr
            or result.stdout
            or f"return code {result.returncode}"
        ).strip()
        return None, error

    payload = _gallery_payload_from_output(result.stdout, url=url)
    if payload is None:
        error = (
            result.stderr
            or "gallery-dl çıktısında kullanılabilir Instagram post metadata bulunamadı"
        ).strip()
        return None, error

    return payload, ""


def _export_browser_cookies(
    url: str,
    browser: str,
    target: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--cookies-from-browser",
        browser,
        "--cookies",
        str(target),
        url,
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Instagram browser cookie export başarısız: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or f"return code {result.returncode}"
            )
        )

    if not target.is_file():
        raise RuntimeError(
            "yt-dlp cookie export tamamlandı fakat cookie dosyası oluşmadı."
        )

    try:
        target.chmod(0o600)
    except OSError:
        pass


def _fetch_metadata(
    url: str,
    *,
    cookies_file: Path | None,
    browser: str | None,
) -> tuple[dict[str, Any], str]:
    normalized = _canonical_url(url)
    is_static_post = bool(normalized and "/p/" in normalized[1])

    errors: list[str] = []

    # Static/photo/carousel posts: gallery-dl is the natural extractor.
    # Reels/video posts: yt-dlp is the natural extractor.
    if is_static_post:
        payload, error = _run_gallery_dl(url, [])
        if payload is not None:
            return payload, "public_gallery_dl"
        if error:
            errors.append(f"gallery-dl(public): {error}")

        # yt-dlp can sometimes still expose post-level metadata even when no
        # downloadable video format exists. Keep it only as a fallback.
        payload, error = _run_yt_dlp(url, [])
        if payload is not None:
            return payload, "public_yt_dlp"
        if error:
            errors.append(f"yt-dlp(public): {error}")
    else:
        payload, error = _run_yt_dlp(url, [])
        if payload is not None:
            return payload, "public_yt_dlp"
        if error:
            errors.append(f"yt-dlp(public): {error}")

    # Reuse a persistent cookie file when available.
    if cookies_file is not None and cookies_file.is_file():
        cookie_args = ["--cookies", str(cookies_file)]

        if is_static_post:
            payload, error = _run_gallery_dl(url, cookie_args)
            if payload is not None:
                return payload, "cookie_file_gallery_dl"
            if error:
                errors.append(f"gallery-dl(cookie): {error}")

        payload, error = _run_yt_dlp(url, cookie_args)
        if payload is not None:
            return payload, "cookie_file_yt_dlp"
        if error:
            errors.append(f"yt-dlp(cookie): {error}")

    # Browser access is explicit. For static posts, let gallery-dl consume
    # browser cookies directly first. For reels, export cookies once via
    # yt-dlp and reuse the local cookie file afterwards.
    if browser:
        target = cookies_file or DEFAULT_COOKIES_FILE

        if is_static_post:
            payload, error = _run_gallery_dl(
                url,
                ["--cookies-from-browser", browser],
            )
            if payload is not None:
                return payload, "browser_gallery_dl"
            if error:
                errors.append(f"gallery-dl(browser): {error}")

        try:
            _export_browser_cookies(url, browser, target)
        except Exception as exc:
            errors.append(f"cookie export: {exc}")
        else:
            cookie_args = ["--cookies", str(target)]

            if is_static_post:
                payload, error = _run_gallery_dl(url, cookie_args)
                if payload is not None:
                    return payload, "browser_export_gallery_dl"
                if error:
                    errors.append(f"gallery-dl(exported cookie): {error}")

            payload, error = _run_yt_dlp(url, cookie_args)
            if payload is not None:
                return payload, "browser_export_yt_dlp"
            if error:
                errors.append(f"yt-dlp(exported cookie): {error}")

    joined = " | ".join(errors[-4:]) if errors else "bilinmeyen hata"
    raise RuntimeError(
        "Tekil Instagram metadata alınamadı. "
        f"URL={url}. Son denemeler: {joined}."
    )

def _metadata_handle(payload: dict[str, Any]) -> str | None:
    """Return a username-like owner handle when yt-dlp provides one."""
    for key in ("channel", "uploader", "uploader_id"):
        value = str(payload.get(key) or "").strip().lstrip("@")
        if not value:
            continue

        # Numeric IDs are not usernames and should not be compared to handles.
        if value.isdigit():
            continue

        return value

    return None


def _validate_and_normalize(
    payload: dict[str, Any],
    *,
    url: str,
    expected_handle: str,
) -> dict[str, Any]:
    normalized_url = _canonical_url(url)
    if normalized_url is None:
        raise ValueError(f"Geçersiz Instagram URL: {url}")

    shortcode, canonical = normalized_url

    actual_id = str(payload.get("id") or "").strip()
    if actual_id and actual_id != shortcode:
        raise ValueError(
            f"Instagram shortcode uyuşmuyor: URL={shortcode}, metadata={actual_id}"
        )

    actual_handle = _metadata_handle(payload)
    if (
        actual_handle is not None
        and actual_handle.casefold() != expected_handle.casefold()
    ):
        raise ValueError(
            f"Yanlış Instagram hesabı: @{actual_handle}; "
            f"beklenen @{expected_handle}."
        )

    upload_date = str(payload.get("upload_date") or "").strip()

    if not re.fullmatch(r"\d{8}", upload_date):
        timestamp = payload.get("timestamp")

        if isinstance(timestamp, (int, float)):
            upload_date = datetime.fromtimestamp(
                float(timestamp),
                tz=timezone.utc,
            ).strftime("%Y%m%d")
        else:
            raise ValueError(
                "Instagram metadata içinde güvenilir upload_date/timestamp yok."
            )

    normalized = dict(payload)
    normalized["id"] = shortcode
    normalized["channel"] = expected_handle
    normalized["webpage_url"] = canonical
    normalized["original_url"] = canonical
    normalized["upload_date"] = upload_date
    normalized["platform"] = "instagram"

    return normalized


def _archive_source_files(
    source_files: list[Path],
    root: Path,
) -> None:
    """Move successfully consumed discovery JSONs out of the project root."""
    if not source_files:
        return

    processed = root / "inbox" / "processed"
    processed.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for index, path in enumerate(source_files, start=1):
        if not path.exists():
            continue

        target = processed / f"{stamp}_{index:02d}_{path.name}"
        shutil.move(str(path), str(target))



def _pending_path(root: Path) -> Path:
    return root / "inbox" / "pending.json"


def _load_pending(root: Path, expected_handle: str) -> list[dict[str, Any]]:
    path = _pending_path(root)
    if not path.is_file():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(payload, dict):
        return []

    handle = str(payload.get("profile_handle") or "").strip().lstrip("@")
    if handle and handle.casefold() != expected_handle.casefold():
        return []

    items = payload.get("items")
    if not isinstance(items, list):
        return []

    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _canonical_url(str(item.get("url") or ""))
        if normalized is None:
            continue
        result.append(
            {
                "url": normalized[1],
                "dateafter": item.get("dateafter"),
                "datebefore": item.get("datebefore"),
                "attempts": int(item.get("attempts") or 0),
                "last_error": str(item.get("last_error") or ""),
            }
        )
    return result


def _write_pending(
    root: Path,
    *,
    handle: str,
    items: list[dict[str, Any]],
) -> None:
    path = _pending_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not items:
        if path.exists():
            path.unlink()
        return

    payload = {
        "schema": "skinfluencer-instagram-pending-v1",
        "profile_handle": handle,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_candidate_jobs(
    urls: list[str],
    *,
    dateafter: str | None,
    datebefore: str | None,
    pending: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Pending jobs keep the date window from the run in which they were first
    # discovered. This prevents a later state cursor from silently discarding
    # a previously unresolved post.
    jobs: dict[str, dict[str, Any]] = {}

    for item in pending:
        normalized = _canonical_url(str(item.get("url") or ""))
        if normalized is None:
            continue
        shortcode, canonical = normalized
        jobs[shortcode] = {
            "url": canonical,
            "dateafter": item.get("dateafter"),
            "datebefore": item.get("datebefore"),
            "attempts": int(item.get("attempts") or 0),
            "from_pending": True,
        }

    for url in urls:
        normalized = _canonical_url(url)
        if normalized is None:
            continue
        shortcode, canonical = normalized
        jobs.setdefault(
            shortcode,
            {
                "url": canonical,
                "dateafter": dateafter,
                "datebefore": datebefore,
                "attempts": 0,
                "from_pending": False,
            },
        )

    return list(jobs.values())

def collect(
    influencer: str,
    *,
    dateafter: str | None = None,
    datebefore: str | None = None,
    max_posts: int = 100,
    cookies_file: str | Path | None = None,
    browser: str | None = None,
) -> dict[str, Any]:
    """Incrementally collect Instagram metadata from bookmarklet candidates.

    Beta workflow:
      1. Bookmarklet writes candidate /reel/ and /p/ URLs to project root.
      2. Reels/video metadata is fetched with yt-dlp.
      3. Static/photo/carousel metadata prefers gallery-dl on the direct URL.
      4. Account + real publication date are validated before archiving.
      5. Transiently unresolved candidates move to pending.json and do NOT
         fail the whole incremental update. They are retried next run.
      6. Consumed bookmarklet JSON files move to inbox/processed/.
    """
    del max_posts  # retained for pipeline API compatibility

    if influencer not in INFLUENCERS:
        raise ValueError(f"Unknown influencer: {influencer}")

    handle = str(
        INFLUENCERS[influencer].get("instagram_handle") or ""
    ).strip().lstrip("@")

    if not handle:
        raise ValueError(
            f"Instagram handle missing for influencer: {influencer}"
        )

    if cookies_file is None:
        cookies_path = (
            DEFAULT_COOKIES_FILE
            if DEFAULT_COOKIES_FILE.is_file()
            else None
        )
    else:
        cookies_path = Path(cookies_file).expanduser().resolve()

    root = DATA_ROOT / influencer / "instagram"
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    before = {
        p.stem.removesuffix(".info")
        for p in raw.glob("*.info.json")
    }

    urls, source_files = _load_candidates(handle)
    pending_before = _load_pending(root, handle)
    jobs = _merge_candidate_jobs(
        urls,
        dateafter=dateafter,
        datebefore=datebefore,
        pending=pending_before,
    )

    new_ids: list[str] = []
    existing_ids: list[str] = []
    outside_ids: list[str] = []
    rejected: list[dict[str, str]] = []
    pending_next: list[dict[str, Any]] = []
    auth_modes: dict[str, int] = {}

    for job in jobs:
        canonical = str(job["url"])
        normalized = _canonical_url(canonical)
        if normalized is None:
            continue

        shortcode, canonical = normalized
        target = raw / f"{shortcode}.info.json"

        if target.exists():
            existing_ids.append(shortcode)
            continue

        try:
            payload, auth_mode = _fetch_metadata(
                canonical,
                cookies_file=cookies_path,
                browser=browser,
            )
            auth_modes[auth_mode] = auth_modes.get(auth_mode, 0) + 1

            payload = _validate_and_normalize(
                payload,
                url=canonical,
                expected_handle=handle,
            )

            published = datetime.strptime(
                payload["upload_date"],
                "%Y%m%d",
            )

            job_start = _parse_date_arg(job.get("dateafter"))
            job_end = _parse_date_arg(job.get("datebefore"))

            if job_start and published.date() < job_start.date():
                outside_ids.append(shortcode)
                continue

            if job_end and published.date() > job_end.date():
                outside_ids.append(shortcode)
                continue

            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            new_ids.append(shortcode)

        except ValueError as exc:
            # Deterministic validation failures (wrong account, bad shortcode,
            # missing trustworthy date) should not be retried forever.
            rejected.append({"url": canonical, "error": str(exc)})

        except Exception as exc:
            # Network/extractor failures are non-fatal. Keep the candidate for
            # a future retry while allowing the rest of the update/state to
            # complete.
            pending_next.append(
                {
                    "url": canonical,
                    "dateafter": job.get("dateafter"),
                    "datebefore": job.get("datebefore"),
                    "attempts": int(job.get("attempts") or 0) + 1,
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            )

    _write_pending(root, handle=handle, items=pending_next)

    # Every root discovery file has now either been resolved, rejected, found
    # outside the date window, or preserved in pending.json. It is therefore
    # safe to archive the bookmarklet input even when pending candidates exist.
    _archive_source_files(source_files, root)

    after = {
        p.stem.removesuffix(".info")
        for p in raw.glob("*.info.json")
    }

    summary: dict[str, Any] = {
        "platform": "instagram",
        "influencer": influencer,
        "handle": handle,
        "discovery_mode": "project_root_bookmarklet",
        "project_root": str(PROJECT_ROOT),
        "inbox_files": len(source_files),
        "bookmarklet_candidate_urls": len(urls),
        "pending_before": len(pending_before),
        "candidates_processed": len(jobs),
        "new": len(new_ids),
        "new_ids": sorted(new_ids),
        "existing_candidates": len(existing_ids),
        "outside_date_window": len(outside_ids),
        "rejected_wrong_or_invalid": len(rejected),
        "pending_unresolved": len(pending_next),
        "failures": 0,
        "archive_before": len(before),
        "archive_after": len(after),
        "dateafter": dateafter,
        "datebefore": datebefore,
        "auth_modes": auth_modes,
        "pending_file": str(_pending_path(root)),
        "persistent_cookie_file": str(DEFAULT_COOKIES_FILE),
    }

    if rejected:
        summary["rejected_examples"] = rejected[:5]
    if pending_next:
        summary["pending_examples"] = pending_next[:5]

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary

