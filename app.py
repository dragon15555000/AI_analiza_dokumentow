#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import urllib.request
import re
import os
import hashlib
import time
import sqlite3
import threading
import subprocess
import platform
from pathlib import Path
from collections import defaultdict
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file
import io
import logging
import requests   # ← dodane dla lepszej obsługi rozłączeń klienta w streamach SSE
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

# Wczytaj .env jeśli istnieje
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Bezpieczne importy opcjonalne
try: import docx
except ImportError: docx = None
try: import pdfplumber
except ImportError: pdfplumber = None
try: import openpyxl
except ImportError: openpyxl = None
try: import pytesseract
except ImportError: pytesseract = None
try: from pdf2image import convert_from_path
except ImportError: convert_from_path = None

app = Flask(__name__)

# === Podstawowe logowanie ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ai_analiza")

# === Walidacja wymaganych zmiennych środowiskowych ===
required_env = ["QDRANT_URL", "QDRANT_KEY"]
missing = [key for key in required_env if not os.environ.get(key)]
if missing:
    logger.critical(f"Brak wymaganych zmiennych środowiskowych: {missing}")
    logger.critical("Upewnij się, że plik .env istnieje i zawiera poprawne wartości.")
    raise RuntimeError(f"Brakujące zmienne środowiskowe: {missing}")

QDRANT_URL        = os.environ["QDRANT_URL"]
QDRANT_KEY        = os.environ["QDRANT_KEY"]
ACTIVE_COLLECTION = os.environ.get("ACTIVE_COLLECTION", "dokumenty")
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL         = os.environ.get("LLM_MODEL", "llama3")

# OpenRouter
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_MODEL_VERIFY = os.environ.get("OPENROUTER_MODEL_VERIFY", "google/gemini-2.0-flash-exp:free")
OPENROUTER_FALLBACK_TO_OLLAMA = os.environ.get("OPENROUTER_FALLBACK_TO_OLLAMA", "false").lower() in ("1", "true", "yes")
try:
    OPENROUTER_MAX_RETRIES = max(1, int(os.environ.get("OPENROUTER_MAX_RETRIES", "3")))
except ValueError:
    logger.warning("Nieprawidłowa wartość OPENROUTER_MAX_RETRIES w .env, używam domyślnej: 3")
    OPENROUTER_MAX_RETRIES = 3

DEFAULT_LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()   # ollama | openrouter

SEARCH_ROOTS      = [p.strip() for p in os.environ.get("SEARCH_ROOTS", "").split(':') if p.strip()]


# ============================================================
# LLM PROVIDER ABSTRACTION (Ollama <-> OpenRouter)
# ============================================================

def get_llm_provider(request_provider: str | None = None) -> str:
    """Zwraca aktywny provider: 'ollama' lub 'openrouter'."""
    if request_provider:
        p = request_provider.lower()
        if p in ("ollama", "openrouter"):
            return p
    return DEFAULT_LLM_PROVIDER


# ---- Rate limit helpers (OpenRouter) ----

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


def call_llm(prompt: str, system: str = "", stream: bool = False,
             provider: str | None = None, model: str | None = None,
             max_tokens: int = 2000, temperature: float = 0.2) -> dict | requests.Response:
    """
    Uniwersalna funkcja do wywoływania LLM.
    Zwraca dict dla non-stream lub Response dla stream.
    """
    prov = get_llm_provider(provider)

    if prov == "openrouter":
        if not OPENROUTER_API_KEY:
            raise RuntimeError("Brak OPENROUTER_API_KEY w .env")
        return _call_openrouter(prompt, system, stream, model or OPENROUTER_MODEL, max_tokens, temperature)
    else:
        return _call_ollama(prompt, system, stream, model or LLM_MODEL)


def stream_llm_tokens(prompt: str, system: str = "",
                      provider: str | None = None, model: str | None = None,
                      max_tokens: int = 2000, temperature: float = 0.2):
    """
    Generator zwracający kolejne tokeny tekstu z LLM (działa dla Ollama i OpenRouter).
    Używany w streamingowych endpointach.
    """
    prov = get_llm_provider(provider)
    effective_model = model or (OPENROUTER_MODEL if prov == "openrouter" else LLM_MODEL)

    if prov == "openrouter":
        if not OPENROUTER_API_KEY:
            yield "Błąd: Brak klucza OPENROUTER_API_KEY"
            return

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": effective_model,
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
                return  # sukces
            except Exception as e:
                last_error = e
                if _is_rate_limit_error(e):
                    if attempt < OPENROUTER_MAX_RETRIES - 1:
                        wait = _get_retry_after(e) or (1.5 ** attempt)
                        wait = min(wait, 12.0)
                        logger.warning(f"OpenRouter 429 (próba {attempt+1}/{OPENROUTER_MAX_RETRIES}) — czekam {wait:.1f}s")
                        time.sleep(wait)
                    continue
                else:
                    # Inny błąd — nie retry'ujemy
                    break

        # Po wyczerpaniu prób
        err_msg = str(last_error) if last_error else "nieznany błąd"

        # Opcjonalny automatyczny fallback na Ollama przy rate limitach OpenRouter
        if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            yield "[FALLBACK] Limit OpenRouter — przełączam na lokalny Ollama...\n\n"
            logger.info("OpenRouter rate limit → fallback do Ollama (OPENROUTER_FALLBACK_TO_OLLAMA=true)")
            # Przepuść przez ścieżkę Ollama
            ollama_url = OLLAMA_URL + "/api/generate"
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

    else:
        # Ollama streaming
        url = OLLAMA_URL + "/api/generate"
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


def _call_ollama(prompt: str, system: str, stream: bool, model: str) -> dict | requests.Response:
    url = OLLAMA_URL + "/api/generate"
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


def _call_openrouter(prompt: str, system: str, stream: bool, model: str,
                     max_tokens: int, temperature: float) -> dict | requests.Response:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "http://localhost",  # opcjonalnie
        "X-Title": "AI Analiza Dokumentów"
    }

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    if stream:
        return requests.post(url, headers=headers, json=payload, stream=True, timeout=300)

    # Non-streaming with retry on 429
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
            if _is_rate_limit_error(e):
                if attempt < OPENROUTER_MAX_RETRIES - 1:
                    wait = _get_retry_after(e) or (1.5 ** attempt)
                    wait = min(wait, 12.0)
                    logger.warning(f"OpenRouter 429 (non-stream, próba {attempt+1}/{OPENROUTER_MAX_RETRIES}) — czekam {wait:.1f}s")
                    time.sleep(wait)
                continue
            else:
                break

    # Final failure
    raise last_error if last_error else RuntimeError("OpenRouter call failed")


# ---- Qdrant Client (reuse connection) ----
_qdrant_client = None
_qdrant_lock = threading.Lock()

def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    with _qdrant_lock:
        if _qdrant_client is None:
            from httpx import Limits
            _qdrant_client = QdrantClient(
                url=QDRANT_URL,
                api_key=QDRANT_KEY,
                timeout=30.0,
                limits=Limits(
                    max_keepalive_connections=5,
                    max_connections=20,
                    keepalive_expiry=30
                )
            )
        return _qdrant_client

# ---- Cache (dokumenty i sugestie) ----
_docs_cache = {"data": None, "ts": 0}
DOCS_CACHE_TTL = 300  # 5 minut
_suggestions_cache = {"data": None, "ts": 0}
SUGGESTIONS_TTL = 1800  # 30 minut

# ---- Cache embeddingów (SQLite) ----
_CACHE_DB_PATH = Path(__file__).parent / "embedding_cache.db"

def _init_embed_cache():
    """Inicjalizuje tabelę w bazie SQLite, jeśli nie istnieje."""
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, vector TEXT)")
        # Policz istniejące wpisy
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM embeddings")
            count = cursor.fetchone()[0]
        logger.info(f"Załadowano cache embeddingów (SQLite): {count} wpisów")
    except Exception as e:
        logger.warning(f"Błąd inicjalizacji cache embeddingów: {e}")

_init_embed_cache()

def _ensure_collection_exists():
    """Tworzy ACTIVE_COLLECTION jeśli nie istnieje (np. świeży lokalny Qdrant)."""
    try:
        from qdrant_client.models import VectorParams, Distance
        client = get_qdrant_client()
        if not client.collection_exists(ACTIVE_COLLECTION):
            client.create_collection(
                ACTIVE_COLLECTION,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE)
            )
            logger.info(f"Kolekcja '{ACTIVE_COLLECTION}' utworzona automatycznie")
    except Exception as e:
        logger.warning(f"Nie udało się sprawdzić/utworzyć kolekcji '{ACTIVE_COLLECTION}': {e}")

_ensure_collection_exists()

def get_embedding(text: str) -> list:
    import hashlib as _hl
    key = _hl.sha256(text[:1500].encode('utf-8', errors='replace')).hexdigest()

    # 1. Sprawdzaj, czy wektor jest w bazie
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            cursor = conn.execute("SELECT vector FROM embeddings WHERE hash = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logger.warning(f"Błąd odczytu cache embeddingów: {e}")

    # 2. Jeśli nie ma w cache, odpytaj Ollamę (z retry przy Connection reset)
    url = OLLAMA_URL + "/api/embeddings"
    payload = {"model": "nomic-embed-text", "prompt": text[:1500]}

    for attempt in range(4):  # max 4 próby
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=45) as r:
                vec = json.loads(r.read().decode("utf-8"))["embedding"]

            # 3. Zapisz nowy wektor natychmiast do bazy SQLite
            try:
                with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
                    conn.execute("INSERT OR REPLACE INTO embeddings (hash, vector) VALUES (?, ?)",
                                 (key, json.dumps(vec)))
            except Exception as e:
                logger.warning(f"Błąd zapisu cache embeddingów: {e}")

            return vec

        except Exception as e:
            if "Connection reset by peer" in str(e) or "104" in str(e):
                wait = (2 ** attempt) * 0.4   # 0.4s, 0.8s, 1.6s, 3.2s
                logger.warning(f"Ollama embedding reset (próba {attempt+1}/4) — czekam {wait:.1f}s...")
                time.sleep(wait)
                continue
            else:
                logger.error(f"Ollama Embedding Error: {e}")
                return [0.0] * 768

    logger.error("Ollama Embedding Error: wszystkie próby nieudane (Connection reset)")
    return [0.0] * 768

