# -*- coding: utf-8 -*-
"""
LLM Client — komunikacja z modelami sztucznej inteligencji.
Obsługuje: Gemini, OpenRouter, Ollama, custom endpoints.
"""

import time
import logging
import urllib.error
import urllib.request
import json
import requests
import re
import threading
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ai_analiza")

# ============================================================
# GLOBALS — zmienne konfiguracyjne (ustawiane przez app.py)
# ============================================================
# Te zmienne będą importowane z app.py lub ustawiane warunkowo
# Deklarujemy je tutaj jako None aby uniknąć NameError

GEMINI_API_KEY = None
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = None
GEMINI_MODEL_DEFAULT = "gemini-2.0-flash"
GEMINI_DEPRECATED_MODELS = {"gemini-1.5-pro", "gemini-1.5-flash"}
GEMINI_MAX_RETRIES = 5
GEMINI_RETRY_INITIAL_DELAY = 2.5
GEMINI_RETRY_MAX_WAIT_SEC = 60.0

OPENROUTER_API_KEY = None
OPENROUTER_MODEL = None
OPENROUTER_MODEL_VERIFY = None
OPENROUTER_FALLBACK_TO_OLLAMA = True
OPENROUTER_MAX_RETRIES = 3
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

OLLAMA_URL = "http://localhost:11434"
LLM_MODEL = None

GROQ_MODEL = None

DEFAULT_LLM_PROVIDER = "openrouter"

ANTHROPIC_API_KEY = None
CLAUDE_MODEL = "claude-3-5-sonnet-20241022"
CLAUDE_API_BASE = "https://api.anthropic.com/v1"

# Funkcje z app.py które będą ustawiane dynamicznie
_load_provider_pool_func = None
_openrouter_rr_idx = 0
_openrouter_rr_lock = threading.Lock()


def _set_load_provider_pool(func):
    """Ustawia funkcję _load_provider_pool (wywoływane przez app.py)."""
    global _load_provider_pool_func
    _load_provider_pool_func = func


def _load_provider_pool():
    """Wrapper dla _load_provider_pool z app.py."""
    if _load_provider_pool_func is None:
        raise RuntimeError("_load_provider_pool nie został ustawiony. Upewnij się że app.py go inicjalizuje.")
    return _load_provider_pool_func()


def _pick_openrouter_key() -> str:
    """Zwraca aktywny klucz OpenRouter z puli, z fallbackiem do .env."""
    global _openrouter_rr_idx
    pool = _load_provider_pool()
    active = [e for e in pool.get("openrouter_keys", []) if e.get("active") and e.get("key")]
    if not active:
        return (OPENROUTER_API_KEY or "").strip()
    with _openrouter_rr_lock:
        idx = _openrouter_rr_idx % len(active)
        _openrouter_rr_idx = (idx + 1) % len(active)
    return active[idx]["key"]


def _pick_ollama_url() -> str:
    """Zwraca pierwszy aktywny adres Ollama z puli albo domyślny OLLAMA_URL."""
    pool = _load_provider_pool()
    active = [e for e in pool.get("ollama_urls", []) if e.get("active") and e.get("url")]
    return active[0]["url"] if active else OLLAMA_URL


def _sync_llm_client_config(**kwargs):
    """Synchronizuje zmienne konfiguracyjne LLM (wywoływane przez app.py)."""
    global GEMINI_API_KEY, GEMINI_MODEL, OPENROUTER_API_KEY, OPENROUTER_MODEL
    global OPENROUTER_MODEL_VERIFY, OPENROUTER_FALLBACK_TO_OLLAMA, OPENROUTER_MAX_RETRIES
    global LLM_MODEL, GROQ_MODEL, DEFAULT_LLM_PROVIDER
    global ANTHROPIC_API_KEY, CLAUDE_MODEL

    GEMINI_API_KEY = kwargs.get('GEMINI_API_KEY', GEMINI_API_KEY)
    GEMINI_MODEL = kwargs.get('GEMINI_MODEL', GEMINI_MODEL)
    OPENROUTER_API_KEY = kwargs.get('OPENROUTER_API_KEY', OPENROUTER_API_KEY)
    OPENROUTER_MODEL = kwargs.get('OPENROUTER_MODEL', OPENROUTER_MODEL)
    OPENROUTER_MODEL_VERIFY = kwargs.get('OPENROUTER_MODEL_VERIFY', OPENROUTER_MODEL_VERIFY)
    OPENROUTER_FALLBACK_TO_OLLAMA = kwargs.get('OPENROUTER_FALLBACK_TO_OLLAMA', OPENROUTER_FALLBACK_TO_OLLAMA)
    OPENROUTER_MAX_RETRIES = kwargs.get('OPENROUTER_MAX_RETRIES', OPENROUTER_MAX_RETRIES)
    LLM_MODEL = kwargs.get('LLM_MODEL', LLM_MODEL)
    GROQ_MODEL = kwargs.get('GROQ_MODEL', GROQ_MODEL)
    DEFAULT_LLM_PROVIDER = kwargs.get('DEFAULT_LLM_PROVIDER', DEFAULT_LLM_PROVIDER)
    ANTHROPIC_API_KEY = kwargs.get('ANTHROPIC_API_KEY', ANTHROPIC_API_KEY)
    CLAUDE_MODEL = kwargs.get('CLAUDE_MODEL', CLAUDE_MODEL)



def get_llm_provider(request_provider: str | None = None) -> str:
    """Zwraca aktywny provider: 'ollama', 'openrouter', 'gemini', 'ollama:<id>' lub ID custom endpointu."""
    if request_provider:
        p = request_provider.strip()
        pl = p.lower()
        if pl == "openrouter":
            return "openrouter"
        if pl == "gemini":
            return "gemini"
        if pl == "claude":
            return "claude"
        if pl == "ollama" or pl.startswith("ollama:"):
            return "ollama"
        pool = _load_provider_pool()
        for endpoint in pool.get("custom_endpoints", []):
            if endpoint.get("id") == p and endpoint.get("active", True):
                return p
    return DEFAULT_LLM_PROVIDER



