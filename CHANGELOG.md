# Changelog

Wszystkie istotne zmiany w projekcie według wersji i daty.
Format: [Semantic Versioning](https://semver.org/lang/pl/) · [Keep a Changelog](https://keepachangelog.com/pl/1.0.0/)

---

## [Unreleased] — branch `claude/remove-local-cloud-refs-xIlp7`

### Dodano
- **🤖 Rój agentów** (`/agents/swarm/stream`) — nowa zakładka z trzema trybami pracy:
  - 🎯 Jakość — 3 workery, Llama 3.3 70B, hierarchiczna synteza dwupoziomowa
  - ⚡ Szybkość — 4 workery, Gemini Flash, wyniki streamowane na bieżąco
  - 🔒 Incognito — 5 workerów z losowymi modelami i potasowanymi fragmentami; żaden LLM nie widzi >20% dokumentu
  - Dashboard workerów aktualizowany w czasie rzeczywistym (SSE)
- **⚡ Flota LLM** — rejestr 7 darmowych modeli z live-pingiem i rankingiem (jakość × szybkość × dostępność)
  - `GET /api/models/registry` — spec + statystyki live
  - `POST /api/models/ping/all` — SSE: ping wszystkich równolegle
  - `GET /api/models/recommend` — rekomendacja modelu dla danego zadania
- **POST `/compare`** — porównanie dwóch dokumentów przez LLM (brakujący endpoint)
- **GET `/api/collection/profile`** — profilowanie kolekcji wg typów plików, sugestia trybu
- **🔑 Pula dostawców API** — wiele kluczy OpenRouter + wiele adresów Ollama z rotacją round-robin:
  - `GET /api/providers` — lista z zamaskowanymi kluczami
  - `POST /api/providers` — dodaj klucz/URL (typy: `openrouter_key`, `ollama_url`, `custom_endpoint`)
  - `DELETE /api/providers/<id>` — usuń wpis
  - `POST /api/providers/<id>/toggle` — włącz/wyłącz
  - `POST /api/providers/<id>/test` — ping z latencją (ms)
  - Panel "Dostawcy API" w zakładce ⚡ Flota LLM z formularzem dodawania i tabelą wpisów
  - `.providers.json` dodany do `.gitignore` (przechowuje klucze API — nie commitowany)

### Zmieniono
- **UI: topbar** — przeprojektowany na dwa wiersze (`topbar-main` + `topbar-controls`); statystyki chowane na mobile (`stat-hide-mobile`)
- **UI: zakładki** — opakowane w `nav-tabs-wrap` z `overflow-x:auto`; wszystkie 10 zakładek przewijalne na każdym ekranie
- **CSS design system** — zmienne CSS (`--c-brand`, `--c-accent`, `--shadow-*`, `--r-*`, `--t`) użyte w całym pliku zamiast rozproszonych wartości
- **`SWARM_MODES`** — rejestr trybów roju jako konfiguracja backendu
- **`FREE_MODELS_LIST`** — lista darmowych modeli wyodrębniona jako stała
- **`MODEL_REGISTRY`** — pełna specyfikacja 7 modeli (kontekst, limity, specjalizacje, ikony)

### Naprawiono — bezpieczeństwo
- **XSS — `renderMarkdownTable`** (`index.html`): brakujące `escapeHtml()` na komórkach tabeli
- **XSS — import log** (`index.html`): `d.file` i `d.reason` bez escapowania w `innerHTML`
- **XSS — `highlight_backend`** (`app.py`): przy pustym `query` zwracał surowy HTML; teraz zawsze escapuje
- **SQL Injection — `_is_sql_safe`** (`app.py`): `count(";") > 1` → `> 0` (pojedynczy średnik był dozwolony)
- **SQL Injection — `sql_write` preview** (`app.py`): brakująca walidacja `_is_sql_safe` + regex nazwy tabeli przed zapytaniem COUNT
- **Spoofing X-Forwarded-For** (`app.py`): `_is_local_request()` i `_localhost_only()` teraz ufają nagłówkowi tylko gdy `TRUST_PROXY=true`

### Naprawiono — błędy logiki
- **`_do_restart`** (`app.py`): brakujące sprawdzenie `returncode == 0` przed powrotem (zawsze przechodziło do `os._exit(0)`)
- **`/health` endpoint** (`app.py`): ping OpenRouter bramkowany przez `OPENROUTER_API_KEY`, nie przez `APP_API_KEY`
- **`stream_llm_tokens`** (`app.py`): filtr tokenów `[FALLBACK]` — prefix przeciekał do streamowanej odpowiedzi
- **`loadCollectionProfile`** (`index.html`): odwrócona logika tłumienia bannera — pokazywał się gdy tryb był już ustawiony
- **`/compare` — NameError** (`app.py`): brakujący `from qdrant_client.models import Filter, FieldCondition, MatchValue` w `_fetch_chunks`
- **`/compare` — TypeError w JS** (`app.py`): `call_llm` zwracał dict zamiast stringa; dodano `_llm_response_text(result)`

### Inne
- **`.gitignore`** — dodano `.claude/` i `.providers.json`
- **`AGENTS.md`** — zaktualizowany z instrukcjami dla AI coderów, sposobami uruchamiania, gotcha i opisem architektury

---

## [2026.12] — master (poprzednia produkcja)

Wersja bazowa przed zmianami z tej sesji. Zawiera:
- Flask RAG z Qdrant + Ollama/OpenRouter
- Sieć powiązań D3.js
- Oś czasu (chronologia RegEx + LLM)
- Raporty śledcze
- SQL Server integration (Text-to-SQL)
- Import SSE z OCR
- Self-update (`/api/update/pull`, `/api/update/restart`)
- Diagnostyka startowa (modal checklisty)
- Konfiguracja LLM z UI (`.llm_config.json`)
- Licznik tokenów i prędkości streamowania
