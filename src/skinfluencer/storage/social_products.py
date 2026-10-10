"""Shared TikTok/Instagram validation and SQL storage. No network or OpenAI dependency."""
from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import re
import unicodedata

ACCOUNTS = {'naturally_serein': 'cisemcakir', 'yagmurvardar': 'yagmurvardar_'}
NAMES = {'naturally_serein': 'Çisem Çakır', 'yagmurvardar': 'Yağmur Vardar'}
CATEGORIES = {'skincare', 'makeup', 'haircare', 'bodycare', 'fragrance', 'other_beauty'}
SOCIAL_SCHEMA = '''
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

CREATE TABLE IF NOT EXISTS social_post_matches (
 id INTEGER PRIMARY KEY,
 post_id_a INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
 post_id_b INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
 match_type TEXT NOT NULL CHECK(match_type IN ('exact','near')),
 similarity REAL CHECK(similarity IS NULL OR (similarity>=0 AND similarity<=1)),
 date_distance_days INTEGER CHECK(date_distance_days IS NULL OR date_distance_days>=0),
 source_file TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK(post_id_a < post_id_b),
 UNIQUE(post_id_a,post_id_b)
);
CREATE INDEX IF NOT EXISTS idx_social_post_matches_a ON social_post_matches(post_id_a);
CREATE INDEX IF NOT EXISTS idx_social_post_matches_b ON social_post_matches(post_id_b);
CREATE INDEX IF NOT EXISTS idx_social_posts_influencer ON social_posts(influencer_id);
CREATE INDEX IF NOT EXISTS idx_social_products_product ON social_post_products(product_id);
CREATE INDEX IF NOT EXISTS idx_social_products_status ON social_post_products(status);
CREATE INDEX IF NOT EXISTS idx_social_context_relationship ON social_product_contexts(relationship);
CREATE INDEX IF NOT EXISTS idx_social_context_status ON social_product_contexts(status);
DROP VIEW IF EXISTS approved_social_product_links;
CREATE VIEW approved_social_product_links AS
SELECT spp.product_id, p.brand, p.product_name, p.category,
 i.slug AS influencer_slug, i.display_name AS influencer_name,
 sp.id AS social_post_id, sp.platform, sp.external_post_id, sp.caption,
 sp.original_url, sp.published_at, spp.evidence_text, spp.confidence,
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
'''

def _normalize_evidence_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip().casefold()

def normalize(text):
    # Reuse the package-level SQLite identity normalizer.
    # This keeps YouTube and social product identities identical after the refactor.
    from .sqlite_importer import normalize_text
    return normalize_text(text)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def social_candidate_key(brand, product_name):
    """Stable identity for one extracted social product candidate."""
    return digest(json.dumps([normalize(brand or ''), normalize(product_name or '')]))


def context_candidates(extraction_payload):
    """Return approved product candidates eligible for caption-context extraction."""
    products = extraction_payload.get('products') or []
    result = []
    for item in products:
        if not isinstance(item, dict) or item.get('status') != 'approved':
            continue
        brand = str(item.get('brand') or '').strip() or None
        name = str(item.get('product_name') or '').strip()
        if not name:
            continue
        result.append({
            'candidate_key': social_candidate_key(brand, name),
            'brand': brand,
            'product_name': name,
            'category': item.get('category'),
            'product_evidence_text': str(item.get('evidence_text') or ''),
        })
    return result


def context_candidates_sha256(candidates):
    stable = [
        {
            'candidate_key': item['candidate_key'],
            'brand': item.get('brand'),
            'product_name': item.get('product_name'),
            'category': item.get('category'),
            'product_evidence_text': item.get('product_evidence_text'),
        }
        for item in candidates
    ]
    return digest(json.dumps(stable, ensure_ascii=False, sort_keys=True))


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def post_from_metadata(data, slug):
    """Validate canonical TikTok metadata and return a social-post record."""
    if not isinstance(data, dict):
        raise ValueError('Metadata bir JSON nesnesi olmalı.')
    if slug not in ACCOUNTS:
        raise ValueError(f'Bilinmeyen influencer: {slug}')
    pid = str(data.get('id') or '')
    if not pid.isdigit():
        raise ValueError('Geçerli TikTok id bulunamadı.')
    url = str(data.get('webpage_url') or data.get('original_url') or '')
    parts = urlsplit(url)
    match = re.fullmatch(r'/@([^/]+)/(video|photo)/(\d+)/?', parts.path)
    if (parts.scheme != 'https' or parts.hostname not in {'www.tiktok.com','tiktok.com'}
        or not match or match[1].casefold() != ACCOUNTS[slug] or match[3] != pid):
        raise ValueError('URL, hesap veya gönderi kimliği uyuşmuyor.')
    date = str(data.get('upload_date') or '')
    published = datetime.strptime(date, '%Y%m%d').date().isoformat()
    caption = data.get('description')
    if caption is None:
        caption = ''
    if not isinstance(caption, str):
        raise ValueError('description metin olmalı.')
    return dict(platform='tiktok', influencer_slug=slug, external_post_id=pid,
                caption=caption, original_url=f'https://www.tiktok.com{parts.path.rstrip("/")}',
                published_at=published, caption_sha256=digest(caption))



