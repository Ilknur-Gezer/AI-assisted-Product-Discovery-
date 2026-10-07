import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from skinfluencer.storage.social_products import (
    instagram_post_from_metadata, instagram_post_from_payload,
)
from skinfluencer.extraction import _instagram_product_extractor as extractor


def metadata(pid='C5YhVliIoit'):
    # Reduced actual archive records; comments/media URLs are irrelevant.
    return json.loads((ROOT / 'tests/fixtures/instagram' / f'{pid}.json').read_text())


@pytest.mark.parametrize('pid,kind,date', [
    ('C5YhVliIoit', 'reel', '2024-04-05'),
    ('DLzlbopItJt', 'reel', '2025-07-07'),
    ('Dd_HjaXRX4W', 'reel', '2026-10-02'),
    ('Dd6TRpcjdzn', 'p', '2026-09-30'),
])
def test_archive_variants(pid, kind, date):
    post = instagram_post_from_metadata(metadata(pid), 'naturally_serein')
    assert post['external_post_id'] == pid
    assert post['original_url'] == f'https://www.instagram.com/{kind}/{pid}'
    assert post['published_at'] == date
    assert post['caption'] == metadata(pid)['description']
    assert instagram_post_from_payload(post, 'naturally_serein') == post


@pytest.mark.parametrize('pid', ['DMIh-_vtLMY', 'DTkZLaFjO83', 'DUSiax2jA0U', 'DYzKhdmsiJ8'])
def test_archive_other_owner_rejected(pid):
    with pytest.raises(ValueError, match='Yanlış Instagram hesabı'):
        instagram_post_from_metadata(metadata(pid), 'naturally_serein')


@pytest.mark.parametrize('field', ['id', 'display_id', 'original_url', 'channel', 'webpage_url'])
def test_contradictory_identity_rejected(field):
    data = metadata()
    data[field] = {
        'id': 'OtherCode', 'display_id': 'OtherCode',
        'original_url': 'https://www.instagram.com/reel/OtherCode/',
        'channel': 'other_account',
        'webpage_url': 'https://www.instagram.com/other_account/reel/C5YhVliIoit/',
    }[field]
    with pytest.raises(ValueError):
        instagram_post_from_metadata(data, 'naturally_serein')


@pytest.mark.parametrize('url', [
    'http://www.instagram.com/reel/C5YhVliIoit/',
    'https://instagram.com.evil.test/reel/C5YhVliIoit/',
    'https://www.instagram.com/reel/C5YhVliIoit/extra',
    'https://other@www.instagram.com/reel/C5YhVliIoit/',
])
def test_invalid_url_rejected(url):
    data = metadata()
    data['webpage_url'] = url
    with pytest.raises(ValueError):
        instagram_post_from_metadata(data, 'naturally_serein')


def test_url_fallback_queries_and_missing_id():
    data = metadata()
    del data['webpage_url'], data['id'], data['display_id']
    data['original_url'] = 'https://instagram.com/CISEMCAKIR/p/C5YhVliIoit/?img_index=1#caption'
    del data['upload_date']
    post = instagram_post_from_metadata(data, 'naturally_serein')
    assert post['external_post_id'] == 'C5YhVliIoit'
    assert post['original_url'] == 'https://www.instagram.com/p/C5YhVliIoit'
    assert post['published_at'] == '2024-04-05'


def test_dry_run_filters_history_and_reaches_decisions_without_writes(tmp_path, monkeypatch, capsys):
    raw = tmp_path / 'naturally_serein/instagram/raw'
    raw.mkdir(parents=True)
    for pid in ['C5YhVliIoit', 'Dd_HjaXRX4W', 'Dd6TRpcjdzn']:
        (raw / f'{pid}.info.json').write_text(json.dumps(metadata(pid)))

    def forbidden(*args, **kwargs):
        pytest.fail('Dry-run attempted an API call or output write')

    monkeypatch.setattr(extractor, 'parse_caption_with_openai', forbidden)
    monkeypatch.setattr(extractor, 'write_json', forbidden)
    monkeypatch.setattr(sys, 'argv', [
        'extract', '--influencer', 'naturally_serein', '--data-root', str(tmp_path),
        '--dateafter', '2026-09-10', '--datebefore', '2026-10-02',
        '--model', 'gpt-4.1-mini', '--dry-run',
    ])
    extractor.main()
    output = capsys.readouterr().out
    summary = json.loads(output[output.index('{'):])
    assert summary['run_counts'] == {'outside_date_window': 1, 'would_call_api': 2}
    assert summary['failures'] == 0
    assert not (raw.parent / 'description_products_llm').exists()
