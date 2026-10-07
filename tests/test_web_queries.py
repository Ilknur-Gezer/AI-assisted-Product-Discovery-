import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from skinfluencer.web import queries

def test_catalog_queries():
    assert queries.database_ready()
    choices=queries.influencer_choices()
    assert 'naturally_serein' in choices and 'yagmurvardar' in choices
    rows=queries.browse_products('all','skincare',limit=3)
    assert rows and all('product_id' in row for row in rows)
