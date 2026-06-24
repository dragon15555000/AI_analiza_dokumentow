"""Minimalna konfiguracja i walidacja runtime dla AI Analiza Dokumentów."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeSettings:
    qdrant_url: str = ""
    qdrant_key: str = ""
    flask_secret_key: str = ""


def load_env_file(env_path: Path | None = None) -> None:
    """Wczytuje lokalny .env bez nadpisywania istniejących zmiennych."""
    path = env_path or (Path(__file__).parent / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key == "SECRET_KEY":
            continue
        os.environ.setdefault(key, value.strip())


def load_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(
        qdrant_url=os.environ.get("QDRANT_URL", ""),
        qdrant_key=os.environ.get("QDRANT_KEY", ""),
        flask_secret_key=os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY", ""),
    )


def validate_runtime_settings(settings: RuntimeSettings | None = None) -> list[str]:
    cfg = settings or load_runtime_settings()
    missing: list[str] = []
    if not cfg.qdrant_url:
        missing.append("QDRANT_URL")
    if not cfg.qdrant_key:
        missing.append("QDRANT_KEY")
    if not cfg.flask_secret_key:
        missing.append("FLASK_SECRET_KEY/SECRET_KEY")
    return missing
