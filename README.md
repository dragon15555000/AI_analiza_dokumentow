# AI Analiza Dokumentów / AI Document Analysis

> **⚠️ OPROGRAMOWANIE PROPRIETARY — WŁASNOŚĆ INTELEKTUALNA PRYWATNA**
>
> Ten projekt **nie jest open source**. Wszystkie prawa autorskie i prawa własności intelektualnej należą wyłącznie do Właściciela.
> Kopiowanie, modyfikowanie, rozpowszechnianie lub jakiekolwiek inne wykorzystanie bez pisemnej zgody jest **ścisłe zabronione**.
> Kod chroniony prawem polskim i międzynarodowymi traktatami.
> Szczegóły w pliku [LICENSE](LICENSE).

---

System RAG (Retrieval-Augmented Generation) do inteligentnej analizy i przeszukiwania dokumentów. Wgrywasz pliki, zadajesz pytania w języku naturalnym — system znajduje odpowiednie fragmenty i syntetyzuje precyzyjną odpowiedź z cytatami źródłowymi.

*A RAG (Retrieval-Augmented Generation) system for intelligent document analysis and search. Upload your files, ask questions in natural language — the system retrieves relevant passages and synthesizes a precise, source-cited answer.*

---

## Jak to działa / How it works

```
Pytanie użytkownika / User question
            ↓
Wyszukiwanie semantyczne w bazie wektorów (Qdrant Cloud)
Semantic search in vector database (Qdrant Cloud)
            ↓
LLM analizuje znalezione fragmenty / LLM analyses retrieved passages
            ↓
Odpowiedź z cytatami + weryfikacja przez drugi model (Krytyk)
Answer with citations + second-model verification (Critic)
```

---

## Możliwości / Features

### Wyszukiwanie i analiza / Search & Analysis
- **5 trybów analizy** — standardowy, detektyw (anomalie), prawny (przepisy), niespójności, ekstrakcja danych
- **Weryfikacja 2× LLM** — Generator odpowiada, Krytyk sprawdza każde twierdzenie względem źródeł
- **Wyszukiwanie hybrydowe** — łączy wyszukiwanie semantyczne z wyszukiwaniem po słowach kluczowych
- **Streaming** — odpowiedź pojawia się słowo po słowie
- **Historia zapytań** — podgląd poprzednich pytań i odpowiedzi

*5 analysis modes — standard, detective (anomalies), legal (regulations), inconsistencies, data extraction · Dual-LLM verification · Hybrid search · Streaming · Query history*

### Obsługiwane formaty / Supported formats
PDF · DOCX · XLSX / XLS (wszystkie arkusze + formuły) · CSV · JSON · MD · TXT · obrazy i skany (OCR)

*PDF · DOCX · XLSX/XLS (all sheets + formulas) · CSV · JSON · MD · TXT · scanned images (OCR)*

### Forensyka Excel / Excel Forensics
- Wykrywa **ślady Goal Seek** — ekstremalnie precyzyjna liczba to sygnał cofania obliczeń
- Weryfikuje zgodność formuł z wartościami zapisanymi w pamięci podręcznej
- Wykrywa **ukryte wiersze i kolumny**
- Komentarz LLM co dana anomalia może oznaczać

*Detects Goal Seek traces, formula vs. cached-value mismatches, hidden rows/columns, with LLM commentary on each finding.*

### Baza danych SQL / SQL Integration
- Konfiguracja połączenia z MS SQL Server / PostgreSQL / MySQL
- Zadawanie pytań do bazy w języku naturalnym (Text-to-SQL)
- Wektoryzacja danych z tabel SQL do Qdrant — przeszukiwanie semantyczne danych relacyjnych

*Natural language queries to SQL databases · Text-to-SQL · Vectorize SQL table data into Qdrant for semantic search.*

### LLM — Ollama lub OpenRouter / LLM Provider
- **Ollama** (domyślny) — lokalny Llama3, bez limitu zapytań
- **OpenRouter** — dostęp do dziesiątek modeli przez jeden klucz API (w tym darmowe: Llama3, Gemini, Qwen)
- Automatyczny retry przy limicie OpenRouter (429) + opcjonalny fallback na Ollama

*Ollama (local Llama3, no rate limits) or OpenRouter (dozens of models incl. free tier). Auto-retry on 429 + optional fallback.*

### Sieć powiązań / Connection Network
- LLM wyciąga osoby, firmy, kwoty, umowy i rysuje interaktywny graf (D3.js)
- Kolory krawędzi: czerwony = przepływ finansowy, fioletowy = zatrudnienie, zielony = przetarg
- Każda krawędź zawiera cytat z dokumentu jako dowód

*D3.js interactive graph: persons, companies, amounts, contracts · Evidence citations per edge.*

### Zarządzanie bazą / Collection Management
- Wiele kolekcji — tworzenie, przełączanie, statystyki zużycia
- Przeglądarka plików z możliwością usuwania (checkbox + bulk delete)
- Zbiorczy raport metadanych dla zaznaczonych plików (JSON + DOCX)
- Automatyczne wykrywanie śmieci · Przeglądarka dysków

*Multiple collections · Bulk delete · Metadata reports (JSON + DOCX) · Noise detection · Disk browser.*

### Export
- **DOCX** — raport z odpowiedzią LLM i fragmentami źródłowymi
- **CSV** — eksport wyników ekstrakcji danych

---

## Stos technologiczny / Tech Stack

