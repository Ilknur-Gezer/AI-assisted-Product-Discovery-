"""Human-approved CSV gate for new YouTube, TikTok and Instagram product identities.

Export: python -m skinfluencer.storage.pending_products export --database ... --csv pending.csv
Edit decision=existing with canonical_sql_id, or decision=new with approved name.
Apply:  python -m skinfluencer.storage.pending_products apply --database ... --csv pending.csv
No LLM/network dependencies; only an explicit, reviewed 'new' decision can INSERT.
"""
from __future__ import annotations
import argparse
import csv
import json
import sqlite3
from collections import defaultdict, Counter
from pathlib import Path
from .sqlite_importer import normalize_text, product_identity_key, find_product_row

FIELDS = ['extracted_brand','extracted_product_name','category','source_refs','occurrences',
          'decision','canonical_sql_id','approved_brand','approved_product_name']


def pending_rows(conn):
    grouped = {}
    for row in conn.execute('''SELECT id,extracted_brand,extracted_product_name,category
        FROM social_post_products WHERE status='review' AND status_reason='pending_canonical_approval' ORDER BY id'''):
        key = normalize_text(row['extracted_brand']),normalize_text(row['extracted_product_name'])
        group=grouped.setdefault(key, {'extracted_brand':row['extracted_brand'] or '',
            'extracted_product_name':row['extracted_product_name'],'category':row['category'], 'refs':[]})
        group['refs'].append(f's:{row["id"]}')
    for row in conn.execute('''SELECT id,canonical_brand,canonical_product_name,category
        FROM unresolved_mentions WHERE status='review' AND reason='pending_canonical_approval' ORDER BY id'''):
        key=normalize_text(row['canonical_brand']),normalize_text(row['canonical_product_name'])
        group=grouped.setdefault(key, {'extracted_brand':row['canonical_brand'] or '',
            'extracted_product_name':row['canonical_product_name'] or '',
            'category':row['category'] or 'other_beauty', 'refs':[]})
        group['refs'].append(f'y:{row["id"]}')
    result=[]
    for _,v in sorted(grouped.items()):
        canonical=find_product_row(conn,v['extracted_brand'],v['extracted_product_name'])
        result.append(dict(extracted_brand=v['extracted_brand'],extracted_product_name=v['extracted_product_name'],
            category=v['category'],source_refs=';'.join(v['refs']),occurrences=len(v['refs']),
            decision='existing' if canonical else '',canonical_sql_id=canonical['id'] if canonical else '',
            approved_brand=v['extracted_brand'],approved_product_name=v['extracted_product_name']))
    return result


def export(conn, path):
    result = pending_rows(conn)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result)
    return len(result)


