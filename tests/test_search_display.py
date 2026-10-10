from skinfluencer.web.search_display import collapse_display

def p(i, brand, name, reviews=1):
    return dict(product_id=i, brand=brand, product_name=name, category='makeup', review_count=reviews, influencer_count=1, social_post_count=0)

def test_case_punctuation_and_brand_alias():
    items=[p(1,'Maybelline New York','Lumi-Matte Fondöten'),p(2,'maybelline','Lumi Matte Foundation',4)]
    assert [r['product_id'] for r in collapse_display(items)] == [2]

def test_keep_shade_and_different_series():
    items=[p(1,'Maybelline','Lumi Matte Foundation'),p(2,'Maybelline','Lumi Matte Foundation 96'),p(3,'Maybelline','Fit Me Foundation')]
    assert len(collapse_display(items))==3

def test_other_category_same_name():
    a=p(1,'X','Serum');b=p(2,'X','Serum');b['category']='haircare'
    assert len(collapse_display([a,b]))==2

def test_no_fuzzy_merge_or_removed_rows():
    items=[p(1,'Kiehls','1 cc Collashot Serum'),p(2,"Kiehl’s",'1cc Collashot Serum')]
    assert len(collapse_display(items))==2
    assert len(items)==2

def test_unchanged_single_and_empty():
    x=p(7,'Benton','Fermentation Eye Cream')
    assert collapse_display([x])==[x]
    assert collapse_display([])==[]
