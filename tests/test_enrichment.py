import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
# Import-free structural test because CI installs requirements before runtime tests.
def test_enrichment_modules_present():
    assert (ROOT/'src/skinfluencer/enrichment/purchase_links.py').is_file()
    assert (ROOT/'src/skinfluencer/enrichment/product_images.py').is_file()


def _purchase_links_module():
    from skinfluencer.enrichment import purchase_links
    return purchase_links


def test_purchase_family_merges_safe_shade_translation_variant():
    pl = _purchase_links_module()
    base = pl.product_family_key("About Tone", "Skin Layer Fit Foundation")
    assert base == pl.product_family_key("About tone", "skin layer fit fondöten 21 numara")
    assert base != pl.product_family_key("About Tone", "Nothing But Nude Foundation")


def test_purchase_brand_handle_cleanup():
    pl = _purchase_links_module()
    assert pl.clean_brand_for_search("@lunovaskin") == "lunovaskin"


def test_purchase_vague_range_is_never_searchable():
    pl = _purchase_links_module()
    family = {
        "brand": "Alix Avien",
        "product_name": "kapatıcıların 901-903 numaraları",
        "search_brand": "Alix Avien",
        "search_product_name": "kapatıcıların 901-903 numaraları",
    }
    family["search_quality"] = pl.search_quality_score(family)
    eligible, reason = pl.product_search_eligibility(family)
    assert not eligible
    assert "plural_or_range" in reason


def test_purchase_family_aggregates_relevance_signals():
    pl = _purchase_links_module()
    products = [
        {"product_id": 1, "brand": "About Tone", "product_name": "Skin Layer Fit Foundation", "category": "makeup", "review_count": 1, "social_post_count": 0, "influencer_count": 1},
        {"product_id": 2, "brand": "About tone", "product_name": "skin layer fit fondöten 21 numara", "category": "makeup", "review_count": 0, "social_post_count": 2, "influencer_count": 1},
    ]
    families = pl.prepare_product_families(products)
    assert len(families) == 1
    assert families[0]["review_count"] == 1
    assert families[0]["social_post_count"] == 2
    assert families[0]["family_size"] == 2


def test_low_quality_caption_title_is_free_only_not_paid_candidate():
    pl = _purchase_links_module()
    family = {
        "brand": "acropass",
        "product_name": "patch pdrn ve retinol içeren",
        "search_brand": "acropass",
        "search_product_name": "patch pdrn ve retinol içeren",
    }
    quality = pl.search_quality_score(family)
    assert quality >= pl.DEFAULT_MIN_SEARCH_QUALITY
    assert quality < pl.DEFAULT_MIN_OPENAI_QUALITY
