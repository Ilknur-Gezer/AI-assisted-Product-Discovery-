import sqlite3
from pathlib import Path
import importlib.util
from skinfluencer.web.product_families import family_ids, collapse_catalog

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/create_product_family.py'
spec = importlib.util.spec_from_file_location('create_product_family', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_family_migration_and_catalog(tmp_path):
    db = tmp_path / 'family.sqlite'
    con = sqlite3.connect(db)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT, product_name TEXT)')
    for pid in module.MAYBELLINE_LUMI:
        con.execute('INSERT INTO products VALUES(?,?,?)', (pid,'Maybelline', 'Lumi Matte 96' if pid == 364 else 'Lumi Matte'))
    con.execute('INSERT INTO products VALUES(9999,?,?)', ('Other', 'Other Product'))
    with con:
        module.migrate(con,apply=True)
        module.migrate(con,apply=True)
    assert len(family_ids(con,115)) == 11
    assert family_ids(con,9999) == [9999]
    assert con.execute('SELECT variant_label FROM product_family_members WHERE product_id=364').fetchone()[0] == '96'
    catalog = [dict(product_id=pid, brand='Maybelline', product_name='Lumi Matte', category='makeup', review_count=1, social_post_count=1, influencer_count=1) for pid in module.MAYBELLINE_LUMI]
    assert len(collapse_catalog(con,catalog)) == 1
    assert collapse_catalog(con,catalog)[0]['review_count'] == 11
    assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 12
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