def get_embeddings_batch(texts: list, batch_size: int = 6) -> list:
    """Batch embeddings z mniejszą równoległością (domyślnie 6 zamiast 8), żeby mniej obciążać Ollamę."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [None] * len(texts)

    def embed_one(idx_text):
        idx, text = idx_text
        return idx, get_embedding(text)

    with ThreadPoolExecutor(max_workers=batch_size) as ex:
        futures = {ex.submit(embed_one, (i, t)): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            try:
                idx, vec = fut.result()
                results[idx] = vec
            except Exception as e:
                logger.error(f"Batch embedding error: {e}")
                results[futures[fut]] = [0.0] * 768

    return results

SEARCH_MODES = {
    "normal": {
        "label": "Standardowy",
        "system": (
            "Jesteś precyzyjnym asystentem analityczno-śledczym. Odpowiadaj zawsze po polsku, "
            "krótko, konkretnie i wyłącznie na podstawie dostarczonych dokumentów. "
            "Jeśli w dokumentach znajdują się liczby, kwoty, nazwy firm, nazwiska lub paragrafy, podaj je w pierwszej kolejności."
        ),
        "prompt_suffix": "Podaj zwięzłą syntezę dowodów:"
    },
    "detective": {
        "label": "Detektyw — anomalie",
        "system": (
            "Jesteś analitykiem śledczym specjalizującym się w wykrywaniu nadużyć finansowych i korupcji. "
            "Szukasz ANOMALII, NIESPÓJNOŚCI i PODEJRZANYCH WZORCÓW między dokumentami. "
            "Porównaj dane z różnych źródeł. Wskazuj konkretne rozbieżności: różne kwoty dla tej samej pozycji, "
            "sprzeczne daty, podejrzane zbieżności, brakujące dokumenty. "
            "Każde znalezisko oznacz: [ANOMALIA], [NIESPÓJNOŚĆ], [PODEJRZANE], [WYMAGA SPRAWDZENIA]. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Wskaż anomalie, niespójności i miejsca wymagające sprawdzenia:"
    },
    "legal": {
        "label": "Prawny — przepisy",
        "system": (
            "Jesteś prawnikiem specjalizującym się w prawie zamówień publicznych i spółkach komunalnych. "
            "Identyfikuj każde odwołanie do ustaw, rozporządzeń i przepisów w dokumentach. "
            "Dla każdego przepisu oceń: (1) czy jest aktualny na dzień dokumentu, "
            "(2) czy rzeczywiście dotyczy tej organizacji / branży i kontekstu sprawy, "
            "(3) czy jest zastosowany prawidłowo w kontekście. "
            "Flaguj błędy: [PRZEPIS NIEAKTUALNY], [PRZEPIS NIEADEKWATNY], [BŁĘDNE ZASTOSOWANIE], [PRZEPIS NIEZGODNY]. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Oceń prawidłowość powołanych przepisów prawnych:"
    },
    "inconsistency": {
        "label": "Niespójności",
        "system": (
            "Jesteś audytorem dokumentacji. Szukasz SPRZECZNOŚCI i NIESPÓJNOŚCI w treści dokumentów. "
            "Gdzie ta sama liczba, fakt, data lub stwierdzenie pojawia się inaczej w różnych dokumentach? "
            "Format odpowiedzi: 'Dokument A twierdzi: [X]. Dokument B twierdzi: [Y]. SPRZECZNOŚĆ: [opis].' "
            "Wskazuj też wewnętrzne niespójności w jednym dokumencie. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Znajdź sprzeczności i niespójności między dokumentami:"
    },
    "extract": {
        "label": "Ekstrakcja danych",
        "system": (
            "Jesteś ekstrakatorem danych strukturalnych. Z dokumentów wyciągasz ustrukturyzowane fakty. "
            "Zwróć WYŁĄCZNIE tabelę Markdown z kolumnami: | Typ | Wartość | Dokument | Kontekst |. "
            "Typy: KWOTA, DATA, OSOBA, FIRMA, UMOWA, PARAGRAF, UCHWAŁA, KARA, PRZETARG, INNE. "
            "Każdy znaleziony fakt to osobny wiersz. Minimum 5 wierszy jeśli dane pozwalają. "
            "Odpowiadaj wyłącznie po polsku. Nie pisz nic poza tabelą."
        ),
        "prompt_suffix": "Wyciągnij ustrukturyzowane dane z dokumentów jako tabela Markdown:"
    }
}

def generate_answer(query: str, contexts: list, mode: str = "normal",
                    provider: str | None = None, model: str | None = None) -> str:
    """Generuje odpowiedź używając wybranego providera (ollama lub openrouter)."""
    context_str = "\n\n".join([f"[Dokument: {c['file']}]: {c['text'][:1400]}" for c in contexts])
    cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])
    prompt = f"KONTEKST Z DOKUMENTÓW:\n{context_str}\n\nZAPYTANIE: {query}\n\n{cfg['prompt_suffix']}"

    try:
        result = call_llm(
            prompt=prompt,
            system=cfg["system"],
            stream=False,
            provider=provider,
            model=model
        )
        if isinstance(result, dict):
            return result.get("response", str(result))
        return str(result)
    except Exception as e:
        prov = get_llm_provider(provider)
        if _is_rate_limit_error(e) and OPENROUTER_FALLBACK_TO_OLLAMA and prov == "openrouter":
            logger.warning("generate_answer: OpenRouter 429 → fallback do Ollama")
            try:
                result = _call_ollama(prompt, cfg["system"], stream=False, model=LLM_MODEL)
                if isinstance(result, dict):
                    return result.get("response", str(result))
                return str(result)
            except Exception as fb_e:
                return f"[RATE_LIMIT + Błąd fallback] {fb_e}"
        logger.error(f"LLM error in generate_answer ({prov}): {e}")
        if _is_rate_limit_error(e):
            return "[RATE_LIMIT] Przekroczono limit OpenRouter. Spróbuj później lub użyj Ollama."
        return f"Błąd syntezy LLM ({prov}): {e}"

def verify_answer(answer: str, contexts: list, query: str, provider: str | None = None, model: str | None = None) -> dict:
    """Krytyk — używa wybranego providera (call_llm)."""
    context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:1000]}" for c in contexts])
    system = (
        "Jesteś rygorystycznym weryfikatorem faktów śledczych. Twoja rola to KRYTYCZNA OCENA odpowiedzi "
        "innego asystenta. Masz dostęp do oryginalnych dokumentów — to jedyne źródło prawdy. "
        "NIE ufasz odpowiedzi asystenta — sprawdzasz każde twierdzenie. "
        "Odpowiadaj wyłącznie po polsku. Bądź precyzyjny i bezlitosny wobec nieścisłości."
    )
    prompt = (
        f"ORYGINALNE DOKUMENTY (źródło prawdy):\n{context_str}\n\n"
        f"ZAPYTANIE UŻYTKOWNIKA: {query}\n\n"
        f"ODPOWIEDŹ ASYSTENTA DO WERYFIKACJI:\n{answer}\n\n"
        "Zadanie: sprawdź KAŻDE twierdzenie faktyczne w odpowiedzi asystenta.\n"
        "Format obowiązkowy — każde twierdzenie w osobnej linii:\n"
        "✓ [POTWIERDZONE] <twierdzenie> → <cytat z dokumentu>\n"
        "⚠ [CZĘŚCIOWE] <twierdzenie> → <co jest nieprecyzyjne>\n"
        "✗ [BRAK PODSTAW] <twierdzenie> → <czego brak w dokumentach>\n\n"
        "Na końcu jedna linia:\n"
        "WERDYKT: WIARYGODNA | CZĘŚCIOWO WIARYGODNA | ZAWIERA HALUCYNACJE\n"
        "UZASADNIENIE: <jedno zdanie>\n\n"
        "Weryfikacja:"
    )

    try:
        # Używamy dedykowanego modelu do weryfikacji (jeśli ustawiony), żeby nie dobić limitu tego samego modelu co główna odpowiedź
        effective_verify_model = model
        if not effective_verify_model and get_llm_provider(provider) == "openrouter":
            effective_verify_model = OPENROUTER_MODEL_VERIFY
        result = call_llm(prompt=prompt, system=system, stream=False, provider=provider, model=effective_verify_model)
        raw = result.get("response", str(result)) if isinstance(result, dict) else str(result)

        # Wyciągnij werdykt
        verdict = "NIEOKREŚLONY"
        justification = ""
        for line in raw.splitlines():
            l = line.strip()
            if l.startswith("WERDYKT:"):
                verdict = l.replace("WERDYKT:", "").strip()
            if l.startswith("UZASADNIENIE:"):
                justification = l.replace("UZASADNIENIE:", "").strip()

        confirmed = raw.count("✓")
        partial    = raw.count("⚠")
        hallucin   = raw.count("✗")
        total = confirmed + partial + hallucin
        confidence_pct = round(confirmed / total * 100) if total > 0 else None

        return {
            "success": True,
            "raw": raw,
            "verdict": verdict,
            "justification": justification,
            "confirmed": confirmed,
            "partial": partial,
            "hallucinations": hallucin,
            "confidence_pct": confidence_pct
        }
    except urllib.error.URLError as e:
        logger.error(f"LLM connection error (verify_answer): {e}")
        return {"success": False, "error": "Błąd połączenia z weryfikatorem (Ollama)."}
    except Exception as e:
        logger.error(f"LLM error in verify_answer: {e}")
        return {"success": False, "error": str(e)}

@app.route('/verify', methods=['POST'])
def verify_endpoint():
    data = request.get_json()
    answer   = data.get('answer', '').strip()
    query    = data.get('query', '').strip()
    contexts = data.get('contexts', [])
    llm_provider = data.get('llm_provider')
    openrouter_model = data.get('openrouter_model')
    if not answer or not query or not contexts:
        return jsonify({"success": False, "error": "Brak danych do weryfikacji"})
    # Przekazujemy provider, żeby weryfikacja też szła przez wybrany model (w tym osobny model_verify)
    result = verify_answer(answer, contexts, query, provider=llm_provider, model=openrouter_model)
    return jsonify(result)

def highlight_backend(text: str, query: str) -> str:
    if not query: return text
    words = [w.strip() for w in query.split() if len(w.strip()) > 2]
    if not words: return text
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    for word in words:
        clean_word = re.sub(r'[.,\/#!$%\^&\*;:{}=\-_`~()]', '', word)
        root = clean_word[:-2] if len(clean_word) > 4 else clean_word
        if not root: continue
        try:
            pattern = re.compile(rf"({root}[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]*)", re.IGNORECASE)
            escaped = pattern.sub(r"<mark>\1</mark>", escaped)
        except: continue
    return escaped

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

def make_chunks(text: str) -> list:
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        # Zakończ na granicy zdania jeśli możliwe (ostatnia kropka/newline w ogonie)
        if end < len(text):
            for sep in ('. ', '.\n', '\n\n', '\n', ' '):
                cut = chunk.rfind(sep, CHUNK_SIZE // 2)
                if cut != -1:
                    chunk = chunk[:cut + len(sep)]
                    break
        if chunk.strip():
            chunks.append(chunk)
        start += max(len(chunk) - CHUNK_OVERLAP, step)
    return chunks

def _extract_xls(file_path: Path) -> str:
    """Parser starego formatu .xls — czyta też ukryte wiersze i kolumny."""
    try:
        import xlrd
        wb = xlrd.open_workbook(str(file_path), formatting_info=True)
        parts = []
        for sheet in wb.sheets():
            if sheet.nrows == 0:
                continue

            # Wykryj ukryte wiersze i kolumny
            hidden_rows = set()
            hidden_cols = set()
            try:
                for ri in range(sheet.nrows):
                    ri_obj = sheet.rowinfo_map.get(ri)
                    if ri_obj and ri_obj.hidden:
                        hidden_rows.add(ri)
            except Exception:
                pass
            try:
                for ci in range(sheet.ncols):
                    ci_obj = sheet.colinfo_map.get(ci)
                    if ci_obj and ci_obj.hidden:
                        hidden_cols.add(ci)
            except Exception:
                pass

            hidden_info = []
            if hidden_rows: hidden_info.append(f"{len(hidden_rows)} ukrytych wierszy")
            if hidden_cols: hidden_info.append(f"{len(hidden_cols)} ukrytych kolumn")

            lines = [f"\n=== Arkusz: {sheet.name}"
                     + (f" [UWAGA: {', '.join(hidden_info)}]" if hidden_info else "") + " ==="]

            # Nagłówki
            headers = []
            header_row = -1
            for ri in range(min(5, sheet.nrows)):
                row = [str(sheet.cell_value(ri, ci)).strip() for ci in range(sheet.ncols)]
                if len([v for v in row if v and v != '0.0']) >= 2:
                    headers = row
                    lines.append("Nagłówki: " + " | ".join(h for h in headers if h))
                    header_row = ri
                    break

            for ri in range(sheet.nrows):
                if ri == header_row:
                    continue
                is_hidden_row = ri in hidden_rows
                cells = []
                for ci in range(sheet.ncols):
                    val = sheet.cell_value(ri, ci)
                    if val == '' or val is None:
                        continue
                    ctype = sheet.cell_type(ri, ci)
                    if ctype == xlrd.XL_CELL_DATE:
                        try:
                            val = xlrd.xldate_as_datetime(val, wb.datemode).strftime('%Y-%m-%d')
                        except Exception:
                            pass
                    elif ctype == xlrd.XL_CELL_NUMBER and float(val) == int(val) and abs(val) < 1e12:
                        val = int(val)
                    hdr = headers[ci] if ci < len(headers) and headers[ci] else f"Kol{ci+1}"
                    hidden_col_mark = " [UKRYTA_KOL]" if ci in hidden_cols else ""
                    cells.append(f"{hdr}{hidden_col_mark}: {val}")

                if cells:
                    prefix = "[UKRYTY_WIERSZ] " if is_hidden_row else ""
                    lines.append(prefix + " | ".join(cells))

            parts.append("\n".join(lines))
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Błąd parsowania .xls {file_path.name}: {e}")
        return ""

def _extract_excel(file_path: Path) -> str:
    # Stary format — użyj xlrd
    if file_path.suffix.lower() == '.xls':
        return _extract_xls(file_path)
    parts = []
    # Dwa odczyty: raz z formułami, raz z wartościami (cache Excel)
    try:
        wb_vals = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    except Exception:
        wb_vals = None
    try:
        wb_form = openpyxl.load_workbook(file_path, data_only=False, read_only=True)
    except Exception:
        wb_form = None

    if not wb_vals and not wb_form:
        return ""

    sheet_names = (wb_vals or wb_form).sheetnames

    for sheet_name in sheet_names:
        ws_v = wb_vals[sheet_name]  if wb_vals and sheet_name in (wb_vals.sheetnames)  else None
        ws_f = wb_form[sheet_name]  if wb_form and sheet_name in (wb_form.sheetnames)  else None
        if not ws_v and not ws_f:
            continue

        rows_v = list(ws_v.iter_rows(values_only=True))        if ws_v else []
        rows_f = list(ws_f.iter_rows(values_only=False))       if ws_f else []
        if not rows_v and not rows_f:
            continue

        # Wykryj ukryte wiersze i kolumny (tylko bez read_only)
        hidden_rows = set()
        hidden_cols = set()
        try:
            ws_meta = openpyxl.load_workbook(file_path, data_only=True)[sheet_name]
            for ri_h, rd in ws_meta.row_dimensions.items():
                if rd.hidden:
                    hidden_rows.add(ri_h - 1)  # 0-based
            for col_letter, cd in ws_meta.column_dimensions.items():
                if cd.hidden:
                    from openpyxl.utils import column_index_from_string
                    hidden_cols.add(column_index_from_string(col_letter) - 1)
        except Exception:
            pass

        hidden_info = []
        if hidden_rows: hidden_info.append(f"{len(hidden_rows)} ukrytych wierszy")
        if hidden_cols: hidden_info.append(f"{len(hidden_cols)} ukrytych kolumn")

        # Nagłówki kolumn — pierwsza niepusta linia z wartościami tekstowymi
        headers = []
        header_row_idx = -1
        source_rows = rows_v or [list(r) for r in rows_f]
        for ri_h, row in enumerate(source_rows[:5]):
            if sum(1 for c in row if c is not None and str(c).strip()) >= 2:
                headers = [str(c).strip() if c is not None else f"Kol{i+1}"
                           for i, c in enumerate(row)]
                header_row_idx = ri_h
                break

        lines = [f"\n=== Arkusz: {sheet_name}"
                 + (f" [UWAGA: {', '.join(hidden_info)}]" if hidden_info else "") + " ==="]
        if headers:
            lines.append("Nagłówki: " + " | ".join(headers))

        for ri, row_v in enumerate(rows_v):
            if ri == header_row_idx:
                continue
            vals = [v for v in row_v if v is not None and str(v).strip()]
            if not vals:
                continue

            is_hidden = ri in hidden_rows
            cells = []
            for ci, val in enumerate(row_v):
                if val is None:
                    continue
                hdr = headers[ci] if ci < len(headers) else f"Kol{ci+1}"
                hidden_col_mark = " [UKRYTA_KOL]" if ci in hidden_cols else ""

                formula = None
                if rows_f and ri < len(rows_f):
                    rf_row = rows_f[ri]
                    if ci < len(rf_row):
                        fval = rf_row[ci].value if hasattr(rf_row[ci], 'value') else None
                        if isinstance(fval, str) and fval.startswith('='):
                            formula = fval

                if formula:
                    cells.append(f"{hdr}{hidden_col_mark}: {val} [wzór: {formula}]")
                else:
                    cells.append(f"{hdr}{hidden_col_mark}: {val}")

            if cells:
                prefix = "[UKRYTY_WIERSZ] " if is_hidden else ""
                lines.append(prefix + " | ".join(cells))

        # Jeśli data_only zwrócił same None (plik niezapisany przez Excel),
        # fallback: odczytaj formuły bezpośrednio
        data_lines = len([l for l in lines if l.startswith("Kol") or ": " in l])
        if data_lines == 0 and rows_f:
            for rf_row in rows_f:
                cells = []
                for ci, cell in enumerate(rf_row):
                    v = cell.value if hasattr(cell, 'value') else None
                    if v is None:
                        continue
                    hdr = headers[ci] if ci < len(headers) else f"Kol{ci+1}"
                    cells.append(f"{hdr}: {v}")
                if cells:
                    lines.append(" | ".join(cells))

        parts.append("\n".join(lines))

    if wb_vals:
        try: wb_vals.close()
        except Exception: pass
    if wb_form:
        try: wb_form.close()
        except Exception: pass

    return "\n\n".join(parts)


def extract_file_metadata(f_path: Path) -> dict:
    """Zbiera bogate metadane pliku (systemowe + formatowe) do celów śledczych."""
    meta = {
        "file_name": f_path.name,
        "full_path": str(f_path),
        "size_bytes": None,
        "size_human": None,
        "created": None,
        "modified": None,
        "accessed": None,
        "file_type": f_path.suffix.lower().lstrip('.'),
        "embedded": {}
    }

    try:
        stat = f_path.stat()
        meta["size_bytes"] = stat.st_size
        meta["size_human"] = f"{stat.st_size / 1024:.1f} KB" if stat.st_size < 10*1024*1024 else f"{stat.st_size / (1024*1024):.2f} MB"

        # Czas modyfikacji jest wiarygodny wszędzie
        meta["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))

        # Czas dostępu
        if hasattr(stat, "st_atime"):
            meta["accessed"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_atime))

        # Czas utworzenia — Windows ma st_ctime jako creation time, Unix ma jako change time
        if os.name == 'nt' and hasattr(stat, "st_ctime"):
            meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime))
        else:
            # Na Linux/WSL często nie mamy wiarygodnego created time z filesystemu
            meta["created"] = None

    except Exception as e:
        logger.warning(f"Błąd pobierania stat dla {f_path.name}: {e}")

    # === Metadane wbudowane w pliki ===
    ext = f_path.suffix.lower()

    # PDF
    if ext == '.pdf' and pdfplumber:
        try:
            with pdfplumber.open(f_path) as pdf:
                if pdf.metadata:
                    meta["embedded"]["pdf"] = {k: str(v) for k, v in pdf.metadata.items() if v}
        except Exception:
            pass

    # DOCX
    if ext == '.docx' and docx:
        try:
            d = docx.Document(f_path)
            core_props = d.core_properties
            embedded = {}
            if core_props.author: embedded["author"] = core_props.author
            if core_props.title: embedded["title"] = core_props.title
            if core_props.last_modified_by: embedded["last_modified_by"] = core_props.last_modified_by
            if core_props.created: embedded["created"] = str(core_props.created)
            if core_props.modified: embedded["modified"] = str(core_props.modified)
            if embedded:
                meta["embedded"]["docx"] = embedded
        except Exception:
            pass

    # XLSX / XLS
    if ext in ['.xlsx', '.xls'] and openpyxl:
        try:
            wb = openpyxl.load_workbook(f_path, data_only=True)
            props = wb.properties
            embedded = {}
            if props.creator: embedded["author"] = props.creator
            if props.title: embedded["title"] = props.title
            if props.lastModifiedBy: embedded["last_modified_by"] = props.lastModifiedBy
            if props.created: embedded["created"] = str(props.created)
            if props.modified: embedded["modified"] = str(props.modified)
            if embedded:
                meta["embedded"]["excel"] = embedded
            wb.close()
        except Exception:
            pass

    return meta


def extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    try:
        if ext in ['.md', '.txt']:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        elif ext == '.json':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return json.dumps(json.load(f), indent=2, ensure_ascii=False)
        elif ext == '.docx' and docx:
            return "\n".join([p.text for p in docx.Document(file_path).paragraphs])
        
        elif ext == '.pdf':
            text = ""
            # Próba 1: Zwykłe wyciągnięcie tekstu cyfrowego
            if pdfplumber:
                with pdfplumber.open(file_path) as pdf:
                    text = "\n".join([page.extract_text() or "" for page in pdf.pages])
            
            # Próba 2 (Fallback OCR): Jeżeli plik to skan (brak tekstu), uruchom Tesseract
            if len(text.strip()) < 20 and pytesseract and convert_from_path:
                logger.info(f"[OCR] Analiza wizualna pliku: {file_path.name}...")
                try:
                    pages = convert_from_path(file_path, dpi=200)
                    ocr_text = []
                    for page_img in pages:
                        page_text = pytesseract.image_to_string(page_img, lang='pol')
                        ocr_text.append(page_text)
                    text = "\n\n".join(ocr_text)
                    logger.info(f"[OCR] Zakończono dla: {file_path.name} (Pobrano znaków: {len(text)})")
                except Exception as ocr_err:
                    logger.warning(f"Błąd OCR dla pliku {file_path.name}: {ocr_err}")
            
            return text

        # Obsługa obrazów bezpośrednio (jpg, png, tiff itp.) przez OCR
        elif ext in ['.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp'] and pytesseract:
            if convert_from_path:
                # pdf2image może obsłużyć niektóre obrazy, ale dla pewności używamy PIL jeśli dostępna
                try:
                    from PIL import Image
                    img = Image.open(file_path)
                    text = pytesseract.image_to_string(img, lang='pol')
                    logger.info(f"[OCR] Przetworzono obraz: {file_path.name}")
                    return text
                except ImportError:
                    pass  # fallback poniżej
            # Fallback - spróbuj przez pdf2image jeśli PIL nie ma
            if convert_from_path:
                try:
                    pages = convert_from_path(file_path, dpi=200)
                    ocr_text = []
                    for page_img in pages:
                        page_text = pytesseract.image_to_string(page_img, lang='pol')
                        ocr_text.append(page_text)
                    text = "\n\n".join(ocr_text)
                    logger.info(f"[OCR] Przetworzono obraz przez pdf2image: {file_path.name}")
                    return text
                except Exception as ocr_err:
                    logger.warning(f"Błąd OCR obrazu {file_path.name}: {ocr_err}")
            return ""

        elif ext in ['.xlsx', '.xls'] and openpyxl:
            return _extract_excel(file_path)
        elif ext == '.csv':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Błąd parsowania pliku {file_path.name}: {e}")
    return ""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        client = get_qdrant_client()
        info = client.get_collection(collection_name=ACTIVE_COLLECTION)
        return jsonify({
            "success": True,
            "vectors_count": getattr(info, 'vectors_count', None) or info.points_count,
            "points_count": info.points_count,
            "status": info.status,
            "active_collection": ACTIVE_COLLECTION
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/stats/storage', methods=['GET'])
def stats_storage():
    global ACTIVE_COLLECTION
    try:
        client = get_qdrant_client()
        cols = client.get_collections().collections
        result = []
        total_vectors = 0
        total_points  = 0

        for c in cols:
            info = client.get_collection(c.name)
            pts   = info.points_count or 0
            vsize = info.config.params.vectors.size if info.config.params.vectors else 768
            # Szacunek: wektory (float32) + payload (avg 900 B/chunk) + indeks HNSW (~20%)
            vec_bytes     = pts * vsize * 4
            payload_bytes = pts * 900
            index_bytes   = int(vec_bytes * 0.20)
            total_bytes   = vec_bytes + payload_bytes + index_bytes

            result.append({
                "name":    c.name,
                "points":  pts,
                "indexed": info.indexed_vectors_count or 0,
                "status":  str(info.status),
                "vector_size": vsize,
                "distance": str(info.config.params.vectors.distance) if info.config.params.vectors else "Cosine",
                "est_mb":  round(total_bytes / 1_048_576, 2),
                "active":  c.name == ACTIVE_COLLECTION
            })
            total_vectors += pts
            total_points  += pts

        # Free tier Qdrant Cloud: ~4 GB disk / 1 GB RAM
        FREE_DISK_MB = 4096
        used_mb = sum(r["est_mb"] for r in result)
        return jsonify({
            "success": True,
            "collections": result,
            "total_points": total_points,
            "used_mb": round(used_mb, 2),
            "free_disk_mb": FREE_DISK_MB,
            "used_pct": round(used_mb / FREE_DISK_MB * 100, 1),
            "active_collection": ACTIVE_COLLECTION,
            "max_collections": 1000
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/create', methods=['POST'])
def create_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name     = data.get('name', '').strip().replace(' ', '_')
    vec_size = int(data.get('vector_size', 768))
    distance = data.get('distance', 'Cosine').capitalize()
    switch   = data.get('switch_to', False)

    if not name:
        return jsonify({"success": False, "error": "Brak nazwy kolekcji"})
    if not name.replace('_','').replace('-','').isalnum():
        return jsonify({"success": False, "error": "Nazwa może zawierać tylko litery, cyfry, _ i -"})

    try:
        from qdrant_client.models import VectorParams, Distance
        dist_map = {"Cosine": Distance.COSINE, "Euclid": Distance.EUCLID, "Dot": Distance.DOT}
        dist = dist_map.get(distance, Distance.COSINE)

        client = get_qdrant_client()
        if client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' już istnieje"})

        client.create_collection(name, vectors_config=VectorParams(size=vec_size, distance=dist))

        if switch:
            ACTIVE_COLLECTION = name
            _qdrant_client = None  # Reset połączenia po zmianie kolekcji
            _suggestions_cache["data"] = None; _docs_cache["data"] = None

        return jsonify({"success": True, "name": name, "switched": switch, "active": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/switch', methods=['POST'])
def switch_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    try:
        client = get_qdrant_client()
        if not client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' nie istnieje"})
        ACTIVE_COLLECTION = name
        _qdrant_client = None  # Reset połączenia po zmianie kolekcji
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "active_collection": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/delete', methods=['POST'])
def delete_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    if name == ACTIVE_COLLECTION:
        return jsonify({"success": False, "error": "Nie można usunąć aktywnej kolekcji. Najpierw przełącz się na inną."})
    try:
        client = get_qdrant_client()
        client.delete_collection(name)
        _qdrant_client = None  # Reset połączenia po usunięciu kolekcji
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/browse', methods=['GET'])
def browse():
    raw = request.args.get('path', '/mnt').strip()
    show_all = request.args.get('all', '0') == '1'   # ?all=1 → pokaż wszystkie pliki (nie tylko dokumenty)
    p = Path(raw)

    if not p.exists() or not p.is_dir():
        parent = p.parent
        if parent.exists() and parent.is_dir():
            p = parent
        else:
            p = Path('/mnt')

    try:
        entries = []
        total_children = 0
        permission_denied = 0

        for child in sorted(p.iterdir()):
            total_children += 1
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "type": "dir"})
                else:
                    ext = child.suffix.lower().lstrip('.')
                    is_doc = ext in ['docx','pdf','xlsx','xls','csv','md','json','txt']
                    if show_all or is_doc:
                        entries.append({
                            "name": child.name,
                            "path": str(child),
                            "type": "file",
                            "ext": ext
                        })
            except PermissionError:
                permission_denied += 1
                pass

        # Lepsza diagnostyka dla pustych / problematycznych dysków (np. /mnt/g w WSL)
        is_empty = (total_children == 0) or (len(entries) == 0 and permission_denied == 0)
        has_hidden_or_other = (total_children > 0) and (len(entries) == 0) and (permission_denied == 0)

        return jsonify({
            "success": True,
            "current": str(p),
            "parent": str(p.parent) if p != p.parent else None,
            "entries": entries,
            "is_empty": is_empty,
            "total_children": total_children,
            "permission_denied": permission_denied,
            "has_unlisted_items": has_hidden_or_other or permission_denied > 0,
            "show_all": show_all
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============================================================
# AUTOMATYCZNE MAPOWANIE DYSKÓW LOKALNYCH (dla zakładki Import)
# ============================================================

def _discover_local_drives():
    """Zwraca listę sensownych punktów startowych (dysków/mountów) bez dodatkowych zależności."""
    import string
    drives = []
    sysname = platform.system()
    home = str(Path.home())

    try:
        if sysname == "Windows":
            for letter in string.ascii_uppercase:
                p = Path(f"{letter}:\\")
                if p.exists():
                    drives.append({
                        "path": str(p),
                        "label": f"{letter}:",
                        "kind": "drive",
                        "icon": "💾"
                    })
        else:
            # Linux / WSL / macOS
            candidates = ['/', home, '/mnt', '/media', '/data']
            for c in candidates:
                pp = Path(c)
                if pp.exists() and pp.is_dir():
                    label = "🏠 Home" if c == home else c
                    drives.append({"path": str(pp), "label": label, "kind": "dir", "icon": "📁" if c != home else "🏠"})

            # WSL — dyski Windows widoczne jako /mnt/c, /mnt/d itd.
            mnt = Path("/mnt")
            if mnt.exists():
                for child in sorted(mnt.iterdir()):
                    if child.is_dir() and len(child.name) == 1:
                        letter = child.name.upper()
                        drives.append({
                            "path": str(child),
                            "label": f"{letter}: (Windows)",
                            "kind": "wsl",
                            "icon": "🪟"
                        })

            # Spróbuj wyciągnąć prawdziwe montowania z /proc/mounts (najlepszy wysiłek)
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) < 3:
                            continue
                        dev, mp, fstype = parts[0], parts[1], parts[2]
                        # Tylko sensowne systemy plików dyskowych
                        if fstype in ("ext4", "ext3", "xfs", "btrfs", "ntfs", "fuseblk", "vfat") and \
                           mp not in ("/", "/boot", "/boot/efi", "/proc", "/sys", "/dev", "/run"):
                            if len(mp) <= 24 and not any(x in mp for x in ("/snap/", "/docker/", "/tmp/")):
                                if not any(d["path"] == mp for d in drives):
                                    drives.append({"path": mp, "label": mp, "kind": "mount", "icon": "💿"})
            except Exception:
                pass
    except Exception:
        pass

    # Zawsze dodaj Home jeśli nie ma
    if not any(d["path"] == home for d in drives):
        drives.insert(0, {"path": home, "label": "🏠 Home", "kind": "dir", "icon": "🏠"})

    # Deduplikacja + limit
    seen = set()
    out = []
    for d in drives:
        if d["path"] not in seen:
            seen.add(d["path"])
            out.append(d)
    return out[:14]


@app.route('/api/drives')
def api_drives():
    try:
        drives = _discover_local_drives()
        return jsonify({
            "success": True,
            "platform": platform.system(),
            "drives": drives
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/import/stream')
def import_stream():
    folder = request.args.get('folder', '').strip()
    exts = request.args.getlist('ext') or ['docx','pdf','xlsx','xls','csv','md','json']

    def generate():
        def sse(event, data):
            import json as _json
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        folder_path = Path(folder)
        if not folder or not folder_path.exists():
            yield sse("error", {"msg": f"Ścieżka nie istnieje: {folder}"})
            return

        ext_pattern = " ".join([f'-name "*.{e}"' for e in exts])
        or_pattern = " -o ".join([f'-name "*.{e}"' for e in exts])
        cmd = f'find "{folder_path}" -type f \\( {or_pattern} \\)'
        files = [Path(l.strip()) for l in os.popen(cmd).readlines() if l.strip()]

        if not files:
            yield sse("done", {"count": 0, "chunks": 0, "skipped": 0, "msg": "Brak kompatybilnych plików."})
            return

        yield sse("start", {"total": len(files)})

        qdrant = get_qdrant_client()
        imported = 0
        skipped = 0
        total_chunks = 0
        new_chunks = 0

        for i, f_path in enumerate(files):
            try:
                text = extract_text(f_path)
                if not text or len(text.strip()) < 10:
                    skipped += 1
                    yield sse("skip", {"file": f_path.name, "reason": "pusty", "i": i+1, "total": len(files)})
                    continue

                chunks = make_chunks(text)
                file_new = 0

                # Metadane pobieramy TYLKO RAZ na plik (nie w pętli batchy — ogromna oszczędność IO)
                file_meta = extract_file_metadata(f_path)

                # Deduplikacja — sprawdź które chunki już są w bazie
                chunk_ids = [hashlib.md5(c.encode('utf-8', errors='replace')).hexdigest() for c in chunks]
                existing_ids = set()
                for batch_start in range(0, len(chunk_ids), 100):
                    batch = chunk_ids[batch_start:batch_start+100]
                    found = qdrant.retrieve(collection_name=ACTIVE_COLLECTION, ids=batch,
                                           with_payload=False, with_vectors=False)
                    existing_ids.update(p.id for p in found)

                new_chunks_data = [(cid, chunk) for cid, chunk in zip(chunk_ids, chunks)
                                   if cid not in existing_ids]
                total_chunks += len(chunks)

                # Batch embeddings — 6 równolegle (zgodne z get_embeddings_batch default, żeby nie obciążać Ollamy)
                BATCH = 6
                for b in range(0, len(new_chunks_data), BATCH):
                    batch_items = new_chunks_data[b:b+BATCH]
                    batch_texts  = [item[1] for item in batch_items]
                    batch_ids    = [item[0] for item in batch_items]
                    vectors = get_embeddings_batch(batch_texts, batch_size=BATCH)

                    points  = [
                        PointStruct(
                            id=cid,
                            vector=vec,
                            payload={
                                "file": f_path.name,
                                "text": txt,
                                "full_path": str(f_path),
                                "metadata": file_meta
                            }
                        )
                        for cid, vec, txt in zip(batch_ids, vectors, batch_texts)
                        if vec and any(v != 0.0 for v in vec)
                    ]
                    if points:
                        qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                    new_chunks += len(points)
                    file_new  += len(points)

                imported += 1
                yield sse("file", {
                    "file": f_path.name,
                    "chunks": len(chunks),
                    "new": file_new,
                    "i": i+1,
                    "total": len(files)
                })
                time.sleep(0.02)
            except Exception as e:
                skipped += 1
                yield sse("skip", {"file": f_path.name, "reason": str(e)[:80], "i": i+1, "total": len(files)})

        yield sse("done", {
            "count": imported,
            "chunks": total_chunks,
            "new_chunks": new_chunks,
            "skipped": skipped,
            "msg": f"Przetworzono {imported} plików · {new_chunks} nowych chunków · {total_chunks - new_chunks} duplikatów pominiętych"
        })

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/collection/clear', methods=['POST'])
def clear_collection():
    try:
        client = get_qdrant_client()
        info = client.get_collection(ACTIVE_COLLECTION)
        count_before = info.points_count
        # Pobierz aktualny wymiar wektora zanim usuniesz kolekcję
        vsize = info.config.params.vectors.size if info.config.params.vectors else 768
        from qdrant_client.models import VectorParams, Distance
        client.delete_collection(ACTIVE_COLLECTION)
        client.create_collection(
            ACTIVE_COLLECTION,
            vectors_config=VectorParams(size=vsize, distance=Distance.COSINE)
        )
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "deleted": count_before})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/import', methods=['POST'])
def import_folder():
    data = request.get_json()
    return jsonify({"success": False, "error": "Użyj /import/stream (SSE)"})

def wsl_to_win(path: str) -> str:
    """Konwertuje ścieżkę WSL /mnt/g/... na Windows G:\\..."""
    if not path:
        return ""
    m = re.match(r'^/mnt/([a-zA-Z])/(.*)', path)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace('/', '\\')
        return f"{drive}:\\{rest}"
    return path


def open_file_safely(win_path: str) -> bool:
    """
    Bezpiecznie otwiera plik za pomocą domyślnej aplikacji systemowej.
    Unika shell injection (w przeciwieństwie do poprzedniego os.popen).
    """
    if not win_path:
        return False

    try:
        if platform.system() == "Windows":
            # Najbezpieczniejsza metoda na Windows
            os.startfile(win_path)
            logger.info(f"Otwarto plik: {win_path}")
            return True
        else:
            # Fallback dla Linux/macOS (przydatne przy testach z WSL)
            subprocess.run(["xdg-open", win_path], check=False)
            logger.info(f"Otwarto plik (xdg-open): {win_path}")
            return True
    except Exception as e:
        logger.warning(f"Nie udało się otworzyć pliku '{win_path}': {e}")
        return False

@app.route('/file/open', methods=['POST'])
def file_open():
    data = request.get_json()
    wsl_path = data.get('path', '').strip()
    if not wsl_path:
        return jsonify({"success": False, "error": "Brak ścieżki"})

    win_path = wsl_to_win(wsl_path)
    if not win_path:
        return jsonify({"success": False, "error": "Nie można skonwertować ścieżki"})

    success = open_file_safely(win_path)

    if success:
        return jsonify({"success": True, "win_path": win_path})
    else:
        return jsonify({"success": False, "error": "Nie udało się otworzyć pliku"})

@app.route('/hybrid/stream', methods=['POST'])
def hybrid_stream():
    """Wyszukiwanie hybrydowe: RAG + SQL równolegle — SSE."""
    data       = request.get_json()
    query_text = data.get('query', '').strip()
    conn_cfg   = data.get('conn', {})
    schema_str = data.get('schema', '')
    limit      = min(int(data.get('limit', 5)), 20)
    mode       = data.get('mode', 'normal')
    file_filter= data.get('file_filter', None)
    llm_provider = data.get('llm_provider')
    openrouter_model = data.get('openrouter_model')

    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"}), 400

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

        try:
            # 1. RAG — Qdrant query
            client = get_qdrant_client()
            vector = get_embedding(query_text)

            if file_filter:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector,
                                          limit=limit, query_filter=qfilter)
            else:
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

            rag_contexts = []
            rag_results = []
            for point in res.points:
                p = point.payload
                rag_contexts.append({"file": p.get("file",""), "text": p.get("text","")})
                rag_results.append({
                    "file": p.get("file","Nieznany"),
                    "score": f"{point.score:.4f}",
                    "text": highlight_backend(p.get("text",""), query_text),
                    "full_path": p.get("full_path",""),
                    "win_path": wsl_to_win(p.get("full_path",""))
                })

            yield sse("rag_results", {"results": rag_results, "contexts": rag_contexts})

            # 2. SQL — jeśli konfiguracja dostępna
            sql_data = {"success": False, "table": "", "columns": [], "rows": [], "sql": ""}
            sql_query = ""

            if conn_cfg and conn_cfg.get("server") and conn_cfg.get("database"):
                try:
                    system_sql = (
                        "Jesteś ekspertem T-SQL (MS SQL Server). "
                        "Na podstawie schematu bazy generujesz zapytania SELECT. "
                        "Odpowiadasz WYŁĄCZNIE samym SQL — bez wyjaśnień, bez markdown."
                    )
                    prompt_sql = (
                        f"SCHEMAT BAZY:\n{schema_str}\n\n"
                        f"PYTANIE: {query_text}\n\n"
                        "Wygeneruj SELECT:"
                    )
                    payload = {"model": LLM_MODEL, "prompt": prompt_sql, "system": system_sql,
                               "stream": False, "options": {"num_ctx": 4096}}
                    req = urllib.request.Request(
                        OLLAMA_URL + "/api/generate",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST"
                    )
                    with urllib.request.urlopen(req, timeout=90) as r:
                        sql_query = json.loads(r.read().decode("utf-8"))["response"].strip()

                    sql_query = re.sub(r'^```\w*\n?', '', sql_query, flags=re.MULTILINE)
                    sql_query = re.sub(r'```$', '', sql_query.strip()).strip()

                    first_word = sql_query.split()[0].upper() if sql_query.split() else ""
                    if first_word not in ("SELECT", "WITH"):
                        sql_data["error"] = f"LLM nie zwrócił SELECT: {first_word}"
                    else:
                        conn = _get_sql_conn(conn_cfg)
                        cur  = conn.cursor(as_dict=True)
                        cur.execute(sql_query)
                        rows = cur.fetchmany(200)
                        cols = list(rows[0].keys()) if rows else []

                        result_rows = []
                        for row in rows:
                            result_rows.append({k: (str(v) if v is not None else "") for k, v in row.items()})

                        sql_data = {
                            "success": True,
                            "table": "wynik SQL",
                            "columns": cols,
                            "rows": result_rows,
                            "sql": sql_query[:120],
                            "total": len(result_rows)
                        }
                        conn.close()

                except Exception as e:
                    sql_data["error"] = str(e)[:100]

            yield sse("sql_results", {"sql_results": sql_data})

            # 3. LLM synteza — łączy RAG + SQL
            if not rag_contexts:
                yield sse("done", {"ai_answer": "Brak dokumentów w bazie RAG."})
                return

            cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])

            # Buduj prompt syntezy
            rag_preview = "\n\n".join([f"[{c['file']}]: {c['text'][:800]}" for c in rag_contexts[:5]])
            sql_preview = ""
            if sql_data.get("success") and sql_data.get("rows"):
                rows_str = "\n".join([
                    " | ".join([f"{k}={v}" for k, v in zip(sql_data["columns"],
                             [r.get(col, "") for col in sql_data["columns"]])])
                    for r in sql_data["rows"][:10]
                ])
                sql_preview = f"\nDANE Z BAZY (tabela SQL):\n{rows_str}"

            prompt = (
                f"DOKUMENTY:\n{rag_preview}"
                f"{sql_preview}\n\n"
                f"PYTANIE: {query_text}\n\n"
                f"{cfg['prompt_suffix']}\n"
                "Podaj: (1) co wynika z bazy danych, (2) co potwierdzają dokumenty, (3) wnioski."
            )
            payload = {"model": LLM_MODEL, "prompt": prompt, "system": cfg["system"],
                       "stream": True, "options": {"num_ctx": 8192}}

            # Streaming odpowiedzi LLM (obsługuje zarówno Ollama jak i OpenRouter)
            full_answer = ""

            try:
                for token in stream_llm_tokens(
                    prompt=prompt,
                    system=cfg["system"],
                    provider=llm_provider,
                    model=openrouter_model
                ):
                    if isinstance(token, str) and (token.startswith("[Błąd") or token.startswith("[RATE_LIMIT]")):
                        logger.warning(f"LLM stream error (hybrid): {token[:120]}")
                        yield sse("error", {"error": token, "provider": get_llm_provider(llm_provider)})
                        return
                    full_answer += token
                    yield sse("token", {"token": token})
            except Exception as e:
                logger.warning(f"LLM stream error: {e}")
                yield sse("error", {"error": f"Błąd streamingu LLM: {str(e)}"})

            yield sse("done", {"ai_answer": full_answer})

        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/search/stream', methods=['POST'])
def search_stream():
    """Wyszukiwanie z streamingiem LLM — wyniki natychmiast, odpowiedź słowo po słowie."""
    data       = request.get_json()
    query_text = data.get('query', '').strip()
    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"})
    limit      = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode        = data.get('mode', 'normal')
    llm_provider = data.get('llm_provider')
    if mode not in SEARCH_MODES:
        mode = 'normal'

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

        try:
            client = get_qdrant_client()
            vector = get_embedding(query_text)

            if file_filter:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector,
                                          limit=limit, query_filter=qfilter)
            else:
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

            raw_contexts = []
            results = []
            for point in res.points:
                p = point.payload
                raw_contexts.append({"file": p.get("file",""), "text": p.get("text","")})
                results.append({
                    "file": p.get("file","Nieznany"),
                    "score": f"{point.score:.4f}",
                    "text": highlight_backend(p.get("text",""), query_text),
                    "full_path": p.get("full_path",""),
                    "win_path": wsl_to_win(p.get("full_path",""))
                })

            # Wyślij wyniki od razu
            yield sse("results", {"results": results, "contexts": raw_contexts,
                                  "mode": mode, "mode_label": SEARCH_MODES[mode]["label"]})

            if not raw_contexts:
                yield sse("done", {"ai_answer": "Brak dokumentów."})
                return

            # Streaming LLM
            cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])
            context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:1400]}" for c in raw_contexts])
            prompt = f"KONTEKST:\n{context_str}\n\nZAPYTANIE: {query_text}\n\n{cfg['prompt_suffix']}"

            # Streaming LLM — obsługuje Ollama i OpenRouter
            full_answer = ""
            openrouter_model = data.get("openrouter_model")

            try:
                for token in stream_llm_tokens(
                    prompt=prompt,
                    system=cfg["system"],
                    provider=llm_provider,
                    model=openrouter_model
                ):
                    if isinstance(token, str) and (token.startswith("[Błąd") or token.startswith("[RATE_LIMIT]")):
                        # Czysty błąd zamiast zaśmiecania odpowiedzi
                        logger.warning(f"LLM stream error (search): {token[:120]}")
                        yield sse("error", {"error": token, "provider": get_llm_provider(llm_provider)})
                        return
                    full_answer += token
                    yield sse("token", {"token": token})
            except Exception as e:
                logger.warning(f"LLM stream error w search_stream: {e}")
                yield sse("error", {"error": "Błąd streamingu modelu językowego."})

            yield sse("done", {"ai_answer": full_answer})

        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    query_text = data.get('query', '').strip()
    if not query_text: return jsonify({"success": False, "error": "Zapytanie puste"})
    limit = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode = data.get('mode', 'normal')
    llm_provider = data.get('llm_provider')  # 'ollama' lub 'openrouter' z UI
    if mode not in SEARCH_MODES:
        mode = 'normal'

    try:
        client = get_qdrant_client()
        vector = get_embedding(query_text)

        if file_filter:
            # Używa indeksu keyword na polu 'file' — szybkie, bez skanowania całości
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
            res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector,
                                      limit=limit, query_filter=qfilter)
            if not res.points:
                return jsonify({"success": True, "results": [], "ai_answer": "Brak dokumentów dla wybranego pliku."})
        else:
            res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

        raw_contexts = []
        results = []
        for point in res.points:
            p = point.payload
            raw_contexts.append({"file": p.get("file", "Nieznany"), "text": p.get("text", "")})
            full_path = p.get("full_path", "")
            results.append({
                "file": p.get("file", "Nieznany"),
                "score": f"{point.score:.4f}",
                "text": highlight_backend(p.get("text", ""), query_text),
                "full_path": full_path,
                "win_path": wsl_to_win(full_path)
            })

        openrouter_model = data.get("openrouter_model")
        ai_answer = generate_answer(query_text, raw_contexts, mode, provider=llm_provider, model=openrouter_model) if raw_contexts else "Brak dokumentów."
        return jsonify({
            "success": True,
            "results": results,
            "ai_answer": ai_answer,
            "mode": mode,
            "mode_label": SEARCH_MODES[mode]["label"],
            "contexts": raw_contexts  # potrzebne do weryfikacji po stronie klienta
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/suggestions', methods=['GET'])
def get_suggestions():
    force = request.args.get('force', '0') == '1'
    now = time.time()
    if not force and _suggestions_cache["data"] and (now - _suggestions_cache["ts"]) < SUGGESTIONS_TTL:
        return jsonify({"success": True, "suggestions": _suggestions_cache["data"], "cached": True})
    try:
        client = get_qdrant_client()
        # Losowa próbka: pobierz ~300 chunków, wybierz 25 z różnych plików
        records, _ = client.scroll(
            collection_name=ACTIVE_COLLECTION,
            limit=300,
            offset=None,
            with_payload=["file", "text"],
            with_vectors=False
        )
        import random
        seen_files = set()
        sample = []
        random.shuffle(records)
        for r in records:
            fname = r.payload.get("file", "")
            if fname not in seen_files and len(r.payload.get("text", "")) > 100:
                seen_files.add(fname)
                sample.append(r.payload)
            if len(sample) >= 25:
                break

        context = "\n\n".join([
            f"[{p['file']}]: {p['text'][:600]}" for p in sample
        ])
        prompt = (
            "Na podstawie poniższych fragmentów dokumentów śledczych, wygeneruj dokładnie 8 konkretnych pytań analitycznych. "
            "Każde pytanie musi być zwięzłe (max 12 słów), dotyczyć konkretnych faktów, kwot, dat, nazwisk lub zdarzeń z tych dokumentów. "
            "Zwróć TYLKO listę 8 pytań, każde w osobnej linii, bez numeracji, bez komentarzy.\n\n"
            f"DOKUMENTY:\n{context}"
        )
        url = OLLAMA_URL + "/api/generate"
        payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False,
                   "system": "Jesteś analitykiem śledczym. Odpowiadasz wyłącznie po polsku. Zwracasz tylko listę pytań.",
                   "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = json.loads(r.read().decode("utf-8"))["response"]

        lines = [l.strip().lstrip("-•·1234567890.). ") for l in raw.strip().splitlines()]
        suggestions = [l for l in lines if len(l) > 10][:8]

        _suggestions_cache["data"] = suggestions
        _suggestions_cache["ts"] = now
        return jsonify({"success": True, "suggestions": suggestions, "cached": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

NOISE_PATTERNS = [
    # Licencje i pliki prawne bibliotek
    r'^(GPL|LGPL|MIT|Apache License|OFL|OFL-\d|SIL Open Font|Architects Daughter SIL|LICENSE|COPYING|NOTICE)(\.\w+)?$',
    # Logi i pliki tymczasowe
    r'.*\.log$', r'^cat-log.*', r'^SaveAs_log.*', r'^ChangeLog.*',
    # Pliki bibliotek/fontów
    r'^OFL.*\.txt$', r'^OFL-FAQ.*',
    # Zaszyfrowane/tokenizowane nazwy URL (długie stringi base64/JWT z = lub +)
    r'^[A-Za-z0-9+/=]{40,}.*',
    # Dokumentacja techniczna niezwiązana ze sprawą (można rozszerzyć)
    r'^sss\..*',
]

import re as _re

def _is_noise(fname: str) -> str:
    """Zwraca powód detekcji szumu lub pusty string jeśli plik jest OK."""
    for pat in NOISE_PATTERNS:
        if _re.match(pat, fname, _re.IGNORECASE):
            return f"wzorzec: {pat[:40]}"
    # Zaszyfrowana nazwa — długa, zawiera = lub + ale nie jest normalną nazwą pliku
    if len(fname) > 60 and ('=' in fname or (fname.count('+') > 2)):
        return "zaszyfrowana nazwa (token URL)"
    return ""

@app.route('/documents/scan-noise', methods=['GET'])
def scan_noise():
    try:
        client = get_qdrant_client()
        file_chunks = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION, limit=250, offset=offset,
                with_payload=["file"], with_vectors=False
            )
            for r in records:
                fname = r.payload.get("file", "")
                if fname:
                    file_chunks[fname] = file_chunks.get(fname, 0) + 1
            if offset is None:
                break

        noise = []
        for fname, count in file_chunks.items():
            reason = _is_noise(fname)
            if reason:
                noise.append({"file": fname, "chunks": count, "reason": reason})

        noise.sort(key=lambda x: -x["chunks"])
        return jsonify({"success": True, "noise": noise, "total_chunks": sum(n["chunks"] for n in noise)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/documents/delete', methods=['POST'])
def delete_documents():
    data = request.get_json()
    files_to_delete = data.get('files', [])
    if not files_to_delete:
        return jsonify({"success": False, "error": "Brak listy plików"})
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        client = get_qdrant_client()
        # Skasuj wszystkie punkty gdzie payload.file znajduje się na liście do usunięcia
        client.delete(
            collection_name=ACTIVE_COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="file", match=MatchAny(any=files_to_delete))]
            )
        )
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "files_count": len(files_to_delete)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/documents', methods=['GET'])
def get_documents():
    force = request.args.get('force', '0') == '1'
    now = time.time()
    if not force and _docs_cache["data"] and (now - _docs_cache["ts"]) < DOCS_CACHE_TTL:
        return jsonify({"success": True, "documents": _docs_cache["data"],
                        "total": len(_docs_cache["data"]), "cached": True})
    try:
        client = get_qdrant_client()
        file_chunks = {}
        file_paths  = {}
        file_meta   = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION,
                limit=250, offset=offset,
                with_payload=["file", "full_path", "metadata"],
                with_vectors=False
            )
            for r in records:
                fname = r.payload.get("file", "")
                if fname:
                    file_chunks[fname] = file_chunks.get(fname, 0) + 1
                    if fname not in file_paths and r.payload.get("full_path"):
                        file_paths[fname] = r.payload["full_path"]
                    if fname not in file_meta and r.payload.get("metadata"):
                        file_meta[fname] = r.payload["metadata"]
            if offset is None:
                break
        docs = sorted(
            [{
                "file": f,
                "chunks": c,
                "full_path": file_paths.get(f, ""),
                "metadata": file_meta.get(f)
            }
             for f, c in file_chunks.items()],
            key=lambda x: -x["chunks"]
        )
        _docs_cache["data"] = docs
        _docs_cache["ts"]   = now
        return jsonify({"success": True, "documents": docs, "total": len(docs), "cached": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/export/metadata_report', methods=['POST'])
def export_metadata_report_docx():
    """Generuje raport DOCX z metadanymi wybranych dokumentów (dla śledczych)."""
    data = request.get_json() or {}
    selected = data.get('files', [])  # lista obiektów z file, full_path, metadata

    if not selected:
        return jsonify({"success": False, "error": "Brak wybranych plików"}), 400

    try:
        import docx as _docx
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT

        doc = _docx.Document()

        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(11)

        # Nagłówek
        h = doc.add_heading('Raport Metadanych Wybranych Dokumentów', 0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph(f'Wygenerowano: {time.strftime("%d.%m.%Y %H:%M")}')
        doc.add_paragraph(f'Liczba plików: {len(selected)}')
        doc.add_paragraph()

        # Podsumowanie
        doc.add_heading('Podsumowanie', level=1)
        doc.add_paragraph('Poniżej znajdują się metadane systemowe oraz wbudowane dla wybranych dokumentów. '
                          'Dane pochodzą z oryginalnych plików w momencie importu.')

        # Dla każdego pliku
        for idx, f in enumerate(selected, 1):
            doc.add_heading(f'{idx}. {f.get("file", "Nieznany plik")}', level=1)

            meta = f.get('metadata') or {}

            # Metadane systemowe
            doc.add_heading('Metadane systemowe', level=2)
            table = doc.add_table(rows=1, cols=2)
            table.style = 'Table Grid'
            hdr_cells = table.rows[0].cells
            hdr_cells[0].text = 'Pole'
            hdr_cells[1].text = 'Wartość'

            for key in ['size_human', 'created', 'modified', 'accessed']:
                if meta.get(key):
                    row_cells = table.add_row().cells
                    row_cells[0].text = key.replace('_', ' ').title()
                    row_cells[1].text = str(meta[key])

            # Metadane wbudowane
            if meta.get('embedded'):
                doc.add_heading('Metadane wbudowane w plik', level=2)
                for fmt, emb in meta['embedded'].items():
                    p = doc.add_paragraph()
                    p.add_run(f'{fmt.upper()}: ').bold = True
                    p.add_run(str(emb))

            # Pełna ścieżka
            if meta.get('full_path'):
                p = doc.add_paragraph()
                p.add_run('Pełna ścieżka: ').bold = True
                p.add_run(meta['full_path'])

            doc.add_paragraph()  # odstęp

        # Stopka
        doc.add_paragraph('─' * 70)
        p = doc.add_paragraph()
        run = p.add_run('Wygenerowano przez AI Analiza Dokumentów | Narzędzie wspomagające analizę śledczą')
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x6c, 0x75, 0x7d)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        fname = f'raport_metadanych_{time.strftime("%Y%m%d_%H%M")}.docx'
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/export/docx', methods=['POST'])
def export_docx():
    data        = request.get_json()
    query       = data.get('query', '')
    ai_answer   = data.get('ai_answer', '')
    results     = data.get('results', [])
    mode_label  = data.get('mode_label', 'Standardowy')

    try:
        import docx as _docx
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = _docx.Document()

        # Styl dokumentu
        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(11)

        # Nagłówek
        h = doc.add_heading('Raport Śledczy — Analiza RAG', 0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph(f'Data: {time.strftime("%d.%m.%Y %H:%M")}   |   Tryb: {mode_label}')
        doc.add_paragraph(f'Kolekcja: {ACTIVE_COLLECTION}   |   Wyników: {len(results)}')
        doc.add_paragraph()

        # Zapytanie
        doc.add_heading('Zapytanie', level=1)
        p = doc.add_paragraph()
        p.add_run(query).bold = True

        # Odpowiedź LLM
        doc.add_heading('Synteza AI (Llama3)', level=1)
        for line in ai_answer.split('\n'):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(style='List Bullet' if line.startswith('-') else 'Normal')
            p.add_run(line.lstrip('- '))

        # Źródła
        doc.add_heading(f'Dokumenty źródłowe ({len(results)})', level=1)
        for i, r in enumerate(results, 1):
            doc.add_heading(f'{i}. {r.get("file","?")}', level=2)
            score_p = doc.add_paragraph()
            run = score_p.add_run(f'Dopasowanie: {float(r.get("score",0))*100:.1f}%')
            run.font.color.rgb = RGBColor(0x6c, 0x75, 0x7d)
            run.font.size = Pt(9)
            # Usuń tagi HTML z tekstu
            clean = re.sub(r'<[^>]+>', '', r.get('text', ''))
            doc.add_paragraph(clean[:1500])

            if r.get('win_path'):
                p = doc.add_paragraph()
                run = p.add_run(f'Ścieżka: {r["win_path"]}')
                run.font.color.rgb = RGBColor(0x0d, 0x6e, 0xfd)
                run.font.size = Pt(9)

            doc.add_paragraph()

        # Stopka
        doc.add_paragraph('─' * 60)
        doc.add_paragraph('Wygenerowano przez AI Analiza Dokumentów | Llama3 + Qdrant Cloud').runs[0].font.size = Pt(8)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        fname = f'raport_{time.strftime("%Y%m%d_%H%M")}.docx'
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/network', methods=['POST'])
def build_network():
    data    = request.get_json()
    query   = data.get('query', '').strip()
    limit   = min(int(data.get('limit', 10)), 20)
    llm_provider = data.get('llm_provider')
    raw = ""

    if not query:
        return jsonify({"success": False, "error": "Brak zapytania"})

    try:
        client = get_qdrant_client()
        vector = get_embedding(query)
        res    = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

        contexts = [{"file": p.payload.get("file",""), "text": p.payload.get("text","")} for p in res.points]
        context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:800]}" for c in contexts])

        system = (
            "Jesteś ekspertem analityki śledczej. Twoim zadaniem jest wyciągnięcie jak najbogatszej sieci powiązań z dokumentów.\n\n"
            "ZASADY BEZWZGLĘDNE:\n"
            "1. Używaj WYŁĄCZNIE prawdziwych nazw, kwot, dat i faktów z dokumentów.\n"
            "2. NIGDY nie używaj placeholderów typu 'nazwa', 'firma', 'X', 'osoba A'.\n"
            "3. Każdy label musi być rzeczywistą wartością z tekstu.\n\n"
            "FORMAT JSON (zwracaj tylko poprawny JSON):\n"
            '{\n'
            '  "nodes": [ {"id": "jan_kowalski", "type": "osoba", "label": "Jan Kowalski"}, ... ],\n'
            '  "edges": [ {"source": "jan_kowalski", "target": "abc_spolka", "label": "prezes zarządu", "doc": "umowa_2023.pdf", "evidence": "Podpisał umowę 12.03.2023", "date": "2023-03-12", "strength": 3}, ... ]\n'
            '}\n\n'
            "Typy węzłów (type): osoba, firma, kwota, dokument, przetarg, umowa, inne\n\n"
            "Bogate typy relacji (label krawędzi) — używaj precyzyjnych:\n"
            "- prezes / członek zarządu / dyrektor\n"
            "- podpisał umowę / aneks\n"
            "- zapłacił / wystawił fakturę / otrzymał płatność\n"
            "- wygrał przetarg / złożył ofertę\n"
            "- zlecił / wykonał usługę\n"
            "- jest właścicielem / beneficjentem\n"
            "- zatwierdził / skontrolował\n\n"
            "W polu 'evidence' wstaw krótki, dosłowny cytat z dokumentu.\n"
            "Jeśli w tekście pojawia się data (nawet przybliżona) — dodaj ją w polu 'date' w formacie YYYY-MM-DD lub YYYY-MM.\n"
            "W polu 'strength' (opcjonalnie 1-5) podaj jak silne jest to powiązanie na podstawie liczby i jakości dowodów w tym dokumencie.\n"
            "Jeśli ta sama relacja pojawia się w wielu dokumentach — LLM może zwrócić kilka krawędzi; my je później zsumujemy.\n"
            "Staraj się wyciągać jak najwięcej dat, ról, numerów dokumentów i konkretnych kwot.\n\n"
            "id: snake_case, bez polskich znaków, max 40 znaków."
        )
        prompt = (
            f"DOKUMENTY DO ANALIZY:\n{context_str}\n\n"
            f"ZADANIE: {query}\n\n"
            "Wyciągnij sieć powiązań używając PRAWDZIWYCH nazw, kwot i danych z powyższych dokumentów.\n"
            "NIE używaj placeholderów. Każdy label = prawdziwa wartość z tekstu.\n"
            "JSON:"
        )
        openrouter_model = data.get("openrouter_model")
        result = call_llm(
            prompt=prompt,
            system=system,
            stream=False,
            provider=llm_provider,
            model=openrouter_model,
            max_tokens=3000
        )
        raw = result.get("response", str(result)) if isinstance(result, dict) else str(result)

        # Wyciągnij JSON z odpowiedzi
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            return jsonify({"success": False, "error": "LLM nie zwrócił JSON", "raw": raw})

        graph = json.loads(json_match.group())

        # Walidacja i deduplikacja
        seen_nodes = {}
        clean_nodes = []
        for n in graph.get("nodes", []):
            nid = str(n.get("id","")).strip()
            if nid and nid not in seen_nodes:
                seen_nodes[nid] = True
                clean_nodes.append({
                    "id": nid,
                    "type": n.get("type","inne"),
                    "label": n.get("label", nid)[:40]
                })

        # Agregacja powiązań — liczymy SIŁĘ POWIĄZANIA (ile dowodów na daną relację)
        # Zamiast odrzucać duplikaty, grupujemy je i sumujemy
        edge_groups = defaultdict(list)  # key=(src,tgt) -> lista krawędzi

        for e in graph.get("edges", []):
            src = str(e.get("source", "")).strip()
            tgt = str(e.get("target", "")).strip()
            if not src or not tgt or src not in seen_nodes or tgt not in seen_nodes:
                continue
            key = (src, tgt)
            edge_groups[key].append({
                "label":    (e.get("label") or "")[:40],
                "doc":      (e.get("doc") or "")[:90],
                "evidence": (e.get("evidence") or "")[:180],
                "date":     (e.get("date") or "")[:20],
                "strength": max(1, min(5, int(e.get("strength", 1))))
            })

        clean_edges = []
        for (src, tgt), items in edge_groups.items():
            # Wybierz najbardziej powtarzający się label jako główny
            labels = [it["label"] for it in items if it["label"]]
            main_label = max(set(labels), key=labels.count) if labels else items[0]["label"]

            # Zbierz wszystkie unikalne dowody (max 6)
            seen_ev = set()
            evidences = []
            for it in items:
                ev_key = (it["doc"], it["evidence"][:80])
                if ev_key not in seen_ev:
                    seen_ev.add(ev_key)
                    evidences.append({
                        "doc": it["doc"],
                        "evidence": it["evidence"],
                        "date": it["date"]
                    })
                if len(evidences) >= 6:
                    break

            total_strength = sum(it["strength"] for it in items) + (len(items) - 1)  # bonus za powtarzalność

            clean_edges.append({
                "source": src,
                "target": tgt,
                "label": main_label,
                "doc": items[0]["doc"],           # pierwszy dokument dla kompatybilności
                "evidence": items[0]["evidence"], # pierwszy dowód
                "date": items[0]["date"],
                "strength": max(1, min(12, total_strength)),  # cap
                "evidence_count": len(evidences),
                "evidences": evidences
            })

        # Sortuj krawędzie malejąco po sile (najmocniejsze relacje na wierzchu)
        clean_edges.sort(key=lambda e: e.get("strength", 1), reverse=True)

        return jsonify({
            "success": True,
            "nodes": clean_nodes,
            "edges": clean_edges,
            "query": query,
            "sources": len(contexts)
        })
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"Błąd parsowania JSON: {e}", "raw": raw[:500]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _excel_forensics(file_path: Path) -> dict:
    """Pełna analiza forensyczna pliku Excel — szuka błędów rachunkowych i manipulacji."""
    findings = []
    summary  = {"total_formulas": 0, "errors": 0, "goal_seek": 0, "overrides": 0,
                "warnings": 0, "hidden_rows": 0, "hidden_cols": 0}

    try:
        wb_f = openpyxl.load_workbook(file_path, data_only=False)
        wb_v = openpyxl.load_workbook(file_path, data_only=True)
    except Exception as e:
        return {"success": False, "error": str(e)}

    # --- Wykryj ukryte wiersze i kolumny we wszystkich arkuszach ---
    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        sheet_hidden_rows = [ri for ri, rd in ws.row_dimensions.items() if rd.hidden]
        sheet_hidden_cols = [cl for cl, cd in ws.column_dimensions.items() if cd.hidden]

        if sheet_hidden_rows:
            summary["hidden_rows"] += len(sheet_hidden_rows)
            # Zbierz wartości z ukrytych wierszy
            hidden_data = []
            for ri in sheet_hidden_rows[:20]:  # max 20 ukrytych wierszy w raporcie
                row_cells = []
                for cell in ws[ri]:
                    if cell.value is not None and str(cell.value).strip():
                        row_cells.append(f"{cell.column_letter}: {cell.value}")
                if row_cells:
                    hidden_data.append(f"wiersz {ri}: " + " | ".join(row_cells[:8]))
            findings.append({
                "severity": "high",
                "sheet": sn, "cell": f"wiersze {sheet_hidden_rows[:5]}{'...' if len(sheet_hidden_rows)>5 else ''}",
                "formula": "",
                "stored": None,
                "calculated": None,
                "issue": f"UKRYTE WIERSZE: {len(sheet_hidden_rows)} wierszy w arkuszu '{sn}'",
                "detail": (f"Znaleziono {len(sheet_hidden_rows)} ukrytych wierszy. "
                           + ("Dane w nich: " + "; ".join(hidden_data[:3]) if hidden_data
                              else "Wiersze są puste."))
            })

        if sheet_hidden_cols:
            summary["hidden_cols"] += len(sheet_hidden_cols)
            findings.append({
                "severity": "high",
                "sheet": sn, "cell": f"kolumny {sheet_hidden_cols[:5]}",
                "formula": "",
                "stored": None,
                "calculated": None,
                "issue": f"UKRYTE KOLUMNY: {len(sheet_hidden_cols)} kolumn w arkuszu '{sn}'",
                "detail": f"Kolumny {', '.join(sheet_hidden_cols[:10])} są ukryte — mogą zawierać dane pominięte w analizie."
            })

    # Zbierz wszystkie wartości arkusza do słownika dla cross-check
    sheet_values = {}
    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        sheet_values[sn] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    sheet_values[sn][cell.coordinate] = cell.value

    # --- Próba ewaluacji przez xlcalculator ---
    calc_values = {}
    try:
        from xlcalculator import ModelCompiler, Evaluator
        compiler  = ModelCompiler()
        model     = compiler.read_and_parse_archive(str(file_path))
        evaluator = Evaluator(model)
        for sn in wb_f.sheetnames:
            ws_f = wb_f[sn]
            for row in ws_f.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith('='):
                        key = f"{sn}!{cell.coordinate}"
                        try:
                            val = evaluator.evaluate(key)
                            calc_values[key] = val
                        except Exception:
                            pass
    except Exception:
        pass  # xlcalculator niedostępny lub błąd parsowania

    # --- Analiza komórek ---
    for sn in wb_f.sheetnames:
        ws_f = wb_f[sn]
        ws_v = wb_v[sn]

        for row in ws_f.iter_rows():
            for cell_f in row:
                formula = cell_f.value
                if not isinstance(formula, str) or not formula.startswith('='):
                    continue

                summary["total_formulas"] += 1
                coord   = cell_f.coordinate
                stored  = sheet_values.get(sn, {}).get(coord)
                calc_key = f"{sn}!{coord}"

                # 1. Brak wartości — plik niezapisany przez Excel (np. LibreOffice)
                if stored is None:
                    findings.append({
                        "severity": "warning",
                        "sheet": sn, "cell": coord,
                        "formula": formula[:80],
                        "stored": None,
                        "calculated": None,
                        "issue": "Brak zapisanej wartości",
                        "detail": "Plik nie był przeliczony przez Excel lub zapisany przez LibreOffice. Wartości formuł mogą być błędne."
                    })
                    summary["warnings"] += 1
                    continue

                # 2. Ślad Goal Seek — ekstremalna precyzja dziesiętna
                if isinstance(stored, float):
                    s_str     = repr(stored)
                    after_dot = s_str.split('.')[-1] if '.' in s_str else ''
                    if len(after_dot) > 9:
                        findings.append({
                            "severity": "high",
                            "sheet": sn, "cell": coord,
                            "formula": formula[:80],
                            "stored": stored,
                            "calculated": None,
                            "issue": f"Ślad Goal Seek ({len(after_dot)} miejsc po przecinku)",
                            "detail": f"Wartość {stored} ma {len(after_dot)} miejsc po przecinku — charakterystyczne dla narzędzia Goal Seek (wsteczne dobieranie wartości)."
                        })
                        summary["goal_seek"] += 1

                # 3. Niezgodność formuły z wartością wyliczoną przez xlcalculator
                if calc_key in calc_values:
                    calc_val = calc_values[calc_key]
                    if calc_val is not None and stored is not None:
                        try:
                            diff = abs(float(stored) - float(calc_val))
                            rel  = diff / max(abs(float(calc_val)), 1e-9)
                            if rel > 0.001 and diff > 0.01:  # >0.1% rozbieżność i >1 grosz
                                findings.append({
                                    "severity": "critical",
                                    "sheet": sn, "cell": coord,
                                    "formula": formula[:80],
                                    "stored": stored,
                                    "calculated": round(float(calc_val), 6),
                                    "diff": round(diff, 2),
                                    "issue": f"NIEZGODNOŚĆ: zapisano {stored}, formuła daje {round(float(calc_val),2)}",
                                    "detail": f"Różnica: {round(diff,2)} ({round(rel*100,2)}%). Możliwa ręczna modyfikacja."
                                })
                                summary["overrides"] += 1
                                summary["errors"] += 1
                        except (ValueError, TypeError):
                            pass

                # 4. Prosta weryfikacja SUM — parsuj zakres i sumuj ręcznie
                m_sum = re.match(r'^=SUM\(([A-Z]+\d+):([A-Z]+\d+)\)$', formula, re.IGNORECASE)
                if m_sum and isinstance(stored, (int, float)):
                    try:
                        from openpyxl.utils import range_boundaries
                        min_col, min_row, max_col, max_row = range_boundaries(f"{m_sum.group(1)}:{m_sum.group(2)}")
                        total = 0.0
                        for r in ws_v.iter_rows(min_row=min_row, max_row=max_row,
                                                min_col=min_col, max_col=max_col):
                            for c in r:
                                if isinstance(c.value, (int, float)):
                                    total += c.value
                        diff = abs(stored - total)
                        if diff > 0.02:
                            findings.append({
                                "severity": "critical",
                                "sheet": sn, "cell": coord,
                                "formula": formula[:80],
                                "stored": stored,
                                "calculated": round(total, 2),
                                "diff": round(diff, 2),
                                "issue": f"BŁĄD SUM: zapisano {stored}, rzeczywista suma = {round(total,2)}",
                                "detail": f"Różnica {round(diff,2)} zł. Formuła {formula} nie zgadza się z sumą zakresu."
                            })
                            summary["errors"] += 1
                    except Exception:
                        pass

    wb_f.close()
    wb_v.close()

    # Posortuj: najpierw critical, potem high, potem warning
    sev_order = {"critical": 0, "high": 1, "warning": 2}
    findings.sort(key=lambda x: sev_order.get(x["severity"], 3))

    return {
        "success": True,
        "file": file_path.name,
        "summary": summary,
        "findings": findings
    }


@app.route('/analyze/excel', methods=['POST'])
def analyze_excel():
    data      = request.get_json()
    win_path  = data.get('win_path', '')
    wsl_path  = data.get('wsl_path', '')
    fname     = data.get('file', '')

    # Ustal ścieżkę WSL
    path = None

    # 1. Bezpośrednia ścieżka WSL
    if wsl_path and Path(wsl_path).exists():
        path = Path(wsl_path)

    # 2. Ścieżka Windows → WSL
    if not path and win_path:
        m = re.match(r'^([A-Za-z]):\\(.*)', win_path)
        if m:
            p = Path(f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92),'/')}")
            if p.exists():
                path = p

    # 3. Szukaj po nazwie rekurencyjnie
    if not path and fname:
        for root in SEARCH_ROOTS:
            rp = Path(root)
            if not rp.exists():
                continue
            # find przez system — szybciej niż Python glob
            result = os.popen(f'find "{root}" -type f -name "{fname}" 2>/dev/null | head -1').read().strip()
            if result and Path(result).exists():
                path = Path(result)
                break

    if not path:
        return jsonify({"success": False, "error": f"Nie znaleziono pliku '{fname}' — zaimportuj go ponownie z opcją śledzenia ścieżek (full_path)."})

    result = _excel_forensics(path)

    # Jeśli są krytyczne błędy — poproś LLM o komentarz
    critical = [f for f in result.get("findings", []) if f["severity"] == "critical"]
    if critical and result.get("success"):
        ctx = "\n".join([f"[{f['sheet']}!{f['cell']}] {f['issue']} | {f['detail']}" for f in critical[:8]])
        prompt = (
            f"Plik Excel: {path.name}\n"
            f"Znaleziono {len(critical)} krytycznych niezgodności w formułach:\n{ctx}\n\n"
            "Oceń: czy to błąd rachunkowy, celowa manipulacja, czy błąd techniczny? "
            "Co to oznacza w kontekście śledztwa finansowego? Odpowiedz po polsku, krótko."
        )
        system = "Jesteś biegłym rewidentem śledczym. Analizujesz anomalie w plikach Excel pod kątem fałszowania dokumentów finansowych."
        payload = {"model": LLM_MODEL, "prompt": prompt, "system": system, "stream": False, "options": {"num_ctx": 8192}}
        try:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                result["llm_comment"] = json.loads(r.read().decode("utf-8"))["response"]
        except Exception as e:
            result["llm_comment"] = f"(Błąd LLM: {e})"

    return jsonify(result)


@app.route('/compare', methods=['POST'])
def compare_documents():
    """Porównanie dwóch konkretnych plików — pobiera wszystkie ich chunki i wysyła do LLM."""
    data   = request.get_json()
    file_a = data.get('file_a', '').strip()
    file_b = data.get('file_b', '').strip()
    focus  = data.get('focus', 'Porównaj te dokumenty — wskaż różnice, sprzeczności i podobieństwa.').strip()

    if not file_a or not file_b:
        return jsonify({"success": False, "error": "Podaj dwa pliki do porównania"})

    try:
        client = get_qdrant_client()
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        def get_chunks(fname, max_chunks=12):
            qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=fname))])
            # Wektor zerowy = pobierz dowolne chunki (scroll z filtrem)
            records, _ = client.scroll(
                collection_name=ACTIVE_COLLECTION, limit=max_chunks,
                query_filter=qfilter, with_payload=["text"], with_vectors=False
            )
            return [r.payload.get("text", "") for r in records]

        chunks_a = get_chunks(file_a)
        chunks_b = get_chunks(file_b)

        if not chunks_a:
            return jsonify({"success": False, "error": f"Brak danych dla pliku: {file_a}"})
        if not chunks_b:
            return jsonify({"success": False, "error": f"Brak danych dla pliku: {file_b}"})

        text_a = "\n\n".join(chunks_a[:10])[:4000]
        text_b = "\n\n".join(chunks_b[:10])[:4000]

        system = (
            "Jesteś analitykiem śledczym porównującym dokumenty. "
            "Wskazujesz różnice, sprzeczności, brakujące informacje i podejrzane rozbieżności. "
            "Odpowiadaj zawsze po polsku, konkretnie, z cytatami."
        )
        prompt = (
            f"DOKUMENT A: {file_a}\n{text_a}\n\n"
            f"---\n\n"
            f"DOKUMENT B: {file_b}\n{text_b}\n\n"
            f"ZADANIE: {focus}\n\n"
            "Porównanie (wskaż konkretne różnice z cytatami):"
        )
        payload = {"model": LLM_MODEL, "prompt": prompt, "system": system,
                   "stream": False, "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            answer = json.loads(r.read().decode("utf-8"))["response"]

        return jsonify({
            "success": True,
            "file_a": file_a, "chunks_a": len(chunks_a),
            "file_b": file_b, "chunks_b": len(chunks_b),
            "comparison": answer
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/export/csv', methods=['POST'])
def export_csv():
    """Konwertuje tabelę Markdown z trybu extract → plik CSV."""
    data     = request.get_json()
    md_table = data.get('table', '')
    if not md_table:
        return jsonify({"success": False, "error": "Brak tabeli"}), 400
    try:
        import csv, io as _io
        lines = [l.strip() for l in md_table.splitlines() if l.strip()]
        rows  = []
        for line in lines:
            if re.match(r'^\s*\|[-| ]+\|\s*$', line):
                continue  # separator
            if line.startswith('|'):
                cells = [c.strip() for c in line.split('|')[1:-1]]
                rows.append(cells)

        buf = _io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow(row)

        output = buf.getvalue().encode('utf-8-sig')  # BOM dla Excela
        fname  = f"ekstrakcja_{time.strftime('%Y%m%d_%H%M')}.csv"
        return send_file(
            _io.BytesIO(output), as_attachment=True,
            download_name=fname, mimetype='text/csv; charset=utf-8'
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/cache/stats', methods=['GET'])
def cache_stats():
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM embeddings")
            count = cursor.fetchone()[0]
    except Exception:
        count = 0

    size_mb = round(_CACHE_DB_PATH.stat().st_size / 1_048_576, 2) if _CACHE_DB_PATH.exists() else 0
    return jsonify({"entries": count, "size_mb": size_mb})


# ============================================================
# MODUŁ SQL — MS SQL Server 2016
# ============================================================

SQL_CONFIG_PATH = Path(__file__).parent / ".sql_config.json"

def _load_sql_config() -> dict:
    """Załaduj zapisaną konfigurację SQL Server."""
    if SQL_CONFIG_PATH.exists():
        try:
            return json.loads(SQL_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}

def _save_sql_config(cfg: dict):
    """Zapisz konfigurację SQL Server do pliku."""
    try:
        SQL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        logger.warning(f"Błąd zapisu config: {e}")

def _get_sql_conn(cfg: dict):
    """Tworzy połączenie z MS SQL Server przez pymssql.
    Format server '127.0.0.1:1433' omija weryfikację SSL (SQL Server 2025+)."""
    import pymssql
    server = cfg.get("server", "127.0.0.1")
    port   = str(cfg.get("port", 1433))
    # Jeśli serwer nie zawiera portu — dołącz
    if ":" not in server and "\\" not in server:
        server = f"{server}:{port}"
    db = cfg.get("database", "")
    return pymssql.connect(
        server   = server,
        user     = cfg.get("user", ""),
        password = cfg.get("password", ""),
        database = db if db else None,
        timeout  = 15,
        charset  = "UTF-8"
    )

@app.route('/sql/config', methods=['GET', 'POST'])
def sql_config():
    """Zapisz/załaduj konfigurację SQL Server."""
    if request.method == 'GET':
        cfg = _load_sql_config()
        return jsonify({
            "success": True,
            "config": cfg if cfg else None,
            "has_config": bool(cfg)
        })

    # POST — zapisz nową config
    data = request.get_json()
    cfg = {
        "server": data.get("server", "").strip(),
        "port": int(data.get("port", 1433)),
        "database": data.get("database", "").strip(),
        "user": data.get("user", "").strip(),
        "password": data.get("password", "")
    }
    _save_sql_config(cfg)
    return jsonify({"success": True, "message": "Konfiguracja zapisana"})

@app.route('/sql/test', methods=['POST'])
def sql_test():
    """Test połączenia z bazą SQL."""
    cfg = request.get_json()
    try:
        conn = _get_sql_conn(cfg)
        cur  = conn.cursor()
        cur.execute("SELECT @@VERSION")
        version = cur.fetchone()[0]
        # Lista tabel użytkownika
        cur.execute("""
            SELECT TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')
            ORDER BY TABLE_NAME
        """)
        tables = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        conn.close()
        return jsonify({"success": True, "version": version[:120], "tables": tables})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

def _validate_table_name(table_name: str) -> bool:
    """Walidacja nazwy tabeli — zabezpieczenie przed SQL Injection."""
    if not table_name or not isinstance(table_name, str):
        return False
    return bool(re.match(r'^[\w\-\.]+$', table_name))

def _validate_column_names(col_names: list) -> bool:
    """Walidacja listy nazw kolumn."""
    if not col_names or not isinstance(col_names, list):
        return True
    for col in col_names:
        if not isinstance(col, str) or not re.match(r'^[\w\-\.]+$', col.strip()):
            return False
    return True

@app.route('/sql/schema', methods=['POST'])
def sql_schema():
    """Pobiera schemat wybranej tabeli."""
    data  = request.get_json()
    cfg   = data.get("conn", {})
    table = data.get("table", "").strip()

    if not _validate_table_name(table):
        return jsonify({"success": False, "error": f"Niepoprawna nazwa tabeli: '{table}'"})

    try:
        conn = _get_sql_conn(cfg)
        cur  = conn.cursor()
        cur.execute(f"""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (table,))
        cols = [{"name": r[0], "type": r[1], "max_len": r[2], "nullable": r[3]}
                for r in cur.fetchall()]
        cur.execute(f"SELECT COUNT(*) FROM [{table}]")
        row_count = cur.fetchone()[0]
        conn.close()
        return jsonify({"success": True, "table": table, "columns": cols, "row_count": row_count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/sql/ask', methods=['POST'])
def sql_ask():
    """Text-to-SQL: pytanie po polsku → LLM generuje SQL → wykonaj → zwróć wyniki."""
    data     = request.get_json()
    cfg      = data.get("conn", {})
    question = data.get("question", "").strip()
    schema   = data.get("schema", "")   # opis tabel przesłany z frontendu

    if not question:
        return jsonify({"success": False, "error": "Brak pytania"})

    sql_query = ""
    cols = []
    try:
        system_sql = (
            "Jesteś ekspertem T-SQL (MS SQL Server 2016). "
            "Na podstawie schematu bazy danych generujesz zapytania SQL. "
            "ZASADY: używaj tylko SELECT (nigdy DELETE/UPDATE/DROP). "
            "Odpowiadasz WYŁĄCZNIE samym zapytaniem SQL — bez wyjaśnień, bez markdown, bez ```sql. "
            "Jeśli pytanie jest niejasne — zgadnij najbardziej sensowne zapytanie."
        )
        prompt_sql = (
            f"SCHEMAT BAZY:\n{schema}\n\n"
            f"PYTANIE UŻYTKOWNIKA: {question}\n\n"
            "Wygeneruj zapytanie T-SQL:"
        )
        payload = {"model": LLM_MODEL, "prompt": prompt_sql, "system": system_sql,
                   "stream": False, "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            sql_query = json.loads(r.read().decode("utf-8"))["response"].strip()

        sql_query = re.sub(r'^```\w*\n?', '', sql_query, flags=re.MULTILINE)
        sql_query = re.sub(r'```$', '', sql_query.strip()).strip()

        first_word = sql_query.split()[0].upper() if sql_query.split() else ""
        if first_word not in ("SELECT", "WITH"):
            return jsonify({"success": False, "error": f"LLM wygenerował niedozwolone polecenie: {first_word}",
                            "sql": sql_query})

        conn = _get_sql_conn(cfg)
        cur  = conn.cursor(as_dict=True)
        cur.execute(sql_query)
        rows = cur.fetchmany(500)
        cols = list(rows[0].keys()) if rows else []
        result_rows = []
        for row in rows:
            result_rows.append({k: (str(v) if v is not None else "") for k, v in row.items()})
        conn.close()

        result_preview = "\n".join([", ".join([f"{k}={v}" for k, v in r.items()]) for r in result_rows[:10]])
        prompt_interp = (
            f"Pytanie użytkownika: {question}\n"
            f"Wykonane zapytanie SQL: {sql_query}\n"
            f"Liczba wyników: {len(result_rows)}\n"
            f"Przykładowe wyniki:\n{result_preview}\n\n"
            "Odpowiedz po polsku: co wynika z tych danych? Podaj konkretne liczby i wnioski."
        )
        payload2 = {"model": LLM_MODEL, "prompt": prompt_interp,
                    "system": "Jesteś analitykiem danych. Interpretujesz wyniki SQL po polsku.",
                    "stream": False, "options": {"num_ctx": 4096}}
        req2 = urllib.request.Request(
            OLLAMA_URL + "/api/generate",
            data=json.dumps(payload2).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req2, timeout=120) as r2:
            interpretation = json.loads(r2.read().decode("utf-8"))["response"]

        return jsonify({
            "success":    True,
            "sql":        sql_query,
            "columns":    cols,
            "rows":       result_rows,
            "total":      len(result_rows),
            "truncated":  len(result_rows) == 500,
            "interpretation": interpretation
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e),
                        "sql": sql_query})


@app.route('/sql/write', methods=['POST'])
def sql_write():
    """Generuje i wykonuje zapytanie INSERT/UPDATE/DELETE po potwierdzeniu przez użytkownika."""
    data      = request.get_json()
    cfg       = data.get("conn", {})
    question  = data.get("question", "").strip()
    schema    = data.get("schema", "")
    confirmed = data.get("confirmed", False)   # True = użytkownik potwierdził wykonanie
    sql_query = data.get("sql", "")            # Przy potwierdzeniu — SQL z poprzedniego kroku

    if not question and not sql_query:
        return jsonify({"success": False, "error": "Brak pytania lub zapytania SQL"})

    try:
        # Krok 1: generuj SQL (tylko jeśli nie przyszło gotowe)
        if not sql_query:
            system_sql = (
                "Jesteś ekspertem T-SQL (MS SQL Server). "
                "Generujesz zapytania modyfikujące dane: INSERT, UPDATE, DELETE, CREATE TABLE. "
                "Odpowiadasz WYŁĄCZNIE samym zapytaniem SQL — bez wyjaśnień, bez markdown, bez ```sql. "
                "Bądź precyzyjny — podaj konkretne wartości i warunki WHERE."
            )
            prompt_sql = (
                f"SCHEMAT BAZY:\n{schema}\n\n"
                f"ZADANIE: {question}\n\n"
                "Wygeneruj zapytanie T-SQL (INSERT/UPDATE/DELETE):"
            )
            payload = {"model": LLM_MODEL, "prompt": prompt_sql, "system": system_sql,
                       "stream": False, "options": {"num_ctx": 4096}}
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                sql_query = json.loads(r.read().decode("utf-8"))["response"].strip()
            sql_query = re.sub(r'^```\w*\n?', '', sql_query, flags=re.MULTILINE)
            sql_query = re.sub(r'```$', '', sql_query.strip()).strip()

        # Sprawdź typ polecenia
        first_word = sql_query.split()[0].upper() if sql_query.split() else ""
        allowed_write = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "MERGE")
        if first_word not in allowed_write and first_word not in ("SELECT", "WITH"):
            return jsonify({"success": False, "error": f"Nieznane polecenie: {first_word}", "sql": sql_query})

        # Krok 2: jeśli nie potwierdzone — zwróć SQL do podglądu
        if not confirmed:
            # UWAGA: Szacowanie wpływu dla UPDATE/DELETE zostało wyłączone ze względów bezpieczeństwa
            # (WHERE clause pochodzące z LLM mogą zawierać SQL injection)
            impact_warning = ""
            if first_word in ("UPDATE", "DELETE"):
                impact_warning = f"⚠️ {first_word} polecenie — sprawdź WHERE warunek przed potwierdzeniem!"

            return jsonify({
                "success": True,
                "preview": True,   # sygnał dla frontendu — pokaż przycisk Potwierdź
                "sql": sql_query,
                "impact": impact_warning,
                "first_word": first_word
            })

        # Krok 3: wykonaj po potwierdzeniu (z bezpiecznym zamykaniem zasobów)
        conn = None
        cur = None
        try:
            conn = _get_sql_conn(cfg)
            cur = conn.cursor()
            cur.execute(sql_query)
            rows_affected = cur.rowcount
            conn.commit()
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

        # Log audytu
        logger.debug(f"[SQL WRITE] {first_word} | rows={rows_affected} | {sql_query[:100]}")

        return jsonify({
            "success":       True,
            "executed":      True,
            "sql":           sql_query,
            "rows_affected": rows_affected,
            "message":       f"Wykonano. Zmieniono/dodano {rows_affected} wierszy."
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e), "sql": locals().get("sql_query", "")})