def instagram_post_from_metadata(data, slug):
    """Validate downloaded Instagram metadata and return a social-post record."""
    if not isinstance(data, dict):
        raise ValueError('Instagram metadata bir JSON nesnesi olmalı.')
    if slug not in NAMES:
        raise ValueError(f'Bilinmeyen influencer: {slug}')

    from ..config.influencers import INFLUENCERS

    expected_handle = INFLUENCERS[slug]['instagram_handle'].casefold()
    identities = []
    kind = None
    # Historical yt-dlp URLs include /<account>/reel/<shortcode>/.
    # Check both URL fields rather than hiding a contradictory original_url.
    for key in ('webpage_url', 'original_url'):
        url = str(data.get(key) or '').strip()
        if not url:
            continue
        parts = urlsplit(url)
        match = re.fullmatch(
            r'/(?:(?P<account>[A-Za-z0-9_.]+)/)?'
            r'(?P<kind>reel|p)/(?P<shortcode>[A-Za-z0-9_-]+)/?',
            parts.path,
        )
        if (parts.scheme != 'https'
            or parts.hostname not in {'www.instagram.com', 'instagram.com'}
            or parts.username is not None or parts.password is not None
            or not match):
            raise ValueError('Instagram URL veya gönderi kimliği uyuşmuyor.')
        account = match['account']
        if account and account.casefold() != expected_handle:
            raise ValueError(f'Yanlış Instagram hesabı: @{account}; beklenen @{expected_handle}.')
        identities.append(match['shortcode'])
        kind = kind or match['kind']

    if not identities:
        raise ValueError('Geçerli Instagram URL bulunamadı.')
    pid = identities[0]
    for key in ('id', 'display_id'):
        value = str(data.get(key) or '').strip()
        if value:
            identities.append(value)
    if any(value != pid for value in identities):
        raise ValueError('Instagram URL veya gönderi kimliği uyuşmuyor.')

    # yt-dlp channel is the username; uploader is a display name and
    # uploader_id is a numeric account ID, neither is a reliable handle.
    account = str(data.get('channel') or '').strip().lstrip('@')
    if account and account.casefold() != expected_handle:
        raise ValueError(f'Yanlış Instagram hesabı: @{account}; beklenen @{expected_handle}.')

    upload_date = str(data.get('upload_date') or '').strip()
    if upload_date:
        try:
            published = datetime.strptime(upload_date, '%Y%m%d').date().isoformat()
        except ValueError as exc:
            raise ValueError('Instagram upload_date YYYYMMDD olmalı.') from exc
    else:
        timestamp = data.get('timestamp')
        if timestamp is None:
            raise ValueError('Instagram upload_date/timestamp eksik.')
        try:
            published = datetime.fromtimestamp(
                float(timestamp), tz=timezone.utc
            ).date().isoformat()
        except (TypeError, ValueError, OSError) as exc:
            raise ValueError('Geçersiz Instagram timestamp.') from exc

    caption = data.get('description')
    if caption is None:
        caption = ''
    if not isinstance(caption, str):
        raise ValueError('Instagram description metin olmalı.')

    return dict(
        platform='instagram',
        influencer_slug=slug,
        external_post_id=pid,
        caption=caption,
        original_url=f'https://www.instagram.com/{kind}/{pid}',
        published_at=published,
        caption_sha256=digest(caption),
    )

