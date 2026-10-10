"""Non-destructive global catalog reconciliation. No network or writes during scan.

A family is a product identity, not a license to merge incompatible formulas or
SPF strengths. Shade/size variants require human approval unless normalized names
are identical. All original foreign keys remain untouched.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

ALIASES = {'l oreal':'loreal','l oreal paris':'loreal','loreal paris':'loreal',
           'maybelline new york':'maybelline','kiehl s':'kiehls',
           'the ordinary':'ordinary','la roche posay':'larocheposay'}
FILLER = {'the', 'a', 'an'}
PROTECTED = {'spf','pa','retinol','niacinamide','aha','bha','vitamin','serum','cream',
             'foundation','concealer','powder','sunscreen','toner','shampoo','mask',
             'matte','glow','oil','gel','lotion','primer','cleanser','lipstick'}

def plain(s):
    s = str(s or '').casefold().replace('ı','i').replace('œ','oe').replace('æ','ae')
    s = ''.join(c for c in unicodedata.normalize('NFKD',s) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+',s))

def brand(s):
    p=plain(s)
    return ALIASES.get(p,p)

def features(name):
    tokens=plain(name).split()
    tokens=['foundation' if x=='fondoten' else x for x in tokens]
    return [x for x in tokens if x not in FILLER]

def fingerprint(name):
    return ' '.join(features(name))

def protected(name):
    words=features(name)
    # Shade numbers and SPF must not disappear under fuzzy matching.
    numeric=set(x for x in words if x.isdigit() or any(c.isdigit() for c in x))
    return numeric, set(words)&PROTECTED

def similarity(a, b):
    """Order-independent similarity, with an explicit difflib fallback."""
    from difflib import SequenceMatcher
    ta, tb = ' '.join(sorted(features(a))), ' '.join(sorted(features(b)))
    try:
        from rapidfuzz.fuzz import ratio, token_sort_ratio
        return round(max(ratio(fingerprint(a), fingerprint(b)), token_sort_ratio(fingerprint(a), fingerprint(b))), 1)
    except ImportError:
        return round(100 * max(SequenceMatcher(None, fingerprint(a), fingerprint(b)).ratio(),
                               SequenceMatcher(None, ta, tb).ratio()), 1)


def _categories_conflict(a, b):
    ca, cb = str(a.get('category') or '').strip().lower(), str(b.get('category') or '').strip().lower()
    return bool(ca and cb and ca != cb and 'other' not in (ca, cb))


def classify(a,b):
    if brand(a['brand']) != brand(b['brand']) or not brand(a['brand']):
        return 'different', 0, 'brand_mismatch'
    na, nb = fingerprint(a['product_name']), fingerprint(b['product_name'])
    if not na or not nb:
        return 'different', 0, 'missing_name'
    score = similarity(a['product_name'], b['product_name'])
    # Only exact same token *multiset* can be auto-approved. Reordering is benign,
    # dropping SPF, changing shade or changing formula are not.
    identical = sorted(features(a['product_name'])) == sorted(features(b['product_name']))
    if _categories_conflict(a,b):
        return 'review', score, 'category_conflict'
    if identical:
        return 'safe', 100, 'exact_normalized_tokens'
    nums_a, types_a = protected(a['product_name'])
    nums_b, types_b = protected(b['product_name'])
    if nums_a != nums_b:
        return 'review', score, 'variant_or_strength_conflict'
    if types_a != types_b:
        return 'review', score, 'formula_or_type_conflict'
    if score >= 67:
        return 'review', score, 'fuzzy_candidate'
    return 'different', score, 'low_similarity'

def load(conn):
    conn.row_factory=sqlite3.Row
    return [dict(x) for x in conn.execute('SELECT id,brand,product_name,category FROM products ORDER BY id')]

def scan(conn, min_score=67):
    products=load(conn)
    groups=defaultdict(list)
    for p in products: groups[brand(p['brand'])].append(p)
    existing={int(r[0]):int(r[1]) for r in conn.execute('SELECT product_id,family_id FROM product_family_members')}
    proposals=[]
    for key,members in groups.items():
        if not key: continue
        for i,a in enumerate(members):
            for b in members[i+1:]:
                # Two unrelated products sharing a generic term must not reach the
                # review queue just because their category/SPF/formula differs.
                wa, wb = set(features(a['product_name'])), set(features(b['product_name']))
                common = wa & wb
                if not common:
                    continue
                status,score,reason = classify(a,b)
                if status == 'different':
                    continue
                # A score is always computed BEFORE safety gates; block weak pairs
                # unless all tokens are equal (including permutations).
                if status != 'safe' and score < min_score:
                    continue
                fa,fb=existing.get(a['id']),existing.get(b['id'])
                if fa and fb and fa!=fb:
                    status,reason='review','existing_family_conflict'
                proposals.append({'id_a':a['id'],'id_b':b['id'],'brand_a':a['brand'],
                    'name_a':a['product_name'],'brand_b':b['brand'],'name_b':b['product_name'],
                    'decision':status,'score':score,'reason':reason,
                    'family_a':fa,'family_b':fb})
    return products,proposals

def write_report(out,products,proposals):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    fields=['id_a','id_b','brand_a','name_a','brand_b','name_b','decision','score','reason','family_a','family_b']
    with (out/'candidate_pairs.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(proposals)
    summary={'products_scanned':len(products),'candidate_pairs':len(proposals),
             'safe_pairs':sum(p['decision']=='safe' for p in proposals),
             'review_pairs':sum(p['decision']=='review' for p in proposals),
             'automatic_decisions':'strict normalized-name equality only',
             'data_mutated':False}
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
    return summary

def apply_to_copy(conn,proposals):
    """Use ONLY on a backed-up COPY. Fail closed on family conflicts."""
    conn.execute('PRAGMA foreign_keys=ON')
    parents={}
    def root(x):
        parents.setdefault(x,x)
        while parents[x]!=x:
            parents[x]=parents[parents[x]];x=parents[x]
        return x
    def union(a,b):
        a,b=root(a),root(b)
        if a!=b:parents[max(a,b)]=min(a,b)
    existing={int(x[0]):int(x[1]) for x in conn.execute('SELECT product_id,family_id FROM product_family_members')}
    for p in proposals:
        if p['decision']=='safe':union(p['id_a'],p['id_b'])
    grouped=defaultdict(list)
    for pid in parents:grouped[root(pid)].append(pid)
    made=0;added=0;skipped=0
    with conn:
        for ids in grouped.values():
            if len(ids)<2:continue
            linked={existing[i] for i in ids if i in existing}
            if len(linked)>1:
                skipped+=1;continue
            if linked:
                fid=next(iter(linked))
            else:
                rep=min(ids)
                row=conn.execute('SELECT brand,product_name FROM products WHERE id=?',(rep,)).fetchone()
                key='reconciled-exact-'+str(rep)
                conn.execute('INSERT OR IGNORE INTO product_families (family_key,representative_product_id,brand,product_name) VALUES (?,?,?,?)',(key,rep,row[0],row[1]))
                fid=conn.execute('SELECT id FROM product_families WHERE family_key=?',(key,)).fetchone()[0]
                made+=1
            for pid in ids:
                if pid not in existing:
                    conn.execute('INSERT INTO product_family_members (product_id,family_id,variant_label) VALUES (?,?,NULL)',(pid,fid));added+=1
    if conn.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Foreign key check failed')
    return {'families_created':made,'members_added':added,'conflicting_groups_skipped':skipped}
