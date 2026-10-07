#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skinfluencer.config.influencers import INFLUENCERS, influencer_slugs
from skinfluencer.config.settings import DATABASE_PATH, DATA_ROOT


@contextmanager
def argv_for(program: str, args: list[str]):
    previous = sys.argv[:]
    sys.argv = [program, *args]
    try:
        yield
    finally:
        sys.argv = previous


def run_main(module, args: list[str]) -> None:
    with argv_for(module.__name__, args):
        module.main()


def selected_influencers(value: str | None) -> list[str]:
    return [value] if value else influencer_slugs()


def _validate_iso_date(value: str | None, option: str) -> str | None:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"{option} YYYY-MM-DD olmalı: {value}") from exc
    return value


def _state_path(influencer: str) -> Path:
    return DATA_ROOT / influencer / "incremental_update_state.json"


def _load_update_state(influencer: str) -> dict:
    path = _state_path(influencer)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_since(influencer: str, explicit: str | None) -> str:
    if explicit:
        return _validate_iso_date(explicit, "--since") or explicit
    state = _load_update_state(influencer)
    value = str(state.get("last_successful_update_date") or "").strip()
    if value:
        _validate_iso_date(value, "state.last_successful_update_date")
        return value
    raise SystemExit(
        f"{influencer}: ilk incremental run için --since YYYY-MM-DD verin. "
        "Başarılı run'dan sonra tarih otomatik state'ten alınacak."
    )