def instagram_post_from_payload(payload, slug):
    """Validate an Instagram extraction payload without assuming TikTok is canonical."""
    if not isinstance(payload, dict):
        raise ValueError('Instagram payload bir JSON nesnesi olmalı.')
    pid = str(payload.get('external_post_id') or '').strip()
    if not pid:
        raise ValueError('Geçerli Instagram id bulunamadı.')

    url = str(payload.get('original_url') or '').strip()
    parts = urlsplit(url)
    match = re.fullmatch(r'/(reel|p)/([^/]+)/?', parts.path)
    if (parts.scheme != 'https'
        or parts.hostname not in {'www.instagram.com', 'instagram.com'}
        or not match
        or match[2] != pid):
        raise ValueError('Instagram URL veya gönderi kimliği uyuşmuyor.')

    published = str(payload.get('published_at') or '').strip()
    try:
        published = datetime.strptime(published, '%Y-%m-%d').date().isoformat()
    except ValueError as exc:
        raise ValueError('Instagram published_at YYYY-MM-DD olmalı.') from exc

    caption = payload.get('caption')
    if caption is None:
        caption = ''
    if not isinstance(caption, str):
        raise ValueError('caption metin olmalı.')

    return dict(
        platform='instagram',
        influencer_slug=slug,
        external_post_id=pid,
        caption=caption,
        original_url=f'https://www.instagram.com/{match[1]}/{pid}',
        published_at=published,
        caption_sha256=digest(caption),
    )


def social_post_from_payload(payload):
    """Validate shared extraction metadata for either supported social platform."""
    if not isinstance(payload, dict):
        raise ValueError('Extraction payload bir JSON nesnesi olmalı.')
    if payload.get('extraction_status') != 'success':
        raise ValueError('Extraction durumu success değil.')

    platform = str(payload.get('platform') or '').strip().lower()
    slug = str(payload.get('influencer_slug') or '').strip()
    if platform not in {'tiktok', 'instagram'}:
        raise ValueError(f'Desteklenmeyen social platform: {platform}')
    if slug not in NAMES:
        raise ValueError(f'Bilinmeyen influencer: {slug}')

    if platform == 'tiktok':
        post = post_from_metadata(
            dict(
                id=payload.get('external_post_id'),
                webpage_url=payload.get('original_url'),
                upload_date=str(payload.get('published_at') or '').replace('-', ''),
                description=payload.get('caption'),
            ),
            slug,
        )
    else:
        post = instagram_post_from_payload(payload, slug)

    if post['caption_sha256'] != str(payload.get('caption_sha256') or ''):
        raise ValueError('Caption hash uyuşmuyor.')
    return post

def validate_products(items, caption):
    """Revalidate every imported product.

    Brand may be supported by the full caption context, while the product name
    must still be supported by the item's evidence_text.
    """
    if not isinstance(items, list):
        raise ValueError('products liste olmalı.')

    unique = {}

    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Ürün nesne olmalı.')

        brand = str(item.get('brand') or '').strip()
        name = str(item.get('product_name') or '').strip()
        evidence = str(item.get('evidence_text') or '')
        confidence = float(item.get('confidence', 0))

        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('confidence 0-1 aralığında olmalı.')

        if not name:
            continue

        ev_tokens = re.findall(r'\w+', normalize(evidence))
        caption_tokens = re.findall(r'\w+', normalize(caption))

        def supported(value, source_tokens):
            tokens = re.findall(r'\w+', normalize(value))

            return bool(tokens) and all(
                any(
                    t == source_token
                    or (
                        len(t) >= 4
                        and source_token.startswith(t)
                    )
                    for source_token in source_tokens
                )
                for t in tokens
            )

        reason = None

        # Evidence itself must still be an exact substring of the caption.
        if not evidence or evidence not in caption:
            reason = 'evidence_not_exact_substring'

        # Brand does not have to be repeated inside every evidence snippet.
        # It is enough for the brand to be explicitly supported somewhere
        # in the full caption.
        elif not brand:
            reason = 'brand_missing'

        elif not supported(brand, caption_tokens):
            reason = 'brand_not_supported_by_caption'

        # Product name remains strict: it should be supported by the specific
        # evidence snippet associated with that product.
        elif not supported(name, ev_tokens):
            reason = 'name_not_supported_by_evidence'

        elif re.search(
            r'\b(bu urun|bu versiyon|klasik versiyon|diger renkliler)\b',
            normalize(name)
        ):
            reason = 'contextual_product_name'

        elif confidence < .80:
            reason = 'confidence_below_0_80'

        elif item.get('status') in {'review', 'rejected'}:
            reason = str(
                item.get('status_reason')
                or 'source_requires_review'
            )

        category = item.get('category')

        if category not in CATEGORIES:
            category = 'other_beauty'
            reason = reason or 'invalid_category'

        result = dict(
            brand=brand or None,
            product_name=name,
            category=category,
            evidence_text=evidence,
            confidence=confidence,
            status='review' if reason else 'approved',
            status_reason=reason or 'exact_evidence_supports_product'
        )

        key = (
            normalize(brand),
            normalize(name)
        )

        # Duplicate abbreviations are handled by the prompt;
        # do not fuzzy-merge variants.
        previous = unique.get(key)

        if (
            previous is None
            or (result['status'] == 'approved', confidence)
            > (previous['status'] == 'approved', previous['confidence'])
        ):
            unique[key] = result

    return list(unique.values())


