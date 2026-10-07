# Skinfluencer

Skinfluencer collects beauty content, extracts canonical products and YouTube review opinions, enriches products with verified purchase links/images/stock, stores the result in SQLite, and serves a Shiny discovery UI.

## Refactored architecture

```text
pipeline.py                  # single CLI entry point
src/skinfluencer/
  sources/                   # YouTube / TikTok / Instagram boundaries
  extraction/                # product + opinion extraction
  enrichment/                # purchase links + product images/stock
  storage/                   # SQLite builder/importers + schema
  config/                    # paths + influencer configuration
  models/                    # shared content models
  web/
    app.py                   # Shiny orchestration only
    queries.py               # DB/search/query layer
    components/              # product/review/social/filter UI components
    static/                  # Shiny static assets and product images
scripts/legacy/              # pre-refactor scripts retained temporarily
```

The original top-level script names remain as compatibility wrappers. New work should use `pipeline.py`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Keep `OPENAI_API_KEY` and any model setting in `.env`; secrets are not committed.

## Unified pipeline

```bash
python pipeline.py collect --platform tiktok --influencer naturally_serein
python pipeline.py collect --platform all
python pipeline.py extract-products --platform all
python pipeline.py extract-caption-context --platform tiktok
python pipeline.py extract-opinions --platform youtube
python pipeline.py build-database
python pipeline.py enrich-links
python pipeline.py enrich-images
python pipeline.py update --since-last-run
```

`update --since-last-run` currently relies on the existing cache/idempotency behavior of collectors and extractors; it does not maintain a separate scheduler state table. Instagram has a module boundary but collection is intentionally disabled until the caption collector is implemented.

For an incremental Instagram run from existing metadata:

```bash
python pipeline.py ingest-instagram --influencer naturally_serein --dateafter 2026-09-10 --model gpt-4.1-mini
```

This resumes unchanged extraction caches, requires successful outputs for every valid
post in the window, and imports Instagram posts/products and cross-platform relations
on one disposable database before creating a consistent production backup and activating
the verified copy. It preserves the legacy alternate-link table and does not rebuild
YouTube or TikTok. Use `--skip-extraction --dry-run` to verify completed caches without
API calls or production changes; use `--skip-extraction` to resume only the import stages.

## Run the app

```bash
shiny run --reload app.py
```

The root `app.py` is a thin compatibility launcher for `src/skinfluencer/web/app.py`.

## Safety / database behavior

- Existing SQLite data is preserved by the migrated builder logic.
- TikTok social-product import remains separate internally and is invoked after the main DB build.
- `python pipeline.py build-database --dry-run` works on a temporary SQLite backup and leaves the production DB unchanged.
- Purchase-link verification and product-image/stock enrichment retain their existing logic.
- Existing `www/...` image paths remain compatible: static files now live under `src/skinfluencer/web/static/` and the query layer resolves legacy `www/` prefixes.

## Tests

```bash
python -m pytest -q
```

The original TikTok regression test is retained under `scripts/legacy/test_tiktok_pipeline.py` for reference while the new test suite is migrated incrementally.

## Caption context extraction (TikTok / future Instagram)

Social captions are now a second semantic source in addition to YouTube transcripts. The
product extractor still decides *which product is explicitly named*. A separate context stage
then asks only what the same caption says about those already-approved products.

```bash
# Preview how many cached TikTok product posts would need a context API call
python pipeline.py extract-caption-context --platform tiktok \
  --influencer yagmurvardar --model gpt-4.1-mini --dry-run

# Process one post first
python pipeline.py extract-caption-context --platform tiktok \
  --influencer yagmurvardar --video-id 7680159915989585172 \
  --model gpt-4.1-mini

# Then import the cached context into SQLite
python pipeline.py build-database
```

Context output is cached under
`data/<influencer>/tiktok/caption_context_llm/`. It never creates new products. It can store
recommendation/opinion/comparison/routine context, multiple audience or skin-type contexts,
a short Turkish caption-grounded summary, caption-supported evidence, and confidence. The model returns
compact semantic groups (for example one group for a skin-type recommendation section), which are
expanded deterministically per product to reduce output tokens. Low-confidence
or unsupported claims are not surfaced as recommendations. The Shiny UI explicitly labels these
summaries as caption-derived rather than transcript-derived.

`pipeline.py update` now runs this stage after social product extraction and before the database
build. Instagram uses the same intended abstraction, but its collector/extractor remains disabled
until Instagram ingestion is implemented.
