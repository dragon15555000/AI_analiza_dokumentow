# AI Analiza Dokumentów

> **⚠️ OPROGRAMOWANIE PROPRIETARY / WŁASNOŚĆ INTELEKTUALNA PRYWATNA**
>
> Ten projekt **nie jest open source**. Wszystkie prawa autorskie i prawa własności intelektualnej należą wyłącznie do Właściciela.
> Kopiowanie, modyfikowanie, rozpowszechnianie lub jakiekolwiek inne wykorzystanie bez pisemnej zgody jest **ścisłe zabronione**.
> Kod chroniony prawem polskim i międzynarodowymi traktatami.
>
> Możliwe jest uzyskanie płatnej licencji komercyjnej / wdrożeniowej.
> W sprawie licencji i warunków komercyjnych — kontakt z właścicielem.
>
> Licencja znajduje się w pliku [LICENSE](LICENSE).

---

Lokalny system RAG (Retrieval-Augmented Generation) do inteligentnej analizy i przeszukiwania dokumentów. Działa w pełni offline — dane nigdy nie opuszczają Twojego komputera.

## Na czym polega?

Wgrywasz swoje dokumenty (PDF, Word, Excel, CSV, JSON, Markdown) i możesz zadawać im pytania w języku naturalnym. Zamiast ręcznego przeszukiwania setek plików — system sam znajduje odpowiednie fragmenty i syntetyzuje odpowiedź.

```
Ty piszesz pytanie
        ↓
System szuka semantycznie w bazie wektorów (Qdrant Cloud)
        ↓
Lokalny LLM (Llama3) analizuje znalezione fragmenty
        ↓
Dostajesz konkretną odpowiedź z cytatami źródłowymi
```

## Możliwości

### Wyszukiwanie i analiza
- **5 trybów analizy** — standardowy, detektyw (anomalie), prawny (przepisy), niespójności, ekstrakcja danych
- **Weryfikacja 2× LLM** — pierwszy model odpowiada, drugi sprawdza czy nie zmyślił (Generator + Krytyk)
- **Streaming** — odpowiedź pojawia się słowo po słowie, wyniki widoczne od razu
- **Filtrowanie** — szukaj w całej bazie lub tylko w wybranym pliku

### Importowanie dokumentów
- Obsługuje: PDF, DOCX, XLSX/XLS (wszystkie arkusze + formuły), CSV, JSON, MD, TXT
- Pasek postępu w czasie rzeczywistym — widać każdy plik
- Deduplikacja po treści — ten sam tekst nie wchodzi dwa razy
- Chunking z nakładką 200 znaków — zachowanie kontekstu na granicach

### Forensyka Excel
- Wykrywa **ślady Goal Seek** (ekstremalnie precyzyjna liczba = ktoś cofał obliczenia)
- Weryfikuje czy formuły zgadzają się z zapisanymi wartościami
- Wykrywa **ukryte wiersze i kolumny**
- Komentarz LLM co anomalia może oznaczać

### Zarządzanie bazą
- Przeglądarka dokumentów z możliwością usuwania (checkbox + bulk delete)
- Automatyczne wykrywanie śmieci (licencje, logi, zaszyfrowane nazwy)
- Wiele kolekcji — tworzenie, przełączanie, statystyki zużycia
- Otwarcie pliku źródłowego jednym kliknięciem (Windows Explorer)

### Wizualizacja
- **Sieć powiązań** (D3.js) — LLM wyciąga osoby, firmy, kwoty i rysuje graf relacji
- Kolory krawędzi: czerwony = przepływ finansowy, fioletowy = zatrudnienie, zielony = przetarg
- **Export do DOCX** — raport z odpowiedzią LLM i fragmentami źródłowymi gotowy do wydruku

## Stos technologiczny

| Komponent | Technologia |
|---|---|
| Backend | Python 3.12 + Flask / Gunicorn |
| Baza wektorowa | Qdrant Cloud (darmowy plan wystarczy) |
| Embeddingi | nomic-embed-text 137M (768 dim, lokalnie) |
| LLM | Ollama (lokalnie) + OpenRouter (opcjonalnie, darmowe modele) |
| Frontend | Bootstrap 5 + D3.js + vanilla JS |
| Chunking | 1000 znaków, nakładka 200, granice zdań |

## Wymagania