def _effective_llm_model(provider: str | None = None) -> str:
    """Model pokazywany w diagnostyce dla aktywnego providera."""
    prov = (provider or DEFAULT_LLM_PROVIDER or "").strip().lower()
    if prov == "openrouter":
        return OPENROUTER_MODEL
    if prov == "gemini":
        return GEMINI_MODEL
    if prov == "claude":
        return CLAUDE_MODEL
    if prov == "groq":
        return GROQ_MODEL
    return LLM_MODEL



def _normalize_gemini_model(model: str | None) -> str:
    """Usuwa prefix models/; wycofane gemini-1.5-* mapuje na GEMINI_MODEL_DEFAULT."""
    raw = (model or GEMINI_MODEL or GEMINI_MODEL_DEFAULT).strip()
    if raw.startswith("models/"):
        raw = raw[7:].strip()
    if not raw:
        return GEMINI_MODEL_DEFAULT
    if raw in GEMINI_DEPRECATED_MODELS:
        return GEMINI_MODEL_DEFAULT
    return raw



def _list_gemini_generate_models(api_key: str) -> tuple[bool, list[str], str | None]:
    """Zwraca krótkie nazwy modeli obsługujących generateContent."""
    if not api_key:
        return False, [], "Brak klucza API"
    try:
        url = f"{GEMINI_API_BASE}/models"
        response = _gemini_request_with_retry(
            "GET", url, api_key=api_key, timeout=8, max_retries=2,
        )
        data = response.json()
        names: list[str] = []
        for entry in data.get("models", []):
            methods = entry.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            full_name = (entry.get("name") or "").strip()
            if full_name.startswith("models/"):
                full_name = full_name[7:]
            if full_name:
                names.append(full_name)
        return True, names, None
    except Exception as exc:
        return False, [], str(exc)[:200]



def _gemini_model_hint(api_key: str, model: str) -> str:
    """Sugestia modelu po 404 — gemini-1.5-* jest wycofany od 2025."""
    normalized = _normalize_gemini_model(model)
    if normalized in GEMINI_DEPRECATED_MODELS:
        base = (
            f"Model {normalized} jest wycofany w Gemini API "
            f"(używany automatycznie: {GEMINI_MODEL_DEFAULT})."
        )
    else:
        base = f"Model {normalized} nie istnieje lub nie obsługuje generateContent."
    ok, models, list_err = _list_gemini_generate_models(api_key)
    if ok and models:
        flash = [m for m in models if "flash" in m.lower()][:5]
        sample = flash or models[:5]
        return base + " Dostępne m.in.: " + ", ".join(sample) + "."
    if list_err:
        return base + f" (lista modeli: {list_err})"
    return base



def _gemini_auth_headers(api_key: str | None = None) -> dict[str, str]:
    """Nagłówek x-goog-api-key — zalecany przez Google zamiast ?key= w URL."""
    key = (api_key or GEMINI_API_KEY or "").strip()
    if not key:
        raise ValueError("Brak klucza Gemini API")
    return {"x-goog-api-key": key}



def _gemini_retry_wait_seconds(delay: float, retry_after: str | None) -> float:
    if retry_after:
        try:
            return max(0.5, min(GEMINI_RETRY_MAX_WAIT_SEC, float(retry_after)))
        except Exception:
            pass
    return min(delay, GEMINI_RETRY_MAX_WAIT_SEC)



def _gemini_error_message(status: int | None, detail: str = "") -> str:
    if status == 429:
        return (
            "Limit zapytań Gemini (429). Odczekaj 30–60 s, przełącz na Groq/OpenRouter "
            "lub ogranicz częstotliwość zapytań (darmowy tier ma niski RPM)."
        )
    if status == 404:
        return f"Model Gemini niedostępny (404). {detail}".strip()
    if status == 403:
        return "Odmowa dostępu Gemini (403) — sprawdź klucz API (format AIzaSy… z AI Studio)."
    if detail:
        return _redact_api_keys(detail)[:240]
    return f"Błąd Gemini (HTTP {status})"



def _gemini_generate_url(model: str, stream: bool = False) -> str:
    action = "streamGenerateContent" if stream else "generateContent"
    suffix = "?alt=sse" if stream else ""
    effective_model = _normalize_gemini_model(model)
    return f"{GEMINI_API_BASE}/models/{effective_model}:{action}{suffix}"



def _is_gemini_base_url(url: str, provider_name: str = "") -> bool:
    """Sprawdza czy URL/provider wskazuje na natywne API Gemini."""
    return _custom_endpoint_is_gemini(url, provider_name)



def _gemini_request_with_retry(method: str, url: str, *, api_key: str | None = None,
                               timeout: int = 60,
                               max_retries: int | None = None,
                               initial_delay: float | None = None,
                               **kwargs):
    """Wysyła request do Gemini z nagłówkiem x-goog-api-key i backoff przy 429."""
    retries = GEMINI_MAX_RETRIES if max_retries is None else max_retries
    delay = GEMINI_RETRY_INITIAL_DELAY if initial_delay is None else initial_delay
    last_error = None
    last_status = None
    last_body = ""
    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(_gemini_auth_headers(api_key))
    if kwargs.get("json") is not None and "Content-Type" not in headers:
        headers.setdefault("Content-Type", "application/json")

    for attempt in range(retries):
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            last_status = response.status_code
            last_body = (response.text or "")[:400]
            if response.status_code == 429 and attempt < retries - 1:
                wait = _gemini_retry_wait_seconds(
                    delay, response.headers.get("Retry-After"),
                )
                logger.warning(
                    "Gemini 429 (próba %s/%s) — czekam %.1fs",
                    attempt + 1, retries, wait,
                )
                time.sleep(wait)
                delay = min(delay * 2, GEMINI_RETRY_MAX_WAIT_SEC)
                continue

            if response.status_code == 429:
                raise RuntimeError(_gemini_error_message(429, last_body))

            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last_status = status
            if getattr(exc, "response", None) is not None:
                last_body = (exc.response.text or "")[:400]
            if status == 429 and attempt < retries - 1:
                retry_after = getattr(exc.response, "headers", {}).get("Retry-After") if getattr(exc, "response", None) is not None else None
                wait = _gemini_retry_wait_seconds(delay, retry_after)
                logger.warning(
                    "Gemini 429 HTTPError (próba %s/%s) — czekam %.1fs",
                    attempt + 1, retries, wait,
                )
                time.sleep(wait)
                delay = min(delay * 2, GEMINI_RETRY_MAX_WAIT_SEC)
                continue
            if status == 429:
                raise RuntimeError(_gemini_error_message(429, last_body)) from exc
            raise RuntimeError(_gemini_error_message(status, last_body or str(exc))) from exc
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1 and "429" in str(exc):
                time.sleep(min(delay, GEMINI_RETRY_MAX_WAIT_SEC))
                delay = min(delay * 2, GEMINI_RETRY_MAX_WAIT_SEC)
                continue
            raise RuntimeError(_gemini_error_message(last_status, str(exc))) from exc

    raise RuntimeError(
        _gemini_error_message(last_status or 429, last_body)
        if last_status == 429
        else (str(last_error) if last_error else "Gemini request failed")
    )