def _save_update_state(influencer: str, since: str) -> None:
    path = _state_path(influencer)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    payload = {
        "influencer": influencer,
        "last_successful_since": since,
        "last_successful_update_date": now.date().isoformat(),
        "last_successful_update_at": now.isoformat(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _db_counts() -> dict[str, int]:
    if not DATABASE_PATH.is_file():
        return {}
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        queries = {
            "products": "SELECT COUNT(*) FROM products",
            "social_posts": "SELECT COUNT(*) FROM social_posts",
            "approved_social_products": (
                "SELECT COUNT(*) FROM social_post_products WHERE status='approved'"
            ),
            "cross_platform_matches": (
                "SELECT COUNT(*) FROM social_post_matches"
            ),
        }
        counts: dict[str, int] = {}
        for key, query in queries.items():
            table = {
                "products": "products",
                "social_posts": "social_posts",
                "approved_social_products": "social_post_products",
                "cross_platform_matches": "social_post_matches",
            }[key]
            if table in tables:
                counts[key] = int(connection.execute(query).fetchone()[0])
        return counts
    finally:
        connection.close()


def _print_db_delta(before: dict[str, int], after: dict[str, int]) -> None:
    print("\nDatabase delta")
    for key in sorted(set(before) | set(after)):
        old = before.get(key, 0)
        new = after.get(key, 0)
        delta = new - old
        print(f"  {key:26s} {old:>6} -> {new:<6} ({delta:+d})")


def cmd_collect(args: argparse.Namespace) -> None:
    influencers = selected_influencers(args.influencer)
    platforms = ["youtube", "tiktok", "instagram"] if args.platform == "all" else [args.platform]
    for platform in platforms:
        for influencer in influencers:
            print(f"\n=== collect {platform}: {influencer} ===")
            if platform == "youtube":
                from skinfluencer.sources.youtube import collect
                collect(
                    influencer,
                    shorts=args.shorts,
                    dateafter=args.dateafter,
                    datebefore=args.datebefore,
                )
            elif platform == "tiktok":
                from skinfluencer.sources.tiktok import collect
                collect(
                    influencer,
                    dateafter=args.dateafter,
                    datebefore=args.datebefore,
                )
            else:
                from skinfluencer.sources.instagram import collect
                collect(
                    influencer,
                    dateafter=args.dateafter,
                    datebefore=args.datebefore,
                    cookies_file=args.instagram_cookies,
                    browser=args.instagram_browser,
                )


def cmd_extract_products(args: argparse.Namespace) -> None:
    influencers = selected_influencers(args.influencer)
    platforms = ["youtube", "tiktok", "instagram"] if args.platform == "all" else [args.platform]
    for platform in platforms:
        for influencer in influencers:
            print(f"\n=== extract-products {platform}: {influencer} ===")
            forwarded = ["--influencer", influencer]
            if args.model:
                forwarded += ["--model", args.model]
            if args.limit is not None:
                forwarded += ["--limit", str(args.limit)]
            if args.force:
                forwarded += ["--force"]
            if args.dry_run and platform in {"tiktok", "instagram"}:
                forwarded += ["--dry-run"]
            if platform in {"tiktok", "instagram"}:
                if getattr(args, "dateafter", None):
                    forwarded += ["--dateafter", args.dateafter]
                if getattr(args, "datebefore", None):
                    forwarded += ["--datebefore", args.datebefore]

            if platform == "tiktok":
                from skinfluencer.extraction import _tiktok_product_extractor
                run_main(_tiktok_product_extractor, forwarded)
            elif platform == "instagram":
                from skinfluencer.extraction import _instagram_product_extractor
                run_main(_instagram_product_extractor, forwarded)
            elif platform == "youtube":
                from skinfluencer.extraction import product_extractor
                run_main(product_extractor, forwarded)


def cmd_extract_caption_context(args: argparse.Namespace) -> None:
    if args.platform not in {"tiktok", "all"}:
        raise NotImplementedError("Instagram caption context extraction is not enabled.")
    from skinfluencer.extraction import caption_context_extractor
    for influencer in selected_influencers(args.influencer):
        forwarded = ["--platform", "tiktok", "--influencer", influencer]
        if args.model:
            forwarded += ["--model", args.model]
        if args.limit is not None:
            forwarded += ["--limit", str(args.limit)]
        if args.video_id:
            forwarded += ["--video-id", args.video_id]
        if args.force:
            forwarded += ["--force"]
        if args.dry_run:
            forwarded += ["--dry-run"]
        run_main(caption_context_extractor, forwarded)


def cmd_extract_opinions(args: argparse.Namespace) -> None:
    if args.platform != "youtube":
        raise SystemExit("Opinion extraction is currently supported only for YouTube transcripts.")
    from skinfluencer.extraction import opinion_extractor
    for influencer in selected_influencers(args.influencer):
        forwarded = ["--influencer", influencer]
        if args.model:
            forwarded += ["--model", args.model]
        if args.limit is not None:
            forwarded += ["--limit", str(args.limit)]
        if args.force:
            forwarded += ["--force"]
        run_main(opinion_extractor, forwarded)


def _has_instagram_extractions(influencers: list[str]) -> bool:
    return any(
        any(
            path.name not in {
                "summary.json",
                "failures.json",
                "description_products_all.json",
            }
            for path in (
                DATA_ROOT / influencer / "instagram" / "description_products_llm"
            ).glob("*.json")
        )
        for influencer in influencers
    )


def _has_match_json(influencers: list[str]) -> bool:
    return any(
        (DATA_ROOT / influencer / "instagram" / "tiktok_matches.json").is_file()
        for influencer in influencers
    )


def cmd_build_database(args: argparse.Namespace) -> None:
    from skinfluencer.storage import sqlite_importer
    from skinfluencer.storage import _tiktok_importer
    from skinfluencer.storage import _instagram_importer
    from skinfluencer.storage import platform_match_importer

    selected = selected_influencers(args.influencer)
    social_args: list[str] = []
    for influencer in selected:
        social_args += ["--influencer", influencer]
    if args.dateafter:
        social_args += ["--dateafter", args.dateafter]
    if args.datebefore:
        social_args += ["--datebefore", args.datebefore]

    has_instagram = _has_instagram_extractions(selected)
    has_matches = _has_match_json(selected)

    if args.dry_run:
        # Rebuild and all social imports run against one temporary SQLite copy.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="skinfluencer-db-check-") as tmp:
            temp_db = Path(tmp) / "skinfluencer.sqlite"
            if DATABASE_PATH.exists():
                source = sqlite3.connect(DATABASE_PATH)
                target = sqlite3.connect(temp_db)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()

            rebuild_args = ["--database", str(temp_db)]
            if args.reset:
                rebuild_args.append("--reset")
            run_main(sqlite_importer, rebuild_args)

            # The database itself is disposable, so importers should ACTIVATE
            # into this temp path. Passing their --dry-run flags would prevent
            # downstream Instagram/match steps from seeing earlier temp changes.
            run_main(
                _tiktok_importer,
                ["--database", str(temp_db), *social_args],
            )

            if has_instagram:
                run_main(
                    _instagram_importer,
                    ["--database", str(temp_db), *social_args],
                )

            if has_instagram and has_matches:
                run_main(
                    platform_match_importer,
                    ["--database", str(temp_db), *social_args],
                )

        print("Dry-run completed on temporary SQLite copy; production DB unchanged.")
        return

    forwarded: list[str] = []
    if args.reset:
        forwarded.append("--reset")
    run_main(sqlite_importer, forwarded)
    run_main(_tiktok_importer, social_args)

    if has_instagram:
        run_main(_instagram_importer, social_args)

    if has_instagram and has_matches:
        run_main(platform_match_importer, social_args)


def cmd_ingest_instagram(args: argparse.Namespace) -> None:
    """Resume cached extraction, then atomically activate posts and relations together."""
    import tempfile
    from skinfluencer.extraction import _instagram_product_extractor
    from skinfluencer.storage import _instagram_importer, platform_match_importer
    from skinfluencer.storage.social_products import instagram_post_from_metadata
    from skinfluencer.storage.sqlite_importer import (
        create_consistent_database_backup, make_rebuild_path,
        activate_rebuilt_database, remove_database_files,
    )

    since = _validate_iso_date(args.dateafter, '--dateafter')
    until = _validate_iso_date(args.datebefore, '--datebefore') or datetime.now().date().isoformat()
    if since > until:
        raise ValueError('--dateafter must be <= --datebefore')
    common = ['--influencer', args.influencer, '--data-root', str(DATA_ROOT),
              '--dateafter', since, '--datebefore', until]
    root = DATA_ROOT / args.influencer / 'instagram'
    eligible = []
    for path in sorted((root / 'raw').glob('*.info.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        try:
            post = instagram_post_from_metadata(data, args.influencer)
        except ValueError:
            # Invalid records outside this window are excluded, never imported.
            date = datetime.strptime(str(data.get('upload_date') or ''), '%Y%m%d').date().isoformat()
            if since <= date <= until:
                raise
            continue
        if since <= post['published_at'] <= until:
            eligible.append(post)
    if not eligible:
        raise ValueError('No valid Instagram posts in selected date window')
    if not args.skip_extraction:
        extract_args = common + (['--model', args.model] if args.model else [])
        if args.dry_run:
            extract_args.append('--dry-run')
        try:
            run_main(_instagram_product_extractor, extract_args)
        except SystemExit as exc:
            if exc.code != 1:
                raise
            # Extraction also scans historical invalid metadata. Only tolerate
            # failures after proving every requested post has a valid cache.
            if args.dry_run:
                raise
    if args.dry_run and not args.skip_extraction:
        return
    for post in eligible:
        path = root / 'description_products_llm' / (post['external_post_id'] + '.json')
        if not path.is_file():
            raise ValueError(f'Missing successful extraction; production unchanged: {path}')
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('extraction_status') != 'success' or any(
            payload.get(key) != value for key, value in post.items()
        ):
            raise ValueError(f'Missing/current successful extraction required: {path}')

    work = make_rebuild_path(args.database)
    if work.exists():
        raise ValueError(f'Temporary DB already exists: {work}')
    try:
        with tempfile.TemporaryDirectory(prefix='skinfluencer-instagram-') as tmp:
            staged = Path(tmp) / 'skinfluencer.sqlite'
            with sqlite3.connect(args.database) as source, sqlite3.connect(staged) as target:
                source.backup(target)
            run_main(_instagram_importer, ['--database', str(staged), *common])
            run_main(platform_match_importer, ['--database', str(staged), *common])
            if args.dry_run:
                print('DRY RUN: Instagram posts and relations verified; production unchanged.')
                return
            backup = create_consistent_database_backup(args.database)
            if backup is None:
                raise ValueError('Consistent backup could not be created')
            with sqlite3.connect(staged) as source, sqlite3.connect(work) as target:
                source.backup(target)
            activate_rebuilt_database(work, args.database)
            print('Instagram ingestion activated. Backup:', backup)
    finally:
        if work.exists():
            remove_database_files(work)


def cmd_enrich_links(args: argparse.Namespace) -> None:
    from skinfluencer.enrichment import purchase_links
    forwarded: list[str] = []
    if args.product_id is not None:
        forwarded += ["--product-id", str(args.product_id)]
    if getattr(args, "social_only", False):
        forwarded.append("--social-only")
    if args.limit is not None:
        forwarded += ["--limit", str(args.limit)]
    if args.force:
        forwarded.append("--force")
    if getattr(args, "retry_unresolved", False):
        forwarded.append("--retry-unresolved")
    if getattr(args, "dry_run", False):
        forwarded.append("--dry-run")
    if args.model:
        forwarded += ["--model", args.model]
    run_main(purchase_links, forwarded)


def cmd_enrich_images(args: argparse.Namespace) -> None:
    from skinfluencer.enrichment import product_images
    forwarded: list[str] = []
    if args.product_id is not None:
        forwarded += ["--product-id", str(args.product_id)]
    if args.limit is not None:
        forwarded += ["--limit", str(args.limit)]
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    run_main(product_images, forwarded)


def _run_matcher(influencer: str) -> None:
    script = ROOT / "scripts" / "match_instagram_tiktok.py"
    subprocess.run(
        [sys.executable, str(script), "--influencer", influencer],
        check=True,
    )


def _update_one(args: argparse.Namespace, influencer: str) -> None:
    since = _resolve_since(influencer, args.since)
    until = _validate_iso_date(args.until, "--until")
    display_name = INFLUENCERS[influencer]["display_name"]
    print("\n" + "=" * 88)
    print("SKINFLUENCER INCREMENTAL UPDATE")
    print("=" * 88)
    print(f"Influencer: {display_name} ({influencer})")
    print(f"Since:      {since}")
    if until:
        print(f"Until:      {until}")

    before_db = _db_counts()

    # 1) TikTok is the canonical short-form extraction source.
    from skinfluencer.sources.tiktok import collect as collect_tiktok
    tiktok_summary = collect_tiktok(
        influencer,
        dateafter=since,
        datebefore=until,
    )

    # 2) Product extraction only. Context/opinion is optional, never required.
    from skinfluencer.extraction import _tiktok_product_extractor
    product_args = ["--influencer", influencer, "--dateafter", since]
    if until:
        product_args += ["--datebefore", until]
    if args.model:
        product_args += ["--model", args.model]
    run_main(_tiktok_product_extractor, product_args)

    # 3) Import only this incremental date window into SQLite.
    from skinfluencer.storage import _tiktok_importer
    import_args = ["--influencer", influencer, "--dateafter", since]
    if until:
        import_args += ["--datebefore", until]
    run_main(_tiktok_importer, import_args)

    if args.with_context:
        cmd_extract_caption_context(
            argparse.Namespace(
                platform="tiktok",
                influencer=influencer,
                model=args.model,
                limit=None,
                video_id=None,
                force=False,
                dry_run=False,
            )
        )
        # Re-import to attach newly generated contexts.
        run_main(_tiktok_importer, import_args)

    # 4) Instagram is a first-class source. Collection is best-effort, but if
    # it fails we do not advance update state. Existing raw archive can still be
    # matched/extracted/imported for this same date window.
    instagram_error: Exception | None = None
    instagram_summary: dict = {}
    if not args.skip_instagram:
        try:
            from skinfluencer.sources.instagram import collect as collect_instagram
            instagram_summary = collect_instagram(
                influencer,
                dateafter=since,
                datebefore=until,
                cookies_file=args.instagram_cookies,
                browser=args.instagram_browser,
            )
        except Exception as exc:
            instagram_error = exc
            print(
                f"\nWARNING: Instagram collection failed: "
                f"{type(exc).__name__}: {exc}"
            )

        # Matcher is discovery/linking metadata only. It no longer writes DB.
        _run_matcher(influencer)

        # Strong exact/near matches reuse TikTok extraction after Instagram
        # caption validation. Others fall back to an Instagram LLM call.
        from skinfluencer.extraction import _instagram_product_extractor
        instagram_extract_args = [
            "--influencer",
            influencer,
            "--dateafter",
            since,
        ]
        if until:
            instagram_extract_args += ["--datebefore", until]
        if args.model:
            instagram_extract_args += ["--model", args.model]
        run_main(_instagram_product_extractor, instagram_extract_args)

        # Instagram rows/products must exist before cross-platform relation import.
        from skinfluencer.storage import _instagram_importer
        instagram_import_args = [
            "--influencer",
            influencer,
            "--dateafter",
            since,
        ]
        if until:
            instagram_import_args += ["--datebefore", until]
        run_main(_instagram_importer, instagram_import_args)

        from skinfluencer.storage import platform_match_importer
        run_main(platform_match_importer, ["--influencer", influencer])

    after_db = _db_counts()
    _print_db_delta(before_db, after_db)

    print("\nRun summary")
    print(f"  TikTok new metadata:       {tiktok_summary.get('new', 0)}")
    if not args.skip_instagram:
        print(f"  Instagram new metadata:    {instagram_summary.get('new', 0)}")
    print("  Caption context:           " + ("enabled" if args.with_context else "skipped"))
    print("  Purchase/image enrichment: skipped")

    if instagram_error is not None:
        raise SystemExit(
            "TikTok/product DB update tamamlandı fakat Instagram collector başarısız oldu. "
            "State ilerletilmedi; aynı tarih penceresi sonraki run'da yeniden denenecek."
        )

    _save_update_state(influencer, since)
    print(f"State: {_state_path(influencer)}")


def cmd_update(args: argparse.Namespace) -> None:
    for influencer in selected_influencers(args.influencer):
        _update_one(args, influencer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Skinfluencer unified data pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="Collect source metadata/transcripts")
    p.add_argument("--platform", choices=["youtube", "tiktok", "instagram", "all"], required=True)
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--shorts", action="store_true")
    p.add_argument("--dateafter")
    p.add_argument("--datebefore")
    p.add_argument("--instagram-cookies", type=Path)
    p.add_argument("--instagram-browser", default=None)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("extract-products", help="Extract products from descriptions/captions")
    p.add_argument("--platform", choices=["youtube", "tiktok", "instagram", "all"], required=True)
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--model")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dateafter")
    p.add_argument("--datebefore")
    p.set_defaults(func=cmd_extract_products)

    p = sub.add_parser("extract-caption-context", help="Optional TikTok recommendation/opinion context")
    p.add_argument("--platform", choices=["tiktok", "all"], default="tiktok")
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--model")
    p.add_argument("--limit", type=int)
    p.add_argument("--video-id")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_extract_caption_context)

    p = sub.add_parser("extract-opinions", help="Extract review opinions from YouTube transcripts")
    p.add_argument("--platform", choices=["youtube"], default="youtube")
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--model")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_extract_opinions)

    p = sub.add_parser("build-database", help="Build/update SQLite and import social products")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--dateafter")
    p.add_argument("--datebefore")
    p.set_defaults(func=cmd_build_database)

    p = sub.add_parser('ingest-instagram', help='Incremental cached extraction + verified Instagram/match import')
    p.add_argument('--influencer', required=True, choices=influencer_slugs())
    p.add_argument('--dateafter', required=True)
    p.add_argument('--datebefore')
    p.add_argument('--model')
    p.add_argument('--database', type=Path, default=DATABASE_PATH)
    p.add_argument('--skip-extraction', action='store_true', help='Resume from successful extraction files')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=cmd_ingest_instagram)

    p = sub.add_parser("enrich-links", help="Find missing verified purchase links")
    p.add_argument("--product-id", type=int)
    p.add_argument("--social-only", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--retry-unresolved", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model")
    p.set_defaults(func=cmd_enrich_links)

    p = sub.add_parser("enrich-images", help="Extract images and stock from verified product pages")
    p.add_argument("--product-id", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_enrich_images)

    p = sub.add_parser("update", help="Incremental TikTok + Instagram update")
    p.add_argument("--influencer", choices=influencer_slugs())
    p.add_argument("--since", help="YYYY-MM-DD. İlk run'da gerekli; sonra state'ten okunur.")
    p.add_argument("--until", help="Optional YYYY-MM-DD upper bound")
    p.add_argument("--model")
    p.add_argument("--with-context", action="store_true", help="Optional caption-context LLM step")
    p.add_argument("--skip-instagram", action="store_true")
    p.add_argument("--instagram-cookies", type=Path)
    p.add_argument(
        "--instagram-browser",
        default=None,
        help="gallery-dl browser cookie source, e.g. chrome/instagram.com",
    )
    p.set_defaults(func=cmd_update)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
