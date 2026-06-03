# AI Document Analysis / AI Analiza Dokumentów

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Waitress-000000?logo=flask)](https://flask.palletsprojects.com/)
[![Qdrant](https://img.shields.io/badge/Qdrant-Vector%20DB-DC244C)](https://qdrant.tech/)
[![License](https://img.shields.io/badge/License-Proprietary-red.svg)](#)

---

## EN

A production-grade **RAG (Retrieval-Augmented Generation)** system for intelligent document analysis. Upload files, ask questions in natural language — the system retrieves the most relevant passages and synthesises a precise, source-cited answer using a dual-LLM pipeline.

### Architecture

```
User question
      ↓
Semantic search  →  Qdrant vector DB  (nomic-embed-text 768-dim)
      ↓
LLM Generator    →  answer with source citations
      ↓
LLM Critic       →  fact-checks every claim against retrieved passages
      ↓
Verified answer  →  User
```

### Key Technical Highlights

| Feature | Details |
|---|---|
| **Dual-LLM verification** | Generator + Critic pattern — second model validates every factual claim |
| **Hybrid search** | RAG + optional Text-to-SQL for structured data queries |
| **5 analysis modes** | Standard · Detective briefing · Legal · Inconsistency detection · Data extraction |
| **Connection network** | D3.js force-directed graph of entities (people, companies, amounts) extracted by LLM; SSE streaming progress |
| **Excel forensics** | Detects Goal Seek traces, formula/cache mismatches, hidden rows, external references |
| **OCR pipeline** | Tesseract-based; auto-detected; status indicator in UI |
| **LLM fleet** | Provider ranking by latency/availability; auto-routing per task type; supports Ollama, OpenRouter, Groq |
| **Streaming** | Word-by-word response + live token counter |
| **Multi-collection** | Create, switch, bulk-delete collections; metadata reports (JSON + DOCX export) |
| **Production server** | Waitress + systemd service; auto-start on WSL2 |

### Supported Formats

PDF · DOCX · XLSX/XLS (all sheets + formulas) · CSV · JSON · MD · TXT · scanned images (OCR)

### Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3.12 + Flask / Waitress |
| Vector DB | Qdrant Cloud or local Qdrant |
| Embeddings | nomic-embed-text 768-dim (via Ollama) |
| LLM | Llama 3 via Ollama · OpenRouter (50+ models) · Groq |
| Frontend | Bootstrap 5 + D3.js + vanilla JS |
| OCR | Tesseract (optional) |
| SQL | pyodbc / psycopg2 / mysql-connector |

### Quick Start (local dev, Linux)

```bash
git clone <repo-url>
cd AI_analiza_dokumentow
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

Three terminals needed (or tmux):
```bash
# 1. LLM
ollama serve
# 2. Vector DB
cd .local && ./qdrant
# 3. App
set -a && source .env && set +a && ./venv/bin/python app.py
```

Health check: `curl http://127.0.0.1:5000/health`

### API Endpoints (selected)

```
POST /query                     — semantic search + LLM answer
POST /upload                    — upload documents
POST /network                   — build entity connection graph (SSE)
GET  /api/llm/fleet             — provider ranking
POST /api/llm/fleet/probe       — latency probe
GET  /health                    — system status (Qdrant, LLM, OCR, parsers)
```

---

## PL

Produkcyjny system **RAG (Retrieval-Augmented Generation)** do inteligentnej analizy dokumentów. Wgrywasz pliki, zadajesz pytania w języku naturalnym — system wyszukuje semantycznie najbardziej trafne fragmenty i syntetyzuje precyzyjną odpowiedź z cytatami, weryfikowaną przez drugi model LLM.

### Główne cechy techniczne

| Funkcja | Szczegóły |
|---|---|
| **Dual-LLM (Writer-Critic)** | Generator tworzy odpowiedź, Krytyk weryfikuje każde twierdzenie względem źródeł |
| **Wyszukiwanie hybrydowe** | RAG + opcjonalne Text-to-SQL dla zapytań do baz danych |
| **5 trybów analizy** | Standardowy · Detektyw · Prawny · Niespójności · Ekstrakcja danych |
| **Sieć powiązań** | Graf D3.js encji (osoby, firmy, kwoty) wyciąganych przez LLM; streaming SSE |
| **Forensyka Excel** | Wykrywanie Goal Seek, rozbieżności SUM/formuła, ukrytych wierszy, zewnętrznych odwołań |
| **OCR** | Tesseract; automatyczne wykrywanie; wskaźnik statusu w UI |
| **Flota LLM** | Ranking dostawców wg opóźnienia; auto-routing per typ zadania; Ollama, OpenRouter, Groq |
| **Streaming** | Odpowiedź słowo po słowie + licznik tokenów |
| **Multi-kolekcja** | Tworzenie, przełączanie, bulk-delete; eksport raportów JSON + DOCX |

### Szybki start

```bash
git clone <repo-url>
cd AI_analiza_dokumentow
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

---

## License / Licencja

Proprietary — all rights reserved. Commercial licensing inquiries welcome.