| Komponent / Component | Technologia / Technology |
|---|---|
| Backend | Python 3.12 + Flask / Waitress |
| Baza wektorowa / Vector DB | Qdrant Cloud |
| Embeddingi / Embeddings | nomic-embed-text 137M (768 dim, via Ollama) |
| LLM | Llama3 via Ollama lub OpenRouter |
| Frontend | Bootstrap 5 + D3.js + vanilla JS |
| Chunking | 1000 znaków, nakładka 200, granice zdań |
| OCR | Tesseract (opcjonalnie / optional) |
| SQL | pyodbc / psycopg2 / mysql-connector |

---

## Wymagania / Requirements

- Python 3.10+
- [Ollama](https://ollama.ai/) z modelami: `llama3`, `nomic-embed-text` (lub klucz OpenRouter)
- Konto [Qdrant Cloud](https://cloud.qdrant.io/) (plan Free: 1 GB RAM, 4 GB dysk)
- Windows + WSL2 lub Linux

---

## Instalacja / Installation

```bash
git clone https://github.com/dragon15555000/AI_analiza_dokumentow.git
cd AI_analiza_dokumentow
pip install -r requirements.txt
cp .env.example .env
nano .env        # wpisz dane Qdrant Cloud / enter your Qdrant Cloud credentials
python app.py
```

Otwórz / Open: `http://localhost:5000`

### OCR (opcjonalnie / optional)

```bash
sudo apt update && sudo apt install tesseract-ocr tesseract-ocr-pol poppler-utils -y
pip install pytesseract pdf2image Pillow
```

### Produkcyjny serwer / Production server (Waitress)

```bash
# 1. Instalacja
pip install waitress

# 2. Skopiuj i włącz serwis systemd
sudo cp ai_analiza.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai_analiza

# 3. Status i logi
sudo systemctl status ai_analiza
sudo journalctl -u ai_analiza -f

# Lub użyj skryptu pomocniczego:
bash migrate_to_waitress.sh
```

---

## Baza wektorowa — Cloud lub lokalnie / Vector DB — Cloud or Local

Aplikacja działa z **Qdrant Cloud** (domyślnie) lub z **lokalnym Qdrant** bez żadnych limitów.
Zmiana wymaga tylko jednej linii w `.env` — kod aplikacji jest identyczny.

*The app works with Qdrant Cloud (default) or a local Qdrant instance with no limits. One line change in `.env` — no code changes.*

### Qdrant lokalnie — uruchomienie / Local Qdrant — setup

**Docker (zalecane):**
```bash
docker run -d --name qdrant \
  -p 6333:6333 \
  -v ~/qdrant_data:/qdrant/storage \
  qdrant/qdrant
```

**Bez Dockera (binarny):**
```bash
# Pobierz ze strony github.com/qdrant/qdrant/releases
tar xzf qdrant-x86_64-unknown-linux-gnu.tar.gz
./qdrant
```

Potem w `.env`:
```env
QDRANT_URL=http://localhost:6333
QDRANT_KEY=                      # zostaw puste
```

> ✅ **Kolekcja tworzy się automatycznie** — przy pierwszym uruchomieniu aplikacja sprawdza czy kolekcja z `.env` (`ACTIVE_COLLECTION`) istnieje i jeśli nie, tworzy ją samodzielnie. Nie trzeba nic klikać ani konfigurować.

| | Qdrant Cloud | Qdrant lokalny |
|---|---|---|
| Limit RAM | 1 GB (free) | brak |
| Limit dysk | 4 GB (free) | brak |
| Dostęp zdalny | ✅ | tylko localhost |
| Backup | automatyczny | ⚠️ ręczny |
| Wymaga Dockera | nie | opcjonalnie |

---

## Konfiguracja / Configuration (`.env`)

```env
# Qdrant Cloud
QDRANT_URL=https://your-cluster.cloud.qdrant.io
QDRANT_KEY=your_qdrant_api_key
ACTIVE_COLLECTION=dokumenty

# Ollama
OLLAMA_URL=http://127.0.0.1:11434
LLM_MODEL=llama3:latest

# OpenRouter (opcjonalnie / optional)
# LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free
OPENROUTER_MODEL_VERIFY=google/gemini-2.0-flash-exp:free
# OPENROUTER_FALLBACK_TO_OLLAMA=true

# SQL Server (opcjonalnie / optional)
SQL_SERVER=127.0.0.1
SQL_PORT=1433
SQL_DATABASE=YourDatabase
SQL_USER=sa
SQL_PASSWORD=YourPassword
```

**Zalecane darmowe modele OpenRouter:**
- `meta-llama/llama-3.3-70b-instruct:free`
- `google/gemini-2.0-flash-exp:free`
- `qwen/qwen-2.5-7b-instruct:free`

---

## Automatyczny start / Auto-start (WSL2 + Windows)

Serwis systemd (`ai_analiza.service`) uruchamia aplikację automatycznie przy starcie WSL2.
Skrypt VBScript w folderze Windows Startup budzi WSL2 i uruchamia Ollama przy logowaniu.

*`ai_analiza.service` auto-starts on WSL2 boot. VBScript in Windows Startup wakes WSL2 and launches Ollama on login.*

---

## Licencja / License

**Ten projekt jest oprogramowaniem zamkniętym (proprietary software).**

Wszelkie prawa autorskie należą wyłącznie do Właściciela. Kopiowanie, modyfikowanie lub dystrybucja bez pisemnej zgody jest zabroniona. Plik [LICENSE](LICENSE) zawiera pełny tekst (PL + EN).

Właściciel jest otwarty na rozmowy w sprawie płatnych licencji komercyjnych, wdrożeniowych, OEM i instytucjonalnych.

*All rights reserved. No open-source license applies. See [LICENSE](LICENSE). Commercial licensing inquiries welcome.*

---

*© 2025 Właściciel projektu. Wszelkie prawa zastrzeżone. / All rights reserved.*
