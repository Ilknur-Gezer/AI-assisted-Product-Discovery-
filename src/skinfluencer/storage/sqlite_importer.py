from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


DEFAULT_DATA_ROOT = Path("data")
DEFAULT_DATABASE = Path("data/database/skinfluencer.sqlite")
DEFAULT_SOURCE_SUBDIR = "product_mentions_llm_final_v2"
SPLIT_SOURCE_SUBDIRS = (
    "product_mentions_llm_final_v2_long",
    "product_mentions_llm_final_v2_shorts",
)
DEFAULT_PURCHASE_LINKS = Path(
    "data/catalog/purchase_links_verified.json"
)
IGNORED_FILENAMES = {
    "summary.json",
    "failures.json",
    "product_mentions_all.json",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str | None) -> str:
    """Create a stable, accent-insensitive search/deduplication value."""
    if not value:
        return ""

    translated = value.translate(
        str.maketrans(
            {
                "ı": "i",
                "İ": "i",
                "ş": "s",
                "Ş": "s",
                "ğ": "g",
                "Ğ": "g",
                "ü": "u",
                "Ü": "u",
                "ö": "o",
                "Ö": "o",
                "ç": "c",
                "Ç": "c",
            }
        )
    )
    decomposed = unicodedata.normalize("NFKD", translated)
    ascii_like = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(ascii_like.casefold().split()).strip()


def product_identity_key(brand: str | None, product_name: str | None) -> str:
    """Yazım farklarına dayanıklı ürün kimliği.

    Noktalama, &/+ işaretleri, ™/®, kelime sırası, marka boşlukları, "15 ml" ile
    "15ml" farkı ve baştaki sıfırları (003 == 03) yok sayar. Ton/numara tokenları
    anahtarda kalır (02M != 03M) ve sondaki '+' korunur (B5 != B5+).
    """
    def prep(value):
        value = re.sub(r"[™®©℠\ufe0f]", "", value or "")
        value = value.replace("%", " pct ").replace("&", " and ")
        value = re.sub(r"(?<=\w)\+(?=\w)", " and ", value)   # Lift+Sculpt
        value = re.sub(r"\s\+\s", " and ", value)             # Ginseng + Retinal
        value = value.replace("+", " plus ")                    # B5+, SPF50+
        text = re.sub(r"[^a-z0-9 ]", " ", normalize_text(value))
        return re.sub(r"(?<=\d)\s+(?=(?:cc|ml|gr|g|mg|oz)\b)", "", text)

    brand_part = prep(brand).replace(" ", "")
    tokens = [t for t in prep(product_name).split() if t not in {"and", "ve", "the"}]
    tokens = [(t.lstrip("0") or "0") if t.isdigit() else t for t in tokens]
    return f"{brand_part}|{' '.join(sorted(tokens))}"


def find_product_row(connection, brand, product_name):
    """Resolve an approved catalog product, including pre-merge name aliases.

    Never create a product here. New discoveries enter the pending CSV workflow.
    """
    norm_brand, norm_name = normalize_text(brand), normalize_text(product_name)
    row = connection.execute(
        "SELECT id FROM products WHERE normalized_brand=? AND normalized_product_name=? "
        "AND verification_status='approved'", (norm_brand, norm_name)
    ).fetchone()
    if row is not None:
        return row
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_aliases'").fetchone():
        row = connection.execute(
            "SELECT p.id FROM product_aliases a JOIN products p ON p.id=a.canonical_product_id "
            "WHERE a.normalized_brand=? AND a.normalized_product_name=? "
            "AND p.verification_status='approved'", (norm_brand, norm_name)
        ).fetchone()
        if row is not None: return row
    return connection.execute(
        "SELECT id FROM products WHERE identity_key=? AND verification_status='approved' "
        "ORDER BY id LIMIT 1", (product_identity_key(brand, product_name),)
    ).fetchone()


def format_upload_date(value: str | None) -> str | None:
    if not value:
        return None

    value = str(value).strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"

    return value


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_schema(schema_path: Path) -> str:
    if not schema_path.exists():
        raise FileNotFoundError(f"SQL şema dosyası bulunamadı: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def connect_database(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def ensure_schema_compatibility(connection: sqlite3.Connection) -> None:
    """Add compatible columns when importing an older Skinfluencer database."""
    migrations = {
        "videos": {
            "content_type": (
                "TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK (content_type IN ('long', 'shorts', 'unknown'))"
            ),
        },
        "purchase_links": {
            "stock_status": (
                "TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK (stock_status IN "
                "('in_stock', 'out_of_stock', 'unknown'))"
            ),
            "stock_checked_at": "TEXT",
        },
    }

    for table, columns in migrations.items():
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )


