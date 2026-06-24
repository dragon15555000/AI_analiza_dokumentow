# n8n Workflows — AI Analiza Dokumentów

## Stan integracji (audyt 2026-06-15)

| Serwis | Port | Status | Uwagi |
|--------|------|--------|-------|
| AI Analiza app | 5000 | ✓ działa | Flask/Waitress, wymaga X-API-Key |
| new-api (OpenAI proxy) | 3000 | ✓ działa | zwraca `new_api_error` bez tokenu |
| Qdrant | 6333 | ✓ działa | wersja 1.18.x |
| Port 5055 | 5055 | ✗ brak | nie istnieje w tym repozytorium |

**ai_crew** jako moduł nie istnieje w kodzie. Równoważny endpoint to `/agents/swarm/stream`.  
**SmartFix** jako endpoint nie istnieje. Równoważny: `/analyze`.

---

## Wymagania wstępne

1. n8n zainstalowany i działający  
2. Aplikacja uruchomiona: `./start --no-pull`  
3. Wartość `APP_API_KEY` z pliku `.env`

---

## Workflowy

### 01_manual_swarm_task.json
**Trigger:** ręczny  
**Endpoint:** `POST /agents/swarm/stream`  
**Co robi:** uruchamia swarm agentów na podanym zapytaniu, parsuje odpowiedź SSE  
**Uwaga:** endpoint zwraca `text/event-stream` — n8n buforuje całość, nie strumieniuje na żywo

### 02_webhook_analyze.json
**Trigger:** webhook POST `http://<n8n>/webhook/ai-analiza-task`  
**Endpoint:** `POST /analyze`  
**Co robi:** przyjmuje JSON `{ "query": "...", "collection": "..." }`, wywołuje analizę, zwraca odpowiedź  
**Przykład wywołania:**
```bash
curl -X POST http://localhost:5678/webhook/ai-analiza-task \
  -H "Content-Type: application/json" \
  -d '{"query": "co mówią dokumenty o budżecie?", "collection": "dokumenty"}'
```

### 03_health_check.json
**Trigger:** co 15 minut  
**Endpointy:** `GET /health`, `GET http://127.0.0.1:3000/v1/models`, `GET http://127.0.0.1:6333/healthz`  
**Co robi:** sprawdza dostępność wszystkich serwisów, przy problemie przechodzi do węzła "Send Alert"  
**Wymagane:** zastąp węzeł `Send Alert (placeholder)` rzeczywistym (Slack, email, webhook)

---

## Import do n8n

1. Otwórz n8n → **Workflows** → **Import from file**  
2. Wybierz plik `.json` z tego katalogu  
3. Przed pierwszym uruchomieniem:
   - zamień `PLACEHOLDER_APP_API_KEY` na wartość z `.env` (`APP_API_KEY`)
   - zamień `PLACEHOLDER_NEW_API_KEY` na klucz do new-api (workflow 03)
   - skonfiguruj węzeł alertów w workflow 03

---

## Znane ograniczenia

- `/agents/swarm/stream` i `/search/stream` używają SSE. n8n (HTTP Request) buforuje całą odpowiedź zamiast strumieniować — dla długich zapytań może przekroczyć timeout (domyślnie 120s, ustawiony w workflowie).
- `/health` nie wymaga `X-API-Key` jeśli `APP_API_KEY` jest puste w `.env` — sprawdź konfigurację.
- new-api na porcie 3000 to zewnętrzny serwis niezależny od tego repozytorium — brak gwarancji formatu odpowiedzi.
- Port 5055 ("Cockpit") nie istnieje w tym projekcie — nie dodano workflowu dla tego endpointu.