def _check_gemini_health() -> dict:
    """Sprawdza klucz Gemini i dostępność skonfigurowanego modelu."""
    if not GEMINI_API_KEY:
        return {"ok": False, "error": "Brak GEMINI_API_KEY"}
    configured = _normalize_gemini_model(GEMINI_MODEL)
    try:
        ok, models, list_err = _list_gemini_generate_models(GEMINI_API_KEY)
        if not ok:
            return {"ok": False, "model": configured, "error": list_err or "Nie udało się pobrać listy modeli"}
        has_model = configured in models
        result = {
            "ok": has_model,
            "models_available": len(models),
            "has_configured_model": has_model,
            "model": configured,
            "deprecated_model": configured in GEMINI_DEPRECATED_MODELS,
            "error": None if has_model else _gemini_model_hint(GEMINI_API_KEY, configured),
        }
        if not has_model:
            flash = [m for m in models if "flash" in m.lower()][:5]
            result["suggested_models"] = flash or models[:5]
        return result
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return {"ok": False, "model": configured, "error": "Limit RPM Gemini (429)"}
        return {"ok": False, "model": configured, "error": str(exc)[:120]}
    except Exception as e:
        return {"ok": False, "model": configured, "error": str(e)[:120]}



def _stream_gemini_tokens(prompt: str, system: str = "", model: str | None = None,
                          max_tokens: int = 8192, temperature: float = 0.2):
    """Generator tokenów z Google Gemini API (streamGenerateContent)."""
    if not GEMINI_API_KEY:
        yield "Błąd: Brak GEMINI_API_KEY w .env"
        return
    effective_model = _normalize_gemini_model(model)
    url = _gemini_generate_url(effective_model, stream=True)
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    try:
        with _gemini_request_with_retry("POST", url, json=payload, stream=True, timeout=600) as r:
            if r.status_code >= 400:
                if r.status_code == 404:
                    yield f"[Błąd Gemini: {_gemini_model_hint(GEMINI_API_KEY, effective_model)}]"
                elif r.status_code == 429:
                    yield f"[RATE_LIMIT] {_gemini_error_message(429)}"
                else:
                    yield f"[Błąd Gemini streaming: {_gemini_error_message(r.status_code, r.text[:400])}]"
                return
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data_str)
                    for cand in chunk.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            token = part.get("text", "")
                            if token:
                                yield token
                except Exception:
                    continue
    except Exception as e:
        yield f"[Błąd Gemini streaming: {str(e)}]"



def _call_gemini(prompt: str, system: str, stream: bool, model: str,
                 max_tokens: int = 8192, temperature: float = 0.2):
    if not GEMINI_API_KEY:
        raise RuntimeError("Brak GEMINI_API_KEY w .env")
    effective_model = _normalize_gemini_model(model)
    if stream:
        return _gemini_request_with_retry(
            "POST",
            _gemini_generate_url(effective_model, stream=True),
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                **({"systemInstruction": {"parts": [{"text": system}]}} if system else {}),
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
            },
            stream=True,
            timeout=600,
            max_retries=3,
        )
    url = _gemini_generate_url(effective_model, stream=False)
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    r = _gemini_request_with_retry("POST", url, json=payload, timeout=300, max_retries=3)
    data = r.json()
    text_parts = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"):
                text_parts.append(part["text"])
    return {"response": "".join(text_parts), "raw": data}


