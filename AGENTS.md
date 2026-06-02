# AGENTS.md

## Cursor Cloud specific instructions

### Product

Single Flask RAG app (`app.py`) for document Q&A. UI at `http://localhost:5000`. See `README.md` for features and `.env.example` for configuration.

### Required runtime services

| Service | Port | Notes |
|---------|------|--------|
| **Qdrant** | `6333` (local) or cloud URL | App **exits on import** without `QDRANT_URL` and `QDRANT_KEY`. Collection auto-created on startup if missing (768 dim). |
| **Ollama** | `11434` | Required for embeddings (`nomic-embed-text`) and default chat (`llama3`). |
| **Flask app** | `5000` | **Dev**: `./venv/bin/python app.py`<br>**Polecane (user service)**: `./restart-app.sh --user`<br>**Pełna prod**: Waitress + `ai_analiza-user.service` (systemd --user) lub systemowe `ai_analiza.service`.<br><br>**Domyślny provider LLM**: **OpenRouter** (dla wszystkiego poza importem/wektoryzacją, które zawsze idą przez Ollama + nomic-embed-text). |

### First-time VM setup (not in update script)

```bash
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

Then start `ollama serve`, `.local/qdrant`, and `./venv/bin/python app.py` (see README „Dev lokalny”).  
Domyślnie aplikacja używa teraz **OpenRouter** do zadań LLM (chat, analiza, sieć powiązań). Import/wektoryzacja zawsze zostaje na Ollama.

`./scripts/setup-local-dev.sh --help` for options.

Optional OCR: `tesseract-ocr`, `tesseract-ocr-pol`, `poppler-utils`.

### Sposoby uruchamiania aplikacji (ważne dla AI)

| Tryb                    | Komenda                              | Kiedy używać                          | Zalety                              |
|-------------------------|--------------------------------------|---------------------------------------|-------------------------------------|
| **Dev (prosty)**        | `./venv/bin/python app.py`           | Szybkie testy, debugowanie            | Najprostszy                         |
| **Zalecany**            | `./restart-app.sh --user`            | Codzienna praca na WSL/laptopie       | Automatyczny restart, dobre logi    |
| **Produkcyjny (user)**  | `systemctl --user start ai_analiza`  | Dłuższe sesje, testy prod-like        | Najlepsza stabilność bez roota      |
| **Pełny system**        | `sudo systemctl start ai_analiza`    | Prawdziwy serwer / VM                 | Uruchamia się przy boocie jako root |

**Najczęściej używany przez deweloperów:**
```bash
./restart-app.sh --user
```

Logi wtedy sprawdzasz przez:
```bash
./restart-app.sh --user logs
# lub
journalctl --user -u ai_analiza -f
```

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
| Testy integracyjne | `./scripts/run-tests.sh` (health, Groq, SSE search; `--skip-e2e` bez search) |
| Health | `curl -s http://127.0.0.1:5000/health` (pola `ocr`, `file_parsers`) |
| Import (SSE) | `curl -sN 'http://127.0.0.1:5000/import/stream?folder=/path&ext=txt'` |

No pytest/ruff in repo — use `py_compile` for sanity checks.

### Gotchas

- **Embeddings always use Ollama** (`nomic-embed-text`), even when chat uses OpenRouter. `/health` `embedding.ok` reflects Ollama only.
- **Detective search**: `POST /search/stream` uses `query` + optional `chat_context`; never append chat history to `query` for embedding.
- **Context preview**: `GET /api/get_context?point_id=&file=&query=` — lazy-load fragmentu; wyniki wyszukiwania zwracają `snippet` + `point_id`, nie pełny `text`.
- **Self-update**: `POST /api/update/pull` (localhost only); then `POST /api/update/restart` or `./restart-app.sh --user`.
- **LLM config from UI** writes `.llm_config.json` — overrides `.env` LLM vars at startup.
- **Network graph** (D3): `renderNetwork` must stop `networkSimulation` before redraw; strength filter 1–12 matches backend cap. **`POST /network`** returns **SSE** (`progress` → `done`), not JSON — frontend uses `streamSSEPost()`, not `r.json()`. Use `fetchJson()` for JSON endpoints (`/analyze`, `/health`, …) to avoid `Unexpected token '<'` on HTML error pages.
- Reinstalling Python packages does not restart Flask/Ollama/Qdrant — restart those after dependency changes.

### Production update (user machine)

```bash
git fetch origin && git checkout master && git pull origin master
./restart-app.sh --user
```

Releases: tags `v2026.10+` on GitHub. See README „Aktualizacja produkcji”.

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
