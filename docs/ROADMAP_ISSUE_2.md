## Plan rozwoju — AI Analiza Dokumentów

**Stan na:** tag **`v2026.11`** (czerwiec 2026) · [`master`](https://github.com/dragon15555000/AI_analiza_dokumentow/tree/master)  
**Szczegóły techniczne:** [`IMPROVEMENT_PLAN.md`](https://github.com/dragon15555000/AI_analiza_dokumentow/blob/master/IMPROVEMENT_PLAN.md) · **Releases:** [v2026.06 … v2026.11](https://github.com/dragon15555000/AI_analiza_dokumentow/releases)

Priorytety: 🔴 wysoki · 🟡 średni · 🟢 niski · ✅ zrobione · 🟡 częściowo

---

## Podsumowanie (co się zmieniło od starego planu)

Od ostatniej wersji tego issue (czerwiec 2026) wdrożono m.in.:

| Obszar | Status |
|--------|--------|
| Dashboard + diagnostyka startowa | ✅ #4, #5 |
| Self-update z GitHub | ✅ #15, #16 (UI + API) |
| Tryb **Detektyw** (briefing śledczy, `chat_context`) | ✅ (v2026.10) |
| **Tryb rozmowy** (pamięć bez psucia RAG) | ✅ #13 |
| Forensyka Excel (UI + backend) | ✅ rozszerzone |
| Sieć D3 (filtry, siła 1–12, SVG, stabilność) | ✅ częściowo #3.3 |
| systemd `--user`, `restart-app.sh` | ✅ |
| Bezpieczeństwo: path allowlist, sanitizacja promptów, SQL safe | 🟡 #28–#30 |
| Cloud embeddingi (Jina) | ⬜ #2 — nadal Ollama-only |
| ISAP / inwentarz prawny | ⬜ #6–#9 |

---

## Faza 1 — Niezależność od GPU i limitów chmury

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 1 | Cloud LLM — OpenRouter (retry, fallback, model verify) | 🔴 | ✅ |
| 2 | Cloud embeddingi (Jina / OpenRouter embed) | 🔴 | ⬜ Embeddingi **zawsze** Ollama `nomic-embed-text` |
| 3 | Panel providerów LLM w UI | 🔴 | ✅ |
| 26 | Lokalny Qdrant + przełącznik cloud/local w UI | 🔴 | ✅ |

**Następny krok Fazy 1:** #2 — opcjonalny provider embeddingów (bez psucia domyślnego flow Ollama).

---

## Faza 2 — Status, monitoring, produkcja

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 4 | Dashboard startowy (`/health`, modal ⚙️, kropki Qdrant/LLM/OCR) | 🔴 | ✅ |
| 5 | Auto-odświeżanie statusu co 30 s | 🟡 | ✅ |
| — | Self-update: `/api/update/*`, Releases, `git pull` + restart | 🔴 | ✅ v2026.09–11 |
| — | `ai_analiza-user.service`, `install-user-service.sh`, `restart-app.sh --user` | 🔴 | ✅ |

**Następny krok Fazy 2:** test Waitress na maszynie docelowej (Faza 1.1 w IMPROVEMENT_PLAN — checklist produkcyjny).

---

## Faza 3 — Inwentarz prawny (ISAP) — bez zmian priorytetu

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 6 | Ekstrakcja cytowań (Dz.U., art., §) | 🔴 | ⬜ Tryb **Prawny** = LLM, bez ISAP |
| 7 | Weryfikacja w ISAP REST API | 🔴 | ⬜ |
| 8 | Tabela wyników w UI | 🔴 | ⬜ |
| 9 | EUR-Lex | 🟢 | ⬜ |

**Następny krok Fazy 3:** prototyp #6 + #7 na jednym dokumencie testowym (MVP tabela).

---

## Faza 4 — Funkcje analityczne

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 10 | Auto-streszczenie przy imporcie | 🔴 | ⬜ |
| 11 | Oś czasu (Timeline, D3) | 🟡 | ⬜ |
| 12 | Scoring podejrzaności dokumentów 0–100 | 🟡 | ⬜ |
| 13 | Chat z pamięcią | 🟡 | ✅ Tryb rozmowy + `chat_context` |
| 14 | OCR skanów (Tesseract) | 🟡 | 🟡 Fallback przy imporcie; brak wymuszenia OCR w UI |
| 27 | Adaptive context (`/api/collection/profile`) | 🔴 | 🟡 Profil kolekcji + baner; bez pełnych zestawów promptów per typ |
| — | **Detektyw — briefing śledczy** | 🔴 | ✅ v2026.10 (min. 12 chunków, dywersyfikacja plików) |
| — | Porównanie 2 dokumentów | 🟡 | ✅ zakładka Porównaj |
| — | Weryfikacja 2× LLM (Krytyk) | 🔴 | ✅ |
| — | Hybryda RAG + SQL | 🟡 | ✅ gdy skonfigurowany SQL |

**Następny krok Fazy 4:** #10 (streszczenie w payload Qdrant) lub dokończenie #27 (prompty per `numerical` / `textual`).

---

## Faza 5 — Wersjonowanie (largely done)

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 15 | Wersja w UI (`git describe` / APP_VERSION) | 🟡 | ✅ |
| 16 | Sprawdzanie GitHub Release | 🟡 | ✅ |
| 17 | Changelog w UI z `CHANGELOG.md` | 🟢 | 🟡 Changelog z API Release w modalu; brak osobnego pliku |

---

## Faza 6 — UX i wygląd

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 18 | Dark mode | 🟡 | ⬜ |
| 19 | Responsywność mobilna | 🟢 | 🟡 Bootstrap; sieć/tabele słabsze na mobile |
| 20 | Skróty klawiszowe | 🟢 | 🟡 Enter w polach wyszukiwania |
| 21 | Onboarding pierwszego uruchomienia | 🟢 | 🟡 Modal diagnostyczny przy starcie |
| 22 | Eksport raportu HTML sesji | 🟡 | ⬜ (jest DOCX + CSV ekstrakcji) |

---

## Faza 7 — Porządek techniczny

| # | Funkcja | Priorytet | Status |
|---|---------|-----------|--------|
| 23 | Podział `app.py` na moduły (`core/`, `llm/`, …) | 🟡 | ⬜ |
| 24 | Autentykacja (`APP_API_KEY`, opcjonalnie login) | 🟡 | 🟡 `APP_API_KEY` + X-API-Key |
| 25 | Audit log operacji | 🟢 | ⬜ |
| — | Wektoryzacja w tle + `/tasks` | 🔴 | ⬜ (IMPROVEMENT_PLAN 1.3) |
| — | Dokończenie migracji na `call_llm()` wszędzie | 🟡 | 🟡 ~kilka bezpośrednich wywołań Ollama |

---

## Audyt bezpieczeństwa (#28–#33)

| # | Funkcja | Status |
|---|---------|--------|
| 28 | SQL Injection (Text-to-SQL) | 🟡 `_is_sql_safe`, walidacja tabel |
| 29 | Prompt Injection | 🟡 `_sanitize_for_prompt` w RAG/weryfikacji |
| 30 | Path Traversal | 🟡 `SEARCH_ROOTS` + `_path_is_allowed` |
| 31 | SQLite embedding cache (WAL) | ⬜ |
| 32 | Zarządzanie zasobami Excel/PDF | 🟡 Forensyka + import; brak podglądu w wynikach |
| 33 | Timeouty / resilience zewnętrznych API | 🟡 Częściowo |

**Następny krok audytu:** przegląd wszystkich endpointów przyjmujących ścieżki plików + testy regresji SQL.

---

## Kolejność wdrożeń — aktualna (Q3 2026)

| Sprint | Zakres | Cel |
|--------|--------|-----|
| **S1** | #10 + #27 (dokończenie) | Szybsza orientacja w kolekcji (streszczenia + prompty adaptacyjne) |
| **S2** | #6, #7, #8 (MVP ISAP) | Prawdziwa weryfikacja przepisów zamiast samego LLM |
| **S3** | #2 (cloud embed) + #31 | Mniej zależności od lokalnej Ollamy przy embeddingu |
| **S4** | #11 + #12 | Timeline + scoring ryzyka dla śledczych |
| **S5** | #14 (OCR UI) + #32 (podgląd wyników) | Lepsze skany i Excel/PDF w UI |
| **S6** | #3.2 PNG grafu w DOCX, #23 moduły | Raportowanie + utrzymanie kodu |
| **S7** | #18, #20, #22 | UX (dark mode, skróty, HTML export) |

---

## ✅ Zrealizowane — rejestr (issue + releases)

| Release | Najważniejsze |
|---------|----------------|
| v2026.06 | OpenRouter hardening, Excel parser fix, bezpieczeństwo SQL |
| v2026.07–08 | Diagnostyka, dynamiczne modele, OCR status, dyski WSL |
| v2026.09 | Self-update, systemd user service |
| v2026.10 | **Detektyw** — briefing, `chat_context`, retrieval |
| v2026.11 | Dokumentacja, sieć D3 (SVG, filtry), bugfixy health/Qdrant |

**Issue z kodu starego planu:** 1, 3, 4, 5, 13, 15, 16, 26, 27 (częściowo), 28–30 (częściowo), Detektyw, forensyka Excel, sieć D3 (częściowo 3.3).

---

## Produkcja — jak śledzić ten plan

```bash
git fetch origin && git checkout master && git pull origin master
git describe --tags --abbrev=0   # oczekiwane: v2026.11+
./restart-app.sh --user
```

Lub: UI → ⚙️ → **Aktualizacje** → Pobierz → Restart.

---

*Ten opis zastępuje poprzednią treść issue — odzwierciedla stan repozytorium po merge PR #16, #17 i release v2026.11.*
