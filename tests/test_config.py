import os
import pytest
from llm.llm_client import LLMClient

def test_config_missing_keys_handled(monkeypatch):
    monkeypatch.setenv("NEW_API_BASE_URL", "https://mock.base.url")
    monkeypatch.setenv("NEW_API_KEY", "mock_key")
    
    client = LLMClient()
    assert client.base_url == "https://mock.base.url"
    assert client.api_key == "mock_key"