@app.route('/sql/vectorize', methods=['POST'])
def sql_vectorize():
    """Wektoryzuje dane z tabeli SQL do Qdrant (SSE)."""
    data  = request.get_json()
    cfg   = data.get("conn", {})
    table = data.get("table", "").strip()
    cols  = data.get("columns", [])  # kolumny do wektoryzacji
    label_col = data.get("label_col", "").strip()  # kolumna jako tytuł chunka

    if not _validate_table_name(table):
        return Response(f"event: error\ndata: {json.dumps({'error': f'Niepoprawna nazwa tabeli: {table}'}, ensure_ascii=False)}\n\n")

    if not _validate_column_names(cols):
        return Response(f"event: error\ndata: {json.dumps({'error': 'Niepoprawne nazwy kolumn'}, ensure_ascii=False)}\n\n")

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"
        try:
            import pymssql
            conn = _get_sql_conn(cfg)
            cur  = conn.cursor(as_dict=True)

            # Ustal kolumny — użyj osobnej zmiennej lokalnej żeby uniknąć konfliktu zakresu
            if cols:
                active_cols = list(cols)
            else:
                cur.execute("""
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME=%s AND DATA_TYPE IN
                    ('varchar','nvarchar','text','ntext','char','nchar','int','decimal','numeric','float','date','datetime')
                    ORDER BY ORDINAL_POSITION
                """, (table,))
                active_cols = [r["COLUMN_NAME"] for r in cur.fetchall()]
            col_list = ", ".join(f"[{c}]" for c in active_cols)

            cur.execute(f"SELECT COUNT(*) as cnt FROM [{table}]")
            total = cur.fetchone()["cnt"]
            yield sse("start", {"total": total, "table": table, "columns": active_cols})

            cur.execute(f"SELECT {col_list} FROM [{table}]")
            qdrant = get_qdrant_client()
            done = 0

            BATCH = 50
            rows_buf = []
            while True:
                batch_rows = cur.fetchmany(BATCH)
                if not batch_rows:
                    break
                for row in batch_rows:
                    # Zbuduj tekst z wiersza
                    label = str(row.get(label_col, "")) if label_col else ""
                    parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
                    text  = (f"[{table}] {label}\n" if label else f"[{table}]\n") + " | ".join(parts)
                    rows_buf.append(text)

                # Batch embeddings
                vecs = get_embeddings_batch(rows_buf, batch_size=6)
                points = []
                for txt, vec in zip(rows_buf, vecs):
                    cid = hashlib.md5(txt.encode('utf-8', errors='replace')).hexdigest()
                    points.append(PointStruct(
                        id=cid, vector=vec,
                        payload={"file": f"[SQL] {table}", "text": txt, "full_path": ""}
                    ))
                if points:
                    qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                done += len(rows_buf)
                rows_buf = []
                yield sse("progress", {"done": done, "total": total,
                                       "pct": round(done/total*100) if total else 0})

            conn.close()
            _docs_cache["data"] = None
            yield sse("done", {"done": done, "total": total,
                                "msg": f"Zwektoryzowano {done} wierszy z tabeli [{table}]"})
        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/sql/vectorize-all', methods=['POST'])
