# Release v2026.15 — 2026-06-04

**Zalecane wydanie** po v2026.14 — refaktoryzacja LLM, integracja Claude/Gemini, kolejka importu i poprawki diagnostyki.

Tag: [`v2026.15`](https://github.com/dragon15555000/AI_analiza_dokumentow/releases/tag/v2026.15)

## Najważniejsze

- **Claude (Anthropic)** — obsługa w `llm_client.py`, endpoint testowy `/claude_test`, custom endpoint w puli dostawców (Flota → Dostawcy API)
- **Lista modeli Claude w topbarze** — Sonnet 4.6 ★, Opus 4.6, Haiku 4.5, legacy 3.5 + opcja „Własny model…”
- **Refaktoryzacja architektury LLM** — logika providerów w `llm_client.py`; moduły: `prompts.py`, `sql_safety.py`, `models_fleet.py`
- **Kolejka zadań importu** — `task_queue.py`, `/import/stream`, monitoring `/tasks`, `/tasks/<id>`, `/tasks/<id>/stream`
- **Gemini** — provider w UI, audyt logów, health-checki, migracja 1.5 → 2.0-flash
- **Diagnostyka** — poprawne `/health` przy `APP_API_KEY`, helpery Gemini, OCR w light poll

## Nowe funkcje

### LLM i providerzy

- Integracja **Anthropic Claude** (API bezpośrednie, klucz z `.env` lub custom endpoint)
- **Gemini** jako provider + audyt dużych plików/logów
- Pula dostawców: typ **`anthropic`** (test, health, routing modeli)
- **Ulubione modele** (☆) w topbarze
- Domyślny chat: **OpenRouter free** (`LLM_PROVIDER=openrouter`)

### Import i OCR

- Checkbox **„Wymuś OCR”** (skany PDF, JPG/PNG/TIFF/BMP)
- **Kolejka zadań** dla długiego importu

### Flota i rój

- `ThreadPoolExecutor` dla ping floty i workerów roju
- Poprawki SSE roju (puste zapytanie, usage)

## Poprawki

- Diagnostyka startowa — brak fałszywych błędów bez `X-API-Key`
- Claude custom endpoint — koniec 404 (test jak Ollama); ignorowanie modeli OpenRouter (Qwen) przy Claude
- Gemini — retry/backoff, auth nagłówkiem, deprecated URL w custom endpoint
- Embedding health — `EMBED_MODEL is not defined`
- Windows paths w `onclick` — fix `SyntaxError`
- `/claude_test` chroniony `APP_API_KEY`

## Refaktoryzacja

| Moduł | Rola |
|--------|------|
| `llm_client.py` | Wywołania LLM (Ollama, OpenRouter, Gemini, Claude, custom) |
| `prompts.py` | Prompty i rejestr modeli |
| `sql_safety.py` | Walidacja SQL |
| `models_fleet.py` | Flota LLM |
| `task_queue.py` | Kolejka zadań w tle |

## Wymagania

- Python 3.12+, **Qdrant**, **Ollama** (`nomic-embed-text`)
- Chat (opc.): OpenRouter / Gemini / Claude / Ollama
- OCR (opc.): `tesseract-ocr`, `tesseract-ocr-pol`, `poppler-utils`

## Aktualizacja

```bash
git fetch origin && git checkout master && git pull origin master
git checkout v2026.15   # opcjonalnie
./restart-app.sh --user
```

Hard refresh UI: **Ctrl+F5**.

**Claude (opc.)** w `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-6
```

## Profil zalecany

| Ustawienie | Wartość |
|------------|---------|
| Provider (codziennie) | OpenRouter + model `:free` |
| Claude | Custom `https://api.anthropic.com/v1` + lista w topbarze |
| Auto-route Floty | **OFF** |
| Diagnostyka | ⚙️ → `APP_API_KEY` jeśli w `.env` |

## Test plan (smoke)

- [ ] `curl -s http://127.0.0.1:5000/health` (z `X-API-Key` jeśli ustawiony)
- [ ] `./scripts/run-tests.sh --skip-e2e`
- [ ] Flota → Test przy Gemini / Claude / custom endpoint
- [ ] Topbar → 🔌 clude → Claude Sonnet 4.6 → wyszukiwanie
- [ ] Import krótkiego folderu (+ opcjonalnie Wymuś OCR)

## Uwagi

- **Claude API jest płatne** — brak trwałego free tieru Anthropic
- **Embeddingi** zawsze przez Ollama (`ollama pull nomic-embed-text`)
- Nie commituj: `.env`, `.providers.json`, `.llm_config.json`

Pełny changelog: [CHANGELOG.md](../CHANGELOG.md)
