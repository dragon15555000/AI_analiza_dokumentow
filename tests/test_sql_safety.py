"""Testy jednostkowe dla funkcji sanitize_sql_params z sql_safety.py."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sql_safety import sanitize_sql_params


def test_removes_semicolon():
    result = sanitize_sql_params({"name": "value; DROP TABLE users"})
    assert result["name"] == "value DROP TABLE users"


def test_removes_double_dash():
    result = sanitize_sql_params({"name": "value -- comment"})
    assert result["name"] == "value  comment"


def test_removes_block_comment():
    result = sanitize_sql_params({"name": "value /* comment */ end"})
    assert result["name"] == "value  end"


def test_removes_multiline_block_comment():
    result = sanitize_sql_params({"col": "a /* multi\nline */ b"})
    assert result["col"] == "a  b"


def test_multiple_dangerous_chars():
    result = sanitize_sql_params({"x": "a; b -- c /* d */ e"})
    assert ";" not in result["x"]
    assert "--" not in result["x"]
    assert "/*" not in result["x"]
    assert "*/" not in result["x"]


def test_keys_unchanged():
    params = {"user_id": "123", "name": "Alice"}
    result = sanitize_sql_params(params)
    assert set(result.keys()) == {"user_id", "name"}


def test_non_string_values_unchanged():
    params = {"count": 42, "flag": True, "price": 3.14, "nothing": None}
    result = sanitize_sql_params(params)
    assert result["count"] == 42
    assert result["flag"] is True
    assert result["price"] == 3.14
    assert result["nothing"] is None


def test_clean_string_unchanged():
    result = sanitize_sql_params({"name": "Alice"})
    assert result["name"] == "Alice"


def test_empty_string():
    result = sanitize_sql_params({"name": ""})
    assert result["name"] == ""


def test_empty_dict():
    assert sanitize_sql_params({}) == {}