def sql_vectorize_all():
    """Wektoryzuje wszystkie tabele SQL do Qdrant (SSE)."""
    data = request.get_json()
    cfg  = data.get("conn", {})

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"
        try:
            import pymssql
            conn = _get_sql_conn(cfg)
            cur  = conn.cursor(as_dict=True)

            # Pobierz wszystkie tabele
            cur.execute("""
                SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')
                ORDER BY TABLE_NAME
            """)
            tables = [r["TABLE_NAME"] for r in cur.fetchall()]
            yield sse("start", {"total_tables": len(tables), "tables": tables})

            qdrant = get_qdrant_client()
            total_rows = 0

            for table_idx, table in enumerate(tables):
                try:
                    yield sse("table_start", {"table": table, "table_idx": table_idx, "total_tables": len(tables)})

                    # Auto-detect kolumny tekstowe
                    cur.execute("""
                        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_NAME=%s AND DATA_TYPE IN
                        ('varchar','nvarchar','text','ntext','char','nchar','int','decimal','numeric','float','date','datetime')
                        ORDER BY ORDINAL_POSITION
                    """, (table,))
                    active_cols = [r["COLUMN_NAME"] for r in cur.fetchall()]
                    if not active_cols:
                        yield sse("table_done", {"table": table, "rows": 0, "table_idx": table_idx})
                        continue

                    col_list = ", ".join(f"[{c}]" for c in active_cols)
                    cur.execute(f"SELECT COUNT(*) as cnt FROM [{table}]")
                    table_total = cur.fetchone()["cnt"]

                    if table_total == 0:
                        yield sse("table_done", {"table": table, "rows": 0, "table_idx": table_idx})
                        continue

                    cur.execute(f"SELECT {col_list} FROM [{table}]")
                    table_done = 0

                    BATCH = 50
                    rows_buf = []
                    while True:
                        batch_rows = cur.fetchmany(BATCH)
                        if not batch_rows:
                            break
                        for row in batch_rows:
                            parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
                            text  = f"[{table}] " + " | ".join(parts)
                            rows_buf.append(text)

                        # Batch embeddings
                        vecs = get_embeddings_batch(rows_buf, batch_size=6)
                        points = []
                        for txt, vec in zip(rows_buf, vecs):
                            cid = hashlib.md5(txt.encode('utf-8', errors='replace')).hexdigest()
                            points.append(PointStruct(
                                id=cid, vector=vec,
                                payload={"file": f"[SQL] {table}", "text": txt, "full_path": ""}
                            ))
                        if points:
                            qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)

                        table_done += len(rows_buf)
                        total_rows += len(rows_buf)
                        rows_buf = []
                        pct = round(table_done/table_total*100) if table_total else 0
                        yield sse("progress", {
                            "table": table, "done": table_done, "total": table_total, "pct": pct,
                            "tables_done": table_idx, "total_rows": total_rows
                        })

                    yield sse("table_done", {"table": table, "rows": table_done, "table_idx": table_idx})

                    # Mała przerwa między tabelami (tymczasowe odciążenie serwera podczas Vectorize All)
                    time.sleep(0.8)

                except Exception as e:
                    yield sse("table_error", {"table": table, "error": str(e)})
                    time.sleep(0.5)  # krótka przerwa nawet przy błędzie

            conn.close()
            _docs_cache["data"] = None
            yield sse("done", {
                "total_rows": total_rows, "total_tables": len(tables),
                "msg": f"Zwektoryzowano {total_rows} wierszy z {len(tables)} tabel"
            })
        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/health', methods=['GET'])
