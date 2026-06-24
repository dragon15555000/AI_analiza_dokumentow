# AI Analiza Dokumentów

System RAG do przeszukiwania i analizy dokumentów. Wrzucasz pliki (PDF, DOCX, XLSX, skany), zadajesz pytania po polsku — dostajesz odpowiedź z dokładnymi cytatami z dokumentów.

*RAG system for document search and analysis. Upload files (PDF, DOCX, XLSX, scanned images), ask questions in natural language — get answers with precise source citations.*

---

Projekt zaczął się od potrzeby szybkiego przeszukiwania dużej liczby umów i dokumentów prawnych. Z czasem rozrósł się o kilka funkcji, które okazały się przydatne w praktyce.

---

## Co robi

Odpowiedź generuje pierwszy model LLM, ale zanim trafi do użytkownika, drugi model ją weryfikuje — sprawdza czy każde twierdzenie ma pokrycie w znalezionych fragmentach. Jeśli nie, odpowiedź jest poprawiana. Redukuje to hallucynacje, które w przypadku dokumentów prawnych czy finansowych są szczególnie problematyczne.

Poza standardowym wyszukiwaniem:

- **Tryb detektyw** — briefing śledczy w sekcjach (Co wiemy / Analiza / Wnioski / Pytania), oznaczanie anomalii i rozbieżności tagami
- **Sieć powiązań** — interaktywny graf D3.js z osobami, firmami, kwotami i relacjami między nimi wyciągniętymi przez LLM z dokumentów
- **Forensyka Excel** — wykrywa ślady Goal Seek, rozbieżności SUM/formuła, ukryte wiersze, zewnętrzne odwołania
- **Text-to-SQL** — gdy skonfigurowana baza, można zadawać pytania do tabel SQL w języku naturalnym
- **Porównanie dokumentów** — dwa pliki, jedno pytanie o różnice

Obsługiwane formaty: PDF, DOCX, XLSX/XLS (wszystkie arkusze), CSV, JSON, MD, TXT, obrazy (OCR via Tesseract).

---

## Stos

Backend w Pythonie 3.12 + Flask, serwer produkcyjny Waitress. Baza wektorowa Qdrant (Cloud lub lokalny). Embeddingi: nomic-embed-text 768-dim przez Ollama. LLM: Llama 3 przez Ollama, albo dowolny model przez OpenRouter lub Groq — można przełączać w trakcie działania aplikacji. Frontend Bootstrap 5 + D3.js.

---

## Uruchomienie (lokalny dev, Linux)

Wymaga zainstalowanego Ollama z modelami `llama3` i `nomic-embed-text`. Konto Qdrant Cloud jest darmowe (plan free: 1 GB RAM).

```bash
git clone <repo-url>
cd AI_analiza_dokumentow
chmod +x scripts/setup-local-dev.sh
./scripts/setup-local-dev.sh --system-deps --pull-models
```

Trzy terminale (lub tmux):
```bash
ollama serve
cd .local && ./qdrant
set -a && source .env && set +a
export SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
./venv/bin/python app.py
```

`curl http://127.0.0.1:5000/health` — powinno zwrócić status wszystkich komponentów.

Jeśli wolisz lokalny Qdrant zamiast Cloud, wystarczy zmienić jedną linię w `.env`.

`SECRET_KEY` nie jest ładowany przez aplikację z `.env`. W produkcji ustaw go jako zmienną środowiskową procesu albo wstrzyknij przez mechanizm secrets, np. systemd `Environment=`, Docker Secrets lub Kubernetes Secret. Wszystkie repliki aplikacji muszą dostać tę samą wartość, inaczej podpisane ciasteczka sesji Flaska nie będą działały między instancjami.

---

*Licencja proprietary. Kod nie jest open-source.*


---

## Testowanie i walidacja (Suite testowy)

Projekt posiada kompletny zestaw testów jednostkowych i integracyjnych, weryfikujący poprawność działania kluczowych elementów architektury. Suite testowy **nie wymaga** aktywnych kluczy API, baz danych ani zewnętrznych zależności (np. binarnego Tesseracta) do uruchomienia podstawowych testów.

### Zakres pokrycia testami (26 testów):
1. **Import Smoke Tests (`test_import_smoke.py`)**: Gwarantuje, że wszystkie kluczowe moduły projektu ładują się bez błędów i bez natychmiastowego odpytywania chmury czy odczytu sekretów.
2. **Mockowana konfiguracja (`test_config.py`)**: Sprawdza, czy klient LLM startuje poprawnie z mockowanymi kluczami bez rzucania błędów inicjalizacji.
3. **Obsługa brakujących plików (`test_missing_file.py`)**: Weryfikuje, czy próba odczytu nieistniejącego pliku przez parser jest logowana i zwraca bezpieczny pusty ciąg zamiast wywołania unhandled crash.
4. **Ekstrakcja tekstu (`test_extraction.py`)**: Sprawdza poprawność parsowania plików tekstowych na bezpiecznym sztucznym dokumencie (`tests/fixtures/simple_document.txt`).
5. **OCR Fallback (`test_ocr_fallback.py`)**: Testuje automatyczną obsługę sytuacji, gdy silnik OCR Tesseract nie jest zainstalowany w systemie operacyjnym (symulowane za pomocą mocków). Silnik łapie wyjątek, loguje ostrzeżenie i zwraca pusty tekst bez wywoływania crashu.
6. **Struktura wyników analizy (`test_analysis_result_shape.py`)**: Testuje endpoint `/analyze` przy użyciu Flask Test Clienta. Gwarantuje, że przy pustym zapytaniu zwracane jest przejrzyste `success: False` wraz z opisem błędu, a przy poprawnym zapytaniu zwracana jest kompletna i stabilna struktura kluczy JSON.
7. **Bezpieczeństwo SQL (`test_sql_safety.py`)**: Testuje mechanizmy zabezpieczeń przed SQL Injection (usuwanie średników, komentarzy blokowych, niebezpiecznych słów kluczowych).
8. **Odporność klienta LLM (`test_llm_client.py` i `test_llm_retry.py`)**: Weryfikuje zachowanie klienta przy kodach 429 (Rate Limit) i 500 (Internal Server Error) z użyciem strategii ponowień (backoff/retry).

### Uruchomienie testów:
Upewnij się, że masz aktywne środowisko wirtualne:
```bash
source venv/bin/activate
pytest -v
```

### Ograniczenia:
- Testy rzeczywistego OCR i wyszukiwania wektorowego w Qdrant wymagają działających instancji lokalnych lub poprawnie ustawionych zmiennych środowiskowych w `.env` i są wyłączone z unit testów.
