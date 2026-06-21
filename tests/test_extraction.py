from pathlib import Path
import pytest
from app import extract_text

def test_extract_text_simple_document():
    fixture_path = Path(__file__).parent / "fixtures" / "simple_document.txt"
    result = extract_text(fixture_path)
    
    assert result != ""
    assert "FV/1/2026" in result
    assert "123,45 PLN" in result
    assert "ACME Test" in result