def health_check():
    """Sprawdza łączność z Qdrant, LLM, SQL i OCR — dla checklisty startowej i paska statusu."""
    result = {
        "qdrant": "error",
        "llm": "error",
        "llm_provider": DEFAULT_LLM_PROVIDER,
        "llm_model": OPENROUTER_MODEL if DEFAULT_LLM_PROVIDER == "openrouter" else os.environ.get("LLM_MODEL", "llama3:latest"),
        "collection": ACTIVE_COLLECTION,
        "vectors_count": 0,
        "sql_configured": False,
        "ocr_available": pytesseract is not None,
    }

    # Qdrant
    try:
        client = get_qdrant_client()
        client.get_collections()
        result["qdrant"] = "ok"
        try:
            info = client.get_collection(ACTIVE_COLLECTION)
            result["vectors_count"] = info.points_count or 0
        except Exception:
            pass
    except Exception as e:
        result["qdrant_error"] = str(e)

    # LLM
    if DEFAULT_LLM_PROVIDER == "openrouter":
        result["llm"] = "ok" if OPENROUTER_API_KEY else "no_key"
    else:
        try:
            import urllib.request as _ur
            _ur.urlopen(_ur.Request(OLLAMA_URL + "/api/tags"), timeout=3)
            result["llm"] = "ok"
        except Exception:
            pass

    # SQL
    sql_cfg = _load_sql_config()
    result["sql_configured"] = bool(sql_cfg.get("server"))
    if result["sql_configured"]:
        result["sql_server"] = sql_cfg.get("server", "")
        result["sql_database"] = sql_cfg.get("database", "")

    return jsonify(result)


