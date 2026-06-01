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
- **5 trybów analizy** — standardowy, **detektyw (briefing śledczy)**, prawny (przepisy), niespójności, ekstrakcja danych
- **Tryb Detektyw** — briefing w sekcjach (*Co wiemy → Analiza → Wnioski → Pytania*), tagi `[ANOMALIA]`, `[ROZBIEŻNOŚĆ]`, `[BRAK DOWODU]`; min. 12 fragmentów z wielu plików; **Tryb rozmowy** (`chat_context` nie psuje embeddingu)
- **Weryfikacja 2× LLM** — Generator odpowiada, Krytyk sprawdza każde twierdzenie względem źródeł
- **Wyszukiwanie hybrydowe** — RAG + SQL (gdy skonfigurowana baza)
- **Streaming** — odpowiedź słowo po słowie + licznik tokenów
- **Historia zapytań** — podgląd poprzednich pytań i odpowiedzi
- **Porównanie dwóch dokumentów** — zakładka Porównaj (LLM)

*5 analysis modes · Detective briefing · Dual-LLM verification · Hybrid RAG+SQL · Streaming · Query history · Document compare*

### Obsługiwane formaty / Supported formats
PDF · DOCX · XLSX / XLS (wszystkie arkusze + formuły) · CSV · JSON · MD · TXT · obrazy i skany (OCR)

*PDF · DOCX · XLSX/XLS (all sheets + formulas) · CSV · JSON · MD · TXT · scanned images (OCR)*

### Forensyka Excel / Excel Forensics
- Przycisk **Forensyka** przy plikach XLSX w przeglądarce dokumentów
- Wykrywa **ślady Goal Seek**, nadpisane komórki, rozjazdy SUM/AVERAGE, ukryte wiersze/kolumny, zewnętrzne odwołania
- Raport z kategoriami, `confidence_pct`, opcjonalny komentarz LLM

*Excel forensics UI · Goal Seek · formula/cache mismatch · hidden rows · LLM commentary.*

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
- LLM wyciąga osoby, firmy, kwoty, umowy i rysuje interaktywny graf (**D3.js force-directed**)
- Filtry: typ węzła, min. siła relacji (1–12), typ relacji, ukrywanie samotnych węzłów
- Zoom/pan, przeciąganie węzłów, panel dowodów przy kliknięciu krawędzi, eksport **SVG**
- Kolory krawędzi: finanse / zatrudnienie / przetarg / decyzja — grubość ∝ siła powiązania (liczba dowodów)

*D3.js graph · filters · evidence panel · SVG export · strength 1–12.*

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
| Baza wektorowa / Vector DB | Qdrant Cloud lub lokalny Qdrant |
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

### Szybki start — dev lokalny (Linux, bez Qdrant Cloud)

