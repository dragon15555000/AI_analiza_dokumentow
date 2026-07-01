"""
Cross-check kandydatów HIGH/CRITICAL z audytu XLSX względem dokumentów OCR
zaindeksowanych w Qdrant. Qdrant przechowuje tylko surowy tekst per plik
(payload: file/text/full_path/metadata/summary) — nie ma dedykowanych pól
faktura/kontrahent/kwota, więc porównanie jest tekstowe (semantic search +
deterministyczne wyszukanie kwot regexem), a nie strukturalnym lookupem.
"""

from __future__ import annotations

import re

from services.financial_forensics import _coerce_number

_MAX_CANDIDATES = 20

_AMOUNT_TOKEN_RE = re.compile(r"-?\d[\d  .,]{0,14}\d|-?\d")


def extract_candidates(evidence_pack: dict) -> tuple[list[dict], bool]:
    """Wyciąga kandydatów do cross-checku z ai_evidence_pack (już ograniczonego do HIGH/CRITICAL).

    Zwraca (candidates, truncated).
    """
    candidates: list[dict] = []
    for sheet in (evidence_pack or {}).get("sheets", []) or []:
        sheet_name = sheet.get("sheet")
        for finding in sheet.get("findings", []) or []:
            severity = (finding.get("severity") or "").upper()
            if severity not in {"HIGH", "CRITICAL"}:
                continue
            fact = finding.get("fact") or {}
            excel_amount = _coerce_number(fact.get("value"))
            finding_id = finding.get("finding_id") or f"{sheet_name}:{fact.get('cell')}"
            query_parts = [fact.get("header"), fact.get("message"), finding.get("label")]
            query_text = " ".join(p for p in query_parts if p).strip()
            if fact.get("value") not in (None, ""):
                query_text = f"{query_text} {fact.get('value')}".strip()
            candidates.append(
                {
                    "finding_id": finding_id,
                    "sheet": fact.get("sheet") or sheet_name,
                    "cell": fact.get("cell"),
                    "severity": severity,
                    "excel_document_id": finding_id,
                    "excel_amount": excel_amount,
                    "query_text": query_text,
                }
            )

    truncated = len(candidates) > _MAX_CANDIDATES
    return candidates[:_MAX_CANDIDATES], truncated


def _extract_amounts(text: str) -> list[float]:
    amounts = []
    for tok in _AMOUNT_TOKEN_RE.findall(text or ""):
        val = _coerce_number(tok)
        if val is not None:
            amounts.append(val)
    return amounts


def _amounts_close(a: float, b: float) -> bool:
    if a is None or b is None:
        return False
    if a == 0 and b == 0:
        return True
    tolerance = max(0.01, abs(a) * 0.01)
    return abs(a - b) <= tolerance


def evaluate_match(candidate: dict, qdrant_points: list[dict]) -> dict:
    """qdrant_points: lista {"file": str, "text": str, "score": float}, posortowana malejąco wg score."""
    base = {
        "finding_id": candidate["finding_id"],
        "sheet": candidate["sheet"],
        "cell": candidate["cell"],
        "excel_document_id": candidate["excel_document_id"],
        "excel_amount": candidate["excel_amount"],
    }

    if not qdrant_points:
        return {
            **base,
            "matched_ocr_document": None,
            "ocr_amount": None,
            "status": "not_found",
            "match_confidence": 0.0,
            "evidence_snippet": "",
            "next_check": "Brak podobnego treściowo dokumentu OCR — sprawdź ręcznie w źródłach papierowych/skanach.",
        }

    best = qdrant_points[0]
    text = best.get("text") or ""
    amounts = _extract_amounts(text)
    excel_amount = candidate["excel_amount"]
    snippet = text[:280]
    match_confidence = round(float(best.get("score") or 0.0), 4)

    if excel_amount is None:
        return {
            **base,
            "matched_ocr_document": best.get("file"),
            "ocr_amount": amounts[0] if amounts else None,
            "status": "ambiguous",
            "match_confidence": match_confidence,
            "evidence_snippet": snippet,
            "next_check": "Wartość w Excelu nie jest liczbą — porównaj treść dokumentu ręcznie.",
        }

    if not amounts:
        return {
            **base,
            "matched_ocr_document": best.get("file"),
            "ocr_amount": None,
            "status": "ambiguous",
            "match_confidence": match_confidence,
            "evidence_snippet": snippet,
            "next_check": "Znaleziono podobny dokument OCR, ale nie wykryto w nim kwot — zweryfikuj ręcznie.",
        }

    matching = [amt for amt in amounts if _amounts_close(amt, excel_amount)]
    if matching:
        return {
            **base,
            "matched_ocr_document": best.get("file"),
            "ocr_amount": matching[0],
            "status": "match",
            "match_confidence": match_confidence,
            "evidence_snippet": snippet,
            "next_check": "Zgodność kwot potwierdzona automatycznie — rekomendowana krótka weryfikacja próbkowa.",
        }

    return {
        **base,
        "matched_ocr_document": best.get("file"),
        "ocr_amount": amounts[0],
        "status": "mismatch",
        "match_confidence": match_confidence,
        "evidence_snippet": snippet,
        "next_check": "Kwota w Excelu różni się od kwoty znalezionej w dokumencie OCR — wymaga weryfikacji ręcznej.",
    }


def unavailable_result(candidate: dict, reason: str) -> dict:
    return {
        "finding_id": candidate["finding_id"],
        "sheet": candidate["sheet"],
        "cell": candidate["cell"],
        "excel_document_id": candidate["excel_document_id"],
        "excel_amount": candidate["excel_amount"],
        "matched_ocr_document": None,
        "ocr_amount": None,
        "status": "unavailable",
        "match_confidence": 0.0,
        "evidence_snippet": "",
        "next_check": reason,
    }


def run_cross_check(candidates: list[dict], search_fn) -> list[dict]:
    """search_fn(query_text: str) -> list[dict] z {"file","text","score"}; może rzucić wyjątek."""
    results = []
    for candidate in candidates:
        if not candidate.get("query_text"):
            results.append(unavailable_result(candidate, "Brak treści do wyszukania semantycznego."))
            continue
        try:
            points = search_fn(candidate["query_text"])
        except Exception as exc:
            results.append(unavailable_result(candidate, f"Qdrant/OCR niedostępny: {str(exc)[:150]}"))
            continue
        results.append(evaluate_match(candidate, points))
    return results
