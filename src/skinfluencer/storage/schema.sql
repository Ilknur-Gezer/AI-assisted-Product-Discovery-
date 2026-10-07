-- Skinfluencer schema v7
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS influencers (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    youtube_video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    upload_date TEXT,
    url TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (content_type IN ('long', 'shorts', 'unknown')),
    source_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (influencer_id, youtube_video_id)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    brand TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other_beauty',
    normalized_brand TEXT NOT NULL,
    normalized_product_name TEXT NOT NULL,
    search_text TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'approved'
        CHECK (verification_status IN ('approved', 'review', 'rejected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (normalized_brand, normalized_product_name)
);

CREATE TABLE IF NOT EXISTS product_mentions (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    candidate_id TEXT,
    mention_status TEXT NOT NULL,
    display_summary TEXT NOT NULL,
    grounded_summary TEXT,
    sentiment TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    raw_product_mentions_json TEXT NOT NULL DEFAULT '[]',
    evidence_texts_json TEXT NOT NULL DEFAULT '[]',
    opinion_points_json TEXT NOT NULL DEFAULT '[]',
    quality_warnings_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'approved'
        CHECK (status IN ('approved', 'review', 'rejected')),
    status_reason TEXT,
    source_prompt_version TEXT,
    source_summary_style_version TEXT,
    extracted_at TEXT,
    source_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, video_id)
);

CREATE TABLE IF NOT EXISTS unresolved_mentions (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    candidate_id TEXT,
    canonical_brand TEXT,
    canonical_product_name TEXT,
    category TEXT,
    mention_status TEXT,
    status TEXT NOT NULL CHECK (status IN ('review', 'rejected')),
    reason TEXT,
    confidence REAL,
    display_summary TEXT,
    raw_payload_json TEXT NOT NULL,
    source_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (
        video_id,
        candidate_id,
        canonical_brand,
        canonical_product_name,
        status
    )
);

CREATE TABLE IF NOT EXISTS purchase_links (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    merchant_name TEXT NOT NULL,
    merchant_slug TEXT NOT NULL,
    link_type TEXT NOT NULL
        CHECK (link_type IN ('retailer', 'official_brand')),
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'review'
        CHECK (verification_status IN ('verified', 'review', 'rejected')),
    match_confidence REAL
        CHECK (
            match_confidence IS NULL
            OR (match_confidence >= 0 AND match_confidence <= 1)
        ),
    source_title TEXT,
    verified_at TEXT,
    checked_at TEXT,
    stock_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (stock_status IN ('in_stock', 'out_of_stock', 'unknown')),
    stock_checked_at TEXT,
    source_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, merchant_slug, url)
);

CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE
        REFERENCES products(id) ON DELETE CASCADE,
    image_url TEXT NOT NULL,
    source_page_url TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('og_image', 'json_ld', 'twitter_image', 'manual')),
    local_path TEXT NOT NULL,
    mime_type TEXT,
    width INTEGER CHECK (width IS NULL OR width > 0),
    height INTEGER CHECK (height IS NULL OR height > 0),
    file_sha256 TEXT,
    verification_status TEXT NOT NULL DEFAULT 'verified'
        CHECK (verification_status IN ('verified', 'review', 'rejected')),
    checked_at TEXT,
    fetched_at TEXT,
    source_file TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    database_path TEXT NOT NULL,
    source_subdir TEXT NOT NULL,
    influencer_slugs_json TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    videos_imported INTEGER NOT NULL DEFAULT 0,
    approved_mentions_imported INTEGER NOT NULL DEFAULT 0,
    unresolved_mentions_imported INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_videos_influencer
    ON videos(influencer_id);

CREATE INDEX IF NOT EXISTS idx_products_category
    ON products(category);

CREATE INDEX IF NOT EXISTS idx_products_search_text
    ON products(search_text);

CREATE INDEX IF NOT EXISTS idx_mentions_influencer
    ON product_mentions(influencer_id);

CREATE INDEX IF NOT EXISTS idx_mentions_product
    ON product_mentions(product_id);

CREATE INDEX IF NOT EXISTS idx_mentions_status
    ON product_mentions(status);

CREATE INDEX IF NOT EXISTS idx_unresolved_status
    ON unresolved_mentions(status);

CREATE INDEX IF NOT EXISTS idx_purchase_links_product
    ON purchase_links(product_id);

CREATE INDEX IF NOT EXISTS idx_purchase_links_status
    ON purchase_links(verification_status);

CREATE INDEX IF NOT EXISTS idx_purchase_links_merchant
    ON purchase_links(merchant_slug);

CREATE INDEX IF NOT EXISTS idx_product_images_status
    ON product_images(verification_status);

CREATE INDEX IF NOT EXISTS idx_product_images_source_domain
    ON product_images(source_domain);

DROP VIEW IF EXISTS approved_product_comments;

