import sqlite3
from skinfluencer.storage.catalog_reconciliation import classify,scan,apply_to_copy

def p(i,name,brand='L’Oréal',category='makeup'):
    return {'id':i,'brand':brand,'product_name':name,'category':category}

def test_alias_and_strict_identity():
    assert classify(p(1,'Glotion 901'),p(2,'glotion 901','L’Oreal Paris'))[0]=='safe'
    assert classify(p(1,'Glotion 901'),p(2,'Glotion 902'))[0]=='review'
    assert classify(p(1,'Sun SPF 30'),p(2,'Sun SPF 50'))[0]=='review'
    assert classify(p(1,'Serum'),p(2,'Cream'))[0]!='safe'
    assert classify(p(1,'Glotion',brand='L’Oréal'),p(2,'Glotion',brand='Maybelline'))[0]=='different'

def test_apply_preserves_every_record_and_is_idempotent():
    con=sqlite3.connect(':memory:')
    con.executescript('''CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT, product_name TEXT, category TEXT);
    CREATE TABLE product_families(id INTEGER PRIMARY KEY, family_key TEXT UNIQUE, representative_product_id INTEGER, brand TEXT,product_name TEXT);
    CREATE TABLE product_family_members(product_id INTEGER PRIMARY KEY, family_id INTEGER,variant_label TEXT);
    CREATE TABLE outbound_clicks(id INTEGER PRIMARY KEY,product_id INTEGER);
    INSERT INTO products VALUES (1,'L’Oréal','Glotion 901','makeup'),(2,'L’Oreal Paris','glotion 901','makeup'),(3,'L’Oreal','Glotion 902','makeup');
    INSERT INTO outbound_clicks VALUES(1,2);''')
    _,candidates=scan(con)
    assert any(q['id_a']==1 and q['id_b']==2 and q['decision']=='safe' for q in candidates)
    assert apply_to_copy(con,candidates)['members_added']==2
    assert apply_to_copy(con,candidates)['members_added']==0
    assert con.execute('select count(*) from products').fetchone()[0]==3
    assert con.execute('select product_id from outbound_clicks').fetchone()[0]==2


def test_reordered_tokens_and_protective_conflicts():
    assert classify(p(1, 'Tiger Grass Cica Color Correcting Cream SPF 50+'),
                    p(2, '50 spf tiger grass cica color correcting cream'))[0] == 'safe'
    assert classify(p(1, 'Glotion 901'), p(2, 'Glotion 902'))[0] == 'review'
    assert classify(p(1, 'SPF 50 Cream'), p(2, 'SPF 30 Cream'))[0] != 'safe'
    decision, score, reason = classify(p(1, 'Glotion 901',category='makeup'),
                                        p(2, 'Glotion 901',category='skincare'))
    assert (decision, reason) == ('review', 'category_conflict') and score == 100


def test_unrelated_shared_generic_word_not_reported():
    con=sqlite3.connect(':memory:')
    con.executescript("""CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT, product_name TEXT, category TEXT);
    CREATE TABLE product_family_members(product_id INTEGER PRIMARY KEY, family_id INTEGER);
    INSERT INTO products VALUES (1,'BrandX','Eye Cream SPF 50','skincare'),
      (2,'BrandX','Hair Cream Keratin Treatment','haircare');""")
    _, candidates=scan(con)
    assert candidates == []