def _call_claude(prompt: str, system: str, stream: bool, model: str,
                 max_tokens: int = 4096, temperature: float = 0.2):
    """Wywołanie API Anthropic (Claude) — system prompt jako osobne pole."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Brak ANTHROPIC_API_KEY — ustaw w .env lub przekaż przez _sync_llm_client_config")
    if stream:
        raise RuntimeError("Claude streaming nie jest jeszcze obsługiwane")

    effective_model = (model or CLAUDE_MODEL or "").strip()
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": effective_model,
        "max_tokens": max(1, int(max_tokens or 4096)),
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    logger.info("Wysyłam zapytanie do Claude (model: %s)", effective_model)
    r = requests.post(f"{CLAUDE_API_BASE}/messages", headers=headers, json=payload, timeout=300)
    if r.status_code != 200:
        detail = _redact_api_keys(r.text or "")[:500]
        logger.error("Błąd Claude API (%s): %s", r.status_code, detail)
        raise RuntimeError(
            f"Claude API {r.status_code}: {detail}" if detail else f"Claude API {r.status_code}"
        )

    data = r.json()
    text_parts = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            text_parts.append(block["text"])
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "response": "".join(text_parts),
        "raw": data,
        "usage": {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
        },
    }


# ---- Rate limit helpers (OpenRouter) ----

# ============================================================
# HEALTH / STATUS DASHBOARD
# ============================================================


def _check_ollama_health() -> dict:
    """Sprawdza czy Ollama działa i czy modele są dostępne."""
    try:
        url = OLLAMA_URL + "/api/tags"
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            has_llm = any(LLM_MODEL in m for m in models)
            has_embed = any(EMBED_MODEL in m for m in models)
            return {
                "ok": True,
                "url": OLLAMA_URL,
                "models_available": len(models),
                "has_llm": has_llm,
                "has_embedding": has_embed,
                "error": None
            }
    except Exception as e:
        return {"ok": False, "url": OLLAMA_URL, "error": str(e)[:120]}



def _call_ollama(prompt: str, system: str, stream: bool, model: str,
                 provider: str | None = None) -> dict | requests.Response:
    url = _ollama_url_for_provider(provider).rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": stream,
        "options": {"temperature": 0.2}
    }
    if stream:
        return requests.post(url, json=payload, stream=True, timeout=300)
    else:
        r = requests.post(url, json=payload, timeout=180)
        r.raise_for_status()
        return r.json()



def _check_openrouter_health() -> dict:
    """Lekkie sprawdzenie OpenRouter (czy klucz działa)."""
    if not OPENROUTER_API_KEY:
        return {"ok": False, "error": "Brak OPENROUTER_API_KEY"}

    try:
        # Używamy lekkiego endpointu (list models jest darmowy i szybki)
        url = "https://openrouter.ai/api/v1/models"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {
                "ok": True,
                "models_available": len(data.get("data", [])),
                "error": None
            }
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}



def _is_openrouter_no_endpoints_error(exc: Exception) -> bool:
    """Sprawdza czy OpenRouter zwrócił 404 'No endpoints found' dla modelu."""
    if exc is None:
        return False

    try:
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
            if status != 404:
                return False
            chunks = [str(exc)]
            try:
                chunks.append(response.text or "")
            except Exception:
                pass
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    chunks.append(json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass
            text = " ".join(chunks).lower()
            return "no endpoints found" in text
    except Exception:
        pass

    return "no endpoints found" in str(exc).lower()



def _openrouter_model_candidates(primary_model: str | None) -> list[str]:
    """Zwraca listę modeli OpenRouter do wypróbowania w kolejności preferencji."""
    primary = (primary_model or OPENROUTER_MODEL or "").strip()
    candidates: list[str] = []
    known_fallbacks = (
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-20b:free",
        "z-ai/glm-4.5-air:free",
        "qwen/qwen3-coder:free",
        "meta-llama/llama-3.2-3b-instruct:free",
    )
    for candidate in (primary, OPENROUTER_MODEL, OPENROUTER_MODEL_VERIFY, *known_fallbacks):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates



def _openrouter_live_free_models(limit: int = 12) -> list[str]:
    """Pobiera aktualne modele :free z OpenRouter; używane tylko w krótkim teście providera."""
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=8)
        r.raise_for_status()
        models = r.json().get("data", [])
        out: list[str] = []
        for item in models:
            model_id = str(item.get("id") or "").strip()
            if not model_id.endswith(":free"):
                continue
            architecture = item.get("architecture") or {}
            output_modalities = architecture.get("output_modalities") or []
            if output_modalities and "text" not in output_modalities:
                continue
            out.append(model_id)
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        logger.debug("Nie udało się pobrać listy modeli OpenRouter: %s", exc)
        return []



def _openrouter_test_model_candidates() -> list[str]:
    """Modele do testu providera: konfiguracja, aktualne :free, potem stabilne fallbacki."""
    candidates: list[str] = []
    for candidate in [*_openrouter_model_candidates(OPENROUTER_MODEL), *_openrouter_live_free_models()]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates



def _test_openrouter_api_key(key: str) -> dict:
    """Krótki test OpenRouter z fallbackiem modeli, odporny na 404 No endpoints found."""
    if not key:
        return {"success": False, "error": "Brak klucza", "ms": 0}

    t0 = time.time()
    last_error = ""
    attempted: list[str] = []
    for model in _openrouter_test_model_candidates():
        attempted.append(model)
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "AI Analiza Dokumentów",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Say: OK"}],
                    "max_tokens": 5,
                },
                timeout=20,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return {"success": True, "ms": ms, "status": 200, "model": model, "error": None}
            last_error = (r.text or f"HTTP {r.status_code}")[:300]
            if (
                r.status_code == 404
                or r.status_code in {429, 502, 503, 504}
            ):
                continue
            return {"success": False, "ms": ms, "status": r.status_code, "model": model, "error": last_error}
        except requests.RequestException as exc:
            last_error = str(exc)[:300]

    return {
        "success": False,
        "ms": int((time.time() - t0) * 1000),
        "status": 404,
        "model": None,
        "attempted_models": attempted[:10],
        "error": last_error or "Brak działającego endpointu OpenRouter dla skonfigurowanych modeli",
    }



def _call_openrouter_once(prompt: str, system: str, model: str,
                          max_tokens: int, temperature: float) -> dict:
    """Jedna próba non-stream dla OpenRouter bez fallbacków modelu."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {_pick_openrouter_key()}",
        "HTTP-Referer": "http://localhost",
        "X-Title": "AI Analiza Dokumentów"
    }

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    last_error = None
    for attempt in range(OPENROUTER_MAX_RETRIES):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=180)
            r.raise_for_status()
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"response": content, "raw": data}
        except Exception as e:
            last_error = e
            if _is_openrouter_no_endpoints_error(e):
                raise
            if _is_rate_limit_error(e):
                if attempt < OPENROUTER_MAX_RETRIES - 1:
                    wait = _get_retry_after(e) or (1.5 ** attempt)
                    wait = min(wait, 12.0)
                    logger.warning(f"OpenRouter 429 (non-stream, próba {attempt+1}/{OPENROUTER_MAX_RETRIES}) — czekam {wait:.1f}s")
                    time.sleep(wait)
                continue
            if OPENROUTER_FALLBACK_TO_OLLAMA and getattr(e, "response", None) is not None:
                try:
                    status = e.response.status_code
                except Exception:
                    status = None
                if status == 400:
                    logger.warning("OpenRouter 400 → fallback do Ollama")
                    result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
                    if isinstance(result, dict):
                        return result
                    return {"response": str(result)}
            break

    if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
        logger.info("OpenRouter rate limit (non-stream) → fallback do Ollama")
        result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
        if isinstance(result, dict):
            return result
        return {"response": str(result)}

    raise last_error if last_error else RuntimeError("OpenRouter call failed")



