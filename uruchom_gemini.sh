#!/usr/bin/env bash
set -euo pipefail

echo "1. Nadpisywanie skryptu test_gemini.py darmowym modelem..."
cat > test_gemini.py <<'PYTHON'
import os
from google import genai

# Inicjalizacja klienta przy użyciu GEMINI_API_KEY ze środowiska
client = genai.Client()

print("Wysyłanie zapytania do modelu gemini-2.5-flash...")
response = client.models.generate_content(
    model='gemini-2.5-flash',
    contents='Napisz wydajną funkcję w Pythonie do wyszukiwania binarnego.',
)

print("\n--- Odpowiedź Gemini ---")
print(response.text)
PYTHON

echo "2. Uruchamianie przy użyciu poprawnej składni uv run --no-project..."
uv run --no-project python test_gemini.py
