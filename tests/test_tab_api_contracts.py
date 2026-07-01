from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def web_client(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "APP_API_KEY", "")
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_health_light_returns_expected_contract(web_client, monkeypatch):
    import app as app_mod

    monkeypatch.setattr(
        app_mod,
        "_check_qdrant_health",
        lambda: {"ok": True, "active_collection_exists": True, "points_in_active": 7},
    )
    monkeypatch.setattr(app_mod, "_ocr_health_status", lambda: {"available": True})
    monkeypatch.setattr(app_mod, "_load_sql_config", lambda: {})
    monkeypatch.setattr(app_mod, "_sql_conn_configured", lambda cfg: False)
    monkeypatch.setattr(app_mod, "_check_gemini_health", lambda: {"ok": True})
    monkeypatch.setattr(app_mod, "_effective_llm_model", lambda provider: "test-model")
    monkeypatch.setattr(app_mod, "_get_app_version", lambda: "v-test")

    response = web_client.get("/health?light=1")
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["overall"] == "ok"
    assert data["version"] == "v-test"
    assert data["active_collection"] == {"name": app_mod.ACTIVE_COLLECTION, "points": 7}
    assert data["llm"]["ok"] is True
    assert data["embedding"]["ok"] is True
    assert data["sql_status"] == "absent"


def test_documents_returns_expected_contract(web_client, monkeypatch):
    import app as app_mod

    class FakeClient:
        def scroll(self, **kwargs):
            return (
                [
                    SimpleNamespace(
                        payload={
                            "file": "umowa.pdf",
                            "full_path": "/tmp/umowa.pdf",
                            "metadata": {"ext": ".pdf"},
                        }
                    ),
                    SimpleNamespace(
                        payload={
                            "file": "umowa.pdf",
                            "full_path": "/tmp/umowa.pdf",
                            "metadata": {"ext": ".pdf"},
                        }
                    ),
                    SimpleNamespace(
                        payload={
                            "file": "raport.xlsx",
                            "full_path": "/tmp/raport.xlsx",
                            "metadata": {"ext": ".xlsx"},
                        }
                    ),
                ],
                None,
            )

    monkeypatch.setattr(app_mod, "get_qdrant_client", lambda: FakeClient())
    monkeypatch.setattr(
        app_mod, "_filter_documents_list", lambda docs, ext_filter, modified_after, modified_before: docs
    )
    monkeypatch.setattr(app_mod, "_docs_cache", {"data": None, "ts": 0})

    response = web_client.get("/documents?force=1")
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["cached"] is False
    assert data["total"] == 2
    assert data["total_unfiltered"] == 2
    assert data["documents"][0]["file"] == "umowa.pdf"
    assert data["documents"][0]["chunks"] == 2


def test_get_context_returns_expected_contract(web_client, monkeypatch):
    import app as app_mod

    class FakeClient:
        def retrieve(self, **kwargs):
            return [
                SimpleNamespace(
                    payload={
                        "file": "umowa.pdf",
                        "text": "To jest testowy fragment",
                        "full_path": "/tmp/umowa.pdf",
                        "metadata": {"page": 1},
                    }
                )
            ]

    monkeypatch.setattr(app_mod, "get_qdrant_client", lambda: FakeClient())
    monkeypatch.setattr(app_mod, "wsl_to_win", lambda path: f"WIN::{path}")

    response = web_client.get("/api/get_context?point_id=abc123&query=test")
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["point_id"] == "abc123"
    assert data["file"] == "umowa.pdf"
    assert data["source"] == "qdrant"
    assert data["win_path"] == "WIN::/tmp/umowa.pdf"


def test_collection_switch_returns_expected_contract(web_client, monkeypatch):
    import app as app_mod

    class FakeClient:
        def collection_exists(self, name):
            return name == "archiwum"

    monkeypatch.setattr(app_mod, "get_qdrant_client", lambda: FakeClient())
    monkeypatch.setattr(app_mod, "_persist_active_collection", lambda name: None)
    monkeypatch.setattr(app_mod, "_suggestions_cache", {"data": ["cached"]})
    monkeypatch.setattr(app_mod, "_docs_cache", {"data": ["cached"], "ts": 0})
    monkeypatch.setattr(app_mod, "_COLLECTION_PROFILE_CACHE", {})

    response = web_client.post("/collections/switch", json={"name": "archiwum"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["success"] is True
    assert data["active_collection"] == "archiwum"


def test_tasks_and_financial_tabs_are_registered_in_template():
    template = Path("templates/index.html").read_text(encoding="utf-8")

    assert "tabTasks" in template
    assert "tabFinancial" in template
    assert "tasks:13" in template
    assert "financial:14" in template
    assert "loadTasksList();" in template
