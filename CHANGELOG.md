# Changelog

Wszystkie istotne zmiany w projekcie **AI Analiza Dokumentów** dokumentujemy w tym pliku.

Format oparty na [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/).  
Wersjonowanie zgodne z tagami git (`v2026.xx`).

---

## [Unreleased] — 2026-06-02

Sesja rozwojowa: wielodostawcowość LLM, UI, bezpieczeństwo i stabilność.

### Dodano

- **Rój agentów** — wieloetapowa analiza z koordynacją ról LLM (Generator / Krytyk / synteza); wsparcie modeli agentowych (np. Groq Compound) w panelu providerów.
- **Flota LLM** — obsługa wielu dostawców (Ollama, OpenRouter, Groq) z jednej abstrakcji `call_llm()` / `stream_llm_tokens()`.
- **Pool kluczy i serwerów z rotacją** (`EndpointPool`) — round-robin + cooldown przy 429, błędach auth i problemach połączenia:
  - `GROQ_API_KEYS` / `groq_keys[]`
  - `OPENROUTER_API_KEYS` / `openrouter_keys[]`
  - `OLLAMA_URLS` / `ollama_urls[]`
  - statystyki pul w `GET /health` (`pools`) i `GET /api/config/llm`
- **`POST /compare`** — porównanie dwóch dokumentów z kolekcji (LLM, wybór fokusu analizy); zakładka **Porównaj** w UI.
- **`GET /api/collection/profile`** — profil kolekcji (numeryczny / tekstowy / mieszany) i baner sugestii trybu wyszukiwania (*Adaptive context*).
- Groq jako provider chmurowy — health check z wykrywaniem blokady organizacji, wybór modelu w UI.
- Anti-flap preflight w `/health` — lżejsze odpytywanie przy szybkim pollingu (`?light=1`).
- Skrypt integracyjny `scripts/run-tests.sh` (health, Groq, opcjonalnie SSE search).

### Zmieniono

- **Redesign UI** — spójny layout, czytelniejsze karty wyników i stany ładowania.
- **Topbar** — wersja aplikacji, liczba wektorów, status systemu, aktywna kolekcja.
- **Zakładki** — uporządkowana nawigacja (Wyszukaj, Dokumenty, AI, Porównaj, Sieć, Raporty, SQL, Kolekcje, Import).
- **CSS design system** — paleta `#1a1f36` / `#6366f1`, badge’e filtrów, karty `.search-card` / `.result-card`, status dots diagnostyki.
- Domyślny provider LLM: **OpenRouter** (embeddingi nadal przez Ollama `nomic-embed-text`).
- Konfiguracja LLM z UI zapisuje się do `.llm_config.json` (natychmiastowy efekt bez restartu).

### Naprawiono (bezpieczeństwo)

- **XSS (×3)** — escapowanie danych LLM/użytkownika w panelu sieci powiązań, wynikach błędów i widoku porównania (`escapeHtml` / `textContent` zamiast surowego `innerHTML`).
- **SQL injection (×2)** — przywrócono `_is_sql_safe`, walidację referencji tabel i fail-closed w `hybrid_stream()`; guard `PYMSSQL_AVAILABLE` przed wykonaniem SQL z LLM.
- **Spoofing konfiguracji** — dostęp do `/api/config/llm` tylko z localhost lub z poprawnym nagłówkiem `X-API-Key`; klucze API nigdy nie zwracane w GET (tylko flagi `*_key_set`).

### Naprawiono (logika)

- **Restart aplikacji** — stabilniejszy flow przez `restart-app.sh --user` i usługę systemd użytkownika.
- **Health** — bogaty endpoint `/health` (Qdrant, LLM per provider, embedding, OCR, parsers, SQL, pule); naprawiono migotanie statusu przy pollingu.
- **Token `[FALLBACK]`** — czytelna obsługa automatycznego przejścia OpenRouter/Groq → Ollama przy rate limit; komunikat w UI zamiast „martwego” streamu.
- **Compare TypeError** — poprawiona obsługa odpowiedzi JSON i błędów w zakładce Porównaj (frontend + backend).
- Retry przy chwilowych resetach LLM (Connection reset) — Ollama i chmura.
- Retry timeline SSE na frontendzie przy zerwanym strumieniu.
- Wybór modelu Groq — respektowanie modelu z requestu/UI zamiast twardego domyślnego.

---

## [v2026.12] — wcześniejsze

- Timeline (oś czasu) — ekstrakcja dat i chronologia (SSE).
- Text-to-SQL — auto-korekta, LIMIT/TOP, wykresy Chart.js.
- Tryb SQLite w zakładce SQL.
- Self-update z GitHub (`/api/update/pull`, modal diagnostyczny).
- Tryb Detektyw — briefing śledczy, `chat_context` bez psucia embeddingu.
- Przeglądarka folderów — dyski WSL (`/mnt/c`, `/mnt/g`), normalizacja ścieżek Windows.

---

*Pełna historia commitów: `git log --oneline` · Roadmap: `docs/ROADMAP_ISSUE_2.md`, `IMPROVEMENT_PLAN.md`*
