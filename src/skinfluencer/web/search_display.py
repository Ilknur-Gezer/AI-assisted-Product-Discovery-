"""Conservative, presentation-only filtering of duplicate catalog entries.

This intentionally never edits product rows, changes IDs, or invents family relationships.
"""
import re
import unicodedata

BRAND_ALIASES = {
    'maybelline new york': 'maybelline', 'kiehls since 1851': 'kiehls',
    'kiehls': 'kiehls', 'l oreal paris': 'loreal', 'loreal paris': 'loreal',
}


def _plain(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(c for c in value if not unicodedata.combining(c))
    value = value.replace('ı', 'i').replace('œ', 'oe').replace('æ', 'ae')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', value)).strip()


def signature(item):
    brand = _plain(item.get('brand'))
    brand = BRAND_ALIASES.get(brand, brand)
    name = _plain(item.get('product_name'))
    # Only harmless orthographic normalization. Don't drop SPF/shade/size/strength numbers.
    tokens = name.split()
    tokens = ['foundation' if t in {'fondoten', 'fondotenim'} else t for t in tokens]
    name = ' '.join(tokens)
    if not brand or not name:
        return None
    # Exact normalized tokens only. Not fuzzy matching or substring collapsing.
    return brand, name, _plain(item.get('category'))


def collapse_display(catalog):
    """Retain highest-information representative for provably equivalent names.

    Unrecognized naming variants remain visible rather than silently masking reviews.
    """
    buckets = {}
    order = []
    for item in catalog:
        key = signature(item) or ('id', int(item['product_id']))
        if key not in buckets:
            buckets[key] = item
            order.append(key)
        else:
            current = buckets[key]
            rank = lambda p: (int(p.get('influencer_count', 0)), int(p.get('review_count', p.get('comment_count', 0))), int(p.get('social_post_count', 0)))
            if rank(item) > rank(current):
                buckets[key] = item
    return [buckets[k] for k in order]
