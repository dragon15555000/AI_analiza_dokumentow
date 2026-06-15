import json
import pytest
from app import app

@pytest.fixture
def web_client(monkeypatch):
    import app as app_mod
    monkeypatch.setattr(app_mod, "APP_API_KEY", "")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()

def test_analyze_empty_query_returns_failure(web_client):
    response = web_client.post("/analyze", json={"query": ""})
    data = json.loads(response.data)
    
    assert response.status_code == 200
    assert data["success"] is False
    assert "error" in data
    assert data["error"] == "Zapytanie puste"

def test_analyze_structure_with_no_documents(web_client, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(app_mod, "_retrieve_search_contexts", lambda *args: ([], []))
    
    response = web_client.post("/analyze", json={"query": "test query"})
    data = json.loads(response.data)
    
    assert response.status_code == 200
    assert data["success"] is True
    assert data["answer"] == "Brak dokumentów w bazie."
    assert "ai_answer" in data
    assert "results" in data
    assert "mode" in data
    assert "mode_label" in data