def apply(conn, path):
    records=list(csv.DictReader(path.open(encoding='utf-8-sig',newline='')))
    used=set()
    stats=Counter()
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('SAVEPOINT pending_approval')
    try:
        for row in records:
            action=row['decision'].strip().lower()
            if not action: continue  # Not yet reviewed; keep it pending.
            if action not in {'existing','new'}:
                raise ValueError('decision must be blank, existing or new')
            brand=row['approved_brand'].strip()
            name=row['approved_product_name'].strip()
            if not brand or not name: raise ValueError('Approved brand and name required')
            refs=row['source_refs'].split(';')
            if not refs or any(not r for r in refs): raise ValueError('source_refs required')
            if any(r in used for r in refs): raise ValueError('Duplicate source ref')
            used.update(refs)
            if action=='existing':
                target=int(row['canonical_sql_id'])
                if not conn.execute("SELECT 1 FROM products WHERE id=? AND verification_status='approved'",(target,)).fetchone():
                    raise ValueError(f'Invalid approved canonical_sql_id {target}')
            else:
                if row['canonical_sql_id'].strip():
                    raise ValueError('New product must not provide canonical_sql_id')
                matched=find_product_row(conn,brand,name)
                if matched: raise ValueError(f'Product already exists: {brand} {name} -> {matched["id"]}')
                state=conn.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_id_state' ").fetchone()
                if state:
                    historic_next=conn.execute('SELECT next_product_id FROM canonical_id_state WHERE singleton=1').fetchone()[0]
                    target=max(historic_next,conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM products').fetchone()[0])
                    conn.execute('UPDATE canonical_id_state SET next_product_id=? WHERE singleton=1',(target+1,))
                else:
                    target=conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM products').fetchone()[0]
                conn.execute('''INSERT INTO products(id,brand,product_name,category,normalized_brand,
                    normalized_product_name,search_text,identity_key,verification_status)
                    VALUES (?,?,?,?,?,?,?,?,'approved')''',
                    (target,brand,name,row['category'] or 'other_beauty',normalize_text(brand),normalize_text(name),
                     normalize_text(brand+' '+name),product_identity_key(brand,name)))
                stats['new_products'] += 1
            for ref in refs:
                kind, num = ref.split(':',1)
                rec_id=int(num)
                if kind=='s':
                    rec=conn.execute('''SELECT * FROM social_post_products
                        WHERE id=? AND status='review' AND status_reason='pending_canonical_approval' ''',(rec_id,)).fetchone()
                    if rec is None: raise ValueError(f'Not pending social ref {ref}')
                    if (normalize_text(rec['extracted_brand']),normalize_text(rec['extracted_product_name'])) != \
                       (normalize_text(row['extracted_brand']),normalize_text(row['extracted_product_name'])):
                        raise ValueError(f'CSV candidate mismatch {ref}')
                    # One product per post: preserve the earliest existing approved relation.
                    existing=conn.execute('''SELECT id FROM social_post_products WHERE social_post_id=?
                        AND product_id=? AND id<>? ORDER BY id LIMIT 1''',
                        (rec['social_post_id'],target,rec_id)).fetchone()
                    if existing:
                        conn.execute('DELETE FROM social_post_products WHERE id=?',(rec_id,))
                        stats['duplicate_social']+=1
                    else:
                        conn.execute('''UPDATE social_post_products SET product_id=?,status='approved',
                            status_reason='approved_from_csv',match_method='canonical_csv' WHERE id=?''',
                            (target,rec_id))
                        stats['approved_social']+=1
                elif kind=='y':
                    rec=conn.execute('''SELECT * FROM unresolved_mentions WHERE id=? AND status='review'
                        AND reason='pending_canonical_approval' ''',(rec_id,)).fetchone()
                    if rec is None: raise ValueError(f'Not pending YouTube ref {ref}')
                    if (normalize_text(rec['canonical_brand']),normalize_text(rec['canonical_product_name'])) != \
                       (normalize_text(row['extracted_brand']),normalize_text(row['extracted_product_name'])):
                        raise ValueError(f'CSV candidate mismatch {ref}')
                    exists=conn.execute('SELECT 1 FROM product_mentions WHERE product_id=? AND video_id=?',
                                        (target,rec['video_id'])).fetchone()
                    if not exists:
                        raw=json.loads(rec['raw_payload_json'])
                        summary=(rec['display_summary'] or raw.get('display_summary') or raw.get('summary') or '').strip()
                        if not summary:
                            raise ValueError(f'YouTube approved mention lacks review text {ref}')
                        conn.execute('''INSERT INTO product_mentions(
                          product_id,video_id,influencer_id,candidate_id,mention_status,display_summary,
                          grounded_summary,sentiment,confidence,raw_product_mentions_json,evidence_texts_json,
                          opinion_points_json,quality_warnings_json,status,status_reason,source_file)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'approved','approved_from_csv',?)''',
                          (target,rec['video_id'],rec['influencer_id'],rec['candidate_id'],
                           rec['mention_status'] or 'reviewed',summary,raw.get('grounded_summary'),
                           raw.get('sentiment') or 'unclear',min(1,max(0,float(rec['confidence'] or 0))),
                           json.dumps(raw.get('raw_product_mentions') or [],ensure_ascii=False),
                           json.dumps(raw.get('evidence_texts') or [],ensure_ascii=False),
                           json.dumps(raw.get('opinion_points') or [],ensure_ascii=False),
                           json.dumps(raw.get('quality_warnings') or [],ensure_ascii=False),rec['source_file']))
                        stats['approved_youtube']+=1
                    conn.execute('DELETE FROM unresolved_mentions WHERE id=?',(rec_id,))
                else: raise ValueError(f'Bad source ref {ref}')
            # Accepted alias, including the extracted name (a previously unseen spelling).
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_aliases'").fetchone():
                for alias_brand,alias_name in ((brand,name),(row['extracted_brand'],row['extracted_product_name'])):
                    conn.execute('''INSERT INTO product_aliases
                       (normalized_brand,normalized_product_name,canonical_product_id) VALUES (?,?,?)
                       ON CONFLICT(normalized_brand,normalized_product_name) DO UPDATE SET
                       canonical_product_id=excluded.canonical_product_id''',
                       (normalize_text(alias_brand),normalize_text(alias_name),target))
            stats['approved_groups']+=1
        if conn.execute('PRAGMA foreign_key_check').fetchall(): raise RuntimeError('Foreign key failure')
        conn.execute('RELEASE SAVEPOINT pending_approval')
        return dict(stats)
    except BaseException:
        conn.execute('ROLLBACK TO SAVEPOINT pending_approval')
        conn.execute('RELEASE SAVEPOINT pending_approval')
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['export','apply'])
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--csv',type=Path,required=True)
    a=p.parse_args()
    with sqlite3.connect(a.database) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        result=export(conn,a.csv) if a.command=='export' else apply(conn,a.csv)
    print(json.dumps({'result':result,'csv':str(a.csv)},ensure_ascii=False))

if __name__=='__main__': main()
