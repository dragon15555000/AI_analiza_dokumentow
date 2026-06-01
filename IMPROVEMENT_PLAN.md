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

1. ~~Dodać `waitress` i zaktualizować service~~ ✅ (zrobione)
2. ~~Dodać listę popularnych darmowych modeli w UI (dropdown)~~ → częściowo zrealizowane przez **dynamiczne sugestie modeli + auto-switch** (v2026.08)
3. Wyczyścić pozostałe bezpośrednie wywołania Ollama
4. Dodać prosty licznik tokenów przy OpenRouter
5. ~~Poprawić komunikaty błędów przy problemach z OpenRouter~~ → znacząco ulepszone w diagnostyce startowej + przycisk „Kopiuj diagnostykę” (v2026.08)

---

## Uwagi

- Największy zwrot z inwestycji obecnie daje **Faza 1** (stabilność) — w dużej mierze zrealizowana.
- Po stabilizacji i poprawie użyteczności (v2026.07 + v2026.08) warto rozważyć dalszy rozwój w kierunku **Fazy 3** (funkcje dla śledczych) oraz dopracowanie zaawansowanego zarządzania modelami (Faza 2).
- OpenRouter + diagnostyka + doświadczenie na WSL to obecnie jedne z najmocniejszych stron narzędzia.

---

*Plan stworzony: maj 2026*  
*Ostatnia duża aktualizacja: lipiec 2026 (po v2026.09 – self-update + systemd user service)*

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

## Zrealizowane – v2026.08 (lipiec 2026)

### Użyteczność i doświadczenie użytkownika
- **Dynamiczne sugestie modeli + auto-switch** — przy zmianie zakładek aplikacja inteligentnie sugeruje najlepszy model (i może automatycznie przełączać).
- **Konfiguracja przez `.env`**:
  - `EMBED_MODEL` — możliwość zmiany modelu embeddingów (domyślnie `nomic-embed-text`)
  - `OCR_LANG` — łatwa zmiana języka OCR
- **Lepsze wsparcie dla Windows + WSL**:
  - Znacznie ulepszone automatyczne wykrywanie dysków Windows (w tym dysk G: przy leniwym montowaniu w WSL)
  - Agresywne sondowanie liter dysków w `/api/drives`

### Diagnostyka i obserwowalność (duży postęp)
- Wzbogacona **Diagnostyka startowa** (modal przy uruchomieniu + przycisk ⚙️):
  - Nowy wiersz **Embedding** (pokazuje czy model embeddingów jest dostępny)
  - Znacznie lepsze, bardziej precyzyjne komunikaty błędów (np. "model nie istnieje → gotowa komenda `ollama pull`")
  - Przycisk **„📋 Kopiuj diagnostykę”** — kopiuje czytelne podsumowanie do schowka (bardzo przydatne przy zgłaszaniu problemów)
  - Wyświetlanie czasu ostatniego sprawdzenia
  - Wersja aplikacji widoczna w nagłówku modala i w topbarze
- Kolorowe kropki statusu w nagłówku (Qdrant / LLM / OCR) + automatyczne odświeżanie co 30s

### Inne
- Wersja aplikacji (`APP_VERSION`) automatycznie odczytywana z tagów git (w trybie dev pokazuje `-dirty`)

Te zmiany znacząco poprawiają codzienne doświadczenie użytkownika, szczególnie na Windows + WSL oraz przy korzystaniu z diagnostyki w razie problemów.

---

## Zrealizowane – Self-update, User Service & Developer Experience (v2026.09)

### Automatyczna aktualizacja z GitHub
- Dodano system sprawdzania i pobierania aktualizacji bezpośrednio z poziomu aplikacji:
  - Nowa sekcja **„Aktualizacje”** w modalu diagnostycznym startowym
  - Automatyczne sprawdzanie GitHub co 20 minut w tle + powiadomienie (kropka na ikonie ⚙️)
  - Przyciski: **Sprawdź aktualizacje** → **Pobierz aktualizację** → **Zrestartuj aplikację**
  - Wyświetlanie changelog bezpośrednio z GitHub Releases
- Endpointy: `/api/update/status`, `/api/update/pull`, `/api/update/restart`

### Lepsze podejście produkcyjne (systemd user service)
- Utworzono `ai_analiza-user.service` – usługa systemd działająca bez uprawnień roota
- Stworzono `install-user-service.sh` – jedno polecenie do instalacji całej usługi użytkownika
- Znacznie rozbudowano `restart-app.sh`:
  - Pełne wsparcie dla `--user` (zarządzanie user service)
  - Podkomendy: `logs`, `status`, `restart`
  - Opcjonalny `git pull` przy restarcie