Wymaga wcześniej zainstalowanego [Ollama](https://ollama.ai/). Skrypt przygotuje `venv`, plik `.env` i binarkę Qdrant w `.local/`:

```bash
git clone https://github.com/dragon15555000/AI_analiza_dokumentow.git
cd AI_analiza_dokumentow
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

Na Ubuntu/Debian flaga `--system-deps` doinstaluje `python3.12-venv`, `zstd` i `curl` (sudo).  
`--pull-models` pobierze `nomic-embed-text` i `llama3` (wymaga działającego `ollama` w PATH).

**Uruchomienie aplikacji** (trzy osobne terminale lub sesje tmux):

```bash
# Terminal 1
ollama serve

# Terminal 2 (z katalogu repo)
cd .local && ./qdrant

# Terminal 3 (z katalogu repo)
set -a && source .env && set +a && ./venv/bin/python app.py
```

Sprawdzenie: `curl -s http://127.0.0.1:5000/health` → `"overall":"ok"`.  
UI: `http://localhost:5000`

Pomoc skryptu: `./scripts/setup-local-dev.sh --help`

### Instalacja z Qdrant Cloud (produkcja / dane w chmurze)

```bash
git clone https://github.com/dragon15555000/AI_analiza_dokumentow.git
cd AI_analiza_dokumentow
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env          # wpisz QDRANT_URL i QDRANT_KEY z https://cloud.qdrant.io/
set -a && source .env && set +a && ./venv/bin/python app.py
```

Otwórz / Open: `http://localhost:5000`

### OCR (opcjonalnie / optional)

Dla **zeskanowanych PDF** i **obrazów** (gdy brak warstwy tekstu). Aplikacja uruchamia OCR automatycznie — nie trzeba nic włączać w UI.

```bash
# System (Ubuntu/Debian)
sudo apt update && sudo apt install tesseract-ocr tesseract-ocr-pol poppler-utils -y

# Python (w venv)
cd AI_analiza_dokumentow
source venv/bin/activate
pip install pytesseract pdf2image Pillow
```

**Sprawdzenie:** topbar aplikacji pokazuje wskaźnik **⬤ OCR**:
- 🟢 zielony — Tesseract zainstalowany i gotowy
- 🟡 żółty — brak Tesseracta; najedź kursorem aby zobaczyć komendę instalacji

Albo przez API:

```bash
curl -s http://127.0.0.1:5000/health | python3 -m json.tool | grep -A8 '"ocr"'
```

Gdy `available: false`, pole `install_hint` zawiera gotową komendę do skopiowania.  
Szczegóły planu rozwoju OCR/Excel: [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) (macierz funkcji).

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

## Bezpieczeństwo / Security

> **Uwaga:** Aplikacja pozwala na generowanie i wykonywanie zapytań SQL przez LLM (Text-to-SQL).  
> Używaj **tylko** z zaufanymi bazami; sprawdzaj zapytania przed wykonaniem, szczególnie przy `/sql/write`.

- Domyślnie nasłuch na **`127.0.0.1`** (`APP_HOST`) — nie wystawiaj na sieć bez reverse proxy
- Opcjonalny **`APP_API_KEY`** — nagłówek `X-API-Key` na API (UI zapyta przy pierwszym wejściu)
- Import i otwieranie plików ograniczone do **`SEARCH_ROOTS`** (patrz `.env.example`)

## Automatyczny start / Auto-start (WSL2 + Windows)

Serwis systemd (`ai_analiza.service`) uruchamia aplikację automatycznie przy starcie WSL2.
Skrypt VBScript w folderze Windows Startup budzi WSL2 i uruchamia Ollama przy logowaniu.

*`ai_analiza.service` auto-starts on WSL2 boot. VBScript in Windows Startup wakes WSL2 and launches Ollama on login.*

---

## Uruchamianie jako użytkownik (systemd --user) – zalecane

Dla większości osób pracujących na WSL lub laptopie najlepszym rozwiązaniem jest **usługa użytkownika** (nie wymaga uprawnień roota).

### Instalacja

```bash
# 1. Skopiuj plik usługi
mkdir -p ~/.config/systemd/user
cp ai_analiza-user.service ~/.config/systemd/user/ai_analiza.service

# 2. Przeładuj i włącz usługę
systemctl --user daemon-reload
systemctl --user enable --now ai_analiza

# 3. (Opcjonalnie) Uruchamiaj usługę nawet po wylogowaniu
loginctl enable-linger $USER
```

### Zarządzanie

```bash
systemctl --user status ai_analiza          # status
journalctl --user -u ai_analiza -f          # logi na żywo
systemctl --user restart ai_analiza         # restart
systemctl --user stop ai_analiza            # stop
```

### Wygodny skrypt

W repozytorium znajduje się skrypt `restart-app.sh`, który obsługuje obie metody:

```bash
./restart-app.sh                 # klasyczny nohup
./restart-app.sh --user          # systemd user service (zalecane)
./restart-app.sh --user --stop   # zatrzymaj user service
```

### Różnice

| Sposób              | Zalety                              | Wady                          |
|---------------------|-------------------------------------|-------------------------------|
| `nohup`             | Bardzo prosty                       | Brak automatycznego restartu  |
| `systemd --user`    | Restart przy błędzie, logi, auto-start | Trochę więcej konfiguracji   |

---

## Produkcja (Waitress + systemd)

Zamiast Flask dev servera użyj **Waitress** i jednostki `ai_analiza.service`:

```bash
chmod +x migrate_to_waitress.sh
./migrate_to_waitress.sh
sudo systemctl status ai_analiza
```

### Aktualizacja produkcji z GitHub

**Zalecane (WSL / laptop):** `systemd --user` + skrypt:

```bash
cd ~/projects/AI_analiza_dokumentow   # dostosuj ścieżkę w ai_analiza-user.service
git fetch origin && git checkout master && git pull origin master
./restart-app.sh --user
```

**Z poziomu aplikacji (localhost):** ikona ⚙️ → Diagnostyka → **Aktualizacje** → Sprawdź → Pobierz → Restart  
Endpointy: `GET /api/update/status`, `POST /api/update/pull`, `POST /api/update/restart`

**Tagi wydania:** `v2026.10` (Detektyw), nowsze — w [Releases](https://github.com/dragon15555000/AI_analiza_dokumentow/releases).

### Diagnostyka

- `GET /health` — Qdrant, LLM, **embedding Ollama** (zawsze wymagany do RAG), OCR, parsery plików
- Modal startowy + kropki statusu w nagłówku (odświeżanie co 30 s)
- Przełącznik Qdrant **lokalny / cloud** w UI (`POST /qdrant/switch`)

---

## Licencja / License

**Ten projekt jest oprogramowaniem zamkniętym (proprietary software).**

Wszelkie prawa autorskie należą wyłącznie do Właściciela. Kopiowanie, modyfikowanie lub dystrybucja bez pisemnej zgody jest zabroniona. Plik [LICENSE](LICENSE) zawiera pełny tekst (PL + EN).

Właściciel jest otwarty na rozmowy w sprawie płatnych licencji komercyjnych, wdrożeniowych, OEM i instytucjonalnych.

*All rights reserved. No open-source license applies. See [LICENSE](LICENSE). Commercial licensing inquiries welcome.*

---

*© 2025 Właściciel projektu. Wszelkie prawa zastrzeżone. / All rights reserved.*
