import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_extraction_modules_present():
    assert (ROOT/'src/skinfluencer/extraction/product_extractor.py').is_file()
    assert (ROOT/'src/skinfluencer/extraction/opinion_extractor.py').is_file()
    assert (ROOT/'src/skinfluencer/extraction/caption_context_extractor.py').is_file()
