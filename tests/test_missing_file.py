from pathlib import Path
import pytest
from app import extract_text

def test_extract_text_missing_file():
    non_existent = Path("non_existent_file_xyz_123.txt")
    result = extract_text(non_existent)
    assert result == ""
