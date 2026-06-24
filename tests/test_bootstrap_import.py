import importlib
import sys

import pytest


def test_app_imports_without_runtime_side_effects(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_KEY", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    import settings

    monkeypatch.setattr(settings, "load_env_file", lambda *args, **kwargs: None)
    sys.modules.pop("app", None)

    app_mod = importlib.import_module("app")

    assert hasattr(app_mod, "bootstrap_runtime")
    assert callable(app_mod.bootstrap_runtime)


def test_bootstrap_runtime_fails_when_required_settings_missing(monkeypatch):
    monkeypatch.delenv("SKIP_QDRANT_INIT", raising=False)

    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")
    monkeypatch.setattr(
        app_mod,
        "validate_runtime_settings",
        lambda *args, **kwargs: ["QDRANT_URL", "QDRANT_KEY", "FLASK_SECRET_KEY/SECRET_KEY"],
    )

    with pytest.raises(RuntimeError, match="Brakujące zmienne środowiskowe"):
        app_mod.bootstrap_runtime()
