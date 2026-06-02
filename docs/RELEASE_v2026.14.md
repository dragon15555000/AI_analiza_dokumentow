# Release v2026.14 — 2026-06-02

**Zalecane wydanie** do pracy na WSL/laptopie (Flota LLM, rój agentów, mitigacje regresji).

## Najważniejsze

- **Flota LLM** — ranking OpenRouter / Groq / Ollama, sonda, opcjonalny auto-route (domyślnie **wyłączony**)
- **Rój LLM** — tryby A (incognito) / B (szybkość) / C (jakość), map-reduce z dokumentów
- **Pule kluczy** — rotacja przy 429, fallback na Ollama
- **UI** — pill’e dashboardowe, topbar z wersją i statusem

## Nowe w v2026.14 (względem v2026.13)

- Auto-route: jawne `llm_provider: auto`, pill w topbarze, `llm_provider_used` w wyszukiwaniu
- Rój: limit 3 równoległych workerów w chmurze; synteza zapasowa gdy Ollama padnie (strict incognito)
- Flota: szybsze odświeżanie (cache sondy, bez auto-sondy przy otwarciu zakładki)
- `scripts/run-tests.sh` — smoke roju i floty

## Wymagania

- Python 3.12, Qdrant, Ollama (`nomic-embed-text` + model chat)
- Opcjonalnie: klucze OpenRouter / Groq

## Aktualizacja

```bash
git fetch origin && git checkout master && git pull origin master
git checkout v2026.14   # opcjonalnie: praca na tagu
./restart-app.sh --user
```

## Profil zalecany (niskie ryzyko)

| Ustawienie | Wartość |
|------------|---------|
| Auto-route Floty | **OFF** |
| Provider topbar | OpenRouter lub Ollama (ręcznie) |
| Rój | **B** lub **C** |
| Przed długą sesją | Flota → **Sonduj wszystkich** |

Pełny changelog: [CHANGELOG.md](../CHANGELOG.md)
