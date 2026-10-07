from pathlib import Path

from skinfluencer.web.i18n import category_tabs, normalize_language, t


def test_ui_translations():
    assert t("purchase_options", "en") == "Purchase options"
    assert t("purchase_options", "tr") == "Satın alma seçenekleri"
    assert category_tabs("en")["skincare"] == "Skincare"
    assert category_tabs("tr")["skincare"] == "Cilt Bakımı"
    assert normalize_language("xx") == "en"


def test_schema_contains_translation_tables():
    root = Path(__file__).resolve().parents[1]
    schema = (root / "src/skinfluencer/storage/schema.sql").read_text(encoding="utf-8")
    assert "product_mention_translations" in schema
    assert "social_product_context_translations" in schema
    assert "caption_context_id" in schema
