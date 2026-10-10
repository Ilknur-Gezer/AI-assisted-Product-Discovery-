"""Apply the approved canonical CSV to a SQLite *working copy*.

Fast, deterministic reconciliation: keep the lowest row id for duplicate
mentions/social post appearances, one image and one purchase URL per product.
Delete products not included in the CSV. Preserves alias mappings for future
incremental imports and old canonical IDs for redirects.
"""
from __future__ import annotations
import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from .sqlite_importer import normalize_text, product_identity_key


def load_map(path: Path):
    rows = list(csv.DictReader(path.open(encoding='utf-8-sig', newline='')))
    if not rows: raise ValueError('Empty canonical CSV')
    assigned = {}
    destinations = {}
    for row in rows:
        target = int(row['canonical_sql_id'])
        if target in destinations: raise ValueError(f'Duplicate target {target}')
        origins = {int(x.strip()) for x in row['orjinal_sql_ids'].split(';') if x.strip()}
        if target not in origins: raise ValueError(f'Target {target} missing from originals')
        for src in origins:
            if src in assigned: raise ValueError(f'Original {src} in multiple groups')
            assigned[src] = target
        destinations[target] = row
    # If two targets have an identical final normalized name, the lower ID wins.
    seen = {}
    collapsed = {}
    for target, row in sorted(destinations.items()):
        key = normalize_text(row['marka']),normalize_text(row['urun_adi'])
        if key in seen: collapsed[target] = seen[key]
        else: seen[key] = target
    for src, target in list(assigned.items()):
        assigned[src] = collapsed.get(target, target)
    destinations = {k:v for k,v in destinations.items() if k not in collapsed}
    return assigned, destinations, collapsed


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def snapshot(conn):
    return {r[0]:conn.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def apply(conn, csv_path):
    assigned, destinations, collapsed = load_map(Path(csv_path))
    old_rows = {r['id']:dict(r) for r in conn.execute('SELECT * FROM products')}
    if not set(assigned).issubset(old_rows) or not set(destinations).issubset(old_rows):
        raise ValueError(f'CSV SQL ID missing; original={sorted(set(assigned)-old_rows)[:10]} target={sorted(set(destinations)-old_rows)[:10]}')
    if any(v not in destinations for v in assigned.values()):
        raise ValueError('Redirect target absent from final catalog')
    before = snapshot(conn)
    counters = Counter()
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS product_id_redirects (
           old_product_id INTEGER PRIMARY KEY,
           canonical_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS product_aliases (
           normalized_brand TEXT NOT NULL,
           normalized_product_name TEXT NOT NULL,
           canonical_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
           PRIMARY KEY(normalized_brand, normalized_product_name)
        )''')
        # Remember the historic high-water mark. SQLite INTEGER PRIMARY KEY
        # without AUTOINCREMENT could otherwise reuse a deleted legacy ID.
        conn.execute('''CREATE TABLE IF NOT EXISTS canonical_id_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            next_product_id INTEGER NOT NULL
        )''')
        next_id = max(old_rows) + 1
        conn.execute('INSERT INTO canonical_id_state(singleton,next_product_id) VALUES(1,?) '
                     'ON CONFLICT(singleton) DO UPDATE SET '
                     'next_product_id=MAX(next_product_id,excluded.next_product_id)', (next_id,))
        allowed = set(assigned)
        def remap(old): return assigned.get(int(old)) if old is not None else None
        # One review per video/product; the first mention (lowest id) wins.
        def remap_dedup(table, fields, *, one_per_product=False):
            entries = conn.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            seen = set()
            kept = []
            # Remove all collisions first, BEFORE changing any FK. SQLite UNIQUE
            # indexes would otherwise reject a row that still holds the old ID.
            for row in entries:
                old = row['product_id']
                if old is None: continue
                target = remap(old)
                if target is None:
                    conn.execute(f'DELETE FROM "{table}" WHERE id=?', (row['id'],))
                    counters[f'{table}_removed_non_csv'] += 1
                    continue
                key = (target,) if one_per_product else (target, *(row[x] for x in fields))
                if key in seen:
                    conn.execute(f'DELETE FROM "{table}" WHERE id=?', (row['id'],))
                    counters[f'{table}_deduped'] += 1
                    continue
                seen.add(key)
                kept.append((row['id'],old,target))
            for row_id, old, target in kept:
                if old != target:
                    conn.execute(f'UPDATE "{table}" SET product_id=? WHERE id=?', (target,row_id))
                    counters[f'{table}_moved'] += 1
        remap_dedup('product_mentions', ['video_id'])
        remap_dedup('social_post_products', ['social_post_id'])
        remap_dedup('purchase_links', [], one_per_product=True)
        remap_dedup('product_images', [], one_per_product=True)
        # Outbound click attribution is event data, never deduplicated.
        if table_exists(conn, 'outbound_clicks'):
            for row in conn.execute('SELECT id,product_id FROM outbound_clicks').fetchall():
                dest=remap(row['product_id'])
                if dest is None: conn.execute('DELETE FROM outbound_clicks WHERE id=?',(row['id'],))
                elif dest != row['product_id']:
                    conn.execute('UPDATE outbound_clicks SET product_id=? WHERE id=?',(dest,row['id']))
        # Family members can share the same destination after CSV merge.
        if table_exists(conn,'product_family_members'):
            seen=set()
            for row in conn.execute('SELECT * FROM product_family_members ORDER BY product_id').fetchall():
                dest=remap(row['product_id'])
                if dest is None or dest in seen:
                    conn.execute('DELETE FROM product_family_members WHERE product_id=?',(row['product_id'],))
                else:
                    seen.add(dest)
                    if dest != row['product_id']:
                        # An existing primary key can be ahead of us; if so keep it instead.
                        already=conn.execute('SELECT 1 FROM product_family_members WHERE product_id=?',(dest,)).fetchone()
                        if already:
                            conn.execute('DELETE FROM product_family_members WHERE product_id=?',(row['product_id'],))
                        else:
                            conn.execute('UPDATE product_family_members SET product_id=? WHERE product_id=?',(dest,row['product_id']))
            if table_exists(conn,'product_families'):
                for row in conn.execute('SELECT id,representative_product_id FROM product_families').fetchall():
                    dest=remap(row['representative_product_id'])
                    if dest is None: conn.execute('DELETE FROM product_families WHERE id=?',(row['id'],))
                    elif dest != row['representative_product_id']:
                        conn.execute('UPDATE product_families SET representative_product_id=? WHERE id=?',(dest,row['id']))
        # All source aliases are captured before deleting source products.
        aliases=[(normalize_text(v['brand']),normalize_text(v['product_name']),assigned[k])
                 for k,v in old_rows.items() if k in allowed]
        for src, dest in sorted(assigned.items()):
            if src != dest:
                conn.execute('INSERT OR REPLACE INTO product_id_redirects(old_product_id,canonical_product_id) VALUES (?,?)',(src,dest))
        for src in sorted(set(old_rows)-set(destinations)):
            conn.execute('DELETE FROM products WHERE id=?',(src,))
            counters['products_removed'] += 1
        # Normalize all final names only after all competing old product names are gone.
        # Sentinel names avoid intermediate UNIQUE constraint collisions.
        for dest in sorted(destinations):
            conn.execute('UPDATE products SET normalized_brand=?, normalized_product_name=? WHERE id=?',
                         (f'__migrating_{dest}',f'__migrating_{dest}',dest))
        for dest, row in destinations.items():
            brand,name = row['marka'].strip(), row['urun_adi'].strip()
            if not brand or not name: raise ValueError(f'Blank canonical name ID={dest}')
            conn.execute('''UPDATE products SET brand=?,product_name=?,normalized_brand=?,normalized_product_name=?,
                          search_text=?,identity_key=?,verification_status='approved',updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                         (brand,name,normalize_text(brand),normalize_text(name),normalize_text(brand+' '+name),
                          product_identity_key(brand,name),dest))
            aliases.append((normalize_text(brand),normalize_text(name),dest))
        # Exact official CSV names win over aliases from old names.
        for brand,name,dest in aliases:
            conn.execute('INSERT INTO product_aliases(normalized_brand,normalized_product_name,canonical_product_id) VALUES (?,?,?) '
                         'ON CONFLICT(normalized_brand,normalized_product_name) DO UPDATE SET canonical_product_id=excluded.canonical_product_id',
                         (brand,name,dest))
        for dest, row in destinations.items():
            conn.execute('INSERT INTO product_aliases(normalized_brand,normalized_product_name,canonical_product_id) VALUES (?,?,?) '
                         'ON CONFLICT(normalized_brand,normalized_product_name) DO UPDATE SET canonical_product_id=excluded.canonical_product_id',
                         (normalize_text(row['marka']),normalize_text(row['urun_adi']),dest))
        problems=conn.execute('PRAGMA foreign_key_check').fetchall()
        if problems: raise RuntimeError(f'Foreign key errors {problems[:5]}')
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM products').fetchone()[0] == len(destinations)
        after = snapshot(conn)
        conn.commit()
        return {'before':before, 'after':after, 'merged_product_ids':len(assigned)-len(destinations),
                'non_csv_ids':len(set(old_rows)-set(assigned)), 'collapsed_final_names':collapsed,
                'aliases':after['product_aliases'], 'redirects':after['product_id_redirects'],
                'operations':dict(counters)}
    except BaseException:
        conn.rollback()
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--csv',type=Path,required=True)
    p.add_argument('--report',type=Path)
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    with sqlite3.connect(a.database) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        if a.dry_run:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.sqlite') as tmp:
                with sqlite3.connect(tmp.name) as clone: conn.backup(clone)
                with sqlite3.connect(tmp.name) as working:
                    working.row_factory=sqlite3.Row
                    result=apply(working,a.csv)
        else: result=apply(conn,a.csv)
    output=json.dumps(result,ensure_ascii=False,indent=2)
    if a.report:
        a.report.parent.mkdir(parents=True,exist_ok=True)
        a.report.write_text(output,encoding='utf-8')
    print(output)

if __name__=='__main__': main()
