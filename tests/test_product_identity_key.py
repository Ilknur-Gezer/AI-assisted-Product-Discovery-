from skinfluencer.storage.sqlite_importer import product_identity_key as key


def test_same_product_different_spelling():
    assert key("Kiehl's", "Ultra Facial Cream") == key("Kiehls", "ultra facial cream")
    assert key("X", "10% Azelaic Acid Serum") == key("X", "Azelaic Acid %10 Serum")
    assert key("X", "Tint 003") == key("X", "Tint 03")


def test_symbols_units_and_plus_between_words():
    assert key("Clinique", "Moisture Surge™️ Active Glow") == key("Clinique", "Moisture Surge Active Glow")
    assert key("Kiehl's", "1 cc Collashot Serum") == key("Kiehl's", "1cc Collashot Serum")
    assert key("EL", "Eye Lift & Sculpt 15ml") == key("EL", "Eye Lift+Sculpt 15 ml")


def test_different_products_stay_apart():
    assert key("X", "Eyeshadow 02M") != key("X", "Eyeshadow 03M")
    assert key("LRP", "Cicaplast Baume B5") != key("LRP", "Cicaplast Baume B5+")
