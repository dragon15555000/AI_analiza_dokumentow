# Plan Usprawnień – AI Analiza Dokumentów

Cel: Zwiększyć stabilność, użyteczność dla śledczych i jakość kodu po dodaniu integracji z OpenRouter.

## Priorytety

### Faza 1 – Stabilność i podstawy (najwyższy priorytet)

**1.1 Produkcyjny serwer WSGI** ✅ W TRAKCIE
- [x] Dodano waitress do requirements.txt
- [x] Stworzono wsgi.py
- [x] Przygotowano ai_analiza.service
- [x] Dodano instrukcję w README.md
- [x] Stworzono skrypt migrate_to_waitress.sh
- [ ] Przetestować na maszynie docelowej + ewentualne poprawki
- **Wpływ**: Bardzo duży (kończy ERR_CONNECTION_RESET)
- **Wysiłek**: Mały

**1.2 Dokończyć migrację do abstrakcji LLM**
- Usunąć wszystkie bezpośrednie wywołania Ollama w `app.py` (pozostało ~12 miejsc)
- Używać wyłącznie `call_llm()` i `stream_llm_tokens()`
- Dotyczy szczególnie: `verify_answer`, hybrydowego streamu, kilku helperów
- **Wpływ**: Wysoki (łatwiejsze utrzymanie + OpenRouter wszędzie)

**1.3 Lepsze zarządzanie ciężkimi operacjami**
- Wektoryzacja "wszystko" powinna działać w tle (np. przez kolejkę lub osobny wątek)
- Dodać endpoint `/tasks` do sprawdzania statusu długich operacji
- Zablokować możliwość jednoczesnego uruchamiania wielu ciężkich wektoryzacji
- **Wpływ**: Wysoki (aplikacja przestaje "umierać" przy wektoryzacji)

### Faza 2 – Lepsze doświadczenie z OpenRouter (bieżący temat)

**2.1 Zaawansowany selektor modeli w UI**
- Zamiast jednego pola tekstowego – lista rozwijana z popularnymi darmowymi/tanimi modelami
- Możliwość dodawania własnych modeli do "ulubionych"
- Pokazywanie aktualnie używanego modelu w nagłówku + badge
- **Wpływ**: Wysoki (użytkownik nie musi pamiętać nazw modeli)

**2.2 Obsługa osobnego modelu embeddingów**
- Dodać `OPENROUTER_EMBED_MODEL` (np. darmowy NVIDIA Nemotron Embed VL)
- Umożliwić niezależne przełączanie embeddingów i chatu
- **Wpływ**: Średni/wysoki (możliwość używania multimodalnych embeddingów za darmo)

**2.3 Podstawowe liczenie kosztów / tokenów**
- Zwracać przybliżoną liczbę tokenów z OpenRouter (jeśli API podaje)
- Prosty licznik w UI: "Użyto ~X tokenów w tej sesji"
- **Wpływ**: Średni (użytkownik widzi ile "kosztuje" korzystanie)

### Faza 3 – Funkcje dla śledczych (wysoka wartość)

**3.1 Oś czasu / Timeline**
- Nowa zakładka lub sekcja "Chronologia"
- Automatyczne wyciąganie dat z dokumentów + powiązanie z encjami
- Wizualizacja na osi czasu (używając dat z sieci powiązań)

**3.2 Eksport grafu**
- Przycisk "Eksportuj aktualny graf jako PNG"
- Opcja dodania grafu do raportu DOCX
- **Wpływ**: Wysoki dla raportowania

**3.3 Lepsze filtrowanie w Sieci powiązań**
- Filtrowanie po zakresie dat
- Filtrowanie po sile powiązania (już częściowo jest)
- Grupowanie automatyczne (clustering) węzłów
- **Wpływ**: Wysoki

**3.4 Podgląd dokumentów w wynikach**
- Kliknięcie w dokument pokazuje podgląd (tekst + ewentualnie obraz dla PDF/zdjęć)
- Szybki dostęp do oryginalnego pliku

### Faza 4 – Jakość kodu i utrzymanie

**4.1 Wydzielenie klienta LLM**
- Stworzyć `llm/` lub `services/llm_client.py`
- Przenieść całą logikę Ollama + OpenRouter do osobnego modułu
- **Wpływ**: Średni (łatwiejszy development)

**4.2 Lepsze logowanie i błędy**
- Spójne logowanie wszystkich wywołań LLM (z providerem i modelem)
- Lepsze komunikaty błędów dla użytkownika
- **Wpływ**: Średni

**4.3 Konfiguracja**
- Lepsze zarządzanie ustawieniami (może pydantic-settings lub po prostu czystszy `.env`)
- Walidacja kluczy przy starcie

### Faza 5 – Długoterminowe / Opcjonalne

