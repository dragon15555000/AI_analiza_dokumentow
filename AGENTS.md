# AGENTS.md

## Cursor Cloud specific instructions

### Product

Single Flask RAG app (`app.py`) for document Q&A. UI at `http://localhost:5000`. See `README.md` for features and `.env.example` for configuration.

### Required runtime services

| Service | Port | Notes |
|---------|------|--------|
| **Qdrant** | `6333` (local) or cloud URL | App **exits on import** without `QDRANT_URL` and `QDRANT_KEY`. Collection auto-created on startup if missing (768 dim). |
| **Ollama** | `11434` | Required for embeddings (`nomic-embed-text`) and default chat (`llama3`). |
| **Flask app** | `5000` | Dev: `./venv/bin/python app.py` from repo root. Prod: Waitress via `wsgi.py` / `ai_analiza.service`. |

### First-time VM setup (not in update script)

```bash
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

Then start `ollama serve`, `.local/qdrant`, and `./venv/bin/python app.py` (see README „Dev lokalny”).  
`./scripts/setup-local-dev.sh --help` for options.

Optional OCR: `tesseract-ocr`, `tesseract-ocr-pol`, `poppler-utils`.

### Security defaults

- App binds to **`127.0.0.1`** by default (`APP_HOST`). Set `APP_HOST=0.0.0.0` only behind a trusted reverse proxy.
- Optional **`APP_API_KEY`**: API routes require `X-API-Key` (SSE import: `?api_key=`). UI stores key in `sessionStorage`.
- File import/browse limited to **`SEARCH_ROOTS`** (or `~`, `/mnt`, repo root if unset).
- Do not commit: `.env`, `.llm_config.json`, `.sql_config.json`, `embedding_cache.db`, `.local/`.

### Common commands

| Task | Command |
|------|---------|
| Local dev bootstrap | `./scripts/setup-local-dev.sh` |
| Install Python deps | `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt` |
| Syntax check | `./venv/bin/python -m py_compile app.py wsgi.py` |
| Health | `curl -s http://127.0.0.1:5000/health` (pola `ocr`, `file_parsers`) |
| Import (SSE) | `curl -sN 'http://127.0.0.1:5000/import/stream?folder=/path&ext=txt'` |

No pytest/ruff in repo — use `py_compile` for sanity checks.

### Gotchas

- **Embeddings always use Ollama** (`nomic-embed-text`), even when chat uses OpenRouter.
- **LLM config from UI** writes `.llm_config.json` — overrides `.env` LLM vars at startup.
- Reinstalling Python packages does not restart Flask/Ollama/Qdrant — restart those after dependency changes.

---

## Instrukcje dla AI coderów (pełny kontekst)

### Architektura

```
app.py              — backend Flask (wszystkie endpointy)
templates/index.html — frontend (Bootstrap 5 + D3.js)
wsgi.py             — Waitress (produkcja)
scripts/setup-local-dev.sh — bootstrap dev lokalny
```

### Konwencje

- LLM: tylko `call_llm()` / `stream_llm_tokens()`
- Qdrant: `get_qdrant_client()`
- API: `jsonify({"success": True/False, ...})`
- SSE: `data: {"event": ..., "data": ...}\n\n` — nie zmieniaj formatu
- JS: unikaj `innerHTML` z danymi LLM/użytkownika — `textContent` / `escapeHtml`
- Nie używaj `os.system()` — `subprocess.run()` bez `shell=True`
- Wektor: **768 dim** — nie zmieniaj bez migracji kolekcji

### Czego nie robić

- Nie rozdzielaj `app.py` bez zgody użytkownika
- Nie commituj sekretów ani `.llm_config.json`
- Nie dodawaj zależności bez `requirements.txt`

Szczegóły produktu i roadmap: README.md, IMPROVEMENT_PLAN.md, GitHub Issues.

*Ostatnia aktualizacja: czerwiec 2026*
