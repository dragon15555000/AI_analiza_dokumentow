#!/usr/bin/env bash
set -euo pipefail

KEY_FILE=".env_key"

if [ ! -f "$KEY_FILE" ]; then
    echo "BŁĄD: Brak zapisanego klucza w .env_key. Uruchom najpierw poprzedni skrypt konfiguracyjny." >&2
    exit 1
fi

CURRENT_KEY=$(cat "$KEY_FILE")
export OPENROUTER_API_KEY="$CURRENT_KEY"

echo "1. Tworzenie zaktualizowanego skryptu test_openrouter_requests.py..."
cat > test_openrouter_requests.py <<'PYTHON'
import os
import sys
import requests
import json

api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key:
    print("BŁĄD: Brak klucza OPENROUTER_API_KEY w środowisku!", file=sys.stderr)
    sys.exit(1)

headers = {
    "Authorization": f"Bearer {api_key.strip()}",
    "HTTP-Referer": "http://localhost:3000",
    "X-Title": "AI Crew New API Test",
    "Content-Type": "application/json"
}

payload = {
    "model": "deepseek/deepseek-chat:free",
    "messages": [
        {
            "role": "user",
            "content": "Napisz jedno słowo: Sukces."
        }
    ]
}

print("Pobieranie danych z nowego punktu końcowego API (DeepSeek Free)...")
try:
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        data=json.dumps(payload),
        timeout=30
    )
    
    if response.status_code != 200:
        print(f"\nBłąd API (Status {response.status_code}): {response.text}", file=sys.stderr)
        sys.exit(1)
        
    result = response.json()
    print("\n--- Odpowiedź pobrana pomyślnie ---")
    print(result['choices'][0]['message']['content'])
    
except Exception as e:
    print(f"\nBłąd połączenia: {e}", file=sys.stderr)
PYTHON

echo "2. Uruchamianie za pomocą uv run z wstrzyknięciem modułu requests..."
env OPENROUTER_API_KEY="$CURRENT_KEY" uv run --no-project --with requests python test_openrouter_requests.py