@app.route('/api/collection/profile', methods=['GET'])
def collection_profile():
    """Liczy typy plików w kolekcji i sugeruje optymalny tryb analizy."""
    try:
        docs = _docs_cache.get("data") or []
        if not docs:
            client = get_qdrant_client()
            seen: set = set()
            offset = None
            while True:
                records, offset = client.scroll(
                    collection_name=ACTIVE_COLLECTION,
                    limit=500, offset=offset,
                    with_payload=["file"],
                    with_vectors=False,
                )
                for r in records:
                    f = r.payload.get("file", "")
                    if f:
                        seen.add(f)
                if offset is None:
                    break
            docs = [{"file": f} for f in seen]

        ext_counts: dict = {}
        for d in docs:
            fname = d.get("file", "")
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "other"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

        total = sum(ext_counts.values())
        if total == 0:
            return jsonify({"success": True, "profile": "empty", "ext_counts": {}, "total_files": 0})

        num_count = sum(ext_counts.get(e, 0) for e in ("xlsx", "xls", "csv"))
        txt_count = sum(ext_counts.get(e, 0) for e in ("pdf", "docx", "doc", "txt", "md"))

        if num_count / total > 0.5:
            profile, mode = "numerical", "extract"
            hint = "Baza zawiera głównie arkusze i dane liczbowe — tryb Ekstrakcja danych da najlepsze wyniki. Tryb Detektyw przyda się do szukania anomalii."
        elif txt_count / total > 0.5:
            profile, mode = "textual", "detective"
            hint = "Baza zawiera głównie dokumenty tekstowe — tryby Detektyw lub Prawny będą skuteczne."
        else:
            profile, mode = "mixed", "normal"
            hint = "Mieszana baza danych — tryb Standardowy sprawdzi się jako punkt wyjścia."

        return jsonify({
            "success": True,
            "profile": profile,
            "suggestion_mode": mode,
            "suggestion_text": hint,
            "ext_counts": ext_counts,
            "total_files": total,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
