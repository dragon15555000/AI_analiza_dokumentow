from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock
import app

def test_ocr_fallback_when_tesseract_missing(monkeypatch):
    mock_pytesseract = MagicMock()
    mock_pytesseract.image_to_string.side_effect = Exception("Tesseract not found in PATH")
    
    monkeypatch.setattr(app, "pytesseract", mock_pytesseract)
    
    test_image = Path("dummy_image.png")
    
    mock_image = MagicMock()
    with patch("PIL.Image.open", return_value=mock_image):
        result = app._ocr_image_file(test_image)
    
    assert result == ""
