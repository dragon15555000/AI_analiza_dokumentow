# MZK RAG — Inteligentny System Śledczy

System RAG (Retrieval-Augmented Generation) do analizy dokumentacji śledczej z wykorzystaniem lokalnego LLM (Ollama) i wektorowej bazy danych Qdrant Cloud.

## Funkcje

### Wyszukiwanie
- **5 trybów analizy**: Standardowy, Detektyw (anomalie), Prawny (przepisy), Niespójności, Ekstrakcja danych
- **Weryfikacja 2× LLM** — Generator odpowiada, Krytyk weryfikuje każde twierdzenie względem źródeł
- **Filtrowanie po pliku** — wyszukiwanie ograniczone do wybranego dokumentu
- **Ścieżka źródłowa** — każdy wynik pokazuje Windows path + przycisk "Otwórz"

### Baza wiedzy
- Import dokumentów: PDF, DOCX, XLSX (wszystkie arkusze + formuły), CSV, MD, JSON
- Chunking z nakładką 200 znaków — zachowanie kontekstu na granicy chunków
- Dedulikacja po treści (MD5 chunka) — ponowny import nie duplikuje danych
- Progress w czasie rzeczywistym (SSE) — podgląd pliku po pliku
- Przeglądarka folderów WSL

### Zarządzanie bazą
- Przeglądarka dokumentów z checkboxami — zaznacz i usuń wybrane pliki
- Automatyczne wykrywanie szumu (licencje, logi, tokeny URL)
- Czyszczenie kolekcji + pełny re-indeks jednym kliknięciem
- Zarządzanie wieloma kolekcjami Qdrant (tworzenie, przełączanie, usuwanie)
- Statystyki zużycia pamięci (szacunek na podstawie liczby wektorów)

### AI z bazy
- Dynamiczne sugestie pytań generowane przez Llama3 z losowej próbki dokumentów
- Cache 30 minut, odświeżanie on-demand

## Wymagania

- Python 3.10+
- [Ollama](https://ollama.ai/) z modelami: `llama3`, `nomic-embed-text`
- Konto [Qdrant Cloud](https://cloud.qdrant.io/) (plan Free wystarczy)
- WSL2 (Windows Subsystem for Linux) — opcjonalnie, dla integracji z Windows

## Instalacja

```bash
git clone https://github.com/marcin-gallos/mzk-rag.git
cd mzk-rag
pip install -r requirements.txt
cp .env.example .env
# Uzupełnij .env swoimi danymi Qdrant Cloud
nano .env
python app.py
```

Aplikacja dostępna pod: `http://localhost:5000`

## Konfiguracja (.env)

```env
QDRANT_URL=https://YOUR-CLUSTER.cloud.qdrant.io
QDRANT_KEY=your_qdrant_api_key
ACTIVE_COLLECTION=mzk_documents
OLLAMA_URL=http://127.0.0.1:11434
```

## Automatyczny start (WSL2 + Windows)

Serwis systemd (`/etc/systemd/system/mzk_web.service`) uruchamia aplikację automatycznie przy starcie WSL2.  
Skrypt VBS w folderze Windows Startup budzi WSL2 i uruchamia Ollama przy logowaniu do Windowsa.

## Architektura

```
Przeglądarka → Flask (WSL2) → Qdrant Cloud (wektory)
                    ↕
              Ollama (Windows)
              ├── nomic-embed-text  (embeddingi)
              └── llama3            (synteza + weryfikacja)
```

## Stos technologiczny

| Komponent | Technologia |
|---|---|
| Backend | Python / Flask |
| Wektorowa baza | Qdrant Cloud |
| Embeddingi | nomic-embed-text (768 dim, lokalnie) |
| LLM | Llama3 8B (lokalnie via Ollama) |
| Frontend | Bootstrap 5, vanilla JS |
| Chunking | Overlap 200 znaków, granice zdań |
| Dedup | MD5 treści chunka |
