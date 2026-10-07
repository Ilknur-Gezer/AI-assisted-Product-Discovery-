"""Import Instagram caption products as first-class social posts on a verified DB copy.

No OpenAI calls. Existing YouTube and TikTok social rows are fingerprint-protected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path

from .sqlite_importer import (
    activate_rebuilt_database,
    connect_database,
    create_consistent_database_backup,
    make_rebuild_path,
    remove_database_files,
)
from .social_products import (
    NAMES,
    SOCIAL_SCHEMA,
    import_payload,
    instagram_post_from_metadata,
)

PROTECTED_BASE = (
    "videos",
    "product_mentions",
    "unresolved_mentions",
    "purchase_links",
    "product_images",
    "social_post_platform_matches",
)


def _hash_rows(rows) -> str:
    return hashlib.sha256(
        json.dumps(
            [tuple(row) for row in rows],
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    result: dict[str, str] = {}

    for table in PROTECTED_BASE:
        if table in existing:
            result[table] = _hash_rows(
                connection.execute(f"SELECT * FROM {table} ORDER BY id")
            )

    if "social_posts" in existing:
        result["tiktok_social_posts"] = _hash_rows(
            connection.execute(
                "SELECT * FROM social_posts "
                "WHERE platform='tiktok' ORDER BY id"
            )
        )

    if {"social_posts", "social_post_products"}.issubset(existing):
        result["tiktok_social_post_products"] = _hash_rows(
            connection.execute(
                """
                SELECT spp.*
                FROM social_post_products AS spp
                JOIN social_posts AS sp ON sp.id = spp.social_post_id
                WHERE sp.platform='tiktok'
                ORDER BY spp.id
                """
            )
        )

    if {
        "social_posts",
        "social_post_products",
        "social_product_contexts",
    }.issubset(existing):
        result["tiktok_social_product_contexts"] = _hash_rows(
            connection.execute(
                """
                SELECT spc.*
                FROM social_product_contexts AS spc
                JOIN social_post_products AS spp
                  ON spp.id = spc.social_post_product_id
                JOIN social_posts AS sp
                  ON sp.id = spp.social_post_id
                WHERE sp.platform='tiktok'
                ORDER BY spc.id
                """
            )
        )

    return result


def _iter_files(data_root: Path, slugs: list[str]):
    for slug in slugs:
        out = data_root / slug / "instagram" / "description_products_llm"
        for path in sorted(out.glob("*.json")):
            if path.name in {
                "summary.json",
                "failures.json",
                "description_products_all.json",
            }:
                continue
            yield slug, path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/database/skinfluencer.sqlite"),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument(
        "--influencer",
        action="append",
        choices=sorted(NAMES),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate on a temporary DB; never activate it.",
    )
    parser.add_argument("--dateafter")
    parser.add_argument("--datebefore")
    args = parser.parse_args()

    if not args.database.is_file():
        parser.error("Mevcut SQLite veritabanı bulunamadı.")

    slugs = args.influencer or list(NAMES)
    files = list(_iter_files(args.data_root, slugs))
    if not files:
        parser.error("Instagram extraction çıktısı bulunamadı.")

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
        before = _fingerprints(connection)
        existing_products = [tuple(row) for row in connection.execute('SELECT * FROM products ORDER BY id')]
        max_product_id = max((row[0] for row in existing_products), default=0)
        connection.executescript(SOCIAL_SCHEMA)

        for slug, path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Extraction JSON nesne olmalı.")

                published = str(payload.get("published_at") or "")
                if args.dateafter and published and published < args.dateafter:
                    counts["outside_date_window"] += 1
                    continue
                if args.datebefore and published and published > args.datebefore:
                    counts["outside_date_window"] += 1
                    continue

                if payload.get("platform") != "instagram":
                    raise ValueError("Instagram extraction payload gerekli.")
                if payload.get("influencer_slug") != slug:
                    raise ValueError("Extraction influencer/dizin uyuşmazlığı.")

                external_id = str(payload.get("external_post_id") or "").strip()
                if not external_id or external_id != path.stem:
                    raise ValueError("Extraction kimliği/dosya adı uyuşmazlığı.")

                raw_path = (
                    args.data_root
                    / slug
                    / "instagram"
                    / "raw"
                    / f"{external_id}.info.json"
                )
                current = instagram_post_from_metadata(
                    json.loads(raw_path.read_text(encoding="utf-8")),
                    slug,
                )
                if any(current[key] != payload.get(key) for key in current):
                    raise ValueError(
                        "Ham Instagram metadata değişmiş; extraction yeniden çalıştırılmalı."
                    )

                with connection:
                    result = import_payload(connection, payload, path)
                counts.update(result)
                counts["posts"] += 1
                if payload.get("extraction_mode") == "tiktok_reuse":
                    counts["reused_posts"] += 1
                elif payload.get("extraction_mode") == "llm":
                    counts["llm_posts"] += 1
            except Exception as exc:
                errors.append(
                    {
                        "file": str(path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False, indent=2))

        if _fingerprints(connection) != before:
            raise RuntimeError(
                "Instagram import sırasında korunan YouTube/TikTok tabloları değişti."
            )
        if [tuple(row) for row in connection.execute(
            'SELECT * FROM products WHERE id<=? ORDER BY id', (max_product_id,)
        )] != existing_products:
            raise RuntimeError('Existing product catalog rows changed during Instagram import.')

        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key hatası")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check başarısız")

        connection.execute("PRAGMA user_version=7")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        print("Instagram import:", json.dumps(dict(counts), ensure_ascii=False))
        print(
            "Instagram social_posts:",
            connection.execute(
                "SELECT COUNT(*) FROM social_posts WHERE platform='instagram'"
            ).fetchone()[0],
        )
        print(
            "Toplam approved ürün–gönderi bağlantısı:",
            connection.execute(
                "SELECT COUNT(*) FROM social_post_products WHERE status='approved'"
            ).fetchone()[0],
        )
        print("YouTube ve TikTok mevcut kayıtları: birebir korundu.")
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