def _stream_openrouter_once(prompt: str, system: str, model: str,
                            max_tokens: int, temperature: float,
                            meta: dict | None = None):
    """Jedna próba stream dla OpenRouter bez fallbacków modelu."""
    if isinstance(meta, dict):
        meta.setdefault("requested_provider", "openrouter")
        meta.setdefault("requested_model", model)
        meta["provider_used"] = "openrouter"
        meta["model_used"] = model
        meta["fallback_used"] = False
        meta["fallback_reason"] = None

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {_pick_openrouter_key()}",
        "Content-Type": "application/json"
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    last_error = None
    for attempt in range(OPENROUTER_MAX_RETRIES):
        try:
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8", errors="replace")
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data_str)
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                yield token
                            if choice.get("finish_reason"):
                                return
                        except Exception:
                            continue
                return
        except Exception as e:
            last_error = e
            if _is_openrouter_no_endpoints_error(e):
                raise
            if _is_rate_limit_error(e):
                if attempt < OPENROUTER_MAX_RETRIES - 1:
                    wait = _get_retry_after(e) or (1.5 ** attempt)
                    wait = min(wait, 12.0)
                    logger.warning(f"OpenRouter 429 (próba {attempt+1}/{OPENROUTER_MAX_RETRIES}) — czekam {wait:.1f}s")
                    time.sleep(wait)
                continue
            break

    err_msg = str(last_error) if last_error else "nieznany błąd"

    if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
        logger.info("OpenRouter rate limit → fallback do Ollama (OPENROUTER_FALLBACK_TO_OLLAMA=true)")
        if isinstance(meta, dict):
            meta["provider_used"] = "ollama"
            meta["model_used"] = LLM_MODEL
            meta["fallback_used"] = True
            meta["fallback_reason"] = "openrouter_rate_limit"
        ollama_url = _pick_ollama_url().rstrip("/") + "/api/generate"
        ollama_payload = {
            "model": LLM_MODEL,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {"temperature": temperature if temperature is not None else 0.2}
        }
        try:
            with requests.post(ollama_url, json=ollama_payload, stream=True, timeout=300) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        tok = data.get("response", "")
                        if tok:
                            yield tok
                        if data.get("done", False):
                            return
                    except Exception:
                        continue
            return
        except Exception as fb_err:
            yield f"[Błąd fallback na Ollama: {fb_err}]"
            return

    if _is_rate_limit_error(last_error):
        friendly = "[RATE_LIMIT] Przekroczono limit zapytań OpenRouter. Poczekaj chwilę lub przełącz na Ollama w ustawieniach."
        yield friendly
    else:
        yield f"[Błąd OpenRouter streaming: {err_msg}]"



def _call_openrouter(prompt: str, system: str, stream: bool, model: str,
                     max_tokens: int, temperature: float) -> dict | requests.Response:
    candidates = _openrouter_model_candidates(model)

    if stream:
        # Zachowujemy zgodność API: zwracamy surowy Response dla pierwszego działającego modelu.
        last_error = None
        for candidate in candidates:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {_pick_openrouter_key()}",
                "HTTP-Referer": "http://localhost",
                "X-Title": "AI Analiza Dokumentów"
            }
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload = {
                "model": candidate,
                "messages": messages,
                "stream": True,
                "max_tokens": max_tokens,
                "temperature": temperature
            }
            try:
                r = requests.post(url, headers=headers, json=payload, stream=True, timeout=300)
                if r.status_code == 404 and "no endpoints found" in (r.text or "").lower():
                    logger.warning(f"OpenRouter model {candidate} nie ma endpointów — próbuję model zapasowy")
                    continue
                r.raise_for_status()
                return r
            except Exception as e:
                last_error = e
                if _is_openrouter_no_endpoints_error(e):
                    logger.warning(f"OpenRouter model {candidate} nie ma endpointów — próbuję model zapasowy")
                    continue
                raise

        if _is_openrouter_no_endpoints_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            logger.info("OpenRouter no-endpoints → fallback do Ollama")
            return _call_ollama(prompt, system, stream=True, model=LLM_MODEL)

        if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            return _call_ollama(prompt, system, stream=True, model=LLM_MODEL)
        raise last_error if last_error else RuntimeError("OpenRouter call failed")

    last_error = None
    for candidate in candidates:
        try:
            return _call_openrouter_once(prompt, system, candidate, max_tokens, temperature)
        except Exception as e:
            last_error = e
            if _is_openrouter_no_endpoints_error(e):
                logger.warning(f"OpenRouter model {candidate} nie ma endpointów — próbuję model zapasowy")
                continue

            if OPENROUTER_FALLBACK_TO_OLLAMA and getattr(e, "response", None) is not None:
                try:
                    status = e.response.status_code
                except Exception:
                    status = None
                if status == 400:
                    logger.warning("OpenRouter 400 → fallback do Ollama")
                    result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
                    if isinstance(result, dict):
                        return result
                    return {"response": str(result)}
            if _is_rate_limit_error(e):
                # 429 nadal obsługujemy na poziomie wywołania modelu; tutaj nie przełączamy modeli.
                break
            raise

    if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
        logger.info("OpenRouter rate limit (non-stream) → fallback do Ollama")
        result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
        if isinstance(result, dict):
            return result
        return {"response": str(result)}

    if _is_openrouter_no_endpoints_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
        logger.info("OpenRouter no-endpoints (non-stream) → fallback do Ollama")
        result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
        if isinstance(result, dict):
            return result
        return {"response": str(result)}

    raise last_error if last_error else RuntimeError("OpenRouter call failed")


# ---- Qdrant Client (reuse connection) ----
_qdrant_client = None
_qdrant_lock = threading.Lock()