def product_id_for(connection, item):
    """Return canonical match only; stage unknown items instead of creating IDs."""
    from .sqlite_importer import find_product_row
    row = find_product_row(connection, item['brand'], item['product_name'])
    if row:
        return int(row['id']), 'canonical_match'
    return None, 'pending_canonical_approval'


def import_payload(connection, payload, source_file):
    """Import a validated TikTok or Instagram product-extraction payload."""
    post = social_post_from_payload(payload)
    platform = post['platform']
    slug = post['influencer_slug']
    products = validate_products(payload.get('products'), post['caption'])

    connection.execute(
        'INSERT INTO influencers(slug,display_name) VALUES (?,?) '
        'ON CONFLICT(slug) DO NOTHING',
        (slug, NAMES[slug]),
    )
    influencer = connection.execute(
        'SELECT id FROM influencers WHERE slug=?', (slug,)
    ).fetchone()[0]

    existing = connection.execute(
        'SELECT id,influencer_id FROM social_posts '
        'WHERE platform=? AND external_post_id=?',
        (platform, post['external_post_id']),
    ).fetchone()
    if existing and existing['influencer_id'] != influencer:
        raise ValueError('Gönderi başka influencer ile kayıtlı.')

    fields = [
        'influencer_id','platform','external_post_id','caption','original_url',
        'published_at','caption_sha256','prompt_version','model','extracted_at','source_file'
    ]
    record = dict(
        post,
        influencer_id=influencer,
        prompt_version=payload.get('prompt_version'),
        model=payload.get('model'),
        extracted_at=payload.get('extracted_at'),
        source_file=str(source_file),
    )
    connection.execute(
        'INSERT INTO social_posts ('+','.join(fields)+') VALUES ('
        +','.join('?' for _ in fields)+') '
        'ON CONFLICT(platform,external_post_id) DO UPDATE SET '
        +','.join(
            f'{field}=excluded.{field}'
            for field in fields
            if field not in {'platform','external_post_id'}
        )
        +',updated_at=CURRENT_TIMESTAMP',
        [record[field] for field in fields],
    )

    post_id = connection.execute(
        'SELECT id FROM social_posts WHERE platform=? AND external_post_id=?',
        (platform, post['external_post_id']),
    ).fetchone()[0]

    # The extraction payload is authoritative for this post.
    connection.execute(
        'DELETE FROM social_post_products WHERE social_post_id=?',
        (post_id,),
    )

    counts = {'approved':0,'review':0,'canonical_match':0,'pending_canonical_approval':0}
    for item in products:
        pid, method = None, 'review'
        if item['status'] == 'approved':
            pid, method = product_id_for(connection, item)
            if pid is None:
                item['status'], item['status_reason'] = 'review', method
        counts[item['status']] += 1
        if method in {'canonical_match', 'pending_canonical_approval'}:
            counts[method] += 1
        key = social_candidate_key(item['brand'], item['product_name'])
        connection.execute(
            '''INSERT INTO social_post_products
              (social_post_id,product_id,candidate_key,extracted_brand,extracted_product_name,category,
               evidence_text,confidence,status,status_reason,match_method,raw_payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                post_id,pid,key,item['brand'],item['product_name'],item['category'],
                item['evidence_text'],item['confidence'],item['status'],
                item['status_reason'],method,json.dumps(item,ensure_ascii=False)
            ),
        )
    return counts

def import_context_payload(connection, payload, product_payload, source_file):
    """Attach cached caption context to an imported TikTok or Instagram post."""
    platform = str(payload.get('platform') or '').strip().lower()
    if payload.get('extraction_status') != 'success' or platform not in {'tiktok', 'instagram'}:
        raise ValueError('Başarılı social caption-context çıktısı gerekli.')
    if platform != str(product_payload.get('platform') or '').strip().lower():
        raise ValueError('Context platform kimliği uyuşmuyor.')
    if payload.get('influencer_slug') != product_payload.get('influencer_slug'):
        raise ValueError('Context influencer kimliği uyuşmuyor.')
    if payload.get('external_post_id') != product_payload.get('external_post_id'):
        raise ValueError('Context post kimliği uyuşmuyor.')
    if payload.get('caption_sha256') != product_payload.get('caption_sha256'):
        raise ValueError('Context caption hash uyuşmuyor.')

    candidates = context_candidates(product_payload)
    expected_hash = context_candidates_sha256(candidates)
    if payload.get('product_candidates_sha256') != expected_hash:
        raise ValueError('Context product candidate hash uyuşmuyor; context yeniden çıkarılmalı.')

    post = connection.execute(
        'SELECT id, caption FROM social_posts WHERE platform=? AND external_post_id=?',
        (platform, payload['external_post_id']),
    ).fetchone()
    if not post:
        raise ValueError('Context için social post DB kaydı bulunamadı.')

    connection.execute(
        '''DELETE FROM social_product_contexts
           WHERE social_post_product_id IN (
             SELECT id FROM social_post_products WHERE social_post_id=?
           )''',
        (post['id'],),
    )

    allowed = {'recommendation','positive_opinion','negative_opinion','comparison',
               'routine','mention_only','mixed'}
    contexts = payload.get('contexts')
    if not isinstance(contexts, list):
        raise ValueError('contexts liste olmalı.')

    count = 0
    for item in contexts:
        if not isinstance(item, dict):
            raise ValueError('Context item nesne olmalı.')
        key = str(item.get('candidate_key') or '')
        row = connection.execute(
            'SELECT id FROM social_post_products WHERE social_post_id=? AND candidate_key=?',
            (post['id'], key),
        ).fetchone()
        if not row:
            raise ValueError(f'Context product eşleşmesi bulunamadı: {key}')

        relationship = str(item.get('relationship') or 'mention_only')
        if relationship not in allowed:
            raise ValueError(f'Geçersiz context relationship: {relationship}')
        confidence = float(item.get('confidence', 0))
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('Context confidence 0-1 aralığında olmalı.')
        status = str(item.get('status') or 'review')
        if status not in {'approved','review','rejected'}:
            raise ValueError('Geçersiz context status.')
        
        evidence = item.get('evidence_texts') or []
        if not isinstance(evidence, list):
            raise ValueError('Context evidence_texts liste olmalı.')

        normalized_caption = _normalize_evidence_text(post['caption'])

        for snippet in evidence:
            normalized_snippet = _normalize_evidence_text(snippet)

            if not normalized_snippet:
                continue

            if normalized_snippet not in normalized_caption:
                raise ValueError(
                    'Context evidence caption içinde normalized exact substring değil.'
                )
        
    
        connection.execute(
            '''INSERT INTO social_product_contexts
               (social_post_product_id,relationship,topics_json,audience_contexts_json,summary,
                opinion_summary,evidence_texts_json,confidence,status,status_reason,prompt_version,
                model,extracted_at,source_file,raw_payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (row['id'], relationship, json.dumps(item.get('topics') or [], ensure_ascii=False),
             json.dumps(item.get('audience_contexts') or [], ensure_ascii=False),
             item.get('summary'), item.get('opinion_summary'),
             json.dumps(evidence, ensure_ascii=False), confidence, status,
             item.get('status_reason'), payload.get('prompt_version'), payload.get('model'),
             payload.get('extracted_at'), str(source_file), json.dumps(item, ensure_ascii=False)),
        )
        count += 1
    return count

def snapshot_social(database):
    """Identity-based snapshot survives reset changing integer IDs."""
    empty = {
        'influencers': [], 'products': [], 'posts': [], 'links': [],
        'contexts': [], 'matches': [], 'legacy_platform_matches': [],
    }
    if not Path(database).exists():
        return empty

    c = sqlite3.connect(f'{Path(database).resolve().as_uri()}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if 'social_posts' not in tables:
            return empty
        return {
            'influencers': [dict(r) for r in c.execute(
                'SELECT * FROM influencers WHERE id IN '
                '(SELECT influencer_id FROM social_posts)'
            )],
            'products': [dict(r) for r in c.execute(
                'SELECT * FROM products WHERE id IN '
                '(SELECT product_id FROM social_post_products WHERE product_id IS NOT NULL)'
            )],
            'posts': [dict(r) for r in c.execute('SELECT * FROM social_posts')],
            'links': [dict(r) for r in c.execute('SELECT * FROM social_post_products')],
            'contexts': [dict(r) for r in c.execute('SELECT * FROM social_product_contexts')]
                if 'social_product_contexts' in tables else [],
            'matches': [dict(r) for r in c.execute('SELECT * FROM social_post_matches')]
                if 'social_post_matches' in tables else [],
            # Temporary compatibility until the old alternate-link table is retired.
            'legacy_platform_matches': [
                dict(r) for r in c.execute('SELECT * FROM social_post_platform_matches')
            ] if 'social_post_platform_matches' in tables else [],
        }
    finally:
        c.close()


def restore_social(c, snapshot):
    def insert(table, row):
        fields = list(row)
        return c.execute(
            'INSERT INTO '+table+' ('+','.join(fields)+') VALUES ('
            +','.join('?' for _ in fields)+')',
            list(row.values()),
        ).lastrowid

    maps = {'influencers': {}, 'products': {}, 'posts': {}, 'links': {}}

    for table, identity in [
        ('influencers', ('slug',)),
        ('products', ('normalized_brand','normalized_product_name')),
    ]:
        for old in snapshot[table]:
            row = dict(old)
            old_id = row.pop('id')
            match = c.execute(
                'SELECT id FROM '+table+' WHERE '
                +' AND '.join(f'{key}=?' for key in identity),
                [row[key] for key in identity],
            ).fetchone()
            maps[table][old_id] = match[0] if match else insert(table, row)

    for old in snapshot['posts']:
        row = dict(old)
        old_id = row.pop('id')
        row['influencer_id'] = maps['influencers'][row['influencer_id']]
        maps['posts'][old_id] = insert('social_posts', row)

    for old in snapshot['links']:
        row = dict(old)
        old_id = row.pop('id')
        row['social_post_id'] = maps['posts'][row['social_post_id']]
        if row['product_id'] is not None:
            row['product_id'] = maps['products'][row['product_id']]
        maps['links'][old_id] = insert('social_post_products', row)

    for old in snapshot.get('contexts', []):
        row = dict(old)
        row.pop('id')
        row['social_post_product_id'] = maps['links'][row['social_post_product_id']]
        insert('social_product_contexts', row)

    for old in snapshot.get('matches', []):
        row = dict(old)
        row.pop('id')
        post_a = maps['posts'][row['post_id_a']]
        post_b = maps['posts'][row['post_id_b']]
        row['post_id_a'], row['post_id_b'] = sorted((post_a, post_b))
        insert('social_post_matches', row)

    # Preserve the old alternate-link rows only during the migration window.
    for old in snapshot.get('legacy_platform_matches', []):
        row = dict(old)
        row.pop('id')
        row['social_post_id'] = maps['posts'][row['social_post_id']]
        insert('social_post_platform_matches', row)

    assert c.execute('SELECT COUNT(*) FROM social_posts').fetchone()[0] == len(snapshot['posts'])
    assert c.execute('SELECT COUNT(*) FROM social_post_products').fetchone()[0] == len(snapshot['links'])
    assert c.execute('SELECT COUNT(*) FROM social_product_contexts').fetchone()[0] == len(snapshot.get('contexts', []))
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_post_matches'").fetchone():
        assert c.execute('SELECT COUNT(*) FROM social_post_matches').fetchone()[0] == len(snapshot.get('matches', []))
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_post_platform_matches'").fetchone():
        assert c.execute('SELECT COUNT(*) FROM social_post_platform_matches').fetchone()[0] == len(snapshot.get('legacy_platform_matches', []))