CREATE VIEW approved_product_comments AS
SELECT
    pm.id AS mention_id,
    p.id AS product_id,
    p.brand,
    p.product_name,
    p.category,
    p.search_text,
    i.id AS influencer_id,
    i.slug AS influencer_slug,
    i.display_name AS influencer_name,
    v.id AS video_id,
    v.youtube_video_id,
    v.title AS video_title,
    v.upload_date,
    v.url AS video_url,
    v.content_type,
    pm.display_summary,
    pm.grounded_summary,
    pm.sentiment,
    pm.confidence,
    pm.raw_product_mentions_json,
    pm.evidence_texts_json,
    pm.opinion_points_json,
    pm.quality_warnings_json,
    pm.status_reason,
    pm.extracted_at
FROM product_mentions pm
JOIN products p ON p.id = pm.product_id
JOIN videos v ON v.id = pm.video_id
JOIN influencers i ON i.id = pm.influencer_id
WHERE pm.status = 'approved';

DROP VIEW IF EXISTS verified_purchase_links;

CREATE VIEW verified_purchase_links AS
SELECT
    pl.id AS purchase_link_id,
    pl.product_id,
    p.brand,
    p.product_name,
    pl.merchant_name,
    pl.merchant_slug,
    pl.link_type,
    pl.url,
    pl.domain,
    pl.match_confidence,
    pl.source_title,
    pl.verified_at,
    pl.checked_at,
    pl.stock_status,
    pl.stock_checked_at
FROM purchase_links pl
JOIN products p ON p.id = pl.product_id
WHERE pl.verification_status = 'verified';


DROP VIEW IF EXISTS verified_product_images;

CREATE VIEW verified_product_images AS
SELECT
    pi.id AS product_image_id,
    pi.product_id,
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
    pi.checked_at,
    pi.fetched_at
FROM product_images pi
JOIN products p ON p.id = pi.product_id
WHERE pi.verification_status = 'verified';


