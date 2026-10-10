import csv
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'llm_reconcile_catalog.py'
spec = importlib.util.spec_from_file_location('llm_reconcile_catalog', SCRIPT)
m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class FakeClient:
    def __init__(self):self.calls=0;self.responses=self
    def create(self,**kwargs):
        self.calls+=1
        return SimpleNamespace(output_text=json.dumps({
            'decision':'uncertain','confidence':0.4,'reason':'ambiguous',
            'canonical_name_suggestion':None,'variant_notes':'',
            'requires_manual_review':False}))

def fixtures(tmp):
    db=tmp/'data.sqlite'
    c=sqlite3.connect(db)
    c.executescript('''CREATE TABLE product_mentions(id integer, product_id integer, grounded_summary text, display_summary text);
        CREATE TABLE social_posts(id integer,platform text,caption text);
        CREATE TABLE social_post_products(id integer, social_post_id integer,product_id integer,evidence_text text);''')
    c.commit();c.close()
    csv_path=tmp/'candidate_pairs.csv'
    with csv_path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['id_a','id_b','brand_a','name_a','brand_b','name_b','decision','score','reason','family_a','family_b'])
        w.writeheader();w.writerow({'id_a':'1','id_b':'2','brand_a':'Brand','brand_b':'Brand',
        'name_a':'Glow Cream','name_b':'Cream Glow','decision':'review','score':'90','reason':'fuzzy_candidate'})
    return SimpleNamespace(csv=csv_path,db=db,output=tmp/'reports',limit=100,min_score=67,
            selection='spread',model='test-model',execute=False,stop_on_error=False)

def test_read_only_dry_run(tmp_path):
    args=fixtures(tmp_path)
    result=m.run(args)
    assert result['planned_without_api']==1 and result['new_api_decisions']==0
    assert not (args.output/'judgments.jsonl').exists()

def test_execute_resume_cache(tmp_path):
    args=fixtures(tmp_path);args.execute=True
    client=FakeClient()
    first=m.run(args,client)
    second=m.run(args,client)
    assert first['new_api_decisions']==1 and second['completed_from_cache']==1
    assert client.calls==1
    assert json.loads((args.output/'judgments.jsonl').read_text())['answer']['requires_manual_review'] is True

def test_rejects_invalid_answers():
    try:m.validate({'decision':'same_product','confidence':1.5})
    except ValueError:pass
    else:assert False

def test_spread_selection(tmp_path):
    p=tmp_path/'pairs.csv'
    with p.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['id_a','id_b','score','decision'])
        w.writeheader()
        for i in range(10):w.writerow({'id_a':i+1,'id_b':i+20,'score':100-i,'decision':'review'})
    rows=m.read_candidates(p,3,67,'spread')
    assert [r['score'] for r in rows]==[100,97,94]

def test_guardrail_different_actives():
    pair={'name_a':'CeraVe Retinol Serum','name_b':'CeraVe Vitamin C Serum'}
    answer={'decision':'same_family','confidence':0.99,'reason':'same brand','requires_manual_review':True}
    out=m.safeguards(pair,answer)
    assert out['decision']=='uncertain'
    assert 'different_named_actives' in out['guardrail_flags']

def test_guardrail_blush_vs_contour():
    pair={'name_a':'Rare Beauty Soft Pinch Blush','name_b':'Rare Beauty Contour'}
    answer={'decision':'same_family','confidence':0.91,'reason':'same brand','requires_manual_review':True}
    assert m.safeguards(pair,answer)['decision']=='uncertain'

def test_guardrail_spf_conflict():
    pair={'name_a':'Sun cream SPF 30','name_b':'Sun cream SPF 50'}
    answer={'decision':'same_product','confidence':0.99,'reason':'same range','requires_manual_review':True}
    assert m.safeguards(pair,answer)['decision']=='uncertain'

def test_shade_variant_not_vetoed():
    pair={'name_a':'NYX Jelly Gloss 05','name_b':'NYX Jelly Gloss 15'}
    answer={'decision':'same_family','confidence':0.9,'reason':'shade only','requires_manual_review':True}
    assert m.safeguards(pair,answer)['decision']=='same_family'

def test_one_plus_bundle_flagged():
    pair={'name_a':'XL Acne Patch & Microneedle Technology Acne Patch','name_b':'XL Acne Patch'}
    answer={'decision':'same_family','confidence':0.9,'reason':'same series','requires_manual_review':True}
    assert 'possible_multi_product_record' in m.safeguards(pair,answer)['guardrail_flags']

def test_versioned_cache_key():
    assert m.PROMPT_VERSION.endswith('20261008')
    assert m.cache_key({'x': 1},'gpt-5-mini') != 'v1'