- Python 3.10+
- [Ollama](https://ollama.ai/) z modelami: `llama3`, `nomic-embed-text`
- Konto [Qdrant Cloud](https://cloud.qdrant.io/) (plan Free: 1 GB RAM, 4 GB dysk)
- Windows + WSL2 (lub Linux bezpośrednio)

## Instalacja

```bash
git clone https://github.com/dragon15555000/AI_analiza_dokumentow.git
cd AI_analiza_dokumentow
pip install -r requirements.txt
cp .env.example .env
nano .env          # wpisz swoje dane Qdrant Cloud
python app.py
```

Otwórz: `http://localhost:5000`

### Opcjonalnie: OCR dla skanów PDF i obrazów

Jeśli chcesz, żeby aplikacja potrafiła czytać **zeskanowane dokumenty** (PDF bez warstwy tekstowej, zdjęcia, zrzuty ekranu), musisz zainstalować silnik OCR:

```bash
# 1. Zainstaluj Tesseract + polski język + narzędzie do PDF
sudo apt update
sudo apt install tesseract-ocr tesseract-ocr-pol poppler-utils -y

# 2. Zainstaluj pakiety Python
pip install pytesseract pdf2image Pillow
```

Po instalacji aplikacja automatycznie użyje OCR dla plików, z których nie da się wyciągnąć tekstu normalnymi metodami (np. skany PDF, zdjęcia dokumentów).

**Uwaga:** OCR jest wolniejszy, dlatego jest uruchamiany tylko jako fallback.

## Konfiguracja (.env)

```env
QDRANT_URL=https://twoj-klaster.cloud.qdrant.io
QDRANT_KEY=twoj_klucz_api
ACTIVE_COLLECTION=dokumenty
OLLAMA_URL=http://127.0.0.1:11434
LLM_MODEL=llama3:latest

# OpenRouter (opcjonalnie — przełącznik w UI)
# LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free
OPENROUTER_MODEL_VERIFY=google/gemini-2.0-flash-exp:free
# OPENROUTER_FALLBACK_TO_OLLAMA=true   # automatyczny fallback przy limicie
```

**Zalecane darmowe modele OpenRouter (2026):**
- `meta-llama/llama-3.3-70b-instruct:free` — obecnie najlepszy stosunek jakości do limitów
- `google/gemini-2.0-flash-exp:free` — bardzo szybki
- `qwen/qwen-2.5-7b-instruct:free` — stabilny

Aplikacja ma wbudowaną obsługę limitów (retry + exponential backoff + opcjonalny fallback na Ollama).

## Bezpieczeństwo

> **Uwaga:** Aplikacja pozwala na generowanie i wykonywanie zapytań SQL przez LLM (funkcja Text-to-SQL).  
> Używaj tej funkcji **tylko** z zaufanymi bazami danych i zawsze sprawdzaj wygenerowane zapytania przed wykonaniem, szczególnie w trybie zapisu (`/sql/write`).

Aplikacja jest przeznaczona do użytku wewnętrznego / instytucjonalnego. Ze względu na specyfikę systemów RAG (Prompt Injection, LLM-generated SQL) zalecane jest:
- Ograniczanie dostępu do interfejsu
- Regularne audyty
- Używanie trybu tylko-do-odczytu gdzie to możliwe

## Automatyczny start (WSL2 + Windows)

Serwis systemd uruchamia aplikację automatycznie przy starcie WSL2.

### Produkcyjny serwer (zalecane)

Zamiast Flask dev servera używamy **waitress**.

**Szybka migracja:**

```bash
# 1. Zainstaluj waitress
source venv/bin/activate
pip install waitress

# 2. Skopiuj serwis
sudo cp mzk_web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mzk_web

# 3. Sprawdź status
sudo systemctl status mzk_web
```

Plik `mzk_web.service` jest w repozytorium (gotowy do użycia).

Skrypt VBScript w folderze Windows Startup budzi WSL2 i uruchamia Ollama przy logowaniu.

---

*Działa w pełni lokalnie — żadne dokumenty nie są wysyłane do zewnętrznych serwerów AI.*

---

# AI Document Analysis (English)

A local RAG system for intelligent document search and analysis. Runs fully offline — your data never leaves your machine.

Upload your documents (PDF, Word, Excel, CSV, JSON, Markdown) and ask questions in natural language. Instead of manually searching hundreds of files — the system finds relevant passages and synthesizes an answer.

**Key features:** semantic search, 5 analysis modes, dual-LLM verification, Excel forensics (Goal Seek detection, hidden rows, formula verification), network graph visualization, DOCX export, multi-collection management.

**Stack:** Python/Flask · Qdrant Cloud · Ollama + OpenRouter (free models) · nomic-embed-text · Bootstrap 5 · D3.js

---

## Licencja i prawa własności intelektualnej

**Ten projekt jest oprogramowaniem zamkniętym (proprietary software).**

- Wszystkie prawa autorskie, prawa do kodu źródłowego, dokumentacji, wzorów, algorytmów i interfejsu użytkownika należą wyłącznie do Właściciela.
- Projekt **nie jest udostępniany na żadnej licencji open source** (MIT, Apache, GPL itp. nie mają zastosowania).
- Jakiekolwiek kopiowanie, modyfikowanie, integracja z innymi systemami, sprzedaż, wynajem lub publiczne udostępnianie bez wyraźnej pisemnej zgody Właściciela jest **zabronione** i będzie ścigane.
- Plik [LICENSE](LICENSE) zawiera pełny tekst licencji (wersja polska + angielska).

### Licencje komercyjne

Właściciel jest otwarty na rozmowy w sprawie płatnych licencji, w tym:
- licencja komercyjna na użytkowanie wewnętrzne
- licencja wdrożeniowa / integracyjna
- licencja OEM / white-label
- licencja dla instytucji (urzędy, sądy, firmy śledcze, kancelarie)

Jeśli jesteś zainteresowany wykorzystaniem tego narzędzia w celach komercyjnych lub instytucjonalnych — skontaktuj się z właścicielem.

*Możliwość generowania przychodu z tego projektu jest brana pod uwagę.*

---

*© 2025 Właściciel projektu. Wszelkie prawa zastrzeżone.*