def database_sidecar_paths(database_path: Path) -> list[Path]:
    return [
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    ]


def make_rebuild_path(database_path: Path) -> Path:
    return database_path.with_name(
        f".{database_path.name}.rebuild-{os.getpid()}"
    )


def make_backup_path(database_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = database_path.with_name(
        f"{database_path.stem}.backup-{timestamp}{database_path.suffix}"
    )
    counter = 1
    while candidate.exists():
        candidate = database_path.with_name(
            f"{database_path.stem}.backup-{timestamp}-{counter}"
            f"{database_path.suffix}"
        )
        counter += 1
    return candidate


def snapshot_existing_purchase_links(
    database_path: Path,
) -> list[dict[str, Any]]:
    """Read purchase links with product identity before a reset rebuild."""
    if not database_path.exists():
        return []

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"products", "purchase_links"}.issubset(tables):
            return []

        purchase_link_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(purchase_links)"
            )
        }
        stock_status_select = (
            "pl.stock_status"
            if "stock_status" in purchase_link_columns
            else "'unknown' AS stock_status"
        )
        stock_checked_at_select = (
            "pl.stock_checked_at"
            if "stock_checked_at" in purchase_link_columns
            else "NULL AS stock_checked_at"
        )

        rows = connection.execute(
            f"""
            SELECT
                p.brand,
                p.product_name,
                pl.merchant_name,
                pl.merchant_slug,
                pl.link_type,
                pl.url,
                pl.domain,
                pl.verification_status,
                pl.match_confidence,
                pl.source_title,
                pl.verified_at,
                pl.checked_at,
                {stock_status_select},
                {stock_checked_at_select},
                pl.source_file
            FROM purchase_links pl
            JOIN products p ON p.id = pl.product_id
            """
        ).fetchall()
        return [
            {
                **dict(row),
                "_preserved_from_database": True,
            }
            for row in rows
        ]
    finally:
        connection.close()


def snapshot_existing_product_images(
    database_path: Path,
) -> list[dict[str, Any]]:
    """Read product image rows with stable product identity before reset."""
    if not database_path.exists():
        return []

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"products", "product_images"}.issubset(tables):
            return []

        rows = connection.execute(
            """
            SELECT
                p.brand,
                p.product_name,
                pi.image_url,
                pi.source_page_url,
                pi.source_domain,
                pi.source_type,
                pi.local_path,
                pi.mime_type,
                pi.width,
                pi.height,
                pi.file_sha256,
                pi.verification_status,
                pi.checked_at,
                pi.fetched_at,
                pi.source_file
            FROM product_images pi
            JOIN products p ON p.id = pi.product_id
            """
        ).fetchall()
        return [
            {
                **dict(row),
                "_preserved_from_database": True,
            }
            for row in rows
        ]
    finally:
        connection.close()


def create_consistent_database_backup(
    database_path: Path,
) -> Path | None:
    """Create a SQLite-consistent backup before replacing an existing DB."""
    if not database_path.exists():
        return None

    backup_path = make_backup_path(database_path)
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def remove_database_files(database_path: Path) -> None:
    """Remove one exact temporary database and its SQLite sidecars."""
    for path in [database_path, *database_sidecar_paths(database_path)]:
        if path.exists():
            path.unlink()


def activate_rebuilt_database(
    rebuild_path: Path,
    database_path: Path,
) -> None:
    """Atomically promote a completed rebuild after clearing stale sidecars."""
    for sidecar in database_sidecar_paths(database_path):
        if sidecar.exists():
            sidecar.unlink()
    os.replace(rebuild_path, database_path)


def discover_influencers(
    data_root: Path,
    requested_slugs: list[str],
    requested_source_subdirs: list[str],
) -> list[str]:
    if requested_slugs:
        return list(dict.fromkeys(requested_slugs))

    discovered = [
        path.name
        for path in sorted(data_root.iterdir())
        if path.is_dir()
        and resolve_source_dirs(
            data_root,
            path.name,
            requested_source_subdirs,
        )
    ]
    return discovered


def resolve_source_dirs(
    data_root: Path,
    influencer_slug: str,
    requested_source_subdirs: list[str],
) -> list[Path]:
    """Resolve extraction folders without mixing split and stale combined runs."""
    influencer_dir = data_root / influencer_slug

    if requested_source_subdirs:
        return list(
            dict.fromkeys(
                influencer_dir / subdir
                for subdir in requested_source_subdirs
                if (influencer_dir / subdir).exists()
            )
        )

    split_dirs = [
        influencer_dir / subdir
        for subdir in SPLIT_SOURCE_SUBDIRS
        if (influencer_dir / subdir).exists()
    ]
    if split_dirs:
        return split_dirs

    default_dir = influencer_dir / DEFAULT_SOURCE_SUBDIR
    return [default_dir] if default_dir.exists() else []


