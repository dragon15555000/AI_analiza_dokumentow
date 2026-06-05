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
