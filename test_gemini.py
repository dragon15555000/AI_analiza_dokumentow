from google import genai

# Inicjalizacja klienta przy użyciu GEMINI_API_KEY ze środowiska
client = genai.Client()

print("Wysyłanie zapytania do modelu gemini-2.5-flash...")
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Napisz wydajną funkcję w Pythonie do wyszukiwania binarnego.",
)

print("\n--- Odpowiedź Gemini ---")
print(response.text)
