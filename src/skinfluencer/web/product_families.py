"""Read-only family mapping. Unmapped products retain their existing behavior."""
from __future__ import annotations
from collections import defaultdict


def family_ids(connection, product_id: int) -> list[int]:
    present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_family_members'").fetchone()
    if not present:
        return [product_id]
    row = connection.execute('SELECT family_id FROM product_family_members WHERE product_id=?', (product_id,)).fetchone()
    if row is None:
        return [product_id]
    return [int(r[0]) for r in connection.execute('SELECT product_id FROM product_family_members WHERE family_id=? ORDER BY product_id', (row[0],))]


def collapse_catalog(connection, catalog):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_family_members'").fetchone():
        return catalog
    lookup = {int(r[0]): (int(r[1]), int(r[2]), r[3], r[4]) for r in connection.execute('''
        SELECT m.product_id, m.family_id, f.representative_product_id, f.brand, f.product_name
        FROM product_family_members m JOIN product_families f ON f.id=m.family_id''')}
    grouped = {}
    for p in catalog:
        info = lookup.get(int(p['product_id']))
        key = ('family', info[0]) if info else ('product', int(p['product_id']))
        if key not in grouped:
            grouped[key] = dict(p)
            if info:
                grouped[key].update(product_id=info[1], brand=info[2], product_name=info[3])
        else:
            for count in ('review_count', 'social_post_count', 'comment_count'):
                if count in p:
                    grouped[key][count] = grouped[key].get(count, 0) + p[count]
            # Do not sum influencer_count: individual creators can repeat across members.
            if 'influencer_count' in p:
                grouped[key]['influencer_count'] = max(grouped[key].get('influencer_count', 0), p['influencer_count'])
    return list(grouped.values())