def _check_custom_endpoint_health(url: str, key: str = "", name: str = "") -> dict:
    """Sprawdza czy custom endpoint działa (Gemini, OpenAI-compatible lub Ollama /api/tags)."""
    if not url:
        return {"ok": False, "error": "Brak URL"}
    try:
        if _custom_endpoint_is_gemini(url, name):
            api_key = _gemini_key_from_url_or_value(url, key)
            t0 = time.time()
            ok, message, ms = _test_gemini_api(api_key)
            if ok:
                return {"ok": True, "url": url, "ms": ms, "error": None}
            return {
                "ok": False,
                "url": url,
                "ms": ms or int((time.time() - t0) * 1000),
                "error": message,
            }

        if _custom_endpoint_is_openai(url):
            t0 = time.time()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            r = requests.post(
                _openai_chat_completions_url(url),
                headers=headers,
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": "Say: OK"}],
                    "max_tokens": 5,
                },
                timeout=20,
            )
            ms = int((time.time() - t0) * 1000)
            return {
                "ok": r.status_code == 200,
                "url": url,
                "ms": ms,
                "status": r.status_code,
                "error": None if r.status_code == 200 else (r.text or "")[:200],
            }

        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url_clean = url.rstrip("/")
        req = urllib.request.Request(
            f"{url_clean}/api/tags",
            headers=headers,
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            return {
                "ok": True,
                "url": url,
                "models_available": len(models),
                "error": None
            }
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)[:120]}



def _custom_endpoint_is_gemini(url: str, name: str = "") -> bool:
    """Czy URL/nazwa wskazuje na natywne API Google Gemini."""
    u = (url or "").lower()
    n = (name or "").lower().strip()
    if "generativelanguage.googleapis.com" in u:
        return True
    if n in ("g1.5f", "gemini") or n.startswith("gemini-"):
        return True
    return False



def _gemini_key_from_url_or_value(url: str, key: str = "") -> str:
    """Zwraca klucz Gemini z pola key albo z query stringu endpointu."""
    if key:
        return key.strip()
    try:
        parsed = urllib.parse.urlparse(url or "")
        params = urllib.parse.parse_qs(parsed.query)
        return (params.get("key") or [""])[0].strip()
    except Exception:
        return ""



def _gemini_custom_generate_url(endpoint_url: str, key: str, model: str | None = None,
                                stream: bool = False) -> str:
    """Buduje kanoniczny URL Gemini — ignoruje wycofany model wpisany w custom endpoint URL."""
    method = "streamGenerateContent" if stream else "generateContent"
    effective_model = _normalize_gemini_model(model or GEMINI_MODEL)
    base = f"{GEMINI_API_BASE}/models/{effective_model}:{method}"

    raw = (endpoint_url or "").strip()
    params: dict[str, str] = {}
    if raw:
        parsed = urllib.parse.urlparse(raw.replace("{api_key}", key or ""))
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        params.pop("key", None)

    if stream:
        params["alt"] = "sse"
    query = urllib.parse.urlencode(params)
    return base + (f"?{query}" if query else "")



def _gemini_generate_payload(prompt: str, system: str = "",
                             max_tokens: int = 2000, temperature: float = 0.2) -> dict:
    """Payload JSON dla Gemini generateContent."""
    text = f"{system.strip()}\n\n{prompt}" if system else prompt
    return {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }



