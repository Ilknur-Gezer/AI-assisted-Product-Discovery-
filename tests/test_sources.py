import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from skinfluencer.config.influencers import INFLUENCERS

def test_influencer_source_config():
    assert set(INFLUENCERS)=={'naturally_serein','yagmurvardar'}
    assert all(v['youtube_url'].startswith('https://www.youtube.com/') for v in INFLUENCERS.values())
    assert all(v['tiktok_handle'] for v in INFLUENCERS.values())
