# AGENTS.md — Instrukcje dla AI coderów

> Ten plik jest przeznaczony dla asystentów AI (Claude Code, Cursor, Copilot, Codex, Gemini i in.).
> Przeczytaj go w całości przed wprowadzeniem jakichkolwiek zmian.

---

## Czym jest ten projekt

**AI Analiza Dokumentów** — produkcyjny system RAG (Retrieval-Augmented Generation) do śledczej analizy dokumentów. Użytkownik wgrywa pliki (PDF, DOCX, XLSX, CSV, JSON, skany OCR), a system:

1. Dzieli je na chunki i wektoryzuje przez Ollama (`nomic-embed-text`)
2. Przechowuje wektory w Qdrant (Cloud lub lokalny)
3. Przy pytaniu robi wyszukiwanie hybrydowe (semantyczne + BM25-style)
4. Generuje odpowiedź przez LLM (Ollama lub OpenRouter)
5. Weryfikuje każde twierdzenie przez drugi model (Krytyk)

Docelowi użytkownicy: **śledczy, prawnicy, analitycy finansowi** — ludzie pracujący z dużymi zbiorami dokumentów i szukający anomalii, powiązań, cytowań prawa.

---

## Architektura — jeden plik, jeden proces

```
app.py          — cały backend Flask (3000+ linii), wszystkie endpointy
templates/
  index.html    — cały frontend (~3500 linii), Bootstrap 5 + D3.js + vanilla JS
wsgi.py         — entry point dla Waitress (produkcja)
.llm_config.json — konfiguracja LLM zapisywana przez UI (nie commitować!)
.sql_config.json — konfiguracja SQL zapisywana przez UI (nie commitować!)
embedding_cache.db — cache embeddingów SQLite (nie commitować!)
```

**Nie ma** osobnych mikroserwisów, nie ma React/Vue, nie ma ORM-a, nie ma testów jednostkowych.

---

## Stos technologiczny

| Warstwa | Technologia |
|---|---|
| Backend | Python 3.12 + Flask + Waitress |
| Baza wektorowa | Qdrant (Cloud lub lokalny, ten sam kod) |
| Embeddingi | nomic-embed-text 768 dim, zawsze przez Ollama |
| LLM chat | Ollama (llama3) LUB OpenRouter (dziesiątki modeli) |
| Frontend | Bootstrap 5 + D3.js + vanilla JS, zero bundlera |
| Chunking | 1000 znaków, 200 nakładka, granice zdań |
| Cache embeddingów | SQLite (`embedding_cache.db`) |
| OCR | Tesseract (opcjonalne) |
| SQL | pymssql / psycopg2 / mysql-connector |

---

## Wymagania uruchomieniowe

| Usługa | Port | Uwaga |
|---|---|---|
| Qdrant | 6333 (lokalny) lub Cloud URL | Wymagane w `.env` |
| Ollama | 11434 | Wymagane dla embeddingów |
| Flask/Waitress | 5000 | `python app.py` lub `python -m waitress ...` |

Konfiguracja `.env` (lokalne środowisko deweloperskie):
```env
QDRANT_URL=http://127.0.0.1:6333
QDRANT_KEY=dev-local-key
OLLAMA_URL=http://127.0.0.1:11434
LLM_MODEL=llama3:latest
ACTIVE_COLLECTION=dokumenty
```

Health check: `curl -s http://127.0.0.1:5000/health`

---

## Kluczowe konwencje kodu

### Python (app.py)

- **Zmienne globalne konfiguracyjne** są na górze pliku (linie 60–120) — `QDRANT_URL`, `OLLAMA_URL`, `OPENROUTER_*`, itd. Przed dodaniem nowej opcji sprawdź czy już tam jest.
- **LLM calls** — używaj wyłącznie `call_llm()` i `stream_llm_tokens()`. Nie wywołuj Ollama/OpenRouter bezpośrednio poza tymi funkcjami.
- **Qdrant client** — zawsze przez `get_qdrant_client()` (thread-safe singleton z timeoutem).
- **Endpointy** zawsze zwracają `jsonify({"success": True/False, ...})`. Nigdy nie zwracaj nagiego stringa.
- **Streaming endpoints** używają `Response(stream_with_context(generator()), mimetype='text/event-stream')` i funkcji pomocniczej `sse(event, data)`.
- **Kolekcja** — ACTIVE_COLLECTION to string zmieniany przez `/collections/switch`. Globalny stan sesji.
- **Cache dokumentów** `_docs_cache` — unieważniaj przez `_docs_cache["data"] = None` po każdej zmianie kolekcji lub imporcie.
- **Błędy** — loguj przez `logger.warning/error(...)`, nie przez `print()`.

### JavaScript (index.html)

- **Nigdy `innerHTML` z danymi LLM lub użytkownika** — używaj `textContent` lub `document.createElement`. Jedynym wyjątkiem jest `formatAiText()` który sanityzuje output LLM.
- **Brak `setTimeout` jako substytutu `await`** — jeśli coś musi poczekać, użyj Promise/.then lub async/await.
- **Zakładki** — `showTab(name)` + div `id="tab${Name}"`. Lista zakładek w funkcji `showTab`.
- **SSE streaming** — wzorzec: `fetch → response.body.getReader() → readChunk loop → JSON.parse(data) → switch(ev.type)`.
- **Globalne zmienne** — `chatHistory`, `allDocs`, `currentCollection`, `window._lastSearchData` — sprawdź przed dodaniem nowej.

