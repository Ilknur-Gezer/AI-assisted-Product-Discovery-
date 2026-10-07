from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config.influencers import INFLUENCERS
from ..config.settings import DATA_ROOT

STATE_FILENAME = "collector_state.json"


def _validate_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _numeric_metadata_paths(raw: Path) -> list[Path]:
    return [
        path
        for path in raw.glob("*.info.json")
        if path.name.removesuffix(".info.json").isdigit()
    ]


def _existing_post_ids(raw: Path) -> set[str]:
    return {
        path.name.removesuffix(".info.json")
        for path in _numeric_metadata_paths(raw)
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _channel_id_from_payload(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    value = str(payload.get("channel_id") or "").strip()
    return value or None


def _load_cached_channel_id(root: Path, raw: Path, handle: str) -> str | None:
    state = _read_json(root / STATE_FILENAME)
    if state and state.get("handle") == handle:
        value = str(state.get("channel_id") or "").strip()
        if value:
            return value

    # Raw TikTok metadata already contains the stable secUid/channel_id.  Reuse
    # the newest valid one instead of hard-coding an influencer-specific value.
    paths = sorted(
        _numeric_metadata_paths(raw),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        value = _channel_id_from_payload(_read_json(path))
        if value:
            return value
    return None




def _canonicalize_metadata_url(path: Path, handle: str) -> bool:
    """Rewrite yt-dlp tiktokuser:<secUid> URLs to the public @handle URL.

    yt-dlp may store the stable secUid/channel_id in the URL path when a
    playlist is collected through ``tiktokuser:<channel_id>``.  Downstream
    validation deliberately requires the configured public handle, so repair
    only the URL identity fields while leaving all source caption/date data
    untouched.
    """
    payload = _read_json(path)
    if not payload:
        return False

    pid = str(payload.get("id") or path.name.removesuffix(".info.json")).strip()
    if not pid.isdigit():
        return False

    kind = "video"
    for key in ("webpage_url", "original_url"):
        value = str(payload.get(key) or "").strip()
        if not value:
            continue
        try:
            parts = urlsplit(value)
        except ValueError:
            continue
        segments = [segment for segment in parts.path.split("/") if segment]
        for candidate in ("video", "photo"):
            if candidate in segments:
                kind = candidate
                break

    canonical = f"https://www.tiktok.com/@{handle}/{kind}/{pid}"
    changed = False
    for key in ("webpage_url", "original_url"):
        if payload.get(key) != canonical:
            payload[key] = canonical
            changed = True

    if not changed:
        return False

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)
    return True


def _canonicalize_metadata_archive(raw: Path, handle: str) -> int:
    """Repair any stable-ID URLs already present in the local archive.

    Running this across the numeric metadata archive is intentional: if a prior
    incremental run downloaded files and then failed before extraction, the
    next run must be able to repair those already-existing files too.
    """
    repaired = 0
    for path in _numeric_metadata_paths(raw):
        if _canonicalize_metadata_url(path, handle):
            repaired += 1
    return repaired

def _save_state(root: Path, *, handle: str, channel_id: str | None) -> None:
    payload = {
        "handle": handle,
        "channel_id": channel_id,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    (root / STATE_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_collection(
    *,
    target: str,
    raw: Path,
    log: Path,
    dateafter: str | None,
    datebefore: str | None,
    max_items: int,
    sleep_requests: float,
) -> tuple[int, bool]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-errors",
        "--skip-download",
        "--write-info-json",
        "--no-write-playlist-metafiles",
        "--no-overwrites",
        "--lazy-playlist",
        "--break-on-reject",
        "--playlist-end",
        str(max_items),
        "--sleep-requests",
        str(sleep_requests),
        "--paths",
        str(raw),
        "-o",
        "%(id)s.%(ext)s",
    ]
    if dateafter:
        command += ["--dateafter", dateafter.replace("-", "")]
    if datebefore:
        command += ["--datebefore", datebefore.replace("-", "")]
    command.append(target)

    log.parent.mkdir(parents=True, exist_ok=True)
    start_offset = log.stat().st_size if log.exists() else 0

    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n" + "=" * 88 + "\n")
        stream.write(f"target={target}\n")
        stream.write(f"dateafter={dateafter} datebefore={datebefore}\n")
        stream.flush()
        result = subprocess.run(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )

    # yt-dlp exits with 101 when --break-on-reject / --break-match-filters
    # deliberately stops a playlist at the first post outside the requested
    # date window.  That is the desired incremental-collection stop condition,
    # not a collector failure.  Inspect only this run's appended log segment so
    # an old message cannot mask a new real error.
    try:
        with log.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(start_offset)
            run_log = stream.read()
    except OSError:
        run_log = ""

    expected_filter_stop = (
        int(result.returncode) == 101
        and bool(dateafter or datebefore)
        and (
            "stopping due to --break-match-filter" in run_log
            or "Encountered a video that did not match filter" in run_log
        )
    )

    return int(result.returncode), expected_filter_stop


def collect(
    influencer: str,
    *,
    dateafter: str | None = None,
    datebefore: str | None = None,
    max_items: int = 100,
    sleep_requests: float = 3.0,
) -> dict[str, Any]:
    """Collect TikTok post metadata without downloading media.

    The caller supplies only influencer slug and optional date window.  The
    collector resolves TikTok's stable secUid/channel_id from local state or
    previously saved metadata.  A profile URL is used only as a bootstrap
    fallback when no stable ID has ever been observed.
    """
    if influencer not in INFLUENCERS:
        raise ValueError(f"Unknown influencer: {influencer}")
    if max_items < 1:
        raise ValueError("max_items must be positive")
    if sleep_requests < 0:
        raise ValueError("sleep_requests cannot be negative")

    dateafter = _validate_date(dateafter, "dateafter")
    datebefore = _validate_date(datebefore, "datebefore")

    handle = str(INFLUENCERS[influencer]["tiktok_handle"])
    root = DATA_ROOT / influencer / "tiktok"
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    log = root / "collection.log"

    before = _existing_post_ids(raw)
    channel_id = _load_cached_channel_id(root, raw, handle)
    target = (
        f"tiktokuser:{channel_id}"
        if channel_id
        else f"https://www.tiktok.com/@{handle}"
    )

    returncode, expected_filter_stop = _run_collection(
        target=target,
        raw=raw,
        log=log,
        dateafter=dateafter,
        datebefore=datebefore,
        max_items=max_items,
        sleep_requests=sleep_requests,
    )

    after = _existing_post_ids(raw)
    new_ids = sorted(after - before)
    canonicalized = _canonicalize_metadata_archive(raw, handle)

    resolved_channel_id = _load_cached_channel_id(root, raw, handle)
    if resolved_channel_id:
        _save_state(root, handle=handle, channel_id=resolved_channel_id)

    if returncode and not expected_filter_stop and not new_ids:
        hint = ""
        if not channel_id:
            hint = (
                " TikTok profil resolver'ı stable channel_id/secUid bulamadı. "
                "Mevcut raw metadata varsa collector bunu otomatik kullanır; "
                "ilk bootstrap için bir geçerli post metadata'sı gerekebilir."
            )
        raise RuntimeError(
            f"TikTok collection failed for {influencer}; see {log}.{hint}"
        )

    summary = {
        "platform": "tiktok",
        "influencer": influencer,
        "handle": handle,
        "target_kind": "stable_channel_id" if resolved_channel_id else "profile",
        "channel_id_cached": bool(resolved_channel_id),
        "existing_before": len(before),
        "existing_after": len(after),
        "new": len(new_ids),
        "new_ids": new_ids,
        "canonicalized_urls": canonicalized,
        "dateafter": dateafter,
        "datebefore": datebefore,
        "returncode": returncode,
        "expected_filter_stop": expected_filter_stop,
        "log": str(log),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
