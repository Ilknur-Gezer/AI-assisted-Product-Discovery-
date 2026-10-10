from __future__ import annotations
import json, os, re, sqlite3, unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from ..config.settings import DATABASE_PATH, STATIC_DIR
from ..config.influencers import INFLUENCERS
from .i18n import category_tabs, normalize_language

DB_PATH = DATABASE_PATH
CATEGORY_LABELS = category_tabs("tr")
INFLUENCER_DISPLAY_NAMES = {slug: cfg["display_name"] for slug, cfg in INFLUENCERS.items()}

def connect_readonly() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"SQLite veritabanı bulunamadı: {DB_PATH}"
        )

    connection = sqlite3.connect(
        f"file:{DB_PATH.resolve()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def fetch_all(
    query: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    with connect_readonly() as connection:
        return connection.execute(query, params).fetchall()


def database_ready() -> bool:
    return DB_PATH.exists()


def influencer_display_name(slug: str, fallback: str) -> str:
    return INFLUENCER_DISPLAY_NAMES.get(slug, fallback)


def get_influencer_id(slug: str) -> int | None:
    """Return the stable database id for an influencer slug."""
    rows = fetch_all(
        "SELECT id FROM influencers WHERE slug = ? LIMIT 1",
        (slug,),
    )
    return int(rows[0]["id"]) if rows else None


def purchase_links_ready() -> bool:
    if not database_ready():
        return False
    rows = fetch_all(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'purchase_links'
        LIMIT 1
        """
    )
    return bool(rows)


def get_purchase_links(product_id: int) -> list[sqlite3.Row]:
    if not purchase_links_ready():
        return []

    return fetch_all(
        """
        SELECT
            merchant_name,
            merchant_slug,
            link_type,
            url,
            match_confidence,
            verified_at,
            stock_status,
            stock_checked_at
        FROM purchase_links
        WHERE product_id = ?
          AND verification_status = 'verified'
        ORDER BY
            CASE merchant_slug
                WHEN 'gratis' THEN 1
                WHEN 'watsons' THEN 2
                WHEN 'rossmann' THEN 3
                WHEN 'korendy' THEN 4
                ELSE 5
            END,
            match_confidence DESC,
            merchant_name COLLATE NOCASE
        """,
        (product_id,),
    )


def product_images_ready() -> bool:
    if not database_ready():
        return False
    rows = fetch_all(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'product_images'
        LIMIT 1
        """
    )
    return bool(rows)


def get_product_image(product_id: int) -> sqlite3.Row | None:
    """Return the selected verified image metadata for one product, if available."""
    if not product_images_ready():
        return None

    rows = fetch_all(
        """
        SELECT
            local_path,
            image_url,
            source_page_url,
            source_type,
            width,
            height
        FROM product_images
        WHERE product_id = ?
          AND verification_status = 'verified'
        LIMIT 1
        """,
        (product_id,),
    )
    return rows[0] if rows else None


def product_image_url(product_id: int) -> str | None:
    """Return the best browser image URL for a product.

    Remote-image-first policy:
    1. Use the verified original ``image_url`` from the brand/retailer CDN.
    2. Fall back to the optional local cache when a remote URL is unavailable.

    This keeps the repository small while preserving backwards compatibility
    with older rows that already contain files under ``STATIC_DIR``.
    """
    row = get_product_image(product_id)
    if row is None:
        return None

    remote_url = str(row["image_url"] or "").strip()
    if remote_url:
        from urllib.parse import urlsplit

        parsed = urlsplit(remote_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return remote_url

    # Backwards-compatible local-cache fallback.
    raw_path = str(row["local_path"] or "").strip().replace("\\", "/")
    if not raw_path:
        return None

    static_root = STATIC_DIR.resolve()
    stored_path = Path(raw_path).expanduser()
    relative_path: Path | None = None

    if stored_path.is_absolute():
        try:
            relative_path = stored_path.resolve().relative_to(static_root)
        except (OSError, ValueError):
            relative_path = None

    if relative_path is None:
        normalized = raw_path.lstrip("/")
        marker = "web/static/"
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
        elif normalized.startswith("static/"):
            normalized = normalized[len("static/"):]
        elif normalized.startswith("www/"):
            normalized = normalized[len("www/"):]
        relative_path = Path(normalized)

    candidate = (STATIC_DIR / relative_path).resolve()
    try:
        relative_path = candidate.relative_to(static_root)
    except ValueError:
        return None

    if not candidate.is_file():
        return None

    return "/" + relative_path.as_posix()


def influencer_choices() -> dict[str, str]:
    """Influencer names represented by an approved review or social post."""
    if not database_ready():
        return {"all": "Tüm influencer'lar"}

    rows = fetch_all(
        """
        SELECT i.slug AS influencer_slug, i.display_name AS influencer_name
        FROM influencers i
        WHERE EXISTS (
            SELECT 1
            FROM approved_product_comments apc
            WHERE apc.influencer_id = i.id
        ) OR EXISTS (
            SELECT 1
            FROM approved_social_product_links aspl
            WHERE aspl.influencer_slug = i.slug
        )
        ORDER BY i.display_name COLLATE NOCASE
        """
    )

    choices = {"all": "Tüm influencer'lar"}
    for row in rows:
        slug = str(row["influencer_slug"])
        choices[slug] = influencer_display_name(
            slug,
            str(row["influencer_name"]),
        )
    return choices


def get_product_catalog(
    influencer_slug: str,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if influencer_slug != "all":
        conditions.append("influencer_slug = ?")
        params.append(influencer_slug)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    rows = fetch_all(
        f"""
        SELECT
            product_id,
            brand,
            product_name,
            category,
            COUNT(*) AS comment_count
        FROM approved_product_comments
        {where}
        GROUP BY product_id, brand, product_name, category
        ORDER BY brand COLLATE NOCASE, product_name COLLATE NOCASE
        """,
        tuple(params),
    )

    return [
        {
            "product_id": int(row["product_id"]),
            "brand": str(row["brand"]),
            "product_name": str(row["product_name"]),
            "category": str(row["category"]),
            "comment_count": int(row["comment_count"]),
        }
        for row in rows
    ]


def get_product_comments(
    product_id: int,
    influencer_slug: str,
    lang: str = "tr",
) -> list[sqlite3.Row]:
    language = normalize_language(lang)
    conditions = ["apc.product_id = ?"]
    params: list[Any] = [language, product_id]

    if influencer_slug != "all":
        conditions.append("apc.influencer_slug = ?")
        params.append(influencer_slug)

    return fetch_all(
        f"""
        SELECT
            apc.*,
            COALESCE(pmt.display_summary, apc.display_summary)
                AS localized_display_summary,
            COALESCE(pmt.grounded_summary, apc.grounded_summary)
                AS localized_grounded_summary,
            COALESCE(pmt.opinion_points_json, apc.opinion_points_json)
                AS localized_opinion_points_json
        FROM approved_product_comments AS apc
        LEFT JOIN product_mention_translations AS pmt
            ON pmt.mention_id = apc.mention_id
           AND pmt.language = ?
        WHERE {" AND ".join(conditions)}
        ORDER BY
            CASE apc.content_type
                WHEN 'long' THEN 0
                WHEN 'shorts' THEN 1
                ELSE 2 END,
            CASE WHEN apc.upload_date IS NULL THEN 1 ELSE 0 END,
            apc.upload_date DESC,
            apc.influencer_name COLLATE NOCASE
        """,
        tuple(params),
    )


def get_social_posts(
    product_id: int,
    influencer_slug: str,
    lang: str = "tr",
) -> list[dict[str, Any]]:
    language = normalize_language(lang)
    conditions = ["asl.product_id = ?"]
    params: list[Any] = [language, product_id]

    if influencer_slug != "all":
        conditions.append("asl.influencer_slug = ?")
        params.append(influencer_slug)

    rows = fetch_all(
        f"""
        SELECT
            asl.*,
            COALESCE(sct.summary, asl.caption_summary)
                AS localized_caption_summary,
            COALESCE(sct.opinion_summary, asl.caption_opinion_summary)
                AS localized_caption_opinion_summary,
            COALESCE(sct.topics_json, asl.caption_topics_json)
                AS localized_caption_topics_json,
            COALESCE(sct.audience_contexts_json, asl.caption_audience_contexts_json)
                AS localized_caption_audience_contexts_json,
            spm.original_url AS instagram_url,
            spm.external_post_id AS instagram_external_post_id,
            spm.match_type AS instagram_match_type,
            spm.similarity AS instagram_similarity
        FROM approved_social_product_links AS asl
        LEFT JOIN social_product_context_translations AS sct
            ON sct.context_id = asl.caption_context_id
           AND sct.language = ?
        LEFT JOIN social_posts AS sp
            ON sp.platform = asl.platform
           AND sp.external_post_id = asl.external_post_id
        LEFT JOIN social_post_platform_matches AS spm
            ON spm.social_post_id = sp.id
           AND spm.platform = 'instagram'
        WHERE {" AND ".join(conditions)}
        ORDER BY
            asl.published_at DESC,
            asl.influencer_name COLLATE NOCASE,
            asl.external_post_id DESC
        """,
        tuple(params),
    )

    from .social_grouping import group_social_posts
    post_ids = [int(row['social_post_id']) for row in rows]
    if not post_ids:
        return []
    placeholders = ','.join('?' for _ in post_ids)
    # Near matches additionally require creator/product/date/similarity safeguards.
    pairs = fetch_all(
        f"""SELECT post_id_a, post_id_b, match_type, similarity FROM social_post_matches
            WHERE match_type IN ('exact', 'near')
              AND post_id_a IN ({placeholders})
              AND post_id_b IN ({placeholders})""",
        tuple(post_ids + post_ids),
    )
    return group_social_posts(rows, [(p['post_id_a'], p['post_id_b'], p['match_type'], p['similarity']) for p in pairs])


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

def normalize_text(value: str | None) -> str:
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
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    without_punctuation = re.sub(
        r"[^a-zA-Z0-9%+]+",
        " ",
        without_marks,
    )
    return " ".join(
        without_punctuation.casefold().split()
    )


def search_aliases(
    brand: str,
    product_name: str,
    category: str,
) -> str:
    """Add a small deterministic bilingual synonym layer."""
    normalized = normalize_text(f"{brand} {product_name}")
    aliases: list[str] = []

    if any(
        token in normalized
        for token in (
            "sun cream",
            "suncream",
            "sunscreen",
            "sun screen",
            "sun protection",
            "spf",
        )
    ):
        aliases.extend(
            [
                "gunes kremi",
                "gunes koruyucu",
                "gunes krem",
                "sun crema",
                "sun creme",
            ]
        )

    if any(
        token in normalized
        for token in (
            "moisturizer",
            "moisturising",
            "moisturizing",
            "hydrating",
        )
    ):
        aliases.extend(["nemlendirici", "nemlendirici krem"])

    if any(
        token in normalized
        for token in ("cleanser", "cleansing", "wash")
    ):
        aliases.extend(["temizleyici", "yuz temizleyici"])

    if "serum" in normalized:
        aliases.append("serum")

    if category == "makeup":
        aliases.extend(["makyaj urunu", "makyaj"])
    elif category == "skincare":
        aliases.extend(["cilt bakimi", "bakim"])
    elif category == "haircare":
        aliases.extend(["sac bakimi", "sac urunu"])
    elif category == "bodycare":
        aliases.extend(["vucut bakimi", "vucut urunu"])

    return " ".join(dict.fromkeys(aliases))


def fuzzy_token_coverage(
    query_tokens: list[str],
    candidate_tokens: list[str],
) -> float:
    if not query_tokens:
        return 0.0

    matched = 0.0

    for query_token in query_tokens:
        if query_token in candidate_tokens:
            matched += 1.0
            continue

        best_similarity = max(
            (
                SequenceMatcher(
                    None,
                    query_token,
                    candidate_token,
                ).ratio()
                for candidate_token in candidate_tokens
            ),
            default=0.0,
        )

        if best_similarity >= 0.86:
            matched += 1.0
        elif best_similarity >= 0.74:
            matched += 0.65

    return matched / len(query_tokens)


def calculate_match_score(
    query: str,
    product: dict[str, Any],
) -> float:
    normalized_query = normalize_text(query)
    if not normalized_query:
        return 0.0

    normalized_brand = normalize_text(product["brand"])
    normalized_name = normalize_text(product["product_name"])
    aliases = search_aliases(
        product["brand"],
        product["product_name"],
        product["category"],
    )
    normalized_candidate = normalize_text(
        f"{product['brand']} {product['product_name']} {aliases}"
    )

    if normalized_query == normalized_brand:
        return 100.0

    if normalized_query == normalized_name:
        return 100.0

    if normalized_query in normalized_candidate:
        return 97.0

    query_tokens = normalized_query.split()
    candidate_tokens = normalized_candidate.split()

    coverage = fuzzy_token_coverage(
        query_tokens,
        candidate_tokens,
    )
    sequence_score = SequenceMatcher(
        None,
        normalized_query,
        normalized_candidate,
    ).ratio()

    # Also compare against brand and product name separately. This helps short
    # brand-only searches and long product names.
    brand_score = SequenceMatcher(
        None,
        normalized_query,
        normalized_brand,
    ).ratio()
    name_score = SequenceMatcher(
        None,
        normalized_query,
        normalized_name,
    ).ratio()

    score = max(
        coverage * 92.0 + sequence_score * 8.0,
        brand_score * 96.0,
        name_score * 94.0,
    )

    # Reward a clearly matched brand even when the rest of the query is a
    # Turkish category phrase, e.g. "Dr Korea güneş kremi".
    if normalized_brand and normalized_brand in normalized_query:
        score = max(score, 88.0 + 12.0 * coverage)

    return min(score, 100.0)


def search_products(
    raw_query: str,
    influencer_slug: str,
    *,
    limit: int = 5,
    score_cutoff: float = 48.0,
) -> list[dict[str, Any]]:
    catalog = get_product_catalog(influencer_slug)
    query = raw_query.strip()

    if not query:
        return []

    # A selected dropdown item returns its numeric product ID.
    if query.isdigit():
        selected_id = int(query)
        exact = [
            {
                **product,
                "match_score": 100.0,
            }
            for product in catalog
            if product["product_id"] == selected_id
        ]
        if exact:
            return exact

    scored = [
        {
            **product,
            "match_score": calculate_match_score(
                query,
                product,
            ),
        }
        for product in catalog
    ]

    scored = [
        product
        for product in scored
        if product["match_score"] >= score_cutoff
    ]
    scored.sort(
        key=lambda item: (
            -item["match_score"],
            item["brand"].casefold(),
            item["product_name"].casefold(),
        )
    )
    return scored[:limit]

CATEGORY_TABS = category_tabs("tr")

def get_commerce_catalog(
    influencer_slug: str,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if influencer_slug != "all":
        conditions.append("influencer_slug = ?")
        params.append(influencer_slug)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    rows = fetch_all(
        f"""
        WITH available_content AS (
            SELECT
                product_id,
                brand,
                product_name,
                category,
                influencer_slug,
                'review' AS content_kind
            FROM approved_product_comments
            {where}

            UNION ALL

            SELECT
                product_id,
                brand,
                product_name,
                category,
                influencer_slug,
                'social' AS content_kind
            FROM approved_social_product_links
            {where}
        )
        SELECT
            product_id,
            brand,
            product_name,
            category,
            SUM(CASE WHEN content_kind = 'review' THEN 1 ELSE 0 END)
                AS review_count,
            SUM(CASE WHEN content_kind = 'social' THEN 1 ELSE 0 END)
                AS social_post_count,
            COUNT(DISTINCT influencer_slug) AS influencer_count
        FROM available_content
        GROUP BY product_id, brand, product_name, category
        ORDER BY
            influencer_count DESC,
            review_count DESC,
            social_post_count DESC,
            brand COLLATE NOCASE,
            product_name COLLATE NOCASE
        """,
        tuple(params + params),
    )

    return [
        {
            "product_id": int(row["product_id"]),
            "brand": str(row["brand"] or "Marka belirtilmemiş"),
            "product_name": str(row["product_name"]),
            "category": str(row["category"]),
            "review_count": int(row["review_count"]),
            "social_post_count": int(row["social_post_count"]),
            "influencer_count": int(row["influencer_count"]),
        }
        for row in rows
    ]


def get_product_by_id(product_id: int) -> dict[str, Any] | None:
    rows = fetch_all(
        """
        WITH available_content AS (
            SELECT
                product_id, brand, product_name, category,
                influencer_slug, 'review' AS content_kind
            FROM approved_product_comments
            WHERE product_id = ?

            UNION ALL

            SELECT
                product_id, brand, product_name, category,
                influencer_slug, 'social' AS content_kind
            FROM approved_social_product_links
            WHERE product_id = ?
        )
        SELECT
            product_id,
            brand,
            product_name,
            category,
            SUM(CASE WHEN content_kind = 'review' THEN 1 ELSE 0 END)
                AS review_count,
            SUM(CASE WHEN content_kind = 'social' THEN 1 ELSE 0 END)
                AS social_post_count,
            COUNT(DISTINCT influencer_slug) AS influencer_count
        FROM available_content
        GROUP BY product_id, brand, product_name, category
        """,
        (product_id, product_id),
    )

    if not rows:
        return None

    row = rows[0]
    return {
        "product_id": int(row["product_id"]),
        "brand": str(row["brand"] or "Marka belirtilmemiş"),
        "product_name": str(row["product_name"]),
        "category": str(row["category"]),
        "review_count": int(row["review_count"]),
        "social_post_count": int(row["social_post_count"]),
        "influencer_count": int(row["influencer_count"]),
    }


def product_matches_category(
    product: dict[str, Any],
    category_code: str,
) -> bool:
    if category_code == "all":
        return True

    if category_code == "sunscreen":
        searchable = normalize_text(
            f"{product['brand']} {product['product_name']} "
            f"{search_aliases(product['brand'], product['product_name'], product['category'])}"
        )
        tokens = (
            "gunes krem",
            "gunes koruyucu",
            "sun cream",
            "suncream",
            "sunscreen",
            "sun protection",
            "spf",
        )
        return any(token in searchable for token in tokens)

    return product["category"] == category_code


def browse_products(
    influencer_slug: str,
    category_code: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    products = [
        product
        for product in get_commerce_catalog(influencer_slug)
        if product_matches_category(product, category_code)
    ]
    return products[:limit]


def search_products_commerce(
    raw_query: str,
    influencer_slug: str,
    category_code: str,
    *,
    limit: int = 24,
    score_cutoff: float = 48.0,
) -> list[dict[str, Any]]:
    catalog = [
        product
        for product in get_commerce_catalog(influencer_slug)
        if product_matches_category(product, category_code)
    ]
    query = raw_query.strip()

    if not query:
        return []

    if query.isdigit():
        selected_id = int(query)
        exact = [
            {**product, "match_score": 100.0}
            for product in catalog
            if product["product_id"] == selected_id
        ]
        if exact:
            return exact

    scored = [
        {
            **product,
            "match_score": calculate_match_score(query, product),
        }
        for product in catalog
    ]
    scored = [
        product
        for product in scored
        if product["match_score"] >= score_cutoff
    ]
    scored.sort(
        key=lambda item: (
            -item["match_score"],
            -item["influencer_count"],
            -item["review_count"],
            -item["social_post_count"],
            item["brand"].casefold(),
            item["product_name"].casefold(),
        )
    )
    return scored[:limit]

# Explicit, non-destructive product families.
from .product_families import family_ids as _family_ids, collapse_catalog as _collapse_catalog

def get_family_product_ids(product_id: int) -> list[int]:
    with connect_readonly() as conn:
        return _family_ids(conn, product_id)

_original_commerce_catalog = get_commerce_catalog
_original_product_catalog = get_product_catalog
_original_get_product_comments = get_product_comments
_original_get_social_posts = get_social_posts
_original_get_purchase_links = get_purchase_links
_original_get_product_by_id = get_product_by_id

def get_commerce_catalog(influencer_slug: str):
    with connect_readonly() as conn:
        return _collapse_catalog(conn, _original_commerce_catalog(influencer_slug))

def get_product_catalog(influencer_slug: str):
    with connect_readonly() as conn:
        return _collapse_catalog(conn, _original_product_catalog(influencer_slug))

def get_product_by_id(product_id: int):
    ids = get_family_product_ids(product_id)
    items = [_original_get_product_by_id(i) for i in ids]
    items = [i for i in items if i]
    if not items:
        return None
    first = next((x for x in items if x['product_id'] == product_id), items[0]).copy()
    if len(ids) > 1:
        with connect_readonly() as conn:
            f = conn.execute('SELECT f.representative_product_id,f.brand,f.product_name FROM product_family_members m JOIN product_families f ON f.id=m.family_id WHERE m.product_id=?',(product_id,)).fetchone()
        first.update(product_id=int(f[0]),brand=f[1],product_name=f[2])
        first['review_count'] = sum(x['review_count'] for x in items)
        first['social_post_count'] = sum(x['social_post_count'] for x in items)
        first['influencer_count'] = max(x['influencer_count'] for x in items)
    return first

def get_product_comments(product_id: int, influencer_slug: str, lang: str='tr'):
    out = []
    seen = set()
    for pid in get_family_product_ids(product_id):
        for r in _original_get_product_comments(pid,influencer_slug,lang):
            if r['mention_id'] not in seen:
                seen.add(r['mention_id'])
                out.append(r)
    return sorted(out, key=lambda r: (0 if r['content_type']=='long' else 1 if r['content_type']=='shorts' else 2, -(int(str(r['upload_date'] or '0000').replace('-','')[:8]) if str(r['upload_date'] or '').replace('-','')[:8].isdigit() else 0)))

def get_social_posts(product_id: int, influencer_slug: str, lang: str='tr'):
    ids = get_family_product_ids(product_id)
    out=[]
    for pid in ids:
        out.extend(_original_get_social_posts(pid,influencer_slug,lang))
    if len(ids)==1:
        return out
    from .social_grouping import group_social_posts
    by_id={}
    for row in out:
        item=dict(row)
        item['product_id']=product_id
        by_id.setdefault(int(item['social_post_id']),item)
    posts=list(by_id)
    if not posts:
        return []
    qs=','.join('?' for _ in posts)
    pairs=fetch_all(f"SELECT post_id_a,post_id_b,match_type,similarity FROM social_post_matches WHERE match_type IN ('exact','near') AND post_id_a IN ({qs}) AND post_id_b IN ({qs})",tuple(posts+posts))
    return group_social_posts(list(by_id.values()),[(r['post_id_a'],r['post_id_b'],r['match_type'],r['similarity']) for r in pairs])

def get_purchase_links(product_id: int):
    out=[]
    seen=set()
    ids=get_family_product_ids(product_id)
    with connect_readonly() as conn:
        variants={int(r[0]):r[1] for r in conn.execute('SELECT product_id,variant_label FROM product_family_members WHERE product_id IN (%s)' % ','.join('?' for _ in ids),ids)} if len(ids)>1 else {}
    for pid in ids:
        for row in _original_get_purchase_links(pid):
            url=str(row['url']).strip()
            key=url.rstrip('/').casefold()
            if key not in seen:
                seen.add(key)
                value=dict(row)
                value['variant_label']=variants.get(pid)
                out.append(value)
    return out

# Never suppress an unmapped product card without merging its reviews.
# Global reconciled family membership is the only deduplication mechanism.