CREATE TABLE IF NOT EXISTS social_posts (
 id INTEGER PRIMARY KEY,
 influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
 platform TEXT NOT NULL CHECK(platform IN ('tiktok','instagram')),
 external_post_id TEXT NOT NULL,
 caption TEXT NOT NULL,
 original_url TEXT NOT NULL,
 published_at TEXT NOT NULL,
 caption_sha256 TEXT NOT NULL,
 prompt_version TEXT,
 model TEXT,
 extracted_at TEXT,
 source_file TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(platform,external_post_id)
);
CREATE TABLE IF NOT EXISTS social_post_products (
 id INTEGER PRIMARY KEY,
 social_post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
 product_id INTEGER REFERENCES products(id) ON DELETE RESTRICT,
 candidate_key TEXT NOT NULL,
 extracted_brand TEXT,
 extracted_product_name TEXT NOT NULL,
 category TEXT NOT NULL,
 evidence_text TEXT NOT NULL,
 confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
 status TEXT NOT NULL CHECK(status IN ('approved','review','rejected')),
 status_reason TEXT,
 match_method TEXT NOT NULL,
 raw_payload_json TEXT NOT NULL,
 CHECK(status<>'approved' OR product_id IS NOT NULL),
 UNIQUE(social_post_id,candidate_key)
);
CREATE TABLE IF NOT EXISTS social_product_contexts (
 id INTEGER PRIMARY KEY,
 social_post_product_id INTEGER NOT NULL UNIQUE REFERENCES social_post_products(id) ON DELETE CASCADE,
 relationship TEXT NOT NULL CHECK(relationship IN (
   'recommendation','positive_opinion','negative_opinion','comparison',
   'routine','mention_only','mixed'
 )),
 topics_json TEXT NOT NULL DEFAULT '[]',
 audience_contexts_json TEXT NOT NULL DEFAULT '[]',
 summary TEXT,
 opinion_summary TEXT,
 evidence_texts_json TEXT NOT NULL DEFAULT '[]',
 confidence REAL NOT NULL CHECK(confidence>=0 AND confidence<=1),
 status TEXT NOT NULL CHECK(status IN ('approved','review','rejected')),
 status_reason TEXT,
 prompt_version TEXT NOT NULL,
 model TEXT NOT NULL,
 extracted_at TEXT,
 source_file TEXT,
 raw_payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_social_posts_influencer ON social_posts(influencer_id);
CREATE INDEX IF NOT EXISTS idx_social_products_product ON social_post_products(product_id);
CREATE INDEX IF NOT EXISTS idx_social_products_status ON social_post_products(status);


-- Cached presentation-layer translations. Original Turkish/source text remains the source of truth.
CREATE TABLE IF NOT EXISTS product_mention_translations (
    mention_id INTEGER NOT NULL
        REFERENCES product_mentions(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language IN ('en')),
    display_summary TEXT,
    grounded_summary TEXT,
    opinion_points_json TEXT NOT NULL DEFAULT '[]',
    source_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    translated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (mention_id, language)
);

CREATE TABLE IF NOT EXISTS social_product_context_translations (
    context_id INTEGER NOT NULL
        REFERENCES social_product_contexts(id) ON DELETE CASCADE,
    language TEXT NOT NULL CHECK(language IN ('en')),
    summary TEXT,
    opinion_summary TEXT,
    topics_json TEXT NOT NULL DEFAULT '[]',
    audience_contexts_json TEXT NOT NULL DEFAULT '[]',
    source_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    translated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (context_id, language)
);

CREATE INDEX IF NOT EXISTS idx_product_mention_translations_language
    ON product_mention_translations(language);
CREATE INDEX IF NOT EXISTS idx_social_context_translations_language
    ON social_product_context_translations(language);

CREATE INDEX IF NOT EXISTS idx_social_context_relationship ON social_product_contexts(relationship);
CREATE INDEX IF NOT EXISTS idx_social_context_status ON social_product_contexts(status);
DROP VIEW IF EXISTS approved_social_product_links;
CREATE VIEW approved_social_product_links AS
SELECT spp.product_id, p.brand, p.product_name, p.category,
 i.slug AS influencer_slug, i.display_name AS influencer_name,
 sp.id AS social_post_id, sp.platform, sp.external_post_id, sp.caption,
 sp.original_url, sp.published_at, spp.evidence_text, spp.confidence,
 spc.id AS caption_context_id,
 spc.relationship AS caption_relationship,
 spc.topics_json AS caption_topics_json,
 spc.audience_contexts_json AS caption_audience_contexts_json,
 spc.summary AS caption_summary,
 spc.opinion_summary AS caption_opinion_summary,
 spc.evidence_texts_json AS caption_context_evidence_json,
 spc.confidence AS caption_context_confidence,
 spc.status AS caption_context_status
FROM social_post_products spp
JOIN social_posts sp ON sp.id=spp.social_post_id
JOIN influencers i ON i.id=sp.influencer_id
JOIN products p ON p.id=spp.product_id
LEFT JOIN social_product_contexts spc ON spc.social_post_product_id=spp.id
WHERE spp.status='approved';


-- First-class cross-platform relationship. Both posts already exist in social_posts.
CREATE TABLE IF NOT EXISTS social_post_matches (
    id INTEGER PRIMARY KEY,

    post_id_a INTEGER NOT NULL
        REFERENCES social_posts(id) ON DELETE CASCADE,

    post_id_b INTEGER NOT NULL
        REFERENCES social_posts(id) ON DELETE CASCADE,

    match_type TEXT NOT NULL
        CHECK(match_type IN ('exact', 'near')),

    similarity REAL
        CHECK(similarity IS NULL OR (similarity >= 0 AND similarity <= 1)),

    date_distance_days INTEGER
        CHECK(date_distance_days IS NULL OR date_distance_days >= 0),

    source_file TEXT,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK(post_id_a < post_id_b),
    UNIQUE(post_id_a, post_id_b)
);

CREATE INDEX IF NOT EXISTS idx_social_post_matches_a
    ON social_post_matches(post_id_a);

CREATE INDEX IF NOT EXISTS idx_social_post_matches_b
    ON social_post_matches(post_id_b);

-- LEGACY: keep only during migration from alternate-link architecture.
CREATE TABLE IF NOT EXISTS social_post_platform_matches (
    id INTEGER PRIMARY KEY,

    social_post_id INTEGER NOT NULL
        REFERENCES social_posts(id) ON DELETE CASCADE,

    platform TEXT NOT NULL
        CHECK(platform IN ('instagram')),

    external_post_id TEXT NOT NULL,
    original_url TEXT NOT NULL,

    match_type TEXT NOT NULL
        CHECK(match_type IN ('exact', 'near')),

    similarity REAL
        CHECK(similarity IS NULL OR (similarity >= 0 AND similarity <= 1)),

    date_distance_days INTEGER
        CHECK(date_distance_days IS NULL OR date_distance_days >= 0),

    source_file TEXT,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(platform, external_post_id)
);

CREATE INDEX IF NOT EXISTS idx_social_platform_matches_post
    ON social_post_platform_matches(social_post_id);

CREATE INDEX IF NOT EXISTS idx_social_platform_matches_platform
    ON social_post_platform_matches(platform);


-- First-party outbound purchase attribution.
CREATE TABLE IF NOT EXISTS outbound_clicks (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL
        REFERENCES products(id) ON DELETE CASCADE,
    influencer_id INTEGER
        REFERENCES influencers(id) ON DELETE SET NULL,
    influencer_slug TEXT NOT NULL,
    merchant_slug TEXT NOT NULL,
    destination_url TEXT NOT NULL,
    clicked_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_product
    ON outbound_clicks(product_id);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_influencer
    ON outbound_clicks(influencer_id);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_merchant
    ON outbound_clicks(merchant_slug);
CREATE INDEX IF NOT EXISTS idx_outbound_clicks_clicked_at
    ON outbound_clicks(clicked_at);

PRAGMA user_version = 9;