def _gemini_response_text(data: dict) -> str:
    """Ekstrahuje tekst z odpowiedzi Gemini generateContent."""
    parts = (
        (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
        or []
    )
    return "".join(str(part.get("text") or "") for part in parts).strip()



def _call_gemini_custom_endpoint(url: str, key: str, prompt: str, system: str,
                                 stream: bool, model: str = "",
                                 max_tokens: int = 2000, temperature: float = 0.2):
    """Natywny Gemini custom endpoint przez POST generateContent."""
    api_key = _gemini_key_from_url_or_value(url, key)
    if not api_key:
        raise RuntimeError("Brak klucza API Gemini")
    req_url = _gemini_custom_generate_url(url, api_key, model or GEMINI_MODEL, stream=stream)
    payload = _gemini_generate_payload(prompt, system, max_tokens, temperature)
    r = _gemini_request_with_retry(
        "POST",
        req_url,
        api_key=api_key,
        json=payload,
        stream=stream,
        timeout=300 if stream else 180,
    )
    if stream:
        return r
    data = r.json()
    return {"response": _gemini_response_text(data), "raw": data}



def _stream_gemini_custom_endpoint(url: str, key: str, prompt: str, system: str,
                                   model: str = "", max_tokens: int = 2000,
                                   temperature: float = 0.2):
    """Generator tokenów z natywnego Gemini streamGenerateContent."""
    try:
        with _call_gemini_custom_endpoint(
            url, key, prompt, system, True, model, max_tokens, temperature
        ) as r:
            if r.status_code == 429:
                yield f"[RATE_LIMIT] {_gemini_error_message(429)}"
                return
            if r.status_code >= 400:
                yield f"[Błąd Gemini custom streaming: {_gemini_error_message(r.status_code, (r.text or '')[:400])}]"
                return
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8", errors="replace")
                if not decoded.startswith("data: "):
                    continue
                try:
                    chunk = json.loads(decoded[6:].strip())
                    text = _gemini_response_text(chunk)
                    if text:
                        yield text
                except Exception:
                    continue
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "Limit zapytań Gemini" in msg:
            yield f"[RATE_LIMIT] {_gemini_error_message(429)}"
        else:
            yield f"[Błąd Gemini custom streaming: {_redact_api_keys(msg)}]"



def _test_gemini_api(api_key: str, model: str | None = None) -> tuple[bool, str, int]:
    """Test Gemini API — weryfikuje model z listy generateContent."""
    if not api_key:
        return False, "Brak klucza API", 0
    effective_model = _normalize_gemini_model(model)
    t0 = time.time()
    try:
        ok, models, list_err = _list_gemini_generate_models(api_key)
        ms = int((time.time() - t0) * 1000)
        if not ok:
            return False, list_err or "Nie udało się pobrać listy modeli", ms
        if effective_model in models:
            return True, "OK", ms
        return False, _gemini_model_hint(api_key, effective_model), ms
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        err = str(exc)
        if "404" in err:
            return False, _gemini_model_hint(api_key, effective_model), ms
        return False, err[:200], ms



def _custom_endpoint_is_openai(url: str) -> bool:
    """Czy URL wskazuje na OpenAI-compatible API (Groq, LM Studio, itp.)."""
    if _custom_endpoint_is_gemini(url):
        return False
    u = (url or "").lower().rstrip("/")
    return "openai/v1" in u or u.endswith("/v1")



def _openai_chat_completions_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"



def _stream_openai_compatible(url: str, key: str, prompt: str, system: str,
                              model: str, max_tokens: int = 2000, temperature: float = 0.2):
    """Generator tokenów z OpenAI-compatible chat/completions (SSE)."""
    chat_url = _openai_chat_completions_url(url)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        with requests.post(chat_url, headers=headers, json=payload, stream=True, timeout=300) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    chunk = json.loads(data_str)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token
                    if choice.get("finish_reason"):
                        return
                except Exception:
                    continue
    except Exception as e:
        yield f"[Błąd OpenAI-compatible streaming: {str(e)}]"



def _call_openai_compatible(url: str, key: str, prompt: str, system: str, stream: bool,
                            model: str, max_tokens: int = 2000, temperature: float = 0.2):
    chat_url = _openai_chat_completions_url(url)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if stream:
        return requests.post(chat_url, headers=headers, json=payload, stream=True, timeout=300)
    r = requests.post(chat_url, headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    return r.json()



def _call_custom_endpoint(url: str, key: str, prompt: str, system: str, stream: bool,
                          model: str = "", max_tokens: int = 2000, temperature: float = 0.2):
    """Custom endpoint: Gemini, OpenAI-compatible albo Ollama."""
    if _custom_endpoint_is_gemini(url):
        return _call_gemini_custom_endpoint(
            url, key, prompt, system, stream, model or GEMINI_MODEL, max_tokens, temperature
        )
    if _custom_endpoint_is_openai(url):
        return _call_openai_compatible(
            url, key, prompt, system, stream, model or LLM_MODEL, max_tokens, temperature
        )
    url_clean = url.rstrip("/") + "/api/generate"
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model or LLM_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": stream,
        "options": {"temperature": 0.2}
    }
    if stream:
        return requests.post(url_clean, json=payload, headers=headers, stream=True, timeout=300)
    else:
        r = requests.post(url_clean, json=payload, headers=headers, timeout=180)
        r.raise_for_status()
        return r.json()



def _is_rate_limit_error(exc: Exception) -> bool:
    """Sprawdza czy wyjątek to 429 Too Many Requests z OpenRouter."""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg or "rate limit" in msg:
        return True
    # requests HTTPError
    if hasattr(exc, "response") and getattr(exc, "response", None) is not None:
        try:
            status = exc.response.status_code
            if status == 429:
                return True
        except Exception:
            pass
    return False



def _get_retry_after(exc: Exception) -> float | None:
    """Próbuje wyciągnąć Retry-After z nagłówków odpowiedzi."""
    try:
        if hasattr(exc, "response") and exc.response is not None:
            ra = exc.response.headers.get("Retry-After")
            if ra:
                return float(ra)
    except Exception:
        pass
    return None



def _redact_sensitive_log_text(text: str) -> str:
    """Redaguje oczywiste sekrety przed wysłaniem logów do chmury."""
    if not text:
        return ""
    patterns = [
        (r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=***REDACTED***"),
        (r"(?i)Bearer\s+\S+", "Bearer ***REDACTED***"),
    ]
    for pattern, repl in patterns:
        text = re.sub(pattern, repl, text)
    return text



def _redact_api_keys(text: str) -> str:
    """Usuwa klucze API z komunikatów błędów przed zwrotem do UI/logiki API."""
    redacted = str(text or "")
    redacted = re.sub(r"([?&]key=)[^&\s\"']+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+", r"\1[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted



def _llm_response_text(result) -> str:
    if isinstance(result, dict):
        return (result.get("response") or "").strip()
    return str(result).strip()



def call_llm(prompt: str, system: str = "", stream: bool = False,
             provider: str | None = None, model: str | None = None,
             max_tokens: int = 2000, temperature: float = 0.2) -> dict | requests.Response:
    """
    Uniwersalna funkcja do wywoływania LLM.
    Zwraca dict dla non-stream lub Response dla stream.
    provider moze byc: 'openrouter', 'ollama', lub custom endpoint ID.
    """
    pool = _load_provider_pool()
    custom_endpoint = None
    if provider:
        for endpoint in pool.get("custom_endpoints", []):
            if endpoint.get("id") == provider and endpoint.get("active", True):
                custom_endpoint = endpoint
                break

    if custom_endpoint:
        return _call_custom_endpoint(
            custom_endpoint.get("url", ""),
            custom_endpoint.get("key", ""),
            prompt, system, stream, model or LLM_MODEL,
            max_tokens, temperature,
        )

    prov = get_llm_provider(provider)

    if prov == "gemini":
        return _call_gemini(
            prompt, system, stream, model or GEMINI_MODEL, max_tokens, temperature
        )

    if prov == "claude":
        return _call_claude(
            prompt, system, stream, model or CLAUDE_MODEL, max_tokens, temperature
        )

    if prov == "openrouter":
        if not OPENROUTER_API_KEY:
            raise RuntimeError("Brak OPENROUTER_API_KEY w .env")
        return _call_openrouter(prompt, system, stream, model or OPENROUTER_MODEL, max_tokens, temperature)
    else:
        return _call_ollama(prompt, system, stream, model or LLM_MODEL, provider=provider)



def _llm_usage_from_result(result) -> dict:
    """Wyciąga usage z wyniku call_llm(); zwraca pusty dict, jeśli provider nie podał metryk."""
    if not isinstance(result, dict):
        return {}

    usage_src = result.get("usage")
    if not isinstance(usage_src, dict):
        raw = result.get("raw")
        if isinstance(raw, dict):
            usage_src = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw

    if not isinstance(usage_src, dict):
        return {}

    def _as_int(value):
        try:
            if value is None:
                return None
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return max(0, int(value))
            text = str(value).strip()
            if not text:
                return None
            return max(0, int(float(text)))
        except Exception:
            return None

    prompt_tokens = _as_int(
        usage_src.get("prompt_tokens")
        or usage_src.get("input_tokens")
        or usage_src.get("prompt_eval_count")
    )
    completion_tokens = _as_int(
        usage_src.get("completion_tokens")
        or usage_src.get("output_tokens")
        or usage_src.get("eval_count")
    )
    total_tokens = _as_int(usage_src.get("total_tokens"))

    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    usage = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    return usage



def stream_llm_tokens(prompt: str, system: str = "",
                      provider: str | None = None, model: str | None = None,
                      max_tokens: int = 2000, temperature: float = 0.2,
                      meta: dict | None = None):
    """
    Generator zwracający kolejne tokeny tekstu z LLM (Ollama, OpenRouter, custom endpoint).
    Używany w streamingowych endpointach.
    provider moze byc: 'openrouter', 'ollama', lub custom endpoint ID.
    """
    pool = _load_provider_pool()
    custom_endpoint = None
    if provider:
        for endpoint in pool.get("custom_endpoints", []):
            if endpoint.get("id") == provider and endpoint.get("active", True):
                custom_endpoint = endpoint
                break

    if custom_endpoint:
        url_base = custom_endpoint.get("url", "")
        if _custom_endpoint_is_gemini(url_base, custom_endpoint.get("name", "")):
            yield from _stream_gemini_custom_endpoint(
                url_base,
                custom_endpoint.get("key", ""),
                prompt, system,
                model or GEMINI_MODEL,
                max_tokens, temperature,
            )
            return
        if _custom_endpoint_is_openai(url_base):
            yield from _stream_openai_compatible(
                url_base,
                custom_endpoint.get("key", ""),
                prompt, system,
                model or LLM_MODEL,
                max_tokens, temperature,
            )
            return
        url = url_base.rstrip("/") + "/api/generate"
        key = custom_endpoint.get("key", "")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model or LLM_MODEL,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {"temperature": temperature}
        }
        try:
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=300) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        token = data.get("response", "")
                        if token:
                            yield token
                        if data.get("done", False):
                            break
                    except Exception:
                        continue
        except Exception as e:
            yield f"[Blad custom endpoint: {str(e)}]"
        return

    prov = get_llm_provider(provider)
    effective_model = model or (OPENROUTER_MODEL if prov == "openrouter" else LLM_MODEL)

    if prov == "gemini":
        yield from _stream_gemini_tokens(
            prompt, system, model or GEMINI_MODEL, max_tokens, temperature
        )
        return

    if prov == "openrouter":
        if not OPENROUTER_API_KEY:
            yield "Błąd: Brak klucza OPENROUTER_API_KEY"
            return
        last_error = None
        for candidate in _openrouter_model_candidates(effective_model):
            try:
                yield from _stream_openrouter_once(
                    prompt, system, candidate, max_tokens, temperature, meta=meta
                )
                if isinstance(meta, dict) and meta.get("provider_used") == "openrouter":
                    meta["model_used"] = meta.get("model_used") or candidate
                    meta["fallback_used"] = bool(candidate != effective_model)
                    if candidate != effective_model and meta.get("fallback_reason") is None:
                        meta["fallback_reason"] = "openrouter_model_unavailable"
                return
            except Exception as e:
                last_error = e
                if _is_openrouter_no_endpoints_error(e):
                    logger.warning(f"OpenRouter model {candidate} nie ma endpointów — próbuję model zapasowy")
                    continue
                raise

        if _is_openrouter_no_endpoints_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            logger.info("OpenRouter no-endpoints → fallback do Ollama")
            if isinstance(meta, dict):
                meta["provider_used"] = "ollama"
                meta["model_used"] = LLM_MODEL
                meta["fallback_used"] = True
                meta["fallback_reason"] = "openrouter_no_endpoints"
            ollama_url = _pick_ollama_url().rstrip("/") + "/api/generate"
            ollama_payload = {
                "model": LLM_MODEL,
                "prompt": prompt,
                "system": system,
                "stream": True,
                "options": {"temperature": temperature if temperature is not None else 0.2}
            }
            try:
                with requests.post(ollama_url, json=ollama_payload, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            tok = data.get("response", "")
                            if tok:
                                yield tok
                            if data.get("done", False):
                                return
                        except Exception:
                            continue
                return
            except Exception as fb_err:
                yield f"[Błąd fallback na Ollama: {fb_err}]"
                return

        if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            logger.info("OpenRouter rate limit → fallback do Ollama (OPENROUTER_FALLBACK_TO_OLLAMA=true)")
            ollama_url = _pick_ollama_url().rstrip("/") + "/api/generate"
            ollama_payload = {
                "model": LLM_MODEL,
                "prompt": prompt,
                "system": system,
                "stream": True,
                "options": {"temperature": temperature if temperature is not None else 0.2}
            }
            try:
                with requests.post(ollama_url, json=ollama_payload, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            tok = data.get("response", "")
                            if tok:
                                yield tok
                            if data.get("done", False):
                                return
                        except Exception:
                            continue
                return
            except Exception as fb_err:
                yield f"[Błąd fallback na Ollama: {fb_err}]"
                return

        if _is_rate_limit_error(last_error):
            friendly = "[RATE_LIMIT] Przekroczono limit zapytań OpenRouter. Poczekaj chwilę lub przełącz na Ollama w ustawieniach."
            yield friendly
        elif last_error is not None:
            yield f"[Błąd OpenRouter streaming: {last_error}]"

    else:
        # Ollama streaming
        url = _ollama_url_for_provider(provider).rstrip("/") + "/api/generate"
        payload = {
            "model": effective_model,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {"temperature": temperature}
        }
        try:
            with requests.post(url, json=payload, stream=True, timeout=300) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        token = data.get("response", "")
                        if token:
                            yield token
                        if data.get("done", False):
                            break
                    except Exception:
                        continue
        except Exception as e:
            yield f"[Błąd Ollama streaming: {str(e)}]"



# ============================================================
# INITIALIZATION
# ============================================================
# llm_client jest zainicjalizowany w app.py poprzez ustawienie
# zmiennych globalnych konfiguracyjnych