---

## Cele jakościowe — czego oczekujemy od każdej zmiany

1. **Zero regresji** — jeśli zmieniasz streaming, przetestuj i Ollama i OpenRouter. Jeśli zmieniasz import, przetestuj PDF i XLSX.
2. **Brak sekretów w kodzie** — klucze API, URL-e chmurowe, nazwy użytkowników nigdy nie trafiają do repozytorium.
3. **Brak hardcoded ścieżek** — żadnych `/home/username/...` w kodzie. Używaj `Path(__file__).parent` lub zmiennych z `.env`.
4. **Brak martwego kodu** — nie zostawiaj zakomentowanych bloków kodu, nie dodawaj `TODO` bez planu.
5. **Obsługa błędów na granicach** — waliduj dane wejściowe od użytkownika, ale nie dodawaj try/except tam gdzie nic nie może się nie udać.
6. **Prostota** — to jest jedna aplikacja Flask, nie framework. Nie dodawaj abstrakcji których nie ma w istniejącym kodzie (blueprints, DI, dataclasses do każdego modelu).

---

## Czego NIE robić

- **Nie zmieniaj formatu SSE** — frontend zakłada `data: {"event": ..., "data": ...}\n\n`. Zmiana formatu zepsuje streaming.
- **Nie rozdzielaj app.py** — jest to świadomy wybór architektoniczny. Jeśli zadanie wymaga refaktoringu, zapytaj użytkownika.
- **Nie dodawaj nowych zależności** bez aktualizacji `requirements.txt` i sprawdzenia czy nie ma prostszego rozwiązania.
- **Nie używaj `os.system()`** — tylko `subprocess.run()` z listą argumentów (bez `shell=True`).
- **Nie commituj** `.llm_config.json`, `.sql_config.json`, `embedding_cache.db`, `.env`.
- **Nie zmieniaj rozmiaru wektora 768** — wszystkie istniejące kolekcje są na tym rozmiarze.
- **Nie blokuj głównego wątku Flask** — długie operacje (wektoryzacja wszystkiego, ciężkie importy) muszą być strumieniowane przez SSE.

---

## Testowanie zmian

```bash
# 1. Sprawdzenie składni
python -m py_compile app.py wsgi.py

# 2. Uruchomienie (dev)
python app.py
# lub Waitress:
python -m waitress --listen=127.0.0.1:5000 wsgi:app

# 3. Health check — sprawdza Qdrant, LLM, SQL, OCR
curl -s http://127.0.0.1:5000/health | python -m json.tool

# 4. Test importu
curl -sN 'http://127.0.0.1:5000/import/stream?folder=/tmp&ext=txt'

# 5. Test wyszukiwania
curl -s -X POST http://127.0.0.1:5000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"test","limit":3}' | python -m json.tool
```

Nie ma zestawu testów automatycznych — każda zmiana wymaga ręcznego testu golden path.

---

## Instalacja środowiska deweloperskiego

```bash
git clone https://github.com/dragon15555000/AI_analiza_dokumentow.git
cd AI_analiza_dokumentow
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
# edytuj .env — wpisz QDRANT_URL, QDRANT_KEY, OLLAMA_URL
python app.py
```

Opcjonalnie — lokalny Qdrant bez Docker:
```bash
mkdir -p .local && cd .local
curl -fsSL -o qdrant.tar.gz \
  "https://github.com/qdrant/qdrant/releases/download/v1.13.6/qdrant-x86_64-unknown-linux-gnu.tar.gz"
tar -xzf qdrant.tar.gz && rm qdrant.tar.gz && chmod +x qdrant
./qdrant   # w osobnej sesji tmux
```

---

## Roadmap / zaplanowane funkcje

Aktualna lista zadań: GitHub Issues w repozytorium.

Najważniejsze kierunki rozwoju:
- **Adaptacyjna detekcja kontekstu** — dobór trybu analizy i promptów do dominującego typu danych w kolekcji
- **Dashboard startowy** — status wszystkich komponentów przy uruchomieniu ✅ (zaimplementowane)
- **Zarządzanie usługą z UI** — restart, logi systemd ✅ (zaimplementowane)
- **Konfiguracja LLM z UI** — zmiana klucza OpenRouter i modeli bez edycji .env ✅ (zaimplementowane)
- **Oś czasu / Timeline** — automatyczna ekstrakcja dat i wizualizacja chronologii
- **Eksport grafu sieci powiązań** — PNG/SVG z D3.js, dołączany do raportu DOCX
- **Prawniczy asystent ISAP** — weryfikacja cytowań ustaw względem bazy ISAP

---

## Ważne uwagi bezpieczeństwa

- Endpointy `/api/service/restart` i `/api/service/status` działają **tylko z localhost** (`request.remote_addr`).
- Klucz API (`OPENROUTER_API_KEY`) nigdy nie wychodzi przez GET `/api/config/llm` — tylko preview 8 znaków.
- SQL: każde zapytanie z `;` jest odrzucane. Słowa kluczowe mutujące (DROP, TRUNCATE) są blokowane w trybie SELECT.
- Import plików ograniczony do `SEARCH_ROOTS` z `.env` — nie można importować dowolnych ścieżek systemowych.
- Użytkownik końcowy nie może wykonywać kodu po stronie serwera przez żadne pole tekstowe (brak `eval`, brak dynamicznych importów z danych użytkownika).

---

*Ostatnia aktualizacja: maj 2026*