def iter_source_payloads(source_dir: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    individual_paths = [
        path
        for path in sorted(source_dir.glob("*.json"))
        if path.name not in IGNORED_FILENAMES
    ]

    if individual_paths:
        for path in individual_paths:
            payload = json.loads(path.read_text(encoding="utf-8"))
            yield path, payload
        return

    combined_path = source_dir / "product_mentions_all.json"
    if not combined_path.exists():
        return

    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    if not isinstance(combined, list):
        raise ValueError(
            f"Birleşik çıktı JSON listesi değil: {combined_path}"
        )

    for index, payload in enumerate(combined, start=1):
        synthetic_path = combined_path.with_name(
            f"{combined_path.stem}__item_{index:04d}.json"
        )
        yield synthetic_path, payload


def upsert_influencer(
    connection: sqlite3.Connection,
    slug: str,
    display_name: str,
) -> int:
    connection.execute(
        """
        INSERT INTO influencers (slug, display_name, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(slug) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (slug, display_name),
    )
    row = connection.execute(
        "SELECT id FROM influencers WHERE slug = ?",
        (slug,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def upsert_video(
    connection: sqlite3.Connection,
    *,
    influencer_id: int,
    youtube_video_id: str,
    title: str,
    upload_date: str | None,
    url: str,
    content_type: str,
    source_file: str,
) -> int:
    connection.execute(
        """
        INSERT INTO videos (
            influencer_id,
            youtube_video_id,
            title,
            upload_date,
            url,
            content_type,
            source_file,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(influencer_id, youtube_video_id) DO UPDATE SET
            title = excluded.title,
            upload_date = excluded.upload_date,
            url = excluded.url,
            content_type = excluded.content_type,
            source_file = excluded.source_file,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            influencer_id,
            youtube_video_id,
            title,
            upload_date,
            url,
            content_type,
            source_file,
        ),
    )
    row = connection.execute(
        """
        SELECT id
        FROM videos
        WHERE influencer_id = ? AND youtube_video_id = ?
        """,
        (influencer_id, youtube_video_id),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def upsert_product(connection: sqlite3.Connection, *, brand: str,
                   product_name: str, category: str) -> int | None:
    """Legacy API name; match only. Unknown products MUST NOT be auto-inserted."""
    row = find_product_row(connection, brand, product_name)
    return int(row['id']) if row else None


def import_approved_mention(
    connection: sqlite3.Connection,
    *,
    influencer_id: int,
    video_id: int,
    mention: dict[str, Any],
    payload: dict[str, Any],
    source_file: str,
) -> None:
    brand = str(mention.get("canonical_brand") or "").strip()
    product_name = str(
        mention.get("canonical_product_name") or ""
    ).strip()
    summary = str(
        mention.get("display_summary")
        or mention.get("summary")
        or ""
    ).strip()

    if not brand or not product_name or not summary:
        raise ValueError(
            "Approved kayıtta marka, ürün adı veya display_summary eksik."
        )

    category = str(mention.get("category") or "other_beauty")
    product_id = upsert_product(
        connection,
        brand=brand,
        product_name=product_name,
        category=category,
    )

    if product_id is None:
        return False

    connection.execute(
        """
        INSERT INTO product_mentions (
            product_id,
            video_id,
            influencer_id,
            candidate_id,
            mention_status,
            display_summary,
            grounded_summary,
            sentiment,
            confidence,
            raw_product_mentions_json,
            evidence_texts_json,
            opinion_points_json,
            quality_warnings_json,
            status,
            status_reason,
            source_prompt_version,
            source_summary_style_version,
            extracted_at,
            source_file,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved',
            ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
        )
        ON CONFLICT(product_id, video_id) DO UPDATE SET
            influencer_id = excluded.influencer_id,
            candidate_id = excluded.candidate_id,
            mention_status = excluded.mention_status,
            display_summary = excluded.display_summary,
            grounded_summary = excluded.grounded_summary,
            sentiment = excluded.sentiment,
            confidence = excluded.confidence,
            raw_product_mentions_json = excluded.raw_product_mentions_json,
            evidence_texts_json = excluded.evidence_texts_json,
            opinion_points_json = excluded.opinion_points_json,
            quality_warnings_json = excluded.quality_warnings_json,
            status = 'approved',
            status_reason = excluded.status_reason,
            source_prompt_version = excluded.source_prompt_version,
            source_summary_style_version = excluded.source_summary_style_version,
            extracted_at = excluded.extracted_at,
            source_file = excluded.source_file,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            product_id,
            video_id,
            influencer_id,
            mention.get("candidate_id"),
            mention.get("mention_status") or "reviewed",
            summary,
            mention.get("grounded_summary"),
            mention.get("sentiment") or "unclear",
            float(mention.get("confidence") or 0.0),
            json_text(mention.get("raw_product_mentions") or []),
            json_text(mention.get("evidence_texts") or []),
            json_text(mention.get("opinion_points") or []),
            json_text(mention.get("quality_warnings") or []),
            mention.get("status_reason"),
            payload.get("prompt_version"),
            payload.get("summary_style_version"),
            payload.get("extracted_at"),
            source_file,
        ),
    )
    return True


def import_unresolved_mention(
    connection: sqlite3.Connection,
    *,
    influencer_id: int,
    video_id: int,
    mention: dict[str, Any],
    source_file: str,
) -> None:
    status = str(mention.get("status") or "review")
    if status not in {"review", "rejected"}:
        status = "review"

    connection.execute(
        """
        INSERT INTO unresolved_mentions (
            video_id,
            influencer_id,
            candidate_id,
            canonical_brand,
            canonical_product_name,
            category,
            mention_status,
            status,
            reason,
            confidence,
            display_summary,
            raw_payload_json,
            source_file,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(
            video_id,
            candidate_id,
            canonical_brand,
            canonical_product_name,
            status
        ) DO UPDATE SET
            reason = excluded.reason,
            confidence = excluded.confidence,
            display_summary = excluded.display_summary,
            raw_payload_json = excluded.raw_payload_json,
            source_file = excluded.source_file,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            video_id,
            influencer_id,
            mention.get("candidate_id"),
            mention.get("canonical_brand"),
            mention.get("canonical_product_name"),
            mention.get("category"),
            mention.get("mention_status"),
            status,
            mention.get("status_reason"),
            mention.get("confidence"),
            mention.get("display_summary") or mention.get("summary"),
            json_text(mention),
            source_file,
        ),
    )


def import_video_payload(
    connection: sqlite3.Connection,
    *,
    influencer_slug: str,
    source_path: Path,
    payload: dict[str, Any],
) -> tuple[int, int]:
    if payload.get("extraction_status") != "success":
        raise ValueError("Extraction durumu success değil.")

    youtube_video_id = str(payload.get("video_id") or "").strip()
    if not youtube_video_id:
        raise ValueError("video_id eksik.")

    channel_name = str(
        payload.get("channel")
        or influencer_slug.replace("_", " ").title()
    ).strip()

    influencer_id = upsert_influencer(
        connection,
        influencer_slug,
        channel_name,
    )
    content_type = str(payload.get("content_type") or "").strip()
    if content_type not in {"long", "shorts"}:
        source_dir_name = source_path.parent.name
        if source_dir_name.endswith("_long"):
            content_type = "long"
        elif source_dir_name.endswith("_shorts"):
            content_type = "shorts"
        else:
            content_type = "unknown"

    video_id = upsert_video(
        connection,
        influencer_id=influencer_id,
        youtube_video_id=youtube_video_id,
        title=str(payload.get("title") or youtube_video_id),
        upload_date=format_upload_date(payload.get("upload_date")),
        url=str(
            payload.get("url")
            or f"https://www.youtube.com/watch?v={youtube_video_id}"
        ),
        content_type=content_type,
        source_file=str(source_path),
    )

    # This source file is authoritative for this video. Remove stale rows before
    # importing the newly generated extraction output.
    connection.execute(
        "DELETE FROM product_mentions WHERE video_id = ?",
        (video_id,),
    )
    connection.execute(
        "DELETE FROM unresolved_mentions WHERE video_id = ?",
        (video_id,),
    )

    approved_count = 0
    unresolved_count = 0

    mentions = payload.get("product_mentions") or []
    if not isinstance(mentions, list):
        raise ValueError("product_mentions alanı liste değil.")

    for mention in mentions:
        status = str(mention.get("status") or "review")
        if status == "approved":
            attached = import_approved_mention(
                connection,
                influencer_id=influencer_id,
                video_id=video_id,
                mention=mention,
                payload=payload,
                source_file=str(source_path),
            )
            if attached:
                approved_count += 1
            else:
                pending = dict(mention, status='review',
                               status_reason='pending_canonical_approval')
                import_unresolved_mention(
                    connection, influencer_id=influencer_id,
                    video_id=video_id, mention=pending, source_file=str(source_path))
                unresolved_count += 1
        else:
            import_unresolved_mention(
                connection,
                influencer_id=influencer_id,
                video_id=video_id,
                mention=mention,
                source_file=str(source_path),
            )
            unresolved_count += 1

    return approved_count, unresolved_count


def load_purchase_link_records(path: Path) -> list[dict[str, Any]]:
    """Load verified/review purchase links produced by the enrichment step."""
    if not path.exists():
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("links", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError(
            f"Satın alma bağlantısı kaynağı liste değil: {path}"
        )
    return [record for record in records if isinstance(record, dict)]


def import_purchase_link_records(
    connection: sqlite3.Connection,
    *,
    records: list[dict[str, Any]],
    source_label: str,
) -> tuple[int, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    imported = 0

    for index, record in enumerate(records, start=1):
        try:
            brand = str(record.get("brand") or "").strip()
            product_name = str(
                record.get("product_name") or ""
            ).strip()
            merchant_name = str(
                record.get("merchant_name") or ""
            ).strip()
            merchant_slug = str(
                record.get("merchant_slug") or ""
            ).strip()
            link_type = str(record.get("link_type") or "").strip()
            url = str(record.get("url") or "").strip()
            verification_status = str(
                record.get("verification_status") or "review"
            ).strip()

            if not all(
                (
                    brand,
                    product_name,
                    merchant_name,
                    merchant_slug,
                    url,
                )
            ):
                raise ValueError("Zorunlu bağlantı alanı eksik.")
            if link_type not in {"retailer", "official_brand"}:
                raise ValueError(f"Geçersiz link_type: {link_type}")
            if verification_status not in {
                "verified",
                "review",
                "rejected",
            }:
                raise ValueError(
                    f"Geçersiz verification_status: {verification_status}"
                )

            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Bağlantı geçerli bir HTTPS URL değil.")
            domain = str(
                record.get("domain") or parsed.hostname
            ).casefold().removeprefix("www.")

            product_row = find_product_row(connection, brand, product_name)
            if product_row is None:
                raise ValueError("Ürün SQLite kataloğunda bulunamadı.")
            first_link = connection.execute(
                "SELECT merchant_slug,url FROM purchase_links WHERE product_id=? ORDER BY id LIMIT 1",
                (int(product_row['id']),),
            ).fetchone()
            if first_link and (first_link['merchant_slug'],first_link['url']) != (merchant_slug,url):
                continue  # Single purchase link per product, per agreed simple rule.

            confidence_value = record.get("match_confidence")
            confidence = (
                float(confidence_value)
                if confidence_value is not None
                else None
            )

            connection.execute(
                """
                INSERT INTO purchase_links (
                    product_id,
                    merchant_name,
                    merchant_slug,
                    link_type,
                    url,
                    domain,
                    verification_status,
                    match_confidence,
                    source_title,
                    verified_at,
                    checked_at,
                    stock_status,
                    stock_checked_at,
                    source_file,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(product_id, merchant_slug, url) DO UPDATE SET
                    merchant_name = excluded.merchant_name,
                    link_type = excluded.link_type,
                    domain = excluded.domain,
                    verification_status = CASE
                        WHEN purchase_links.verification_status = 'verified'
                             OR excluded.verification_status = 'verified'
                        THEN 'verified'
                        ELSE excluded.verification_status
                    END,
                    match_confidence = excluded.match_confidence,
                    source_title = excluded.source_title,
                    verified_at = excluded.verified_at,
                    checked_at = excluded.checked_at,
                    stock_status = CASE
                        WHEN excluded.stock_status <> 'unknown'
                        THEN excluded.stock_status
                        ELSE purchase_links.stock_status
                    END,
                    stock_checked_at = COALESCE(
                        excluded.stock_checked_at,
                        purchase_links.stock_checked_at
                    ),
                    source_file = excluded.source_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(product_row["id"]),
                    merchant_name,
                    merchant_slug,
                    link_type,
                    url,
                    domain,
                    verification_status,
                    confidence,
                    record.get("source_title"),
                    record.get("verified_at"),
                    record.get("checked_at"),
                    (
                        record.get("stock_status")
                        if record.get("stock_status")
                        in {"in_stock", "out_of_stock", "unknown"}
                        else "unknown"
                    ),
                    record.get("stock_checked_at"),
                    str(record.get("source_file") or source_label),
                ),
            )
            imported += 1
        except Exception as error:
            errors.append(
                {
                    "source": f"{source_label}#links[{index}]",
                    "error": f"{type(error).__name__}: {error}",
                    "preserved_from_database": bool(
                        record.get("_preserved_from_database")
                    ),
                    "preserved_verified": bool(
                        record.get("_preserved_from_database")
                        and record.get("verification_status") == "verified"
                    ),
                }
            )

    return imported, errors


def import_product_image_records(
    connection: sqlite3.Connection,
    *,
    records: list[dict[str, Any]],
    source_label: str,
) -> tuple[int, list[dict[str, Any]]]:
    """Restore preserved product images by product identity after a reset."""
    errors: list[dict[str, Any]] = []
    imported = 0

    for index, record in enumerate(records, start=1):
        try:
            brand = str(record.get("brand") or "").strip()
            product_name = str(record.get("product_name") or "").strip()
            image_url = str(record.get("image_url") or "").strip()
            source_page_url = str(record.get("source_page_url") or "").strip()
            source_domain = str(record.get("source_domain") or "").strip()
            source_type = str(record.get("source_type") or "").strip()
            local_path = str(record.get("local_path") or "").strip()
            verification_status = str(
                record.get("verification_status") or "verified"
            ).strip()

            if not all(
                (
                    brand,
                    product_name,
                    image_url,
                    source_page_url,
                    source_domain,
                    source_type,
                    local_path,
                )
            ):
                raise ValueError("Zorunlu ürün görseli alanı eksik.")
            if source_type not in {
                "og_image",
                "json_ld",
                "twitter_image",
                "manual",
            }:
                raise ValueError(f"Geçersiz source_type: {source_type}")
            if verification_status not in {"verified", "review", "rejected"}:
                raise ValueError(
                    f"Geçersiz verification_status: {verification_status}"
                )

            product_row = find_product_row(connection, brand, product_name)
            if product_row is None:
                raise ValueError("Ürün SQLite kataloğunda bulunamadı.")

            connection.execute(
                """
                INSERT INTO product_images (
                    product_id,
                    image_url,
                    source_page_url,
                    source_domain,
                    source_type,
                    local_path,
                    mime_type,
                    width,
                    height,
                    file_sha256,
                    verification_status,
                    checked_at,
                    fetched_at,
                    source_file,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(product_id) DO UPDATE SET
                    image_url = excluded.image_url,
                    source_page_url = excluded.source_page_url,
                    source_domain = excluded.source_domain,
                    source_type = excluded.source_type,
                    local_path = excluded.local_path,
                    mime_type = excluded.mime_type,
                    width = excluded.width,
                    height = excluded.height,
                    file_sha256 = excluded.file_sha256,
                    verification_status = CASE
                        WHEN product_images.verification_status = 'verified'
                             OR excluded.verification_status = 'verified'
                        THEN 'verified'
                        ELSE excluded.verification_status
                    END,
                    checked_at = COALESCE(
                        excluded.checked_at,
                        product_images.checked_at
                    ),
                    fetched_at = COALESCE(
                        excluded.fetched_at,
                        product_images.fetched_at
                    ),
                    source_file = excluded.source_file,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    int(product_row["id"]),
                    image_url,
                    source_page_url,
                    source_domain,
                    source_type,
                    local_path,
                    record.get("mime_type"),
                    record.get("width"),
                    record.get("height"),
                    record.get("file_sha256"),
                    verification_status,
                    record.get("checked_at"),
                    record.get("fetched_at"),
                    str(record.get("source_file") or source_label),
                ),
            )
            imported += 1
        except Exception as error:
            errors.append(
                {
                    "source": f"{source_label}#product_images[{index}]",
                    "error": f"{type(error).__name__}: {error}",
                    "preserved_from_database": bool(
                        record.get("_preserved_from_database")
                    ),
                    "preserved_verified_image": bool(
                        record.get("_preserved_from_database")
                        and record.get("verification_status") == "verified"
                    ),
                }
            )

    return imported, errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "OpenAI product_mentions_llm_final_v2 JSON çıktılarını "
            "idempotent biçimde SQLite veritabanına aktarır."
        )
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).with_name("schema.sql"),
    )
    parser.add_argument(
        "--source-subdir",
        action="append",
        default=[],
        dest="source_subdirs",
        help=(
            "Kaynak alt klasörü. Birden fazla kez verilebilir. Verilmezse "
            "_long/_shorts klasörleri birlikte; onlar yoksa "
            "product_mentions_llm_final_v2 kullanılır."
        ),
    )
    parser.add_argument(
        "--purchase-links",
        type=Path,
        default=DEFAULT_PURCHASE_LINKS,
        help=(
            "Doğrulanmış satın alma bağlantıları JSON dosyası. "
            "Dosya yoksa bu adım sessizce atlanır."
        ),
    )
    parser.add_argument(
        "--influencer",
        action="append",
        default=[],
        help=(
            "İçe aktarılacak influencer slug'ı. Birden fazla kez verilebilir. "
            "Verilmezse source klasörü bulunan tüm influencer'lar taranır."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Yeni veritabanını geçici dosyada oluştur, mevcut veritabanını "
            "yedekle ve doğrulama başarılıysa atomik olarak değiştir."
        ),
    )
    args = parser.parse_args()
    # Never overwrite a finalized catalog by rebuilding from raw extraction files.
    if args.reset and args.database.exists():
        with sqlite3.connect(args.database) as existing:
            if existing.execute("SELECT 1 FROM sqlite_master WHERE name='product_aliases'").fetchone():
                raise SystemExit('--reset is forbidden on the canonicalized database')

    requested_source_subdirs = list(dict.fromkeys(args.source_subdirs))

    influencer_slugs = discover_influencers(
        args.data_root,
        args.influencer,
        requested_source_subdirs,
    )
    if not influencer_slugs:
        raise RuntimeError(
            "İçe aktarılabilecek influencer kaynak klasörü bulunamadı."
        )

    from .social_products import snapshot_social, restore_social
    preserved_social = snapshot_social(args.database) if args.reset else None

    preserved_purchase_links = snapshot_existing_purchase_links(args.database)
    preserved_verified_count = sum(
        1
        for record in preserved_purchase_links
        if record.get("verification_status") == "verified"
    )
    preserved_product_images = (
        snapshot_existing_product_images(args.database) if args.reset else []
    )
    preserved_verified_image_count = sum(
        1
        for record in preserved_product_images
        if record.get("verification_status") == "verified"
    )

    backup_path: Path | None = None
    build_database = args.database
    if args.reset:
        backup_path = create_consistent_database_backup(args.database)
        build_database = make_rebuild_path(args.database)
        remove_database_files(build_database)

    schema_sql = load_schema(args.schema)
    connection = connect_database(build_database)
    errors: list[dict[str, Any]] = []
    files_seen = 0
    videos_imported = 0
    approved_total = 0
    unresolved_total = 0
    purchase_links_total = 0
    product_images_total = 0
    started_at = utc_now_iso()

    try:
        connection.executescript(schema_sql)
        ensure_schema_compatibility(connection)
        # Recreate views after a pre-v3 database receives new columns.
        connection.executescript(schema_sql)
        cursor = connection.execute(
            """
            INSERT INTO import_runs (
                started_at,
                database_path,
                source_subdir,
                influencer_slugs_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                started_at,
                str(args.database),
                json_text(requested_source_subdirs or ["auto"]),
                json_text(influencer_slugs),
            ),
        )
        run_id = int(cursor.lastrowid)

        for slug in influencer_slugs:
            source_dirs = resolve_source_dirs(
                args.data_root,
                slug,
                requested_source_subdirs,
            )
            if not source_dirs:
                errors.append(
                    {
                        "source": str(args.data_root / slug),
                        "error": "Uygun kaynak klasör bulunamadı.",
                    }
                )
                continue

            print(
                f"[{slug}] kaynak: "
                + ", ".join(path.name for path in source_dirs)
            )
            for source_dir in source_dirs:
                for source_path, payload in iter_source_payloads(source_dir):
                    files_seen += 1
                    # Incremental by default: existing videos already contain review
                    # translations; replacing them would cascade-delete translations.
                    existing_video = connection.execute(
                        "SELECT 1 FROM videos v JOIN influencers i ON i.id=v.influencer_id "
                        "WHERE i.slug=? AND v.youtube_video_id=?",
                        (slug, str(payload.get('video_id') or '')),
                    ).fetchone()
                    if existing_video:
                        continue
                    try:
                        with connection:
                            approved_count, unresolved_count = import_video_payload(
                                connection,
                                influencer_slug=slug,
                                source_path=source_path,
                                payload=payload,
                            )
                        videos_imported += 1
                        approved_total += approved_count
                        unresolved_total += unresolved_count
                        print(
                            f"[{slug}] {payload.get('video_id')}: "
                            f"{approved_count} approved, "
                            f"{unresolved_count} unresolved"
                        )
                    except Exception as error:
                        errors.append(
                            {
                                "source": str(source_path),
                                "error": f"{type(error).__name__}: {error}",
                            }
                        )
                        print(
                            f"[{slug}] HATA {source_path.name}: "
                            f"{type(error).__name__}: {error}"
                        )

        # Restore social rows by product/influencer identity after integer IDs change.
        if preserved_social is not None:
            with connection:
                restore_social(connection, preserved_social)

        # The CSV canonical catalog is authoritative; do NOT delete catalog
        # products just because no current review/link points to them.
        with connection:
            external_purchase_links = load_purchase_link_records(
                args.purchase_links
            )
            preserved_imported, preserved_link_errors = (
                import_purchase_link_records(
                    connection,
                    records=preserved_purchase_links,
                    source_label=str(args.database),
                )
            )
            external_imported, external_link_errors = (
                import_purchase_link_records(
                    connection,
                    records=external_purchase_links,
                    source_label=str(args.purchase_links),
                )
            )
            purchase_links_total = external_imported + preserved_imported
            errors.extend(external_link_errors)
            errors.extend(preserved_link_errors)

            preservation_errors = [
                error
                for error in preserved_link_errors
                if error.get("preserved_verified")
            ]
            rebuilt_verified_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM purchase_links
                    WHERE verification_status = 'verified'
                    """
                ).fetchone()[0]
            )
            if preservation_errors or (
                rebuilt_verified_count < preserved_verified_count
            ):
                raise RuntimeError(
                    "Verified satın alma linki koruma kontrolü başarısız: "
                    f"önce={preserved_verified_count}, "
                    f"sonra={rebuilt_verified_count}, "
                    f"eşleşmeyen={len(preservation_errors)}. "
                    "Mevcut veritabanı değiştirilmedi."
                )

            if preserved_product_images:
                product_images_total, product_image_errors = (
                    import_product_image_records(
                        connection,
                        records=preserved_product_images,
                        source_label=str(args.database),
                    )
                )
                errors.extend(product_image_errors)

                image_preservation_errors = [
                    error
                    for error in product_image_errors
                    if error.get("preserved_verified_image")
                ]
                rebuilt_verified_image_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM product_images
                        WHERE verification_status = 'verified'
                        """
                    ).fetchone()[0]
                )
                if image_preservation_errors or (
                    rebuilt_verified_image_count < preserved_verified_image_count
                ):
                    raise RuntimeError(
                        "Verified ürün görseli koruma kontrolü başarısız: "
                        f"önce={preserved_verified_image_count}, "
                        f"sonra={rebuilt_verified_image_count}, "
                        f"eşleşmeyen={len(image_preservation_errors)}. "
                        "Mevcut veritabanı değiştirilmedi."
                    )

            connection.execute(
                """
                UPDATE import_runs
                SET
                    finished_at = ?,
                    files_seen = ?,
                    videos_imported = ?,
                    approved_mentions_imported = ?,
                    unresolved_mentions_imported = ?,
                    errors_json = ?
                WHERE id = ?
                """,
                (
                    utc_now_iso(),
                    files_seen,
                    videos_imported,
                    approved_total,
                    unresolved_total,
                    json_text(errors),
                    run_id,
                ),
            )

        counts_row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM influencers) AS influencers,
                (SELECT COUNT(*) FROM videos) AS videos,
                (SELECT COUNT(*) FROM products) AS products,
                (SELECT COUNT(*) FROM product_mentions) AS mentions,
                (SELECT COUNT(*) FROM unresolved_mentions) AS unresolved,
                (SELECT COUNT(*) FROM purchase_links
                 WHERE verification_status = 'verified') AS purchase_links,
                (SELECT COUNT(*) FROM product_images
                 WHERE verification_status = 'verified') AS product_images
            """
        ).fetchone()
        assert counts_row is not None
        counts = dict(counts_row)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        connection.close()
        if args.reset:
            remove_database_files(build_database)
        raise
    else:
        connection.close()

    if args.reset:
        activate_rebuilt_database(build_database, args.database)

    print()
    print("=" * 80)
    print("SQLITE IMPORT TAMAMLANDI")
    print("=" * 80)
    print(f"Veritabanı:          {args.database}")
    if backup_path is not None:
        print(f"Önceki DB yedeği:    {backup_path}")
    print(f"Influencer:          {counts['influencers']}")
    print(f"Video:               {counts['videos']}")
    print(f"Benzersiz ürün:      {counts['products']}")
    print(f"Approved inceleme:   {counts['mentions']}")
    print(f"İncelenecek kayıt:   {counts['unresolved']}")
    print(f"Verified link:       {counts['purchase_links']}")
    print(f"Ürün görseli:        {counts['product_images']}")
    print(f"Korunan eski link:   {preserved_verified_count}")
    print(f"Korunan eski görsel: {preserved_verified_image_count}")
    print(f"Link import denemesi:{purchase_links_total:>7}")
    print(f"Görsel restore:      {product_images_total:>7}")
    print(f"Bu çalışmada hata:   {len(errors)}")

    if errors:
        error_path = args.database.with_suffix(".import_errors.json")
        error_path.write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Hata raporu:         {error_path}")


if __name__ == "__main__":
    main()
