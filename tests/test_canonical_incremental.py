"""Regression tests for a frozen canonical catalog and CSV approval gate."""
import csv
import json
import sqlite3
import hashlib
from pathlib import Path

from skinfluencer.storage.sqlite_importer import normalize_text, product_identity_key, import_video_payload
from skinfluencer.storage.social_products import import_payload, import_context_payload, context_candidates, context_candidates_sha256
from skinfluencer.storage.pending_products import export, apply

SCHEMA = Path(__file__).resolve().parents[1] / 'src/skinfluencer/storage/schema.sql'


def db():
    c=sqlite3.connect(':memory:')
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA.read_text(encoding='utf-8'))
    return c


def seed(c, name='Vitamin C Serum'):
    c.execute('''INSERT INTO products(brand,product_name,category,normalized_brand,
        normalized_product_name,search_text,identity_key) VALUES (?,?,?,?,?,?,?)''',
        ('Brand',name,'skincare',normalize_text('Brand'),normalize_text(name),
         normalize_text('Brand '+name),product_identity_key('Brand',name)))
    return c.execute('SELECT last_insert_rowid()').fetchone()[0]


def social_payload(name, post_id):
    caption=f'Brand {name} güzel.'
    return dict(extraction_status='success',platform='tiktok',influencer_slug='yagmurvardar',
        external_post_id=str(post_id),caption=caption,
        original_url=f'https://www.tiktok.com/@yagmurvardar_/video/{post_id}',
        published_at='2026-10-10',caption_sha256=hashlib.sha256(caption.encode()).hexdigest(),
        products=[dict(brand='Brand',product_name=name,category='skincare',
            evidence_text=f'Brand {name}',confidence=0.95,status='approved')])


def test_unknown_social_is_review_pending_not_a_new_product(tmp_path):
    c=db()
    pid=seed(c)
    import_payload(c,social_payload('Vitamin C Serum',900000001),'sample.json')
    import_payload(c,social_payload('Unknown Serum',900000002),'sample.json')
    assert c.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1
    attached=c.execute('SELECT product_id,status FROM social_post_products ORDER BY id').fetchall()
    assert tuple(attached[0])==(pid,'approved')
    assert tuple(attached[1])==(None,'review')
    path=tmp_path/'pending.csv'
    assert export(c,path)==1
    with path.open(encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
    rows[0]['decision']='new'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    assert apply(c,path)['new_products']==1
    assert c.execute('SELECT COUNT(*) FROM products').fetchone()[0]==2
    assert c.execute("SELECT COUNT(*) FROM social_post_products WHERE status='approved'").fetchone()[0]==2
    assert export(c,path)==0
    assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_existing_approved_csv_mapping_promotes_without_insert(tmp_path):
    c=db()
    canonical=seed(c,'Vitamin C Serum')
    import_payload(c,social_payload('Vitamin C Serum Limited',900000003),'sample.json')
    path=tmp_path/'pending.csv'
    assert export(c,path)==1
    rows=list(csv.DictReader(path.open(encoding='utf-8-sig')))
    rows[0]['decision']='existing'
    rows[0]['canonical_sql_id']=str(canonical)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    result=apply(c,path)
    assert result.get('new_products', 0) == 0
    assert c.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1
    assert c.execute('SELECT product_id FROM social_post_products').fetchone()[0]==canonical
    assert c.execute('SELECT canonical_product_id FROM product_aliases WHERE normalized_product_name=?',
        (normalize_text('Vitamin C Serum Limited'),)).fetchone()[0]==canonical


def test_unknown_youtube_queued_and_approved(tmp_path):
    c=db()
    payload={'extraction_status':'success','video_id':'A1B2C3D4','channel':'Yağmur Vardar',
        'title':'Review video','url':'https://www.youtube.com/watch?v=A1B2C3D4',
        'product_mentions':[{'status':'approved','canonical_brand':'Brand',
            'canonical_product_name':'Molecular Repair Mask','category':'haircare',
            'display_summary':'Repairs damaged hair.','sentiment':'positive',
            'confidence':0.9,'candidate_id':'c1'}]}
    count=import_video_payload(c,influencer_slug='yagmurvardar',
        source_path=Path('sample-video.json'),payload=payload)
    assert count==(0,1)
    assert c.execute('SELECT COUNT(*) FROM products').fetchone()[0]==0
    path=tmp_path/'pending.csv'
    assert export(c,path)==1
    rows=list(csv.DictReader(path.open(encoding='utf-8-sig')))
    rows[0]['decision']='new'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
    result=apply(c,path)
    assert result['approved_youtube']==1
    assert c.execute('SELECT COUNT(*) FROM product_mentions').fetchone()[0]==1
    assert c.execute('SELECT COUNT(*) FROM unresolved_mentions').fetchone()[0]==0
    assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
