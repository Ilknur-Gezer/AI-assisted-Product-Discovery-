import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from skinfluencer.extraction import _instagram_product_extractor as extractor
from skinfluencer.storage import platform_match_importer as matches
from skinfluencer.storage.social_products import SOCIAL_SCHEMA


@pytest.mark.parametrize('item,expected', [
    ({'match_type': 'exact'}, True),
    ({'match_type': 'near', 'similarity': .97}, True),
    ({'match_type': 'near', 'similarity': .9699}, False),
])
def test_reuse_threshold(item, expected):
    assert extractor._strong_match(item) is expected


def test_reuse_revalidates_instagram_evidence(tmp_path):
    path = tmp_path / 'naturally_serein/tiktok/description_products_llm/123.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        'platform': 'tiktok', 'influencer_slug': 'naturally_serein',
        'external_post_id': '123', 'extraction_status': 'success',
        'products': [{'brand': 'Acme', 'product_name': 'Serum',
                      'category': 'skincare', 'confidence': .99,
                      'evidence_text': 'Acme Serum', 'status': 'approved'}],
    }))
    kwargs = dict(data_root=tmp_path, influencer='naturally_serein',
                  match={'tiktok_id': '123', 'match_type': 'exact'})
    assert extractor._source_reuse_payload(post={'caption': 'Acme Serum'}, **kwargs)
    assert extractor._source_reuse_payload(post={'caption': 'A different caption'}, **kwargs) is None


def test_scoped_match_removal_preserves_history():
    c = sqlite3.connect(':memory:')
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript('CREATE TABLE influencers(id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT);'
                    'CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT, product_name TEXT, category TEXT);'
                    + SOCIAL_SCHEMA)
    c.execute("INSERT INTO influencers VALUES(1,'naturally_serein','Cisem')")
    for pid, platform, external in [(1, 'tiktok', '123'), (2, 'instagram', 'Old'),
                                    (3, 'tiktok', '456'), (4, 'instagram', 'New')]:
        c.execute('INSERT INTO social_posts(id,influencer_id,platform,external_post_id,caption,original_url,published_at,caption_sha256) VALUES(?,1,?,?,?, ?,?,?)',
                  (pid, platform, external, '', 'url', '2026-09-20', 'hash'))
    for a, b in [(1, 2), (3, 4)]:
        c.execute("INSERT INTO social_post_matches(post_id_a,post_id_b,match_type) VALUES(?,?,'exact')", (a, b))
    assert matches._clear_automatic_matches_for_influencer(
        c, influencer='naturally_serein', instagram_ids={'New'}) == 1
    assert c.execute('SELECT post_id_a,post_id_b FROM social_post_matches').fetchall() == [(1, 2)]
    matches._upsert_match(c, influencer='naturally_serein',
                         match={'tiktok_id': '456', 'instagram_id': 'New', 'match_type': 'exact'},
                         source_file=Path('matches.json'))
    assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    assert c.execute('SELECT count(*) FROM social_post_matches').fetchone()[0] == 2


def test_failed_match_stage_never_activates_production(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('ingestion_pipeline', ROOT / 'pipeline.py')
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    raw = tmp_path / 'naturally_serein/instagram/raw'
    raw.mkdir(parents=True)
    data = json.loads((ROOT / 'tests/fixtures/instagram/Dd_HjaXRX4W.json').read_text())
    (raw / 'Dd_HjaXRX4W.info.json').write_text(json.dumps(data))
    from skinfluencer.storage.social_products import instagram_post_from_metadata
    post = instagram_post_from_metadata(data, 'naturally_serein')
    out = raw.parent / 'description_products_llm'
    out.mkdir()
    (out / 'Dd_HjaXRX4W.json').write_text(json.dumps(dict(post, extraction_status='success')))
    db = tmp_path / 'production.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE sentinel(value TEXT)')
        c.execute("INSERT INTO sentinel VALUES('preserved')")
    original = db.read_bytes()
    monkeypatch.setattr(pipeline, 'DATA_ROOT', tmp_path)
    calls = []
    def run(module, args):
        calls.append(module.__name__)
        if module is matches:
            raise ValueError('Missing TikTok post')
    monkeypatch.setattr(pipeline, 'run_main', run)
    import argparse
    args = argparse.Namespace(influencer='naturally_serein', dateafter='2026-09-10',
                              datebefore='2026-10-02', skip_extraction=True, dry_run=False,
                              database=db, model='gpt-4.1-mini')
    with pytest.raises(ValueError, match='Missing TikTok post'):
        pipeline.cmd_ingest_instagram(args)
    assert len(calls) == 2
    assert db.read_bytes() == original
