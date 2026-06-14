#!/usr/bin/env bash
# Automatyzacja zadań ai_crew na projekcie AI_analiza_dokumentow.
# Po każdym kroku testy uruchamiane automatycznie — błąd = przerwanie.

set -euo pipefail

PROJECT="/home/marcin/projects/AI_analiza_dokumentow"
CREW="uv run python /home/marcin/projects/ai_crew/main.py"

run_tests() {
    echo ">>> Uruchamiam testy..."
    cd "$PROJECT"
    uv run pytest tests/ -q || { echo "Testy nie przeszły. Przerwanie."; exit 1; }
    cd - > /dev/null
    echo ">>> Testy OK."
    echo ""
}

echo "=== AI Crew — upgrade AI_analiza_dokumentow ==="
echo "Projekt: $PROJECT"
echo ""

# ──────────────────────────────────────────────
# Krok 1
# ──────────────────────────────────────────────
echo "[ 1/2 ] Dodawanie HEALTH_CHECK_PROMPT do prompts.py..."
$CREW \
  "Dodaj do pliku prompts.py stałą HEALTH_CHECK_PROMPT = 'Czy system działa poprawnie?' na końcu pliku, po wszystkich istniejących definicjach." \
  --project "$PROJECT"

run_tests

# ──────────────────────────────────────────────
# Krok 2
# ──────────────────────────────────────────────
echo "[ 2/2 ] Dodawanie sanitize_sql_params do sql_safety.py..."
$CREW \
  "Dopisz do istniejącego pliku sql_safety.py funkcję sanitize_sql_params(params: dict) -> dict która filtruje wartości parametrów SQL — usuwa z wartości znaki niebezpieczne takie jak średnik, podwójny myślnik i komentarze blokowe /* */. Funkcja powinna zwracać oczyszczony słownik. Dopisz też test jednostkowy do tests/test_sql_safety.py (utwórz plik jeśli nie istnieje)." \
  --project "$PROJECT"

run_tests

# ──────────────────────────────────────────────
# Krok 3
# ──────────────────────────────────────────────
echo "[ 3/3 ] Integracja retry_api_call w llm_client.py..."
$CREW \
  "Zmodyfikuj wyłącznie llm_client.py oraz testy klienta LLM. Nie dodawaj tenacity, async, zmian w task_queue.py, app.py ani templates/. Owiń główne synchroniczne wywołanie API LLM dekoratorem retry_api_call z retry_utils.py (już istnieje w projekcie). Retry dla statusów 429, 500, 502, 503, 504. Brak retry dla 400, 401, 403, 404. Maksymalnie 5 prób, exponential backoff max 10 sekund. Dodaj testy w tests/test_llm_retry.py: (1) 429 retry -> sukces, (2) 500 retry -> sukces, (3) 400 brak retry, (4) 5 nieudanych prób kończy się wyjątkiem. W testach stubuj time.sleep żeby były szybkie." \
  --project "$PROJECT"

run_tests

echo "=== Wszystkie kroki zakończone ==="