- Docker + docker-compose (łatwiejsze wdrożenie)
- Prosty panel administracyjny / statystyki użycia
- Eksport do innych formatów (Excel, JSON z metadanymi)
- Integracja z zewnętrznymi źródłami (opcjonalnie)
- Testy automatyczne kluczowych ścieżek (szczególnie LLM + streaming)

---

## Szybkie zwycięstwa (można zrobić w 1-2 dni)

1. Dodać `waitress` i zaktualizować service (największy ból)
2. Dodać listę popularnych darmowych modeli w UI (dropdown)
3. Wyczyścić pozostałe bezpośrednie wywołania Ollama
4. Dodać prosty licznik tokenów przy OpenRouter
5. Poprawić komunikaty błędów przy problemach z OpenRouter

---

## Uwagi

- Największy zwrot z inwestycji obecnie daje **Faza 1** (stabilność).
- Po stabilizacji warto mocno iść w **Fazę 3** – to wyróżnia narzędzie jako profesjonalne dla śledczych.
- OpenRouter to obecnie duży kierunek – warto dobrze go dopracować (Faza 2).

---

*Plan stworzony: maj 2026*

---

## Zrealizowane w maju/czerwcu 2026 (po recenzji)

### Stabilność importu i wydajność
- **Naprawiono krytyczny błąd parsera Excela** — `return` w `_extract_excel` został zgubiony podczas refaktoringu (copy-paste). Import plików `.xlsx` działa ponownie.
- **Optymalizacja importu metadanych** — `extract_file_metadata()` wywoływana była w pętli batchowej → teraz pobierana jest tylko raz na plik (duża oszczędność IO przy dużych PDF-ach).
- Wyrównano `batch_size` do 6 we wszystkich ścieżkach wektoryzacji (zgodnie z intencją odciążania Ollamy w `get_embeddings_batch`).

### OpenRouter — odporność na limity darmowych modeli
- Dodano pełną obsługę 429 (retry + exponential backoff + szanowanie `Retry-After`).
- Wprowadzono czyste komunikaty `[RATE_LIMIT]` zamiast surowych wyjątków.
- Dodano opcjonalny automatyczny fallback na Ollama (`OPENROUTER_FALLBACK_TO_OLLAMA=true`).
- Weryfikacja (Krytyk) używa teraz osobnego modelu (`OPENROUTER_MODEL_VERIFY`) — zmniejsza presję na limity głównego modelu.
- Zaktualizowano domyślne modele na lepsze darmowe (Llama-3.3-70B free jako główny).
- Poprawiono przekazywanie providera do `/verify`.
- Frontend: ładne alerty z przyciskiem "Przełącz na Ollama" przy trafieniu w limit.

### Inne
- Naprawiono pliki `ai_analiza.service` i `migrate_to_waitress.sh` (pozostałości markdown ` ``` ` na końcu).
- Wyrównano wszystkie wywołania batch embeddings do `batch_size=6`.

Te zmiany znacząco zwiększają niezawodność przy korzystaniu z darmowych modeli OpenRouter.

---

## Macierz funkcji — OCR i zarządzanie plikami (Excel/PDF)

| Obszar | Stan | Priorytet | Co jest dziś | Co brakuje do 🟢 |
|--------|------|-----------|--------------|------------------|
| **OCR (lepsze)** | 🟡 Częściowo | Średni | Fallback Tesseract `pol` dla skanów PDF (&lt;20 znaków) i obrazów; status w `/health` → `ocr` | Wymuszony OCR w UI, postęp per strona, `eng+pol`, preprocessing, podgląd skanu w wynikach |
| **Zarządzanie zasobami plików (Excel/PDF)** | 🟡 Częściowo | Wysoki | Import XLSX (arkusze, formuły, forensyka), PDF tekstowy, metadane, przeglądarka dokumentów, bulk delete | Podgląd fragmentu w wynikach, filtry po typie/dacie, raport „import bez tekstu (tylko OCR)” |

**Sprawdzenie w runtime:** `curl -s http://127.0.0.1:5000/health | python3 -m json.tool` — pola `ocr`, `file_parsers`.

**Instalacja OCR (dev):** README → sekcja „OCR”; checklista przy starcie aplikacji w UI.

### Proponowane zadania (backlog)

**OCR**
- [ ] Checkbox „Wymuś OCR” przy imporcie folderu
- [ ] SSE: postęp `ocr_page` / `ocr_done` dla wielostronicowych PDF
- [ ] Rozszerzyć `lang` na `pol+eng` (konfiguracja w `.env`)
- [ ] Komunikat w UI gdy `ocr.available === false` (z `install_hint`)

**Excel/PDF**
- [ ] Podgląd tekstu źródłowego przy kliknięciu wyniku (modal)
- [ ] Filtr w przeglądarce dokumentów: typ pliku, data modyfikacji
- [ ] Endpoint lub raport: lista plików zaimportowanych wyłącznie przez OCR

*Ostatnia aktualizacja macierzy: czerwiec 2026*