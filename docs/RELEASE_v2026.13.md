# Release v2026.13 — 2026-06-02

**Wydanie funkcjonalne** — Flota LLM, rój agentów, wielodostawcowość i redesign UI.

Sensowna wersja „produkcyjna na laptop/WSL” z kontrolowanym ryzykiem chmury i auto-route: ryzyka są nazwane, domyślnie wyłączone lub ograniczone w kodzie (auto-route Floty **OFF**, fallback na Ollama, pule kluczy z cooldown).

> **Zalecane:** dla codziennej pracy użyj **[v2026.14](https://github.com/dragon15555000/AI_analiza_dokumentow/releases/tag/v2026.14)** — wydanie stabilizacyjne z mitigacjami regresji Floty i roju.

## Najważniejsze

- **Flota LLM** — ranking OpenRouter / Groq / Ollama, sonda, opcjonalny auto-route
- **Rój agentów** — wieloetapowa analiza (Generator / Krytyk / synteza)
- **Pule kluczy** — rotacja przy 429, fallback na Ollama
- **Porównaj** — `POST /compare`, zakładka w UI
- **Redesign UI** — topbar, pill’e dashboardowe, design system

## Aktualizacja

```bash
git fetch origin && git checkout master && git pull origin master
git checkout v2026.13   # opcjonalnie: praca na tagu
./restart-app.sh --user
```

---

Sesja rozwojowa: wielodostawcowość LLM, Flota LLM, rój agentów, UI dashboard.

### Dodano

- **Rój agentów** — wieloetapowa analiza z koordynacją ról LLM (Generator / Krytyk / synteza); wsparcie modeli agentowych (np. Groq Compound) w panelu providerów.
- **Flota LLM** — zakładka w UI z rankingiem dostawców (score, latencja, limity, agenty); API `GET /api/llm/fleet`, `POST /api/llm/fleet/probe`, `POST /api/llm/fleet/auto-route`; cache sondy (szybkie odświeżanie bez pełnego Groq chat ping); statystyki wywołań w `.llm_fleet_stats.json`.
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
- **Zakładki** — uporządkowana nawigacja (Wyszukaj, Dokumenty, AI, Porównaj, Sieć, Raporty, SQL, **Flota LLM**, Kolekcje, Import).
- **CSS design system** — paleta `#1a1f36` / `#6366f1`, badge’e filtrów, karty `.search-card` / `.result-card`, status dots diagnostyki.
- Domyślny provider LLM: **OpenRouter** (embeddingi nadal przez Ollama `nomic-embed-text`).
- Konfiguracja LLM z UI zapisuje się do `.llm_config.json` (natychmiastowy efekt bez restartu).
- **Pill’e dashboardowe** (`.dash-pill`) — statusy z kolorową kropką zamiast emoji w topbarze, flocie i liczniku tokenów.

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

Pełny changelog: [CHANGELOG.md](../CHANGELOG.md)
