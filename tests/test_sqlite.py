import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from skinfluencer.config.settings import DATABASE_PATH

def test_database_integrity():
    c=sqlite3.connect(DATABASE_PATH)
    assert c.execute('pragma quick_check').fetchone()[0]=='ok'
    assert c.execute('pragma foreign_key_check').fetchall()==[]
    assert c.execute('select count(*) from influencers').fetchone()[0] >= 2
    assert c.execute("select count(*) from product_mentions where status='approved'").fetchone()[0] > 0
    assert c.execute("select count(*) from social_post_products where status='approved'").fetchone()[0] > 0
