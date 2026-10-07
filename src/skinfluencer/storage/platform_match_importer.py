"""Import Instagram↔TikTok match JSON into social_post_matches.

Both Instagram and TikTok posts must already exist as first-class rows in
social_posts. No OpenAI calls are made here. The operation is idempotent and
runs on a verified database copy before atomically replacing production.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .sqlite_importer import (
    activate_rebuilt_database,
    connect_database,
    create_consistent_database_backup,
    make_rebuild_path,
    remove_database_files,
)

PROTECTED = (
    "videos",
    "product_mentions",
    "unresolved_mentions",
    "purchase_links",
    "product_images",
    "products",
    "social_posts",
    "social_post_products",
    "social_product_contexts",
    "social_post_platform_matches",
)


def _fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    result: dict[str, str] = {}
    for table in PROTECTED:
        if table not in existing:
            continue
        rows = [
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY id"
            )
        ]
        result[table] = hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
    return result


def _load_matches(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(matches, list):
        raise ValueError(f"Geçersiz match JSON: {path}")
    return [item for item in matches if isinstance(item, dict)]


def _ensure_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_post_matches'"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "social_post_matches tablosu yok. Güncel schema.sql ile DB'yi bir kez build edin."
        )


def _validate_match_set(matches: list[dict[str, Any]], source_file: Path) -> None:
    seen_tiktok: set[str] = set()
    seen_instagram: set[str] = set()

    for index, match in enumerate(matches):
        tiktok_id = str(match.get("tiktok_id") or "").strip()
        instagram_id = str(match.get("instagram_id") or "").strip()

        if not tiktok_id or not instagram_id:
            raise ValueError(
                f"{source_file}: matches[{index}] TikTok/Instagram kimliği eksik."
            )
        if tiktok_id in seen_tiktok:
            raise ValueError(
                f"{source_file}: aynı TikTok postuna birden fazla Instagram eşleşmiş: "
                f"{tiktok_id}"
            )
        if instagram_id in seen_instagram:
            raise ValueError(
                f"{source_file}: aynı Instagram postu birden fazla TikTok postuna eşleşmiş: "
                f"{instagram_id}"
            )

        seen_tiktok.add(tiktok_id)
        seen_instagram.add(instagram_id)
        _validate_match(match)


def _clear_automatic_matches_for_influencer(
    connection: sqlite3.Connection,
    *,
    influencer: str,
    instagram_ids: set[str] | None = None,
) -> int:
    influencer_row = connection.execute(
        "SELECT id FROM influencers WHERE slug = ?",
        (influencer,),
    ).fetchone()
    if influencer_row is None:
        raise ValueError(f"Influencer DB'de bulunamadı: {influencer}")
    influencer_id = int(influencer_row[0])

    scope = ''
    params = [influencer_id, influencer_id]
    if instagram_ids is not None:
        if not instagram_ids:
            return 0
        scope = ''' AND EXISTS (
            SELECT 1 FROM social_posts AS ig
            WHERE ig.id IN (social_post_matches.post_id_a, social_post_matches.post_id_b)
              AND ig.platform='instagram' AND ig.external_post_id IN ('''
        scope += ','.join('?' for _ in instagram_ids) + '))'
        params.extend(sorted(instagram_ids))
    cursor = connection.execute(
        """
        DELETE FROM social_post_matches
        WHERE match_type IN ('exact', 'near')
          AND post_id_a IN (
              SELECT id FROM social_posts WHERE influencer_id = ?
          )
          AND post_id_b IN (
              SELECT id FROM social_posts WHERE influencer_id = ?
          )
          AND EXISTS (
              SELECT 1
              FROM social_posts AS a
              JOIN social_posts AS b
                ON a.id = social_post_matches.post_id_a
               AND b.id = social_post_matches.post_id_b
              WHERE a.platform <> b.platform
                AND a.platform IN ('tiktok', 'instagram')
                AND b.platform IN ('tiktok', 'instagram')
          )
        """ + scope,
        params,
    )
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def _validate_match(match: dict[str, Any]) -> tuple[str, str, str, float | None, int | None]:
    tiktok_id = str(match.get("tiktok_id") or "").strip()
    instagram_id = str(match.get("instagram_id") or "").strip()
    match_type = str(match.get("match_type") or "").strip().lower()

    if not tiktok_id or not instagram_id:
        raise ValueError("Match kaydında TikTok/Instagram kimliği eksik.")
    if match_type not in {"exact", "near"}:
        raise ValueError(f"Geçersiz match_type: {match_type}")

    similarity_raw = match.get("similarity")
    similarity = None
    if similarity_raw is not None:
        similarity = float(similarity_raw)
        if not math.isfinite(similarity) or not 0.0 <= similarity <= 1.0:
            raise ValueError(f"Geçersiz similarity: {similarity_raw}")

    date_distance_raw = match.get("date_distance_days")
    date_distance_days = None
    if date_distance_raw is not None:
        date_distance_days = int(date_distance_raw)
        if date_distance_days < 0:
            raise ValueError(
                f"Negatif date_distance_days: {date_distance_days}"
            )

    return (
        tiktok_id,
        instagram_id,
        match_type,
        similarity,
        date_distance_days,
    )


def _find_post_id(
    connection: sqlite3.Connection,
    *,
    influencer: str,
    platform: str,
    external_post_id: str,
) -> int:
    row = connection.execute(
        """
        SELECT sp.id
        FROM social_posts AS sp
        JOIN influencers AS i ON i.id = sp.influencer_id
        WHERE i.slug = ?
          AND sp.platform = ?
          AND sp.external_post_id = ?
        """,
        (influencer, platform, external_post_id),
    ).fetchone()

    if row is None:
        raise ValueError(
            f"{platform.capitalize()} post DB'de first-class social_post olarak "
            f"bulunamadı: influencer={influencer} id={external_post_id}"
        )
    return int(row[0])


def _upsert_match(
    connection: sqlite3.Connection,
    *,
    influencer: str,
    match: dict[str, Any],
    source_file: Path,
) -> str:
    (
        tiktok_external_id,
        instagram_external_id,
        match_type,
        similarity,
        date_distance_days,
    ) = _validate_match(match)

    tiktok_post_id = _find_post_id(
        connection,
        influencer=influencer,
        platform="tiktok",
        external_post_id=tiktok_external_id,
    )
    instagram_post_id = _find_post_id(
        connection,
        influencer=influencer,
        platform="instagram",
        external_post_id=instagram_external_id,
    )

    if tiktok_post_id == instagram_post_id:
        raise ValueError("Bir social_post kendisiyle eşleştirilemez.")

    post_id_a, post_id_b = sorted((tiktok_post_id, instagram_post_id))

    existing = connection.execute(
        """
        SELECT id
        FROM social_post_matches
        WHERE post_id_a = ? AND post_id_b = ?
        """,
        (post_id_a, post_id_b),
    ).fetchone()

    connection.execute(
        """
        INSERT INTO social_post_matches (
            post_id_a,
            post_id_b,
            match_type,
            similarity,
            date_distance_days,
            source_file,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(post_id_a, post_id_b) DO UPDATE SET
            match_type=excluded.match_type,
            similarity=excluded.similarity,
            date_distance_days=excluded.date_distance_days,
            source_file=excluded.source_file,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            post_id_a,
            post_id_b,
            match_type,
            similarity,
            date_distance_days,
            str(source_file),
        ),
    )

    return "updated" if existing is not None else "inserted"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/database/skinfluencer.sqlite"),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--influencer", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dateafter")
    parser.add_argument("--datebefore")
    args = parser.parse_args()
    for value in (args.dateafter, args.datebefore):
        if value:
            datetime.strptime(value, '%Y-%m-%d')
    if args.dateafter and args.datebefore and args.dateafter > args.datebefore:
        parser.error('--dateafter must be <= --datebefore')

    if not args.database.is_file():
        parser.error(f"SQLite bulunamadı: {args.database}")

    work = make_rebuild_path(args.database)
    if work.exists():
        parser.error(f"Geçici DB zaten var: {work}")

    backup = None
    if args.dry_run:
        with sqlite3.connect(args.database) as source, sqlite3.connect(work) as dest:
            source.backup(dest)
    else:
        backup = create_consistent_database_backup(args.database)
        if backup is None:
            parser.error("DB backup oluşturulamadı.")
        shutil.copy2(backup, work)

    connection = connect_database(work)
    counts = Counter()
    errors: list[dict[str, str]] = []

    try:
        _ensure_table(connection)
        before = _fingerprints(connection)

        for influencer in list(dict.fromkeys(args.influencer)):
            source = (
                args.data_root
                / influencer
                / "instagram"
                / "tiktok_matches.json"
            )
            if not source.is_file():
                errors.append(
                    {"source": str(source), "error": "match JSON bulunamadı"}
                )
                continue

            matches = _load_matches(source)
            _validate_match_set(matches, source)

            instagram_ids = None
            if args.dateafter or args.datebefore:
                instagram_ids = {
                    str(row[0]) for row in connection.execute(
                        "SELECT sp.external_post_id FROM social_posts sp "
                        "JOIN influencers i ON i.id=sp.influencer_id "
                        "WHERE i.slug=? AND sp.platform='instagram' "
                        "AND sp.published_at>=? AND sp.published_at<=?",
                        (influencer, args.dateafter or '0001-01-01',
                         args.datebefore or '9999-12-31'),
                    )
                }
                counts['outside_date_window'] += sum(
                    str(item['instagram_id']) not in instagram_ids for item in matches
                )
                matches = [item for item in matches
                           if str(item['instagram_id']) in instagram_ids]

            with connection:
                removed = _clear_automatic_matches_for_influencer(
                    connection,
                    influencer=influencer,
                    instagram_ids=instagram_ids,
                )
            counts["removed_stale"] += removed

            for match in matches:
                try:
                    with connection:
                        action = _upsert_match(
                            connection,
                            influencer=influencer,
                            match=match,
                            source_file=source,
                        )
                    counts[action] += 1
                except Exception as exc:
                    errors.append(
                        {
                            "source": str(source),
                            "instagram_id": str(match.get("instagram_id") or ""),
                            "tiktok_id": str(match.get("tiktok_id") or ""),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False, indent=2))

        if _fingerprints(connection) != before:
            raise RuntimeError(
                "Cross-platform match import sırasında korunan tablolar değişti."
            )

        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key hatası")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check başarısız")

        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        total = connection.execute(
            "SELECT COUNT(*) FROM social_post_matches"
        ).fetchone()[0]
        print(
            "Cross-platform match import:",
            json.dumps(dict(counts), ensure_ascii=False),
        )
        print("Toplam social_post_matches:", total)

    except BaseException:
        connection.close()
        remove_database_files(work)
        if backup:
            print("Yedek:", backup)
        raise

    connection.close()

    if args.dry_run:
        remove_database_files(work)
        print("DRY RUN: asıl DB değiştirilmedi.")
    else:
        activate_rebuilt_database(work, args.database)
        print("Veritabanı:", args.database)
        print("Yedek:", backup)


if __name__ == "__main__":
    main()