- Usługa domyślnie używa **Waitress** (produkcyjny serwer) i nasłuchuje na `0.0.0.0:5000`
- Lepsze, bardziej eleganckie ładowanie zmiennych z `.env`

### Dokumentacja
- Dodano obszerną sekcję w README o uruchamianiu jako **systemd --user** (zalecany sposób na WSL/laptopach)
- Zaktualizowano `AGENTS.md` – nowe wytyczne dla AI coderów dotyczące sposobów uruchamiania aplikacji

Te zmiany znacząco podnoszą poziom „produkcyjności” lokalnego środowiska deweloperskiego, jednocześnie zachowując prostotę dla codziennej pracy.

---

## Zrealizowane – v2026.10 (Detektyw / wyszukiwanie śledcze)

- Tryb **Detektyw — briefing śledczy**: sekcje odpowiedzi, tagi anomalii, min. 12 chunków, dywersyfikacja po plikach
- **`chat_context`** oddzielnie od `query` — historia rozmowy nie psuje embeddingu RAG
- Merge PR #16, tag release `v2026.10`

---

## Zrealizowane – v2026.11 (dokumentacja, sieć D3, poprawki)

- **README / .env.example / AGENTS.md** — aktualizacja pod Detektywa, self-update, diagnostykę, sieć, Excel
- **Sieć D3**: zatrzymywanie poprzedniej symulacji, tooltips po zoom, suwak siły 1–12, filtr samotnych węzłów, eksport SVG, responsywna wysokość
- **Bugfix**: `fetchHealth` → `updateHealthStatus` po przełączeniu Qdrant
- **Bugfix**: `/health` — `embedding.ok` zawsze z Ollama (nawet przy OpenRouter chat)
- **Bugfix**: `systemctl --user` w statusie/restartcie usługi
- **Bugfix**: bezpieczne parsowanie `strength` w `/network`; `git pull` fallback `main`

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
- [x] Komunikat w UI gdy `ocr.available === false` (z `install_hint`) — kropka ⬤ OCR w topbarze, tooltip z komendą instalacji

**Excel/PDF**
- [x] Rozszerzona forensyka Excel (metadane, AVERAGE/SUM, błędy #REF!, ukryte arkusze, rekomendacje w UI) — 2026-06
- [ ] Podgląd tekstu źródłowego przy kliknięciu wyniku (modal)
- [ ] Filtr w przeglądarce dokumentów: typ pliku, data modyfikacji
- [ ] Endpoint lub raport: lista plików zaimportowanych wyłącznie przez OCR

*Ostatnia aktualizacja macierzy: lipiec 2026 (po v2026.09 – self-update + user service + Gotowe raporty śledcze)*

---

## Końcowa recenzja kodu (01.06.2026)

Na prośbę użytkownika przeprowadzono pełną końcową recenzję kodu całej aplikacji.

**Wykonane działania i poprawki:**
- Dodano nową zakładkę „Raporty śledcze” z 8 gotowymi szablonami demonstracyjnymi możliwości programu.
- Wprowadzono podstawowe przetwarzanie partiami w `build_network()` (partie po 5 dokumentów) + licznik przetworzonych partii.
- Ulepszono komunikaty błędów przy budowaniu sieci powiązań.
- Dodano/ulepszono globalny licznik tokenów w topbarze oraz lepsze liczniki przy odpowiedziach LLM.
- Zwiększono widoczność pasków postępu przy imporcie/wektoryzacji.
- Dodano toast/powiadomienie po udanej aktualizacji z GitHub.
- Poprawiono skrypty `start`, `stop`, `restart-app.sh` i `install-user-service.sh` (kolorowe komunikaty, git pull przy starcie).
- Dodano zarządzanie App API Key w ustawieniach UI.
- Zmieniono domyślny provider LLM na OpenRouter (oprócz importu/embeddings).
- Zaktualizowano dokumentację (README, AGENTS.md, IMPROVEMENT_PLAN.md).
- Przeprowadzono końcową pełną recenzję kodu i naprawiono pozostałości po wcześniejszych refaktoryzacjach.

**Data zakończenia recenzji:** 01 czerwca 2026

Wszystkie większe zmiany są udokumentowane w commitach tej sesji.