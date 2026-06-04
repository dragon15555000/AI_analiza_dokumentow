# -*- coding: utf-8 -*-
"""
SQL Security validation — bezpieczeństwo zapytań SQL generowanych przez LLM.
Przywrócone funkcje bezpieczeństwa SQL (po regresie w 3837d5a/ea77923).
Z PR #11 + 76481ac — wymagane przez hybrid_stream().
Bez nich LLM-generated SQL w hybrydzie jest niebezpieczne.
"""

import re

DANGEROUS_SQL_KEYWORDS = {
    "EXEC", "EXECUTE", "XP_", "SP_", "OPENROWSET", "OPENQUERY",
    "INTO OUTFILE", "INTO DUMPFILE", "LOAD_FILE", "BENCHMARK",
    "SLEEP(", "WAITFOR", "SHUTDOWN"
}


def _is_sql_safe(sql_query: str, allowed_first_words: tuple) -> tuple[bool, str | None]:
    """Rygorystyczna walidacja bezpieczeństwa zapytań SQL generowanych przez LLM."""
    if not sql_query or not sql_query.strip():
        return False, "Puste zapytanie SQL"

    sql_upper = sql_query.strip().upper()

    first_token = sql_upper.split()[0] if sql_upper.split() else ""
    if first_token not in allowed_first_words:
        return False, f"Niedozwolone polecenie: {first_token}"

    if sql_upper.count(";") > 0:
        return False, "Wykryto średnik w zapytaniu SQL (potencjalne SQL Injection)"

    for dangerous in DANGEROUS_SQL_KEYWORDS:
        if dangerous in sql_upper:
            return False, f"Wykryto zabronione słowo kluczowe: {dangerous}"

    if "--" in sql_query or "/*" in sql_query:
        return False, "Wykryto komentarze SQL — potencjalne obejście walidacji"

    return True, None


def _extract_sql_table_refs(sql_query: str) -> set[str]:
    """Wyciąga nazwy tabel z FROM / JOIN (bez aliasów)."""
    refs: set[str] = set()
    pattern = re.compile(
        r'(?:FROM|JOIN)\s+'
        r'(?:\[?([\w]+)\]?\.)?'
        r'\[?([\w]+)\]?',
        re.IGNORECASE
    )
    for schema_part, table_part in pattern.findall(sql_query):
        if table_part:
            refs.add(table_part.lower())
        if schema_part and table_part:
            refs.add(f"{schema_part}.{table_part}".lower())
    return refs


def _validate_sql_table_refs(sql_query: str, known_tables: set[str]) -> tuple[bool, str | None]:
    """Sprawdza czy wszystkie tabele w zapytaniu SQL są znane."""
    if not known_tables:
        return True, None
    refs = _extract_sql_table_refs(sql_query)
    if not refs:
        return True, None
    unknown = sorted(r for r in refs if r not in known_tables)
    if unknown:
        sample = ", ".join(sorted(known_tables)[:12])
        return False, (
            f"Nieznane tabele w zapytaniu: {', '.join(unknown)}. "
            f"Dostępne m.in.: {sample}{'…' if len(known_tables) > 12 else ''}"
        )
    return True, None
