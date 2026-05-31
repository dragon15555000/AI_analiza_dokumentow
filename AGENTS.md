# AGENTS.md

## Cursor Cloud specific instructions

### Product

Single Flask RAG app (`app.py`) for document Q&A. UI at `http://localhost:5000`. See `README.md` for features and `.env.example` for configuration.

### Required runtime services

| Service | Port | Notes |
|---------|------|--------|
| **Qdrant** | `6333` (local) or cloud URL | App **exits on import** without `QDRANT_URL` and `QDRANT_KEY` in `.env` or environment. |
| **Ollama** | `11434` | Required for embeddings (`nomic-embed-text`) and default chat (`llama3`). |
| **Flask app** | `5000` | Dev: `./venv/bin/python app.py` from repo root. |

### First-time VM setup (not in update script)

These system packages were needed on a minimal Ubuntu image:

```bash
sudo apt-get install -y python3.12-venv zstd
```

Optional OCR (not required for core RAG): `tesseract-ocr`, `tesseract-ocr-pol`, `poppler-utils`.

**Ollama:** install via https://ollama.ai/ then `ollama serve` (tmux session) and `ollama pull nomic-embed-text` + `ollama pull llama3`.

**Local Qdrant (dev without cloud):** standalone binary can live under `.local/` (gitignored). Example one-time download (x86_64 Linux):

```bash
mkdir -p .local && cd .local
curl -fsSL -o qdrant.tar.gz "https://github.com/qdrant/qdrant/releases/download/v1.13.6/qdrant-x86_64-unknown-linux-gnu.tar.gz"
tar -xzf qdrant.tar.gz && rm qdrant.tar.gz && chmod +x qdrant
# then in tmux from .local:
./qdrant
```

For local Qdrant, `QDRANT_KEY` in `.env` can be any non-empty string (e.g. `dev-local-key`).

`.env` for local stack (copy from `.env.example`):

```env
QDRANT_URL=http://127.0.0.1:6333
QDRANT_KEY=dev-local-key
OLLAMA_URL=http://127.0.0.1:11434
LLM_MODEL=llama3:latest
```

Production / real data should use **Qdrant Cloud** credentials instead.

### Running the app (tmux)

Use tmux for long-lived processes (`ollama serve`, Qdrant, Flask). Example Flask session:

```bash
cd /workspace && ./venv/bin/python app.py
```

Health check: `curl -s http://127.0.0.1:5000/health`

### Security defaults

- App binds to **`127.0.0.1`** by default (`APP_HOST`). Set `APP_HOST=0.0.0.0` only behind a trusted reverse proxy.
- Optional **`APP_API_KEY`**: when set, all API routes require header `X-API-Key` (SSE import uses `?api_key=`). The UI prompts once and stores the key in `sessionStorage`.
- File import/browse/open are limited to **`SEARCH_ROOTS`** (or `~`, `/mnt`, repo root if unset).

### Common commands

| Task | Command |
|------|---------|
| Install Python deps | `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt` |
| Syntax check | `./venv/bin/python -m py_compile app.py wsgi.py` |
| Import folder (SSE) | `curl -sN 'http://127.0.0.1:5000/import/stream?folder=/path&ext=txt'` |
| Search (JSON) | `POST /search` with `{"query":"...","limit":5}` |

There is **no** pytest/ruff/flake8 config in the repo; use `py_compile` for a quick sanity check.

### Gotchas

- **Embeddings always use Ollama** (`nomic-embed-text`), even when chat uses OpenRouter.
- **Collection must exist** before import: `POST /collections/create` with `{"name":"dokumenty","vector_size":768,"switch_to":true}` or create via UI.
- Reinstalling Python packages does not restart Flask/Ollama/Qdrant — restart those processes after dependency changes.
- `embedding_cache.db` is created beside `app.py` and is gitignored.
