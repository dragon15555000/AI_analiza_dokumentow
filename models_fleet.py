# -*- coding: utf-8 -*-
"""
Models Fleet Management — zarządzanie flotą modeli AI.
Ranking modeli, statystyki dostępności, wybór najlepszych endpointów.
"""

import json
import threading
import logging
from pathlib import Path

logger = logging.getLogger("ai_analiza")

# ============================================================
# GLOBAL STATE — pliki i locki dla statystyk
# ============================================================

_CUSTOM_STATS_FILE = Path(__file__).parent / ".llm_custom_endpoint_stats.json"
_MODEL_LIVE_FILE = Path(__file__).parent / ".llm_model_live_stats.json"
_custom_stats_lock = threading.Lock()
_model_live_lock = threading.Lock()


# ============================================================
# LOAD / SAVE — statystyki custom endpointów i modeli
# ============================================================

def _load_custom_endpoint_stats() -> dict:
    """Wczytuje statystyki custom endpointów z pliku JSON."""
    try:
        if _CUSTOM_STATS_FILE.exists():
            return json.loads(_CUSTOM_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_custom_endpoint_stats(stats: dict) -> None:
    """Zapisuje statystyki custom endpointów do pliku JSON."""
    with _custom_stats_lock:
        _CUSTOM_STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_or_init_endpoint_stats(endpoint_id: str) -> dict:
    """Zwraca lub inicjalizuje strukturę statystyk dla endpointu."""
    stats = _load_custom_endpoint_stats()
    if endpoint_id not in stats:
        stats[endpoint_id] = {
            "ping_ok": None, "ping_ms": None, "ping_ts": None,
            "err_count": 0, "ok_count": 0, "avg_ms": None
        }
        _save_custom_endpoint_stats(stats)
    return stats[endpoint_id]


def _load_model_live_stats() -> dict:
    """Wczytuje utrwalone pingi wbudowanych modeli floty."""
    try:
        if _MODEL_LIVE_FILE.exists():
            data = json.loads(_MODEL_LIVE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Nie wczytano statystyk modeli: %s", exc)
    return {}


def _save_model_live_stats(stats: dict) -> None:
    """Zapisuje pingi wbudowanych modeli floty."""
    try:
        with _model_live_lock:
            _MODEL_LIVE_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("Nie zapisano statystyk modeli: %s", exc)


# ============================================================
# GLOBAL REFERENCES — inicjalizowane przez app.py
# ============================================================

_model_live: dict = {}  # Inicjalizowany przez app.py
_MODEL_REGISTRY: dict = {}  # Importowany z prompts przez app.py


def _set_model_live(model_live: dict) -> None:
    """Ustawia globalną referencję _model_live (wywoływane przez app.py)."""
    global _model_live
    _model_live = model_live


def _set_MODEL_REGISTRY(registry: dict) -> None:
    """Ustawia globalną referencję _MODEL_REGISTRY (wywoływane przez app.py)."""
    global _MODEL_REGISTRY
    _MODEL_REGISTRY = registry


# ============================================================
# SCORING — ranking modeli i custom endpointów
# ============================================================

def _model_score(model_id: str) -> float:
    """Ranking 0–100. Jakość 40% + szybkość 30% + dostępność 30%."""
    reg = _MODEL_REGISTRY.get(model_id, {})
    live = _model_live.get(model_id, {})
    if live.get("ping_ok") is False:
        return 0.0
    quality = (reg.get("quality_tier", 1) - 1) / 2 * 40
    speed   = (4 - reg.get("speed_tier", 2)) / 2 * 30  # szybszy → wyżej
    if live.get("ping_ok") is True:
        ms = live.get("avg_ms") or live.get("ping_ms") or 15000
        avail = max(0.0, 30.0 - ms / 500.0)
    else:
        avail = 12.0  # nieznany — neutralny
    return round(quality + speed + avail, 1)


def _custom_endpoint_score(endpoint_id: str) -> float:
    """Ranking custom endpointu 0-100 na bazie dostepnosci i szybkosci."""
    stats = _load_custom_endpoint_stats().get(endpoint_id, {})
    if stats.get("ping_ok") is False:
        return 0.0
    if stats.get("ping_ok") is True:
        ms = stats.get("avg_ms") or stats.get("ping_ms") or 8000
        score = max(0.0, 70.0 - ms / 1000.0)
    else:
        score = 40.0
    return round(score, 1)


def _get_best_custom_endpoints(limit: int = 3, provider_pool: dict | None = None) -> list[tuple]:
    """Zwraca rankingowe custom endpointy (model_id, model_name) posortowane po score."""
    if provider_pool is None:
        provider_pool = {}
    endpoints = []
    for endpoint in provider_pool.get("custom_endpoints", []):
        if not endpoint.get("active"):
            continue
        eid = endpoint.get("id")
        name = endpoint.get("name", endpoint.get("url"))
        score = _custom_endpoint_score(eid)
        endpoints.append((eid, name, score))
    endpoints.sort(key=lambda x: x[2], reverse=True)
    return [(eid, name) for eid, name, _ in endpoints[:limit]]
