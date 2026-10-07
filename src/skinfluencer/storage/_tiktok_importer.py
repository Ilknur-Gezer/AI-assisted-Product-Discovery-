"""Import TikTok caption products on a verified SQLite copy.

No OpenAI calls, no reset. Existing YouTube and Instagram social rows are
fingerprint-protected.
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
    ACCOUNTS,
    SOCIAL_SCHEMA,
    import_context_payload,
    import_payload,
    post_from_metadata,
)

PROTECTED_BASE = (
    "videos",
    "product_mentions",
    "unresolved_mentions",
    "purchase_links",
    "product_images",
)


def _hash_rows(rows) -> str:
    return hashlib.sha256(
        json.dumps(
            [tuple(row) for row in rows],
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
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

    # TikTok import must never mutate already imported Instagram posts/products.
    if "social_posts" in existing:
        result["instagram_social_posts"] = _hash_rows(
            connection.execute(
                "SELECT * FROM social_posts "
                "WHERE platform='instagram' ORDER BY id"
            )
        )

    if {"social_posts", "social_post_products"}.issubset(existing):
        result["instagram_social_post_products"] = _hash_rows(
            connection.execute(
                """
                SELECT spp.*
                FROM social_post_products AS spp
                JOIN social_posts AS sp ON sp.id = spp.social_post_id
                WHERE sp.platform='instagram'
                ORDER BY spp.id
                """
            )
        )

    if {
        "social_posts",
        "social_post_products",
        "social_product_contexts",
    }.issubset(existing):
        result["instagram_social_product_contexts"] = _hash_rows(
            connection.execute(
                """
                SELECT spc.*
                FROM social_product_contexts AS spc
                JOIN social_post_products AS spp
                  ON spp.id = spc.social_post_product_id
                JOIN social_posts AS sp
                  ON sp.id = spp.social_post_id
                WHERE sp.platform='instagram'
                ORDER BY spc.id
                """
            )
        )

    if "social_post_matches" in existing:
        result["social_post_matches"] = _hash_rows(
            connection.execute(
                "SELECT * FROM social_post_matches ORDER BY id"
            )
        )

    return result


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
        choices=sorted(ACCOUNTS),
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
        parser.error("Mevcut YouTube SQLite veritabanı bulunamadı.")

    slugs = args.influencer or list(ACCOUNTS)
    files = [
        (slug, path)
        for slug in slugs
        for path in sorted(
            (
                args.data_root
                / slug
                / "tiktok"
                / "description_products_llm"
            ).glob("*.json")
        )
        if path.stem.isdigit()
    ]
    if not files:
        parser.error("TikTok extraction çıktısı bulunamadı.")

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
        before = fingerprints(connection)
        connection.executescript(SOCIAL_SCHEMA)

        for slug, path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                published = str(payload.get("published_at") or "")
                if args.dateafter and published and published < args.dateafter:
                    counts["outside_date_window"] += 1
                    continue
                if args.datebefore and published and published > args.datebefore:
                    counts["outside_date_window"] += 1
                    continue

                if (
                    payload["influencer_slug"] != slug
                    or payload["external_post_id"] != path.stem
                ):
                    raise ValueError("Extraction kimliği/dizin uyuşmazlığı")

                raw_path = (
                    args.data_root
                    / slug
                    / "tiktok"
                    / "raw"
                    / f"{path.stem}.info.json"
                )
                current = post_from_metadata(
                    json.loads(raw_path.read_text(encoding="utf-8")),
                    slug,
                )
                if any(current[key] != payload.get(key) for key in current):
                    raise ValueError(
                        "Ham metadata değişmiş; extraction yeniden çalıştırılmalı."
                    )

                with connection:
                    result = import_payload(connection, payload, path)
                    context_path = (
                        args.data_root
                        / slug
                        / "tiktok"
                        / "caption_context_llm"
                        / f"{path.stem}.json"
                    )
                    if context_path.is_file():
                        context_payload = json.loads(
                            context_path.read_text(encoding="utf-8")
                        )
                        imported_contexts = import_context_payload(
                            connection,
                            context_payload,
                            payload,
                            context_path,
                        )
                        counts["caption_contexts"] += imported_contexts

                counts.update(result)
                counts["posts"] += 1
            except Exception as exc:
                errors.append(
                    {
                        "file": str(path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        if errors:
            raise ValueError(json.dumps(errors, ensure_ascii=False, indent=2))

        if fingerprints(connection) != before:
            raise RuntimeError(
                "TikTok import sırasında korunan YouTube/Instagram kayıtları değişti."
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key hatası")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check başarısız")

        connection.execute("PRAGMA user_version=7")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        print("İşlenen:", json.dumps(dict(counts), ensure_ascii=False))
        print(
            "Toplam sosyal gönderi:",
            connection.execute("SELECT count(*) FROM social_posts").fetchone()[0],
        )
        print(
            "Toplam approved ürün–gönderi bağlantısı:",
            connection.execute(
                "SELECT count(*) FROM social_post_products "
                "WHERE status='approved'"
            ).fetchone()[0],
        )
        print(
            "Toplam caption context:",
            connection.execute(
                "SELECT count(*) FROM social_product_contexts"
            ).fetchone()[0],
        )
        print("YouTube, Instagram ve cross-platform match kayıtları korundu.")
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
