#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import html
import urllib.request
import re
import os
import hashlib
import time
import sqlite3
import threading
import subprocess
import platform
from pathlib import Path
from collections import defaultdict
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file
import io
import logging
import requests   # ← dodane dla lepszej obsługi rozłączeń klienta w streamach SSE
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

# Wczytaj .env jeśli istnieje
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Bezpieczne importy opcjonalne
try: import docx
except ImportError: docx = None
try: import pdfplumber
except ImportError: pdfplumber = None
try: import openpyxl
except ImportError: openpyxl = None
try: import pytesseract
except ImportError: pytesseract = None
try: from pdf2image import convert_from_path
except ImportError: convert_from_path = None
try:
    import pymssql
    PYMSSQL_AVAILABLE = True
except ImportError:
    pymssql = None
    PYMSSQL_AVAILABLE = False

# ============================================================
# PRZYWRÓCONE FUNKCJE BEZPIECZEŃSTWA SQL (po regresie w 3837d5a/ea77923)
# Z PR #11 + 76481ac — wymagane przez hybrid_stream()
# Bez nich LLM-generated SQL w hybrydzie jest niebezpieczne.
# ============================================================

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

    if ";" in sql_query:
        return False, "Wykryto średnik w SQL (potencjalne SQL Injection)"

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


def _format_table_key(schema_name: str, table_name: str) -> str:
    if schema_name:
        return f"{schema_name}.{table_name}"
    return table_name


def _sql_dialect(cfg: dict) -> str:
    t = (cfg.get("type") or cfg.get("db_type") or "mssql").strip().lower()
    return "sqlite" if t == "sqlite" else "mssql"


def _sql_conn_configured(cfg: dict) -> bool:
    if not cfg:
        return False
    if _sql_dialect(cfg) == "sqlite":
        raw = cfg.get("db_path") or cfg.get("path") or cfg.get("database") or ""
        return bool(_normalize_sqlite_path_input(raw))
    return bool(cfg.get("server") and cfg.get("database"))


def _normalize_sqlite_path_input(raw: str) -> str:
    """Normalizuje ścieżkę SQLite (WSL: G:\\foo → /mnt/g/foo)."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return raw
    win = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if win:
        drive = win.group(1).lower()
        rest = win.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return raw


def _resolve_sqlite_path(cfg: dict) -> Path:
    raw = (
        cfg.get("db_path") or cfg.get("path") or cfg.get("database") or ""
    ).strip()
    raw = _normalize_sqlite_path_input(raw)
    if not raw:
        raise ValueError(
            "Podaj ścieżkę do pliku SQLite (.db) — np. /mnt/g/dane/eksport.db"
        )
    p = Path(raw).expanduser()
    if not p.is_file():
        raise ValueError(f"Plik bazy SQLite nie istnieje: {raw}")
    if not _path_is_allowed(p):
        raise ValueError("Ścieżka poza dozwolonymi katalogami (SEARCH_ROOTS)")
    if p.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        raise ValueError("Dozwolone rozszerzenia pliku SQLite: .db, .sqlite, .sqlite3")
    return p.resolve()


def _get_mssql_conn(cfg: dict):
    """Tworzy połączenie z MS SQL Server przez pymssql."""
    if not PYMSSQL_AVAILABLE:
        raise RuntimeError("pymssql nie jest zainstalowany")
    server = cfg.get("server", "127.0.0.1")
    port   = str(cfg.get("port", 1433))
    if ":" not in server and "\\" not in server:
        server = f"{server}:{port}"
    db = cfg.get("database", "")
    return pymssql.connect(
        server   = server,
        user     = cfg.get("user", ""),
        password = cfg.get("password", ""),
        database = db if db else None,
        timeout  = 15,
        charset  = "UTF-8"
    )


def _get_sql_conn(cfg: dict, readonly: bool = True):
    """Połączenie z MS SQL Server lub SQLite (domyślnie read-only dla SQLite)."""
    if _sql_dialect(cfg) == "sqlite":
        path = _resolve_sqlite_path(cfg)
        mode = "ro" if readonly else "rw"
        conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn
    return _get_mssql_conn(cfg)


def _require_sql_backend(cfg: dict) -> None:
    if _sql_dialect(cfg) == "mssql" and not PYMSSQL_AVAILABLE:
        raise RuntimeError("Moduł pymssql nie jest zainstalowany")


def _sql_quote_ident(name: str, dialect: str) -> str:
    if dialect == "mssql":
        return f"[{name}]"
    return f'"{name}"'


def _sql_rows_to_dicts(cur, limit: int = 500) -> list[dict]:
    rows = cur.fetchmany(limit)
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            item = row
        elif hasattr(row, "keys"):
            item = {k: row[k] for k in row.keys()}
        else:
            cols = [d[0] for d in (cur.description or [])]
            item = {cols[i]: row[i] for i in range(len(cols))}
        out.append({k: (str(v) if v is not None else "") for k, v in item.items()})
    return out


def _sql_llm_dialect_label(dialect: str) -> str:
    return "SQLite" if dialect == "sqlite" else "T-SQL (MS SQL Server)"


def _fetch_sql_known_tables(cfg: dict) -> set[str]:
    """Zwraca znane nazwy tabel do walidacji SQL z LLM."""
    known: set[str] = set()
    dialect = _sql_dialect(cfg)
    conn = _get_sql_conn(cfg)
    try:
        cur = conn.cursor()
        if dialect == "sqlite":
            cur.execute("""
                SELECT name FROM sqlite_master
                WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
            """)
            for (table_name,) in cur.fetchall():
                known.add(table_name.lower())
        else:
            cur.execute("""
                SELECT TABLE_SCHEMA, TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')
            """)
            for schema_name, table_name in cur.fetchall():
                key = _format_table_key(schema_name, table_name)
                known.add(key.lower())
                known.add(table_name.lower())
    finally:
        conn.close()
    return known


SQL_ROW_LIMIT = 100
SQL_CORRECTION_MAX_RETRIES = 2
SQL_SCHEMA_MAX_TABLES = 50
SQL_SCHEMA_MAX_SAMPLES = 5
SQL_SAMPLE_VALUE_TYPES = frozenset({
    "date", "datetime", "datetime2", "smalldatetime", "timestamp", "time",
    "datetimeoffset", "text", "ntext", "varchar", "nvarchar", "char", "nchar",
})


def _sql_limit_instruction(dialect: str) -> str:
    if dialect == "mssql":
        return f"ZAWSZE używaj TOP {SQL_ROW_LIMIT} (np. SELECT TOP {SQL_ROW_LIMIT} ...)."
    return f"ZAWSZE dodaj LIMIT {SQL_ROW_LIMIT} na końcu zapytania."


def _ensure_sql_row_limit(sql_query: str, dialect: str, limit: int = SQL_ROW_LIMIT) -> str:
    sql = (sql_query or "").strip()
    if not sql:
        return sql
    upper = sql.upper()
    if dialect == "mssql":
        if re.search(r"\bTOP\s+\d+", upper):
            return sql
        idx = upper.rfind("SELECT")
        if idx < 0:
            return sql
        prefix, rest = sql[:idx], sql[idx:]
        rest = re.sub(
            r"(?i)^SELECT\s+(DISTINCT\s+)?",
            lambda m: f"SELECT {m.group(1) or ''}TOP {limit} ",
            rest,
            count=1,
        )
        return prefix + rest
    if re.search(r"\bLIMIT\s+\d+", upper):
        return sql
    return f"{sql.rstrip(';').strip()} LIMIT {limit}"


def _sql_fetch_column_sample(
    conn, table: str, col: str, col_type: str, dialect: str,
) -> str | None:
    ct = (col_type or "").lower().split("(")[0].strip()
    should_sample = (
        ct in SQL_SAMPLE_VALUE_TYPES
        or "date" in ct
        or "time" in ct
    )
    if not should_sample:
        return None
    qtable = _sql_quote_ident(table, dialect)
    qcol = _sql_quote_ident(col, dialect)
    cur = conn.cursor()
    try:
        if dialect == "mssql":
            cur.execute(
                f"SELECT TOP 1 CAST({qcol} AS NVARCHAR(200)) AS v "
                f"FROM {qtable} WHERE {qcol} IS NOT NULL"
            )
        else:
            cur.execute(
                f"SELECT CAST({qcol} AS TEXT) AS v "
                f"FROM {qtable} WHERE {qcol} IS NOT NULL LIMIT 1"
            )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        val = str(row[0]).strip()
        return val[:80] if val else None
    except Exception:
        return None
    finally:
        cur.close()


def _sql_table_column_samples(
    conn, table: str, col_parts: list[str], dialect: str,
    max_samples: int = SQL_SCHEMA_MAX_SAMPLES,
) -> str:
    """Próbki wartości tylko dla kolumn dat/tekst (max N na tabelę — bez skanowania wszystkich kolumn)."""
    samples: list[str] = []
    for part in col_parts:
        if len(samples) >= max_samples:
            break
        m = re.match(r"([\w]+)\s+\(([^)]+)\)", part)
        if not m:
            continue
        col, ctype = m.group(1), m.group(2)
        val = _sql_fetch_column_sample(conn, table, col, ctype, dialect)
        if val:
            samples.append(f'{col} np. "{val}"')
    return "; ".join(samples)


def _build_sql_schema_entries(
    cfg: dict,
    max_tables: int = SQL_SCHEMA_MAX_TABLES,
    include_samples: bool = True,
) -> list[dict]:
    """Pojedyncze wpisy schematu (tabela → tekst) z opcjonalnymi próbkami wartości."""
    dialect = _sql_dialect(cfg)
    conn = _get_sql_conn(cfg)
    entries: list[dict] = []
    try:
        cur = conn.cursor()
        if dialect == "sqlite":
            cur.execute("""
                SELECT name, sql FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                LIMIT ?
            """, (max_tables,))
            table_rows = cur.fetchall()
            if not table_rows:
                cur.execute("""
                    SELECT name, type FROM sqlite_master
                    WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
                    ORDER BY name LIMIT ?
                """, (max_tables,))
                for name, ttype in cur.fetchall():
                    entries.append({
                        "table": name,
                        "text": f"Tabela: {name} ({ttype})",
                    })
            else:
                for name, ddl in table_rows:
                    cur.execute(f'PRAGMA table_info("{name}")')
                    col_rows = cur.fetchall()
                    cols = [f"{r[1]} ({r[2]})" for r in col_rows]
                    col_txt = ", ".join(cols[:25]) + (" …" if len(cols) > 25 else "")
                    text = f"Tabela: {name}\nKolumny: {col_txt}" if col_txt else f"Tabela: {name}"
                    if include_samples and col_txt:
                        samp = _sql_table_column_samples(conn, name, cols[:25], dialect)
                        if samp:
                            text += f"\nPrzykładowe wartości: {samp}"
                    elif ddl and not col_txt:
                        text = f"Tabela: {name}\nDDL: {ddl[:400]}"
                    entries.append({"table": name, "text": text})
        else:
            row_limit = max(100, min(max_tables * 40, 4000))
            cur.execute(f"""
                SELECT TOP {row_limit} c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS c
                INNER JOIN INFORMATION_SCHEMA.TABLES t
                  ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
                WHERE t.TABLE_TYPE IN ('BASE TABLE','VIEW')
                ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
            """)
            grouped: dict[str, list[str]] = defaultdict(list)
            tables_seen: set[str] = set()
            for schema_name, table_name, col_name, data_type in cur.fetchall():
                key = _format_table_key(schema_name, table_name)
                if key not in tables_seen and len(tables_seen) >= max_tables:
                    continue
                tables_seen.add(key)
                grouped[key].append(f"{col_name} ({data_type})")

            if not grouped:
                cur.execute("""
                    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')
                    ORDER BY TABLE_NAME
                """)
                for schema_name, table_name, table_type in cur.fetchall()[:max_tables]:
                    key = _format_table_key(schema_name, table_name)
                    entries.append({
                        "table": key,
                        "text": f"Tabela: {key} ({table_type})",
                    })
            else:
                for key in sorted(grouped.keys()):
                    cols = grouped[key]
                    col_txt = ", ".join(cols[:25]) + (" …" if len(cols) > 25 else "")
                    text = f"Tabela: {key}\nKolumny: {col_txt}"
                    if include_samples:
                        table_only = key.split(".")[-1]
                        samp = _sql_table_column_samples(conn, table_only, cols[:25], dialect)
                        if samp:
                            text += f"\nPrzykładowe wartości: {samp}"
                    entries.append({"table": key, "text": text})
    finally:
        conn.close()
    return entries


def _format_sql_schema_text(entries: list[dict]) -> str:
    header = (
        "UWAGA: Używaj WYŁĄCZNIE poniższych tabel i kolumn. "
        "Nie wymyślaj nazw, jeśli ich nie ma na liście.\n"
    )
    return header + "\n".join(e["text"] for e in entries)


def _build_auto_sql_schema(cfg: dict, max_tables: int = SQL_SCHEMA_MAX_TABLES) -> str:
    entries = _build_sql_schema_entries(cfg, max_tables=max_tables, include_samples=True)
    return _format_sql_schema_text(entries)


def _resolve_sql_schema(cfg: dict, user_schema: str) -> tuple[str, set[str]]:
    """Łączy schemat z UI z automatycznym pobraniem z bazy (INFORMATION_SCHEMA + próbki)."""
    known = _fetch_sql_known_tables(cfg)
    user_schema = (user_schema or "").strip()
    if user_schema and "Kolumny:" in user_schema:
        return user_schema, known
    if user_schema:
        auto = _build_auto_sql_schema(cfg)
        merged = f"{user_schema}\n\n--- Pełny schemat z bazy ---\n{auto}"
        return merged, known
    return _build_auto_sql_schema(cfg), known


def _is_numeric_sql_value(val) -> bool:
    s = str(val or "").strip().replace(",", ".").replace(" ", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _build_sql_chart_config(columns: list[str], rows: list[dict]) -> dict | None:
    """Heurystyka: wykres słupkowy/kołowy/liniowy dla wyników agregacji."""
    if not rows or len(columns) < 2 or len(rows) > 50:
        return None

    label_col = None
    value_col = None
    for col in columns:
        samples = [rows[i].get(col, "") for i in range(min(5, len(rows)))]
        non_empty = [s for s in samples if str(s).strip()]
        if non_empty and all(_is_numeric_sql_value(v) for v in non_empty):
            if value_col is None:
                value_col = col
        elif label_col is None:
            label_col = col

    if (not label_col or not value_col) and len(columns) >= 2:
        if all(_is_numeric_sql_value(rows[i].get(columns[1], "")) for i in range(min(5, len(rows)))):
            label_col, value_col = columns[0], columns[1]

    if not label_col or not value_col or label_col == value_col:
        return None

    labels = [str(r.get(label_col, ""))[:40] for r in rows]
    data: list[float] = []
    for r in rows:
        try:
            data.append(float(str(r.get(value_col, "")).replace(",", ".").replace(" ", "")))
        except ValueError:
            data.append(0.0)

    chart_type = "bar"
    if 2 <= len(labels) <= 8:
        chart_type = "pie"
    label_blob = " ".join(labels[:3]).lower()
    if any(x in label_blob for x in ("202", "201", "-01", "-02", "sty", "lut", "mar", "kwi")):
        chart_type = "line"

    return {
        "type": chart_type,
        "title": f"{value_col} wg {label_col}",
        "labels": labels,
        "datasets": [{"label": value_col, "data": data}],
    }


def _run_text_to_sql_pipeline(
    cfg: dict,
    question: str,
    schema_str: str = "",
    fetch_limit: int = 500,
    with_chart: bool = True,
) -> dict:
    """Text-to-SQL z auto-korektą, LIMIT/TOP, schematem z próbkami i opcjonalnym wykresem."""
    dialect = _sql_dialect(cfg)
    dialect_label = _sql_llm_dialect_label(dialect)
    effective_schema, known_tables = _resolve_sql_schema(cfg, schema_str)

    limit_hint = _sql_limit_instruction(dialect)
    system_sql = (
        f"Jesteś ekspertem SQL ({dialect_label}). "
        "Generujesz wyłącznie zapytania SELECT (nigdy DELETE/UPDATE/DROP). "
        f"{limit_hint} "
        "Używaj formatów dat widocznych w przykładowych wartościach schematu. "
        "Odpowiadasz WYŁĄCZNIE samym SQL — bez wyjaśnień, bez markdown."
    )

    sql_query = ""
    last_error = ""
    corrections = 0

    for attempt in range(SQL_CORRECTION_MAX_RETRIES + 1):
        if attempt == 0:
            prompt_sql = (
                f"SCHEMAT BAZY:\n{effective_schema}\n\n"
                f"PYTANIE UŻYTKOWNIKA: {question}\n\n"
                f"Wygeneruj zapytanie SQL ({dialect_label}):"
            )
        else:
            corrections = attempt
            prompt_sql = (
                f"SCHEMAT BAZY:\n{effective_schema}\n\n"
                f"PYTANIE: {question}\n\n"
                f"Twoje poprzednie zapytanie:\n{sql_query}\n\n"
                f"Serwer bazy zwrócił błąd:\n{last_error}\n\n"
                "Popraw zapytanie SQL. Zwróć WYŁĄCZNIE poprawiony kod SQL:"
            )

        sql_response = call_llm(
            prompt_sql, system_sql, stream=False, max_tokens=2000, temperature=0.1,
        )
        sql_query = _clean_llm_sql(_llm_response_text(sql_response))
        sql_query = _ensure_sql_row_limit(sql_query, dialect)

        first_word = sql_query.split()[0].upper() if sql_query.split() else ""
        if first_word not in ("SELECT", "WITH"):
            last_error = f"Niedozwolone polecenie: {first_word}"
            if attempt >= SQL_CORRECTION_MAX_RETRIES:
                break
            continue

        is_safe, safe_err = _is_sql_safe(sql_query, ("SELECT", "WITH"))
        if not is_safe:
            last_error = safe_err or "Niedozwolone zapytanie SQL"
            if attempt >= SQL_CORRECTION_MAX_RETRIES:
                break
            continue

        ok_tables, table_err = _validate_sql_table_refs(sql_query, known_tables)
        if not ok_tables:
            last_error = table_err or "Nieznane tabele w SQL"
            if attempt >= SQL_CORRECTION_MAX_RETRIES:
                break
            continue

        try:
            conn = _get_sql_conn(cfg, readonly=True)
            cur = conn.cursor(as_dict=True) if dialect == "mssql" else conn.cursor()
            cur.execute(sql_query)
            result_rows = _sql_rows_to_dicts(cur, fetch_limit)
            cols = list(result_rows[0].keys()) if result_rows else []
            conn.close()

            out: dict = {
                "success": True,
                "sql": sql_query,
                "columns": cols,
                "rows": result_rows,
                "total": len(result_rows),
                "truncated": len(result_rows) == fetch_limit,
                "sql_corrections": corrections,
            }
            if with_chart:
                chart = _build_sql_chart_config(cols, result_rows)
                if chart:
                    out["chart"] = chart
            return out
        except Exception as e:
            last_error = str(e)
            if attempt >= SQL_CORRECTION_MAX_RETRIES:
                break

    return {
        "success": False,
        "error": last_error or "Nie udało się wygenerować poprawnego SQL",
        "sql": sql_query,
        "sql_corrections": corrections,
    }


# ============================================================
# KONIEC PRZYWRÓCONYCH FUNKCJI BEZPIECZEŃSTWA SQL
# ============================================================

SQL_CONFIG_PATH = Path(__file__).parent / ".sql_config.json"


def _load_sql_config() -> dict:
    """Pełna konfiguracja SQL z pliku (w tym hasło) — tylko backend."""
    if SQL_CONFIG_PATH.exists():
        try:
            return json.loads(SQL_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_sql_config(cfg: dict) -> None:
    SQL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _llm_response_text(result) -> str:
    if isinstance(result, dict):
        direct = (result.get("response") or "").strip()
        if direct:
            return direct
        choices = result.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message") if isinstance(first.get("message"), dict) else {}
            content = (msg.get("content") or first.get("text") or "").strip()
            if content:
                return content
        return ""
    return str(result).strip()


def _extract_first_json_value(raw_text: str):
    """Wyciąga pierwszy poprawny obiekt albo tablicę JSON z odpowiedzi LLM."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Pusta odpowiedź LLM")

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    decoder = json.JSONDecoder()
    candidates = [m.start() for m in re.finditer(r"[\{\[]", text)]
    for start in candidates:
        try:
            value, _end = decoder.raw_decode(text[start:])
            return value
        except Exception:
            continue
    raise ValueError("Nie udało się odczytać JSON z odpowiedzi LLM")


def _escape_html_text(text: str) -> str:
    return html.escape(text or "", quote=False)


def _clean_llm_sql(raw: str) -> str:
    sql = re.sub(r"^```\w*\n?", "", raw or "", flags=re.MULTILINE)
    sql = re.sub(r"```$", "", sql.strip()).strip()
    return sql


def _validate_sql_table_name(name: str) -> bool:
    return bool(name and re.match(r"^[\w]+$", name))


app = Flask(__name__)

# === Podstawowe logowanie ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ai_analiza")

# === Walidacja wymaganych zmiennych środowiskowych ===
required_env = ["QDRANT_URL", "QDRANT_KEY"]
missing = [key for key in required_env if not os.environ.get(key)]
if missing:
    logger.critical(f"Brak wymaganych zmiennych środowiskowych: {missing}")
    logger.critical("Upewnij się, że plik .env istnieje i zawiera poprawne wartości.")
    raise RuntimeError(f"Brakujące zmienne środowiskowe: {missing}")

QDRANT_URL        = os.environ["QDRANT_URL"]
QDRANT_KEY        = os.environ["QDRANT_KEY"]
QDRANT_LOCAL_URL  = os.environ.get("QDRANT_LOCAL_URL", "http://127.0.0.1:6333")
QDRANT_LOCAL_KEY  = os.environ.get("QDRANT_LOCAL_KEY", "dev-local-key")
QDRANT_CLOUD_URL  = os.environ.get("QDRANT_CLOUD_URL", "")
QDRANT_CLOUD_KEY  = os.environ.get("QDRANT_CLOUD_KEY", "")
ACTIVE_COLLECTION = os.environ.get("ACTIVE_COLLECTION", "dokumenty")
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL         = os.environ.get("LLM_MODEL", "llama3")
EMBED_MODEL       = os.environ.get("EMBED_MODEL", "nomic-embed-text")
OCR_LANG          = os.environ.get("OCR_LANG", "pol+eng")

# OpenRouter
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_MODEL_VERIFY = os.environ.get("OPENROUTER_MODEL_VERIFY", "google/gemini-2.0-flash-exp:free")
OPENROUTER_FALLBACK_TO_OLLAMA = os.environ.get("OPENROUTER_FALLBACK_TO_OLLAMA", "true").lower() in ("1", "true", "yes")
try:
    OPENROUTER_MAX_RETRIES = max(1, int(os.environ.get("OPENROUTER_MAX_RETRIES", "3")))
except ValueError:
    logger.warning("Nieprawidłowa wartość OPENROUTER_MAX_RETRIES w .env, używam domyślnej: 3")

TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() in ("1", "true", "yes")

# Groq (OpenAI-compatible API — szybkie testy)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_VERIFY = os.environ.get("GROQ_MODEL_VERIFY", "llama-3.1-8b-instant")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter").lower()   # openrouter | groq | ollama
FLEET_AUTO_ROUTE = os.environ.get("FLEET_AUTO_ROUTE", "").lower() in ("1", "true", "yes")

SEARCH_ROOTS      = [p.strip() for p in os.environ.get("SEARCH_ROOTS", "").split(':') if p.strip()]

APP_API_KEY = os.environ.get("APP_API_KEY", "").strip()
APP_HOST    = os.environ.get("APP_HOST", "127.0.0.1")

# Wersja aplikacji — automatycznie odczytywana z git tagów w trybie deweloperskim
# (git describe --tags --dirty). W releasach produkcyjnych wraca do stałej.
def _get_app_version() -> str:
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty", "--abbrev=0"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        return out.decode().strip()
    except Exception:
        return "2026.07"

APP_VERSION = _get_app_version()

_ACTIVE_COLLECTION_FILE = Path(__file__).parent / ".active_collection"


# ============================================================
# UPDATE / SELF-UPDATE HELPERS (for local development)
# ============================================================

def _get_remote_latest_tag() -> str | None:
    """Pobiera najnowszy tag z origin (z GitHub)."""
    try:
        # Pobierz listę tagów z remote bez pełnego fetcha (szybkie)
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", "--sort=-v:refname", "origin"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        lines = out.decode().strip().splitlines()
        for line in lines:
            # Format: <sha>    refs/tags/v2026.08
            if "refs/tags/" in line:
                tag = line.split("refs/tags/")[-1].strip()
                # Pomijamy annotated tag pointers (kończą się na ^{})
                if not tag.endswith("^{}"):
                    return tag
        return None
    except Exception:
        return None


def _get_local_latest_tag() -> str:
    """Zwraca aktualny lokalny tag (lub commit jeśli nie ma tagu)."""
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip()
    except Exception:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            return out.decode().strip()
        except Exception:
            return "unknown"


def _git_pull() -> dict:
    """Pobiera najnowszy kod z origin (master, potem main jako fallback)."""
    repo = Path(__file__).parent
    last: dict = {"success": False, "stdout": "", "stderr": ""}
    for branch in ("master", "main"):
        try:
            out = subprocess.run(
                ["git", "pull", "origin", branch],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=60,
            )
            last = {
                "success": out.returncode == 0,
                "stdout": out.stdout.strip(),
                "stderr": out.stderr.strip(),
                "branch": branch,
            }
            if out.returncode == 0:
                return last
        except Exception as e:
            last = {"success": False, "stdout": "", "stderr": str(e), "branch": branch}
    return last


def _try_restart_service() -> dict:
    """
    Próbuje zrestartować aplikację.
    Najpierw próbuje user service (który używa waitress), potem zwraca instrukcję.
    """
    try:
        # Najpierw spróbuj user service (najczęstszy i najbardziej produkcyjny przypadek)
        result = subprocess.run(
            ["systemctl", "--user", "restart", "ai_analiza"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return {
                "success": True,
                "method": "user-service",
                "message": "Usługa użytkownika zrestartowana (używa Waitress)."
            }
    except Exception:
        pass

    # Fallback
    return {
        "success": False,
        "method": "manual",
        "message": "Kod zaktualizowany. Zrestartuj aplikację ręcznie najlepiej przez: ./restart-app.sh --user"
    }


# Proste cache dla GitHub Releases (żeby nie spamować API przy każdym sprawdzeniu)
_github_release_cache = {"data": None, "timestamp": 0}
GITHUB_RELEASE_CACHE_SECONDS = 300  # 5 minut


def _get_latest_github_release() -> dict | None:
    """Pobiera informacje o najnowszym releasie z GitHub (z cache)."""
    import time
    now = time.time()

    if _github_release_cache["data"] and (now - _github_release_cache["timestamp"] < GITHUB_RELEASE_CACHE_SECONDS):
        return _github_release_cache["data"]

    try:
        import urllib.request
        import json as json_module

        url = "https://api.github.com/repos/dragon15555000/AI_analiza_dokumentow/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "AI-Analiza-Dokumentow"})
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json_module.loads(response.read().decode("utf-8"))

        release_info = {
            "tag_name": data.get("tag_name"),
            "name": data.get("name"),
            "body": data.get("body", ""),           # Markdown changelog
            "html_url": data.get("html_url"),
            "published_at": data.get("published_at"),
        }

        _github_release_cache["data"] = release_info
        _github_release_cache["timestamp"] = now
        return release_info

    except Exception:
        return None
ALLOWED_DOC_EXTENSIONS = frozenset(
    {"docx", "pdf", "xlsx", "xls", "csv", "md", "json", "txt"}
)


class EmbeddingError(RuntimeError):
    """Błąd generowania embeddingu — nie używaj wektora zerowego."""


def _load_persisted_collection() -> str | None:
    if _ACTIVE_COLLECTION_FILE.exists():
        name = _ACTIVE_COLLECTION_FILE.read_text(encoding="utf-8").strip()
        if name and re.match(r"^[\w\-]+$", name):
            return name
    return None


_persisted_col = _load_persisted_collection()
if _persisted_col:
    ACTIVE_COLLECTION = _persisted_col


def _persist_active_collection(name: str) -> None:
    try:
        _ACTIVE_COLLECTION_FILE.write_text(name, encoding="utf-8")
    except OSError as e:
        logger.warning("Nie zapisano aktywnej kolekcji: %s", e)


def _resolve_allowed_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in SEARCH_ROOTS:
        try:
            p = Path(raw).expanduser().resolve()
            if p.exists():
                roots.append(p)
        except OSError:
            continue
    if not roots:
        for fallback in (Path.home(), Path("/mnt"), Path(__file__).parent.resolve()):
            try:
                if fallback.exists():
                    roots.append(fallback.resolve())
            except OSError:
                continue
    return roots


def _path_is_allowed(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    for root in _resolve_allowed_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _path_is_browsable(path: Path) -> bool:
    """Folder można otworzyć tylko w obrębie SEARCH_ROOTS (bez /mnt, C: itd.)."""
    return _path_is_allowed(path)


def _default_browse_path() -> Path:
    gd = _google_drive_root()
    if gd:
        return gd
    roots = _resolve_allowed_roots()
    return roots[0] if roots else Path.home()


def _win_to_wsl(path: str) -> str | None:
    """Konwertuje ścieżkę Windows (G:\\...) na WSL przez wslpath."""
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return None
    if path.startswith("/"):
        return path
    try:
        proc = subprocess.run(
            ["wslpath", "-a", path],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                return out
    except Exception as e:
        logger.debug("wslpath failed: %s", e)
    return _normalize_sqlite_path_input(path) or None


def _normalize_browse_path(raw: str) -> str:
    """Akceptuje G:\\... lub /mnt/... — zwraca ścieżkę WSL."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return str(_default_browse_path())
    if re.match(r"^[A-Za-z]:", raw):
        wsl = _win_to_wsl(raw)
        if wsl:
            return wsl
    return raw


def _google_drive_root() -> Path | None:
    """Google Drive w WSL: zwykle /mnt/g/Mój dysk (PL) lub /mnt/g/My Drive."""
    for folder in ("Mój dysk", "My Drive"):
        candidate = Path("/mnt/g") / folder
        if candidate.is_dir() and _path_is_allowed(candidate):
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    g = Path("/mnt/g")
    if g.is_dir() and _path_is_allowed(g):
        try:
            return g.resolve()
        except OSError:
            return g
    return None


def _windows_user_profile() -> Path | None:
    """Profil użytkownika Windows w WSL: /mnt/c/Users/<login>."""
    c = Path("/mnt/c")
    if not c.is_dir():
        return None
    for name in (os.environ.get("USER"), os.environ.get("LOGNAME"), os.environ.get("USERNAME")):
        if not name:
            continue
        profile = c / "Users" / name
        if profile.is_dir() and _path_is_allowed(profile):
            try:
                return profile.resolve()
            except OSError:
                return profile
    return None


def _pick_windows_folder(initial_wsl: str = "") -> str | None:
    """Otwiera natywny dialog wyboru folderu Windows (działa z Google Drive G:)."""
    if platform.system() == "Windows":
        return None
    ps_lines = [
        "Add-Type -AssemblyName System.Windows.Forms",
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
        "$d.Description = 'Wybierz folder z dokumentami (Google Drive, dysk G:, itp.)'",
        "$d.ShowNewFolderButton = $false",
    ]
    if initial_wsl:
        win_init = wsl_to_win(_normalize_browse_path(initial_wsl))
        if win_init:
            safe = win_init.replace("'", "''")
            ps_lines.append(f"$d.SelectedPath = '{safe}'")
    ps_lines += [
        "$r = $d.ShowDialog()",
        "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }",
    ]
    ps_script = "\n".join(ps_lines)
    import base64
    enc = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-EncodedCommand", enc],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None
    win_path = (proc.stdout or "").strip()
    if not win_path or proc.returncode != 0:
        return None
    return _win_to_wsl(win_path)


def _dir_is_readable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        with os.scandir(path) as it:
            next(it, None)
        return True
    except (PermissionError, OSError):
        return False


def _friendly_root_label(p: Path) -> str:
    s = str(p)
    m = re.match(r"/mnt/([a-z])$", s, re.IGNORECASE)
    if m:
        letter = m.group(1).upper()
        if letter == "G":
            return "Google Drive (G:)"
        return f"Dysk {letter}:"
    if p == Path.home():
        return "Home"
    name = p.name
    return name if name else s


def _discover_browse_roots() -> list[dict]:
    """Punkty startowe — Google Drive, dysk C: (lokalny) + SEARCH_ROOTS."""
    drives: list[dict] = []
    seen: set[str] = set()

    gd = _google_drive_root()
    if gd:
        sp = str(gd)
        seen.add(sp)
        drives.append({
            "path": sp,
            "label": "Google Drive (G:)",
            "kind": "gdrive",
            "icon": "☁️",
            "accessible": _dir_is_readable(gd),
            "importable": True,
            "priority": 0,
        })

    profile = _windows_user_profile()
    if profile:
        sp = str(profile)
        if sp not in seen:
            seen.add(sp)
            drives.append({
                "path": sp,
                "label": "Komputer lokalny (C:)",
                "kind": "local",
                "icon": "💻",
                "accessible": _dir_is_readable(profile),
                "importable": True,
                "priority": 1,
            })

    for i, root in enumerate(_resolve_allowed_roots()):
        try:
            sp = str(root.resolve())
        except OSError:
            sp = str(root)
        if sp in seen:
            continue
        if gd and sp == "/mnt/g":
            continue
        if profile and sp == str(profile):
            continue
        seen.add(sp)
        m = re.match(r"/mnt/([a-z])$", sp, re.IGNORECASE)
        icon = "💾" if m else "📂"
        drives.append({
            "path": sp,
            "label": _friendly_root_label(root),
            "kind": "root",
            "icon": icon,
            "accessible": _dir_is_readable(root),
            "importable": True,
            "priority": i + 2,
        })

    drives.sort(key=lambda d: (d["priority"], not d["accessible"]))
    return drives


def _validate_extensions(exts: list) -> list[str]:
    out: list[str] = []
    for e in exts:
        e = str(e).strip().lower().lstrip(".")
        if re.match(r"^[a-z0-9]{1,10}$", e) and e in ALLOWED_DOC_EXTENSIONS:
            out.append(e)
    if not out:
        return ["docx", "pdf", "xlsx", "xls", "csv", "md", "json"]
    return out


def _find_files_safe(folder: Path, exts: list) -> list[Path]:
    """Wyszukiwanie plików bez shella (find jako lista argv)."""
    validated = _validate_extensions(exts)
    seen: set[str] = set()
    files: list[Path] = []
    for ext in validated:
        proc = subprocess.run(
            ["find", str(folder), "-type", "f", "-name", f"*.{ext}"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode not in (0, 1):
            logger.warning("find zakończone kodem %s: %s", proc.returncode, proc.stderr[:200])
            continue
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                p = Path(line).resolve()
            except OSError:
                continue
            key = str(p)
            if key not in seen and p.is_file() and _path_is_allowed(p):
                seen.add(key)
                files.append(p)
    return files


def _find_file_by_name_safe(fname: str) -> Path | None:
    if not fname or not re.match(r"^[^\\/\0]+$", fname):
        return None
    for root in _resolve_allowed_roots():
        if not root.exists():
            continue
        proc = subprocess.run(
            ["find", str(root), "-type", "f", "-name", fname],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode not in (0, 1):
            continue
        for line in proc.stdout.splitlines()[:1]:
            line = line.strip()
            if not line:
                continue
            try:
                p = Path(line).resolve()
            except OSError:
                continue
            if p.is_file() and _path_is_allowed(p):
                return p
    return None


def _redact_sql_config(cfg: dict) -> dict:
    if not cfg:
        return {}
    out = dict(cfg)
    if out.get("password"):
        out["password"] = "********"
    return out


@app.before_request
def _require_api_key():
    # Pełne usunięcie wsparcia dla ?api_key= (zgodnie z zaleceniami audytu bezpieczeństwa).
    # Klucz API może być przekazywany TYLKO przez nagłówek HTTP "X-API-Key".
    # Powody: ochrona przed wyciekiem do logów, historii przeglądarki, Referer itd.
    # Szczególnie ważne dla endpointów SSE (/import/stream, /search/stream, /hybrid/stream).
    if not APP_API_KEY:
        return None
    if request.endpoint == "index":
        return None
    provided = request.headers.get("X-API-Key", "")
    if provided != APP_API_KEY:
        return jsonify(
            {"success": False, "error": "Brak lub nieprawidłowy klucz API (wymagany nagłówek X-API-Key)"}
        ), 401
    return None


def _is_local_request() -> bool:
    """Sprawdza czy żądanie pochodzi z localhost (bezpieczne dla tras konfiguracyjnych)."""
    remote = request.remote_addr or ""
    if remote in ("127.0.0.1", "::1", "localhost"):
        return True
    if TRUST_PROXY:
        # Trust forwarded headers only when explicitly enabled.
        forwarded = request.headers.get("X-Forwarded-For", "") or request.headers.get("X-Real-IP", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first in ("127.0.0.1", "::1", "localhost"):
                return True
    return False


def _require_config_access():
    """
    Ochrona tras konfiguracyjnych (nawet gdy APP_API_KEY jest puste).
    Wymaga: localhost LUB poprawny klucz API (jeśli jest ustawiony).
    """
    if _is_local_request():
        return None
    if not APP_API_KEY:
        # Na produkcji z 0.0.0.0 bez klucza — blokujemy dostęp do konfiguracji
        return jsonify({
            "success": False,
            "error": "Dostęp do konfiguracji tylko z localhost lub po podaniu X-API-Key"
        }), 403
    provided = request.headers.get("X-API-Key", "")
    if provided != APP_API_KEY:
        return jsonify({"success": False, "error": "Brak lub nieprawidłowy klucz API (wymagany nagłówek X-API-Key)"}), 401
    return None


# Konfiguracja LLM zapisywana przez UI (nadpisuje zmienne powyżej)
LLM_CONFIG_PATH = Path(__file__).parent / ".llm_config.json"


def _load_llm_config() -> dict:
    if LLM_CONFIG_PATH.exists():
        try:
            return json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_llm_config(cfg: dict):
    try:
        LLM_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Błąd zapisu llm config: %s", e)


def _apply_llm_config(cfg: dict):
    """Nadpisuje globalne zmienne LLM konfiguracją z pliku."""
    global DEFAULT_LLM_PROVIDER, OPENROUTER_API_KEY, OPENROUTER_MODEL
    global OPENROUTER_MODEL_VERIFY, OPENROUTER_FALLBACK_TO_OLLAMA
    global GROQ_API_KEY, GROQ_MODEL, GROQ_MODEL_VERIFY
    global OLLAMA_URL, LLM_MODEL, APP_API_KEY

    if cfg.get("provider"):
        DEFAULT_LLM_PROVIDER = cfg["provider"]
    if cfg.get("openrouter_key"):
        OPENROUTER_API_KEY = cfg["openrouter_key"]
    if cfg.get("openrouter_model"):
        OPENROUTER_MODEL = cfg["openrouter_model"]
    if cfg.get("openrouter_model_verify"):
        OPENROUTER_MODEL_VERIFY = cfg["openrouter_model_verify"]
    if "openrouter_fallback" in cfg:
        OPENROUTER_FALLBACK_TO_OLLAMA = bool(cfg["openrouter_fallback"])
    if cfg.get("groq_key"):
        GROQ_API_KEY = cfg["groq_key"]
    if cfg.get("groq_model"):
        GROQ_MODEL = cfg["groq_model"]
    if cfg.get("groq_model_verify"):
        GROQ_MODEL_VERIFY = cfg["groq_model_verify"]
    if cfg.get("ollama_url"):
        OLLAMA_URL = cfg["ollama_url"]
    if cfg.get("llm_model"):
        LLM_MODEL = cfg["llm_model"]

    # Obsługa klucza API aplikacji (do ochrony endpointów)
    if cfg.get("app_api_key"):
        APP_API_KEY = cfg["app_api_key"]
    elif "app_api_key" in cfg and not cfg.get("app_api_key"):
        # Pozwól wyczyścić klucz przez UI
        APP_API_KEY = ""

    global FLEET_AUTO_ROUTE
    if "fleet_auto_route" in cfg:
        FLEET_AUTO_ROUTE = bool(cfg["fleet_auto_route"])

    _rebuild_provider_pools(cfg)


# (początkowe _apply_llm_config — wywołane po definicji pul, na końcu sekcji pool)


# ============================================================
# CHRONIONE TRASY KONFIGURACJI LLM (nawet bez APP_API_KEY)
# ============================================================

@app.route('/api/config/llm', methods=['GET'])
def get_llm_config():
    auth = _require_config_access()
    if auth:
        return auth

    cfg = _load_llm_config()

    # Zawsze zwracamy tylko flagi "czy klucz jest ustawiony" — nigdy preview kluczy
    return jsonify({
        "success": True,
        "provider": cfg.get("provider", DEFAULT_LLM_PROVIDER),
        "ollama_url": cfg.get("ollama_url", OLLAMA_URL),
        "llm_model": cfg.get("llm_model", LLM_MODEL),
        "openrouter_model": cfg.get("openrouter_model", OPENROUTER_MODEL),
        "openrouter_model_verify": cfg.get("openrouter_model_verify", OPENROUTER_MODEL_VERIFY),
        "openrouter_fallback": cfg.get("openrouter_fallback", OPENROUTER_FALLBACK_TO_OLLAMA),
        "groq_model": cfg.get("groq_model", GROQ_MODEL),
        "groq_model_verify": cfg.get("groq_model_verify", GROQ_MODEL_VERIFY),

        # Bezpieczne flagi zamiast podglądów kluczy
        "openrouter_key_set": bool(cfg.get("openrouter_key") or cfg.get("openrouter_keys") or OPENROUTER_API_KEY),
        "groq_key_set": bool(cfg.get("groq_key") or cfg.get("groq_keys") or GROQ_API_KEY),
        "app_api_key_set": bool(cfg.get("app_api_key")),

        "groq_keys_count": GROQ_KEY_POOL.count(),
        "openrouter_keys_count": OPENROUTER_KEY_POOL.count(),
        "ollama_urls_count": OLLAMA_URL_POOL.count(),
        "fleet_auto_route": cfg.get("fleet_auto_route", FLEET_AUTO_ROUTE),
        "pools": {
            "groq": GROQ_KEY_POOL.stats(),
            "openrouter": OPENROUTER_KEY_POOL.stats(),
            "ollama": OLLAMA_URL_POOL.stats(),
        },
    })


@app.route('/api/config/llm', methods=['POST'])
def save_llm_config():
    auth = _require_config_access()
    if auth:
        return auth

    data = request.get_json() or {}

    current = _load_llm_config()

    # Aktualizuj tylko dozwolone pola
    allowed = ["provider", "ollama_url", "llm_model", "openrouter_model",
               "openrouter_model_verify", "openrouter_fallback",
               "groq_model", "groq_model_verify", "fleet_auto_route"]

    for k in allowed:
        if k in data:
            current[k] = data[k]

    # Klucze — zapisujemy tylko jeśli użytkownik je podał (nie nadpisujemy pustymi)
    if data.get("openrouter_key"):
        current["openrouter_key"] = data["openrouter_key"]
    if data.get("groq_key"):
        current["groq_key"] = data["groq_key"]
    if isinstance(data.get("openrouter_keys"), list):
        current["openrouter_keys"] = [k.strip() for k in data["openrouter_keys"] if k and str(k).strip()]
    if isinstance(data.get("groq_keys"), list):
        current["groq_keys"] = [k.strip() for k in data["groq_keys"] if k and str(k).strip()]
    if isinstance(data.get("ollama_urls"), list):
        current["ollama_urls"] = [u.strip().rstrip("/") for u in data["ollama_urls"] if u and str(u).strip()]
    if "app_api_key" in data:
        current["app_api_key"] = data["app_api_key"] or ""

    _save_llm_config(current)
    _apply_llm_config(current)

    return jsonify({"success": True, "message": "Konfiguracja LLM zapisana"})


# ============================================================
# POOL KLUCZY / SERWERÓW (rotacja round-robin + cooldown)
# ============================================================

class EndpointPool:
    """Thread-safe round-robin z cooldownem po błędach (429, auth, connection)."""

    def __init__(self, label: str = "pool"):
        self.label = label
        self._lock = threading.Lock()
        self._items: list[str] = []
        self._idx = 0
        self._cooldown: dict[str, float] = {}

    def set_items(self, items: list[str]):
        with self._lock:
            clean: list[str] = []
            seen: set[str] = set()
            for x in items:
                s = (x or "").strip()
                if s and s not in seen:
                    seen.add(s)
                    clean.append(s)
            self._items = clean
            if self._idx >= len(self._items):
                self._idx = 0
            stale = set(self._cooldown) - seen
            for k in stale:
                del self._cooldown[k]

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            available = sum(1 for i in self._items if self._cooldown.get(i, 0) <= now)
            return {
                "total": len(self._items),
                "available": available,
                "in_cooldown": len(self._items) - available,
            }

    def peek_primary(self) -> str | None:
        with self._lock:
            return self._items[0] if self._items else None

    def acquire(self) -> str | None:
        with self._lock:
            if not self._items:
                return None
            now = time.monotonic()
            n = len(self._items)
            for _ in range(n):
                item = self._items[self._idx]
                self._idx = (self._idx + 1) % n
                if self._cooldown.get(item, 0) <= now:
                    return item
            return min(self._items, key=lambda i: self._cooldown.get(i, 0))

    def mark_failure(self, item: str, *, cooldown_sec: float = 60.0):
        if not item:
            return
        with self._lock:
            self._cooldown[item] = time.monotonic() + max(1.0, cooldown_sec)

    def mark_success(self, item: str):
        if not item:
            return
        with self._lock:
            self._cooldown.pop(item, None)


GROQ_KEY_POOL = EndpointPool("groq_keys")
OPENROUTER_KEY_POOL = EndpointPool("openrouter_keys")
OLLAMA_URL_POOL = EndpointPool("ollama_urls")


def _parse_env_list(*names: str) -> list[str]:
    out: list[str] = []
    for name in names:
        val = os.environ.get(name, "").strip()
        if not val:
            continue
        for part in re.split(r"[,;\n\r]+", val):
            p = part.strip()
            if p:
                out.append(p)
    return out


def _merge_unique(*lists) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for x in lst or []:
            s = (x or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _normalize_key_list(cfg: dict, single_key: str, plural_key: str) -> list[str]:
    items: list[str] = []
    raw = cfg.get(plural_key)
    if isinstance(raw, list):
        items.extend(raw)
    elif cfg.get(single_key):
        items.append(cfg[single_key])
    return items


def _normalize_url_list(cfg: dict) -> list[str]:
    urls: list[str] = []
    raw = cfg.get("ollama_urls")
    if isinstance(raw, list):
        urls.extend(raw)
    elif cfg.get("ollama_url"):
        urls.append(cfg["ollama_url"])
    return [u.rstrip("/") for u in urls if u and str(u).strip()]


def _rebuild_provider_pools(cfg: dict | None = None):
    """Buduje pule z .llm_config.json + zmiennych środowiskowych."""
    global GROQ_API_KEY, OPENROUTER_API_KEY, OLLAMA_URL
    cfg = cfg or {}

    groq_keys = _merge_unique(
        _normalize_key_list(cfg, "groq_key", "groq_keys"),
        _parse_env_list("GROQ_API_KEYS", "GROQ_API_KEY"),
    )
    if not groq_keys and GROQ_API_KEY:
        groq_keys = [GROQ_API_KEY.strip()]
    GROQ_KEY_POOL.set_items(groq_keys)
    if groq_keys:
        GROQ_API_KEY = groq_keys[0]

    or_keys = _merge_unique(
        _normalize_key_list(cfg, "openrouter_key", "openrouter_keys"),
        _parse_env_list("OPENROUTER_API_KEYS", "OPENROUTER_API_KEY"),
    )
    if not or_keys and OPENROUTER_API_KEY:
        or_keys = [OPENROUTER_API_KEY.strip()]
    OPENROUTER_KEY_POOL.set_items(or_keys)
    if or_keys:
        OPENROUTER_API_KEY = or_keys[0]

    ollama_urls = _merge_unique(
        _normalize_url_list(cfg),
        _parse_env_list("OLLAMA_URLS", "OLLAMA_URL"),
    )
    if not ollama_urls and OLLAMA_URL:
        ollama_urls = [OLLAMA_URL.rstrip("/")]
    OLLAMA_URL_POOL.set_items(ollama_urls)
    if ollama_urls:
        OLLAMA_URL = ollama_urls[0]


def _ollama_bases() -> list[str]:
    """Kolejność serwerów Ollama do próby (round-robin z puli)."""
    if OLLAMA_URL_POOL.count():
        bases: list[str] = []
        seen: set[str] = set()
        for _ in range(OLLAMA_URL_POOL.count()):
            u = OLLAMA_URL_POOL.acquire()
            if u:
                u = u.rstrip("/")
                if u not in seen:
                    seen.add(u)
                    bases.append(u)
        if bases:
            return bases
    return [OLLAMA_URL.rstrip("/")]


def _cloud_headers(cloud_cfg: dict, api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **cloud_cfg.get("extra_headers", {}),
    }


def _next_cloud_key(cloud_cfg: dict, exclude: set[str] | None = None) -> str | None:
    exclude = exclude or set()
    pool = cloud_cfg.get("pool")
    if pool and pool.count():
        for _ in range(pool.count()):
            k = pool.acquire()
            if k and k not in exclude:
                return k
    k = cloud_cfg.get("api_key")
    return k if k and k not in exclude else None


def _should_rotate_pool(exc: Exception) -> bool:
    return _is_rate_limit_error(exc) or _is_auth_error(exc) or _is_transient_llm_error(exc)


_apply_llm_config(_load_llm_config())


# ============================================================
# FLOTA LLM — sondowanie, ranking, auto-routing
# ============================================================

LLM_FLEET_STATS_PATH = Path(__file__).parent / ".llm_fleet_stats.json"
_fleet_stats_lock = threading.Lock()

LLM_FLEET_CATALOG = {
    "ollama": {
        "label": "Ollama (lokalny)",
        "cost": "darmowy (lokalnie)",
        "limits_hint": "Brak limitów API — zależy od RAM/GPU i modelu.",
        "context_hint": "Zależy od modelu (np. 8k–128k).",
        "agents": ["embedding", "chat", "fallback"],
        "free_models": [],
        "default_model": lambda: LLM_MODEL,
    },
    "openrouter": {
        "label": "OpenRouter (chmura)",
        "cost": "modele :free — bez opłat (limity dzienne)",
        "limits_hint": "Limity per model free; rotacja kluczy przy 429.",
        "context_hint": "Do ~128k (model zależny).",
        "agents": ["chat", "synteza", "weryfikacja", "sieć", "timeline"],
        "free_models": [
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2.5-7b-instruct:free",
            "meta-llama/llama-3.1-8b-instruct:free",
        ],
        "default_model": lambda: OPENROUTER_MODEL,
    },
    "groq": {
        "label": "Groq (chmura)",
        "cost": "darmowy tier z limitami RPM/TPM",
        "limits_hint": "https://console.groq.com/settings/limits — RPM/TPM per model.",
        "context_hint": "Do ~128k (model zależny).",
        "agents": ["chat", "szybki agent", "weryfikacja"],
        "free_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "groq/compound-mini",
            "groq/compound",
        ],
        "default_model": lambda: GROQ_MODEL,
    },
}

FLEET_TASK_HINTS = {
    "general": {"prefer": ["openrouter", "groq", "ollama"], "note": "Domyślny chat"},
    "search": {"prefer": ["openrouter", "groq", "ollama"], "note": "Szybka synteza wyszukiwania"},
    "ai": {"prefer": ["openrouter", "groq", "ollama"], "note": "Analiza z bazy"},
    "compare": {"prefer": ["openrouter", "ollama", "groq"], "note": "Duży kontekst — modele 70B"},
    "network": {"prefer": ["openrouter", "ollama", "groq"], "note": "Ekstrakcja JSON / relacji"},
    "timeline": {"prefer": ["openrouter", "groq", "ollama"], "note": "Ekstrakcja dat z JSON"},
    "swarm": {"prefer": ["openrouter", "groq", "ollama"], "note": "Równoległe workery"},
    "verify": {"prefer": ["openrouter", "groq", "ollama"], "note": "Drugi pass LLM"},
}


def _fleet_default_stats() -> dict:
    return {
        "calls_total": 0,
        "calls_ok": 0,
        "calls_fail": 0,
        "latency_ema_ms": None,
        "last_ok_at": None,
        "last_error": None,
        "last_error_kind": None,
        "last_probe_at": None,
        "last_probe_ms": None,
        "last_probe_ok": None,
        "score": 50.0,
    }


def _fleet_load_all_stats() -> dict:
    if not LLM_FLEET_STATS_PATH.exists():
        return {}
    try:
        return json.loads(LLM_FLEET_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fleet_save_all_stats(data: dict):
    try:
        LLM_FLEET_STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("fleet stats save: %s", e)


def _fleet_get_stats(prov: str) -> dict:
    with _fleet_stats_lock:
        all_s = _fleet_load_all_stats()
        base = _fleet_default_stats()
        base.update(all_s.get(prov) or {})
        return base


def _fleet_compute_score(stats: dict, probe: dict | None, health: dict | None) -> float:
    score = 0.0
    probe_ok = (probe or {}).get("ok")
    health_ok = (health or {}).get("ok")
    if probe_ok is True or (probe_ok is None and health_ok):
        score += 35.0
    elif probe_ok is False or health_ok is False:
        score += 5.0
    else:
        score += 15.0

    lat = (probe or {}).get("latency_ms") or stats.get("last_probe_ms") or stats.get("latency_ema_ms")
    if lat is not None:
        score += max(0.0, 30.0 - min(float(lat), 3000.0) / 100.0)

    total = int(stats.get("calls_total") or 0)
    ok = int(stats.get("calls_ok") or 0)
    if total > 0:
        score += 25.0 * (ok / total)
    else:
        score += 12.0

    kind = (probe or {}).get("kind") or stats.get("last_error_kind")
    if kind in ("rate_limit", "auth", "org_blocked", "no_key"):
        score -= 20.0

    return round(max(0.0, min(100.0, score)), 1)


def _fleet_record_outcome(prov: str, ok: bool, latency_ms: float | None = None,
                          error_kind: str | None = None, error_msg: str | None = None):
    with _fleet_stats_lock:
        all_s = _fleet_load_all_stats()
        st = _fleet_default_stats()
        st.update(all_s.get(prov) or {})
        st["calls_total"] = int(st["calls_total"]) + 1
        if ok:
            st["calls_ok"] = int(st["calls_ok"]) + 1
            st["last_ok_at"] = int(time.time())
        else:
            st["calls_fail"] = int(st["calls_fail"]) + 1
            if error_kind:
                st["last_error_kind"] = error_kind
            if error_msg:
                st["last_error"] = str(error_msg)[:200]
        if latency_ms is not None:
            prev = st.get("latency_ema_ms")
            st["latency_ema_ms"] = (
                float(latency_ms) if prev is None
                else 0.25 * float(latency_ms) + 0.75 * float(prev)
            )
        all_s[prov] = st
        _fleet_save_all_stats(all_s)


def _fleet_probe_provider(prov: str) -> dict:
    """Lekkie ping — mierzy opóźnienie i dostępność."""
    t0 = time.monotonic()
    meta = LLM_FLEET_CATALOG.get(prov, {})
    try:
        if prov == "ollama":
            h = _check_ollama_health()
            ok = h.get("ok", False)
            kind = None if ok else "network"
            err = h.get("error")
        elif prov == "openrouter":
            h = _check_openrouter_health()
            ok = h.get("ok", False)
            kind = "no_key" if "Brak" in str(h.get("error") or "") else ("rate_limit" if "429" in str(h.get("error") or "") else None)
            err = h.get("error")
        elif prov == "groq":
            api_key = GROQ_KEY_POOL.peek_primary() or GROQ_API_KEY
            if not api_key:
                h = {"ok": False, "kind": "no_key", "error": "Brak GROQ_API_KEY"}
                ok, kind, err = False, "no_key", h["error"]
            else:
                try:
                    r = requests.get(
                        "https://api.groq.com/openai/v1/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=8,
                    )
                    if r.status_code == 401:
                        h = {"ok": False, "kind": "auth", "error": "Nieprawidłowy GROQ_API_KEY"}
                    elif r.status_code == 429:
                        h = {"ok": False, "kind": "rate_limit", "error": "Groq rate limit (429)"}
                    else:
                        r.raise_for_status()
                        h = {
                            "ok": True,
                            "models_available": len(r.json().get("data", [])),
                            "model_tested": GROQ_MODEL,
                        }
                    ok = h.get("ok", False)
                    kind = h.get("kind")
                    err = h.get("error")
                except Exception as ex:
                    err = _format_cloud_api_error(ex, "Groq")[:200]
                    kind = "network"
                    ok = False
                    h = {"ok": False, "kind": kind, "error": err}
        else:
            return {"ok": False, "kind": "unknown", "error": "Nieznany provider"}
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        out = {
            "ok": ok,
            "latency_ms": latency_ms,
            "kind": kind,
            "error": err,
            "health": h,
        }
        with _fleet_stats_lock:
            all_s = _fleet_load_all_stats()
            st = _fleet_default_stats()
            st.update(all_s.get(prov) or {})
            st["last_probe_at"] = int(time.time())
            st["last_probe_ms"] = latency_ms
            st["last_probe_ok"] = ok
            if not ok and err:
                st["last_error"] = str(err)[:200]
                st["last_error_kind"] = kind
            st["score"] = _fleet_compute_score(st, out, h)
            all_s[prov] = st
            _fleet_save_all_stats(all_s)
        _fleet_record_outcome(prov, ok, latency_ms, error_kind=kind, error_msg=err)
        return out
    except Exception as e:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        out = {"ok": False, "latency_ms": latency_ms, "kind": "network", "error": str(e)[:200]}
        _fleet_record_outcome(prov, False, latency_ms, error_kind="network", error_msg=str(e))
        return out


def _fleet_health_from_cache(prov: str, stats: dict) -> dict:
    """Szybki status z ostatniej sondy — bez ponownego chat ping (Groq bywa >5 s)."""
    if stats.get("last_probe_at"):
        return {
            "ok": bool(stats.get("last_probe_ok")),
            "cached": True,
            "last_probe_at": stats.get("last_probe_at"),
            "error": stats.get("last_error"),
            "kind": stats.get("last_error_kind"),
        }
    if prov == "ollama":
        return _check_ollama_health()
    if prov == "openrouter":
        api_key = OPENROUTER_KEY_POOL.peek_primary() or OPENROUTER_API_KEY
        if not api_key:
            return {"ok": False, "error": "Brak OPENROUTER_API_KEY", "kind": "no_key"}
        return {"ok": True, "cached": False, "note": "Klucz ustawiony — uruchom sondę dla latencji"}
    api_key = GROQ_KEY_POOL.peek_primary() or GROQ_API_KEY
    if not api_key:
        return {"ok": False, "error": "Brak GROQ_API_KEY", "kind": "no_key"}
    return {"ok": True, "cached": False, "note": "Klucz ustawiony — uruchom sondę dla latencji"}


def _fleet_provider_row(prov: str, *, probe: bool = False) -> dict:
    meta = LLM_FLEET_CATALOG[prov]
    stats = _fleet_get_stats(prov)
    probe_res = _fleet_probe_provider(prov) if probe else None
    if prov == "ollama":
        pool = OLLAMA_URL_POOL.stats()
        key_set = True
    elif prov == "openrouter":
        pool = OPENROUTER_KEY_POOL.stats()
        key_set = bool(OPENROUTER_KEY_POOL.peek_primary() or OPENROUTER_API_KEY)
    else:
        pool = GROQ_KEY_POOL.stats()
        key_set = bool(GROQ_KEY_POOL.peek_primary() or GROQ_API_KEY)

    if probe_res is not None:
        health = probe_res.get("health") or {"ok": probe_res.get("ok"), "error": probe_res.get("error")}
        stats = _fleet_get_stats(prov)
    else:
        health = _fleet_health_from_cache(prov, stats)

    score = _fleet_compute_score(stats, probe_res, health)
    return {
        "id": prov,
        "label": meta["label"],
        "ok": health.get("ok", False),
        "key_configured": key_set,
        "score": score,
        "latency_ms": (probe_res or {}).get("latency_ms") or stats.get("last_probe_ms") or stats.get("latency_ema_ms"),
        "limits_hint": meta["limits_hint"],
        "context_hint": meta["context_hint"],
        "cost": meta["cost"],
        "agents": meta["agents"],
        "free_models": meta.get("free_models") or [],
        "default_model": meta["default_model"](),
        "pool": pool,
        "health": health,
        "stats": {
            "calls_total": stats.get("calls_total", 0),
            "calls_ok": stats.get("calls_ok", 0),
            "calls_fail": stats.get("calls_fail", 0),
            "last_probe_at": stats.get("last_probe_at"),
            "last_error": stats.get("last_error"),
            "last_error_kind": stats.get("last_error_kind"),
        },
        "probe": probe_res,
    }


def _fleet_rank_providers(task: str | None = None) -> list[dict]:
    task = (task or "general").lower()
    hint = FLEET_TASK_HINTS.get(task, FLEET_TASK_HINTS["general"])
    prefer = hint["prefer"]
    rows = [_fleet_provider_row(p, probe=False) for p in LLM_FLEET_CATALOG]
    order = {p: i for i, p in enumerate(prefer)}
    rows.sort(key=lambda r: (-r["score"], order.get(r["id"], 99)))
    return rows


def _fleet_pick_provider(task: str | None = None) -> str | None:
    ranked = _fleet_rank_providers(task)
    for row in ranked:
        if row.get("ok") and row.get("key_configured", True):
            return row["id"]
    for row in ranked:
        if row.get("ok"):
            return row["id"]
    return ranked[0]["id"] if ranked else None


@app.route('/api/llm/fleet', methods=['GET'])
def api_llm_fleet():
    auth = _require_config_access()
    if auth:
        return auth
    task = (request.args.get("task") or "general").strip().lower()
    ranked = _fleet_rank_providers(task)
    hint = FLEET_TASK_HINTS.get(task, FLEET_TASK_HINTS["general"])
    recommended = ranked[0]["id"] if ranked else DEFAULT_LLM_PROVIDER
    return jsonify({
        "success": True,
        "auto_route": FLEET_AUTO_ROUTE,
        "active_provider": DEFAULT_LLM_PROVIDER,
        "recommended_provider": recommended,
        "recommended_for_task": task,
        "task_hint": hint,
        "providers": ranked,
        "timestamp": int(time.time()),
    })


@app.route('/api/llm/fleet/probe', methods=['POST'])
def api_llm_fleet_probe():
    auth = _require_config_access()
    if auth:
        return auth
    data = request.get_json() or {}
    only = (data.get("provider") or "").strip().lower()
    providers = [only] if only in LLM_FLEET_CATALOG else list(LLM_FLEET_CATALOG.keys())
    results = {p: _fleet_probe_provider(p) for p in providers}
    ranked = _fleet_rank_providers(data.get("task") or "general")
    return jsonify({
        "success": True,
        "probed": providers,
        "results": results,
        "providers": ranked,
        "timestamp": int(time.time()),
    })


@app.route('/api/llm/fleet/auto-route', methods=['POST'])
def api_llm_fleet_auto_route():
    auth = _require_config_access()
    if auth:
        return auth
    global FLEET_AUTO_ROUTE
    data = request.get_json() or {}
    enabled = bool(data.get("enabled"))
    FLEET_AUTO_ROUTE = enabled
    cfg = _load_llm_config()
    cfg["fleet_auto_route"] = enabled
    _save_llm_config(cfg)
    return jsonify({
        "success": True,
        "auto_route": FLEET_AUTO_ROUTE,
        "recommended_provider": _fleet_pick_provider("general"),
    })


# ============================================================
# LLM PROVIDER ABSTRACTION (Ollama / OpenRouter / Groq)
# ============================================================

def get_llm_provider(request_provider: str | None = None, task: str | None = None) -> str:
    """Zwraca aktywny provider: 'ollama', 'openrouter' lub 'groq'."""
    if request_provider:
        p = request_provider.lower()
        if p in ("ollama", "openrouter", "groq"):
            return p
    if FLEET_AUTO_ROUTE:
        picked = _fleet_pick_provider(task or "general")
        if picked:
            return picked
    return DEFAULT_LLM_PROVIDER


def _model_from_request(data: dict, provider: str | None = None) -> str | None:
    """Model z body requestu (groq_model / openrouter_model)."""
    prov = get_llm_provider(provider or data.get("llm_provider"))
    if prov == "groq":
        return data.get("groq_model") or data.get("openrouter_model")
    if prov == "openrouter":
        return data.get("openrouter_model")
    return None


def _cloud_llm_settings(prov: str) -> dict:
    """Ustawienia chmurowego providera (OpenAI-compatible chat/completions)."""
    if prov == "groq":
        pool = GROQ_KEY_POOL
        fallback_key = GROQ_API_KEY
        return {
            "api_key": pool.acquire() or fallback_key,
            "url": GROQ_API_URL,
            "default_model": GROQ_MODEL,
            "verify_model": GROQ_MODEL_VERIFY,
            "label": "Groq",
            "key_env": "GROQ_API_KEY",
            "extra_headers": {},
            "pool": pool,
        }
    pool = OPENROUTER_KEY_POOL
    return {
        "api_key": pool.acquire() or OPENROUTER_API_KEY,
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": OPENROUTER_MODEL,
        "verify_model": OPENROUTER_MODEL_VERIFY,
        "label": "OpenRouter",
        "key_env": "OPENROUTER_API_KEY",
        "extra_headers": {
            "HTTP-Referer": "http://localhost",
            "X-Title": "AI Analiza Dokumentów",
        },
        "pool": pool,
    }


# ---- Rate limit helpers (OpenRouter) ----

# ============================================================
# HEALTH / STATUS DASHBOARD
# ============================================================

def _check_ollama_health() -> dict:
    """Sprawdza czy Ollama działa i czy modele są dostępne."""
    base = (_ollama_bases()[0] if _ollama_bases() else OLLAMA_URL).rstrip("/")
    try:
        url = base + "/api/tags"
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            has_llm = any(LLM_MODEL in m for m in models)
            has_embed = any(EMBED_MODEL in m for m in models)
            return {
                "ok": True,
                "url": base,
                "pool": OLLAMA_URL_POOL.stats(),
                "models_available": len(models),
                "has_llm": has_llm,
                "has_embedding": has_embed,
                "error": None
            }
    except Exception as e:
        return {"ok": False, "url": base, "pool": OLLAMA_URL_POOL.stats(), "error": str(e)[:120]}


def _check_openrouter_health() -> dict:
    """Lekkie sprawdzenie OpenRouter (czy klucz działa)."""
    api_key = OPENROUTER_KEY_POOL.peek_primary() or OPENROUTER_API_KEY
    pool_stats = OPENROUTER_KEY_POOL.stats()
    if not api_key:
        return {"ok": False, "error": "Brak OPENROUTER_API_KEY", "pool": pool_stats}

    try:
        url = "https://openrouter.ai/api/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {
                "ok": True,
                "models_available": len(data.get("data", [])),
                "pool": pool_stats,
                "error": None
            }
    except Exception as e:
        return {"ok": False, "pool": pool_stats, "error": str(e)[:120]}


def _format_cloud_api_error(exc: Exception, label: str = "API") -> str:
    """Wyciąga czytelny komunikat z odpowiedzi OpenAI-compatible API (Groq/OpenRouter)."""
    try:
        if hasattr(exc, "response") and exc.response is not None:
            body = exc.response.json()
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
            if msg:
                if "blocked at the organization" in msg.lower():
                    return (
                        f"{msg} Włącz model w panelu organizacji: "
                        "https://console.groq.com/settings/limits"
                    )
                return msg
    except Exception:
        pass
    return str(exc)[:300]


def _check_groq_health() -> dict:
    """Sprawdza Groq: klucz + czy chat na domyślnym modelu nie jest zablokowany w org."""
    api_key = GROQ_KEY_POOL.peek_primary() or GROQ_API_KEY
    pool_stats = GROQ_KEY_POOL.stats()
    if not api_key:
        return {"ok": False, "kind": "no_key", "error": "Brak GROQ_API_KEY", "pool": pool_stats}

    try:
        r = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            timeout=15,
        )
        if r.status_code == 403:
            err = (r.json().get("error") or {}).get("message", r.text)
            err_lower = str(err).lower()
            kind = "forbidden"
            if "blocked at the organization" in err_lower or "console.groq.com" in err_lower:
                kind = "org_blocked"
            return {
                "ok": False,
                "kind": kind,
                "error": str(err)[:200],
                "hint": "https://console.groq.com/settings/limits",
                "pool": pool_stats,
            }
        if r.status_code == 401:
            return {"ok": False, "kind": "auth", "error": "Nieprawidłowy GROQ_API_KEY", "pool": pool_stats}
        if r.status_code == 404:
            return {"ok": False, "kind": "model_not_found", "error": f"Model '{GROQ_MODEL}' nie jest dostępny w Groq", "pool": pool_stats}
        if r.status_code == 429:
            return {"ok": False, "kind": "rate_limit", "error": "Groq rate limit (429)", "pool": pool_stats}
        r.raise_for_status()
        models_r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=6,
        )
        models_count = len(models_r.json().get("data", [])) if models_r.ok else 0
        return {
            "ok": True,
            "kind": "ok",
            "models_available": models_count,
            "model_tested": GROQ_MODEL,
            "pool": pool_stats,
            "error": None,
        }
    except Exception as e:
        err = _format_cloud_api_error(e, "Groq")[:200]
        err_lower = err.lower()
        kind = "network"
        if "429" in err_lower or "rate limit" in err_lower:
            kind = "rate_limit"
        elif "unauthorized" in err_lower or "401" in err_lower or "invalid api key" in err_lower:
            kind = "auth"
        elif "blocked at the organization" in err_lower or "console.groq.com" in err_lower:
            kind = "org_blocked"
        elif "model" in err_lower and "not found" in err_lower:
            kind = "model_not_found"
        return {"ok": False, "kind": kind, "error": err, "pool": pool_stats}


def _sanitize_for_prompt(text: str, max_len: int = 1400) -> str:
    """
    Podstawowa ochrona przed Prompt Injection.
    Usuwa/redaguje próby przejęcia kontroli nad LLM przez złośliwą treść w dokumentach.
    """
    if not text:
        return ""

    text = str(text)

    # 1. Usuwamy / redagujemy typowe frazy jailbreak / prompt injection
    jailbreak_patterns = [
        r"(?i)(ignore|disregard|forget|override|disobey).*?(previous|above|all|earlier|prior).*?instructions?",
        r"(?i)(you are now|act as|pretend to be|roleplay as).*?(different|new|another).*?(assistant|ai|model|character)",
        r"(?i)from now on.*?(you must|you will|always|never)",
        r"(?i)system prompt|initial instructions|hidden instructions",
    ]

    for pattern in jailbreak_patterns:
        text = re.sub(pattern, "[INSTRUCTION REDACTED]", text)

    # 2. Neutralizujemy bloki kodu markdown (często używane do ukrycia instrukcji)
    text = re.sub(r"```[\s\S]*?```", "[CODE BLOCK REDACTED]", text)

    # 3. Usuwamy nadmierne sekwencje backticków lub specjalnych znaków
    text = re.sub(r"`{3,}", "``", text)

    # 4. Ograniczamy długość (kontekst per chunk)
    if len(text) > max_len:
        text = text[:max_len] + " [TRUNCATED]"

    return text


def _mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Redaguje typowe PII przed wysyłką fragmentów do chmury LLM."""
    if not text:
        return "", {}
    out = str(text)
    stats: dict[str, int] = {}

    def _sub(pattern: str, repl: str, key: str) -> None:
        nonlocal out
        out, count = re.subn(pattern, repl, out)
        if count:
            stats[key] = stats.get(key, 0) + count

    _sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", "email")
    _sub(r"\bPL\s?\d{2}(?:\s?\d{4}){5,7}\b", "[IBAN]", "iban")
    _sub(r"\bPL\d{26}\b", "[IBAN]", "iban")
    _sub(r"(?<!\d)(?:\+48|0048|48)[\s-]?(?:\d{3}[\s-]?){2}\d{3}(?!\d)", "[TEL]", "phone")
    _sub(r"\b\d{11}\b", "[PESEL]", "pesel")
    _sub(r"\b\d{3}[-\s]?\d{2,3}[-\s]?\d{2}[-\s]?\d{2,3}\b", "[NIP]", "nip")
    _sub(r"\b\d{26}\b", "[RACHUNEK]", "account")
    return out, stats


def _swarm_prepare_text(text: str, *, max_len: int = 1200, mask_pii: bool = False) -> tuple[str, dict[str, int]]:
    cleaned = _sanitize_for_prompt(text or "", max_len)
    if not mask_pii:
        return cleaned, {}
    masked, stats = _mask_pii(cleaned)
    return masked, stats


def _merge_pii_stats(total: dict[str, int], part: dict[str, int]) -> None:
    for key, val in (part or {}).items():
        total[key] = total.get(key, 0) + int(val or 0)


def _check_qdrant_health() -> dict:
    """Sprawdza połączenie z Qdrant i aktywną kolekcję."""
    try:
        client = get_qdrant_client()
        collections = client.get_collections().collections
        collection_names = [c.name for c in collections]

        active_exists = ACTIVE_COLLECTION in collection_names

        points = 0
        if active_exists:
            info = client.get_collection(ACTIVE_COLLECTION)
            points = info.points_count or 0

        return {
            "ok": True,
            "collections_count": len(collection_names),
            "active_collection": ACTIVE_COLLECTION,
            "active_collection_exists": active_exists,
            "points_in_active": points,
            "error": None
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def _ocr_health_status() -> dict:
    """Status OCR (Tesseract) — opcjonalny fallback dla skanów PDF i obrazów."""
    import shutil
    tesseract_bin = shutil.which("tesseract") is not None
    py_ok = pytesseract is not None
    pdf_ok = convert_from_path is not None
    available = py_ok and tesseract_bin
    hints: list[str] = []
    if not py_ok:
        hints.append("./venv/bin/pip install pytesseract pdf2image Pillow")
    if not pdf_ok:
        hints.append("pip install pdf2image (wymagane dla skanów PDF)")
    if not tesseract_bin:
        hints.append("sudo apt install tesseract-ocr tesseract-ocr-pol poppler-utils")
    return {
        "available": available,
        "python_module": py_ok,
        "tesseract_binary": tesseract_bin,
        "pdf2image": pdf_ok,
        "install_hint": " · ".join(hints) if hints else None,
        "lang": OCR_LANG,
        "note": "Uruchamiane automatycznie gdy PDF ma <20 znaków tekstu lub dla JPG/PNG/TIFF",
    }


def _file_parsers_health() -> dict:
    """Dostępność parserów plików (Excel/PDF/DOCX) — bez OCR."""
    return {
        "pdf_text": pdfplumber is not None,
        "excel": openpyxl is not None,
        "docx": docx is not None,
        "ocr_for_scans": _ocr_health_status()["available"],
        "note": "Excel: wszystkie arkusze + forensyka; PDF: tekst cyfrowy, potem OCR jeśli skan",
    }


@app.route('/qdrant/mode', methods=['GET'])
def qdrant_mode():
    is_cloud = bool(QDRANT_CLOUD_URL) and QDRANT_URL == QDRANT_CLOUD_URL
    return jsonify({
        "success": True,
        "mode": "cloud" if is_cloud else "local",
        "url": QDRANT_URL,
        "cloud_available": bool(QDRANT_CLOUD_URL),
    })


@app.route('/qdrant/switch', methods=['POST'])
def qdrant_switch():
    global QDRANT_URL, QDRANT_KEY, _qdrant_client
    data = request.get_json(force=True) or {}
    mode = data.get('mode')
    if mode == 'local':
        new_url, new_key = QDRANT_LOCAL_URL, QDRANT_LOCAL_KEY
    elif mode == 'cloud':
        if not QDRANT_CLOUD_URL:
            return jsonify({"success": False, "error": "Brak konfiguracji Qdrant Cloud (QDRANT_CLOUD_URL)"}), 400
        new_url, new_key = QDRANT_CLOUD_URL, QDRANT_CLOUD_KEY
    else:
        return jsonify({"success": False, "error": "Nieprawidłowy tryb. Użyj 'local' lub 'cloud'"}), 400
    with _qdrant_lock:
        QDRANT_URL = new_url
        QDRANT_KEY = new_key
        _qdrant_client = None
    logger.info(f"Przełączono Qdrant → {mode} ({new_url})")
    return jsonify({"success": True, "mode": mode, "url": new_url})


def _is_rate_limit_error(exc: Exception) -> bool:
    """Sprawdza czy wyjątek to 429 Too Many Requests z OpenRouter."""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg or "rate limit" in msg:
        return True
    # requests HTTPError
    if hasattr(exc, "response") and getattr(exc, "response", None) is not None:
        try:
            status = exc.response.status_code
            if status == 429:
                return True
        except Exception:
            pass
    return False


def _is_transient_llm_error(exc: Exception) -> bool:
    """Sprawdza chwilowe błędy transportowe, które warto ponowić."""
    msg = str(exc).lower()
    transient_markers = (
        "connection reset by peer",
        "connection aborted",
        "remote disconnected",
        "incomplete read",
        "broken pipe",
        "timed out",
        "timeout",
        "104",
        "connection reset",
    )
    return any(marker in msg for marker in transient_markers)


def _is_auth_error(exc: Exception) -> bool:
    if hasattr(exc, "response") and getattr(exc, "response", None) is not None:
        try:
            return exc.response.status_code in (401, 403)
        except Exception:
            pass
    msg = str(exc).lower()
    return "401" in msg or "403" in msg or "unauthorized" in msg or "invalid api key" in msg


def _failure_cooldown(exc: Exception) -> float:
    if _is_rate_limit_error(exc):
        return min(_get_retry_after(exc) or 60.0, 120.0)
    if _is_auth_error(exc):
        return 300.0
    return 30.0


def _get_retry_after(exc: Exception) -> float | None:
    """Próbuje wyciągnąć Retry-After z nagłówków odpowiedzi."""
    try:
        if hasattr(exc, "response") and exc.response is not None:
            ra = exc.response.headers.get("Retry-After")
            if ra:
                return float(ra)
    except Exception:
        pass
    return None


def call_llm(prompt: str, system: str = "", stream: bool = False,
             provider: str | None = None, model: str | None = None,
             max_tokens: int = 2000, temperature: float = 0.2,
             task: str | None = None) -> dict | requests.Response:
    """
    Uniwersalna funkcja do wywoływania LLM.
    Zwraca dict dla non-stream lub Response dla stream.
    """
    prov = get_llm_provider(provider, task=task)
    t0 = time.monotonic()

    try:
        if prov in ("openrouter", "groq"):
            cfg = _cloud_llm_settings(prov)
            if not cfg["api_key"]:
                raise RuntimeError(f"Brak {cfg['key_env']} w .env")
            result = _call_cloud_chat(
                cfg, prompt, system, stream, model or cfg["default_model"], max_tokens, temperature
            )
        else:
            result = _call_ollama(prompt, system, stream, model or LLM_MODEL)
        if not stream:
            _fleet_record_outcome(prov, True, (time.monotonic() - t0) * 1000)
        return result
    except Exception as e:
        kind = None
        err_s = str(e).lower()
        if _is_rate_limit_error(e):
            kind = "rate_limit"
        elif _is_auth_error(e):
            kind = "auth"
        elif "blocked at the organization" in err_s:
            kind = "org_blocked"
        _fleet_record_outcome(prov, False, (time.monotonic() - t0) * 1000, error_kind=kind, error_msg=str(e))
        raise


def stream_llm_tokens(prompt: str, system: str = "",
                      provider: str | None = None, model: str | None = None,
                      max_tokens: int = 2000, temperature: float = 0.2,
                      task: str | None = None):
    """
    Generator zwracający kolejne tokeny tekstu z LLM (działa dla Ollama i OpenRouter).
    Używany w streamingowych endpointach.
    """
    prov = get_llm_provider(provider, task=task)
    cloud_cfg = _cloud_llm_settings(prov) if prov in ("openrouter", "groq") else None
    effective_model = model or (cloud_cfg["default_model"] if cloud_cfg else LLM_MODEL)

    if prov in ("openrouter", "groq"):
        if not cloud_cfg["api_key"]:
            yield f"Błąd: Brak klucza {cloud_cfg['key_env']}"
            return

        url = cloud_cfg["url"]
        cloud_label = cloud_cfg["label"]
        pool = cloud_cfg.get("pool")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": effective_model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature
        }

        pool_size = max(1, pool.count()) if pool else 1
        max_attempts = OPENROUTER_MAX_RETRIES * pool_size
        current_key = cloud_cfg["api_key"]
        keys_tried: set[str] = set()
        last_error = None

        for attempt in range(max_attempts):
            if not current_key:
                break
            headers = _cloud_headers(cloud_cfg, current_key)
            try:
                with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    if pool:
                        pool.mark_success(current_key)
                    for line in r.iter_lines():
                        if not line:
                            continue
                        line = line.decode("utf-8", errors="replace")
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                return
                            try:
                                chunk = json.loads(data_str)
                                choice = chunk.get("choices", [{}])[0]
                                delta = choice.get("delta", {})
                                token = delta.get("content", "")
                                if token:
                                    yield token
                                if choice.get("finish_reason"):
                                    return
                            except Exception:
                                continue
                return
            except Exception as e:
                last_error = e
                if _should_rotate_pool(e) and pool and pool.count() > 1:
                    pool.mark_failure(current_key, cooldown_sec=_failure_cooldown(e))
                    keys_tried.add(current_key)
                    next_key = _next_cloud_key(cloud_cfg, exclude=keys_tried)
                    if next_key:
                        logger.warning(
                            "%s stream: rotacja klucza (%d/%d) — %s",
                            cloud_label, attempt + 1, max_attempts, type(e).__name__,
                        )
                        current_key = next_key
                        continue
                if _is_rate_limit_error(e):
                    if attempt < max_attempts - 1:
                        wait = _get_retry_after(e) or (1.5 ** (attempt % OPENROUTER_MAX_RETRIES))
                        wait = min(wait, 12.0)
                        logger.warning(f"{cloud_label} 429 (próba {attempt+1}/{max_attempts}) — czekam {wait:.1f}s")
                        time.sleep(wait)
                    continue
                break

        # Po wyczerpaniu prób
        err_msg = str(last_error) if last_error else "nieznany błąd"

        # Fallback po błędzie 400, podobnie jak w non-stream
        if (
            prov in ("openrouter", "groq")
            and OPENROUTER_FALLBACK_TO_OLLAMA
            and getattr(last_error, "response", None) is not None
        ):
            try:
                status = last_error.response.status_code
            except Exception:
                status = None
            if status == 400:
                yield f"[FALLBACK] {cloud_label} zwrócił 400 — przełączam na lokalny Ollama...\n\n"
                logger.warning("%s stream 400 → fallback do Ollama", cloud_label)
                for base in _ollama_bases():
                    ollama_url = base + "/api/generate"
                    ollama_payload = {
                        "model": LLM_MODEL,
                        "prompt": prompt,
                        "system": system,
                        "stream": True,
                        "options": {"temperature": temperature if temperature is not None else 0.2}
                    }
                    try:
                        with requests.post(ollama_url, json=ollama_payload, stream=True, timeout=300) as r:
                            r.raise_for_status()
                            OLLAMA_URL_POOL.mark_success(base)
                            for line in r.iter_lines():
                                if not line:
                                    continue
                                try:
                                    data = json.loads(line)
                                    tok = data.get("response", "")
                                    if tok:
                                        yield tok
                                    if data.get("done", False):
                                        return
                                except Exception:
                                    continue
                        return
                    except Exception as fb_err:
                        OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)
                        last_error = fb_err
                        continue
                yield f"[Błąd fallback na Ollama: {last_error}]"
                return

        # Opcjonalny automatyczny fallback na Ollama przy rate limitach chmury
        if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
            yield f"[FALLBACK] Limit {cloud_label} — przełączam na lokalny Ollama...\n\n"
            logger.info(f"{cloud_label} rate limit → fallback do Ollama (OPENROUTER_FALLBACK_TO_OLLAMA=true)")
            for base in _ollama_bases():
                ollama_url = base + "/api/generate"
                ollama_payload = {
                    "model": LLM_MODEL,
                    "prompt": prompt,
                    "system": system,
                    "stream": True,
                    "options": {"temperature": temperature if temperature is not None else 0.2}
                }
                try:
                    with requests.post(ollama_url, json=ollama_payload, stream=True, timeout=300) as r:
                        r.raise_for_status()
                        OLLAMA_URL_POOL.mark_success(base)
                        for line in r.iter_lines():
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                tok = data.get("response", "")
                                if tok:
                                    yield tok
                                if data.get("done", False):
                                    return
                            except Exception:
                                continue
                    return
                except Exception as fb_err:
                    OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)
                    last_error = fb_err
                    continue
            yield f"[Błąd fallback na Ollama: {last_error}]"
            return

        if _is_rate_limit_error(last_error):
            friendly = f"[RATE_LIMIT] Przekroczono limit zapytań {cloud_label}. Poczekaj chwilę lub przełącz na Ollama w ustawieniach."
            yield friendly
        else:
            detail = _format_cloud_api_error(last_error, cloud_label) if last_error else err_msg
            yield f"[Błąd {cloud_label} streaming: {detail}]"

    else:
        # Ollama streaming — rotacja serwerów z puli
        payload = {
            "model": effective_model,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {"temperature": temperature}
        }
        last_error = None
        for base in _ollama_bases():
            url = base + "/api/generate"
            try:
                with requests.post(url, json=payload, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    OLLAMA_URL_POOL.mark_success(base)
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            token = data.get("response", "")
                            if token:
                                yield token
                            if data.get("done", False):
                                return
                        except Exception:
                            continue
                    return
            except Exception as e:
                last_error = e
                OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)
                logger.warning("Ollama stream (%s): %s — próba następnego serwera", base, e)
        yield f"[Błąd Ollama streaming: {last_error}]"


def _call_ollama(prompt: str, system: str, stream: bool, model: str) -> dict | requests.Response:
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": stream,
        "options": {"temperature": 0.2}
    }
    last_error = None
    for base in _ollama_bases():
        url = base + "/api/generate"
        if stream:
            try:
                r = requests.post(url, json=payload, stream=True, timeout=300)
                r.raise_for_status()
                OLLAMA_URL_POOL.mark_success(base)
                return r
            except Exception as e:
                last_error = e
                OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)
                logger.warning("Ollama stream call (%s): %s — następny serwer", base, e)
                continue
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=180)
                r.raise_for_status()
                OLLAMA_URL_POOL.mark_success(base)
                return r.json()
            except Exception as e:
                last_error = e
                if _is_transient_llm_error(e) and attempt < 2:
                    wait = min(6.0, 0.5 * (2 ** attempt))
                    logger.warning(f"Ollama LLM transient error (próba {attempt+1}/3) — czekam {wait:.1f}s: {e}")
                    time.sleep(wait)
                    continue
                break
        OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)

    raise last_error if last_error else RuntimeError("Ollama call failed")


def _call_cloud_chat(cloud_cfg: dict, prompt: str, system: str, stream: bool, model: str,
                     max_tokens: int, temperature: float) -> dict | requests.Response:
    url = cloud_cfg["url"]
    cloud_label = cloud_cfg["label"]
    pool = cloud_cfg.get("pool")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": temperature
    }

    pool_size = max(1, pool.count()) if pool else 1
    max_attempts = OPENROUTER_MAX_RETRIES * pool_size
    current_key = cloud_cfg["api_key"]
    keys_tried: set[str] = set()

    if stream:
        headers = _cloud_headers(cloud_cfg, current_key)
        return requests.post(url, headers=headers, json=payload, stream=True, timeout=300)

    last_error = None
    for attempt in range(max_attempts):
        if not current_key:
            break
        headers = _cloud_headers(cloud_cfg, current_key)
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=180)
            r.raise_for_status()
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if pool:
                pool.mark_success(current_key)
            return {"response": content, "raw": data}
        except Exception as e:
            last_error = e
            if _should_rotate_pool(e) and pool and pool.count() > 1:
                pool.mark_failure(current_key, cooldown_sec=_failure_cooldown(e))
                keys_tried.add(current_key)
                next_key = _next_cloud_key(cloud_cfg, exclude=keys_tried)
                if next_key:
                    logger.warning(
                        "%s: rotacja klucza (%d/%d) — %s",
                        cloud_label, attempt + 1, max_attempts, type(e).__name__,
                    )
                    current_key = next_key
                    continue
            if _is_rate_limit_error(e):
                if attempt < max_attempts - 1:
                    wait = _get_retry_after(e) or (1.5 ** (attempt % OPENROUTER_MAX_RETRIES))
                    wait = min(wait, 12.0)
                    logger.warning(f"{cloud_label} 429 (non-stream, próba {attempt+1}/{max_attempts}) — czekam {wait:.1f}s")
                    time.sleep(wait)
                continue
            if _is_transient_llm_error(e):
                if attempt < max_attempts - 1:
                    wait = min(6.0, 0.5 * (2 ** (attempt % OPENROUTER_MAX_RETRIES)))
                    logger.warning(f"{cloud_label} transient error (non-stream, próba {attempt+1}/{max_attempts}) — czekam {wait:.1f}s: {e}")
                    time.sleep(wait)
                    continue
                break
            else:
                # 400 (zły model/ payload) — opcjonalny fallback na Ollama
                if OPENROUTER_FALLBACK_TO_OLLAMA and getattr(e, "response", None) is not None:
                    try:
                        status = e.response.status_code
                    except Exception:
                        status = None
                    if status == 400:
                        logger.warning(f"{cloud_label} 400 → fallback do Ollama")
                        result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
                        if isinstance(result, dict):
                            return result
                        return {"response": str(result)}
                break

    # Final failure — optional fallback na Ollama (np. generowanie SQL / synteza)
    if _is_rate_limit_error(last_error) and OPENROUTER_FALLBACK_TO_OLLAMA:
        logger.info(f"{cloud_label} rate limit (non-stream) → fallback do Ollama")
        result = _call_ollama(prompt, system, stream=False, model=LLM_MODEL)
        if isinstance(result, dict):
            return result
        return {"response": str(result)}

    if last_error:
        raise RuntimeError(_format_cloud_api_error(last_error, cloud_label)) from last_error
    raise RuntimeError(f"{cloud_label} call failed")


# ---- Qdrant Client (reuse connection) ----
_qdrant_client = None
_qdrant_lock = threading.Lock()


def _is_local_qdrant_url(url: str | None = None) -> bool:
    """Lokalny Qdrant (HTTP dev) — bez api_key i bez check_compatibility (mniej warningów w logach)."""
    raw = (url or QDRANT_URL or "").strip().rstrip("/")
    if not raw:
        return False
    if raw == QDRANT_LOCAL_URL.strip().rstrip("/"):
        return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw)
        if parsed.scheme != "http":
            return False
        return (parsed.hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    with _qdrant_lock:
        if _qdrant_client is None:
            from httpx import Limits
            is_local = _is_local_qdrant_url()
            _qdrant_client = QdrantClient(
                url=QDRANT_URL,
                api_key=None if is_local else QDRANT_KEY,
                timeout=30.0,
                check_compatibility=not is_local,
                limits=Limits(
                    max_keepalive_connections=5,
                    max_connections=20,
                    keepalive_expiry=30
                )
            )
        return _qdrant_client

# ---- Cache (dokumenty i sugestie) ----
_docs_cache = {"data": None, "ts": 0}
DOCS_CACHE_TTL = 300  # 5 minut
_suggestions_cache = {"data": None, "ts": 0}
SUGGESTIONS_TTL = 1800  # 30 minut

# ---- Cache embeddingów (SQLite) ----
_CACHE_DB_PATH = Path(__file__).parent / "embedding_cache.db"

def _init_embed_cache():
    """Inicjalizuje tabelę w bazie SQLite, jeśli nie istnieje."""
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, vector TEXT)")
        # Policz istniejące wpisy
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM embeddings")
            count = cursor.fetchone()[0]
        logger.info(f"Załadowano cache embeddingów (SQLite): {count} wpisów")
    except Exception as e:
        logger.warning(f"Błąd inicjalizacji cache embeddingów: {e}")

_init_embed_cache()

def _ensure_collection_exists():
    """Tworzy ACTIVE_COLLECTION jeśli nie istnieje (np. świeży lokalny Qdrant)."""
    try:
        from qdrant_client.models import VectorParams, Distance
        client = get_qdrant_client()
        if not client.collection_exists(ACTIVE_COLLECTION):
            client.create_collection(
                ACTIVE_COLLECTION,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE)
            )
            logger.info(f"Kolekcja '{ACTIVE_COLLECTION}' utworzona automatycznie")
    except Exception as e:
        logger.warning(f"Nie udało się sprawdzić/utworzyć kolekcji '{ACTIVE_COLLECTION}': {e}")

_ensure_collection_exists()

def get_embedding(text: str) -> list:
    import hashlib as _hl
    key = _hl.sha256(text[:1500].encode('utf-8', errors='replace')).hexdigest()

    # 1. Sprawdzaj, czy wektor jest w bazie
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
            cursor = conn.execute("SELECT vector FROM embeddings WHERE hash = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logger.warning(f"Błąd odczytu cache embeddingów: {e}")

    # 2. Jeśli nie ma w cache, odpytaj Ollamę (rotacja serwerów + retry przy Connection reset)
    payload = {"model": EMBED_MODEL, "prompt": text[:1500]}
    last_error = None

    for base in _ollama_bases():
        url = base + "/api/embeddings"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=45) as r:
                    vec = json.loads(r.read().decode("utf-8"))["embedding"]

                try:
                    with sqlite3.connect(_CACHE_DB_PATH, timeout=10) as conn:
                        conn.execute("INSERT OR REPLACE INTO embeddings (hash, vector) VALUES (?, ?)",
                                     (key, json.dumps(vec)))
                except Exception as e:
                    logger.warning(f"Błąd zapisu cache embeddingów: {e}")

                OLLAMA_URL_POOL.mark_success(base)
                return vec

            except Exception as e:
                last_error = e
                if "Connection reset by peer" in str(e) or "104" in str(e) or _is_transient_llm_error(e):
                    wait = (2 ** attempt) * 0.4
                    logger.warning(f"Ollama embedding ({base}, próba {attempt+1}/4) — czekam {wait:.1f}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"Ollama Embedding Error ({base}): {e}")
                break
        OLLAMA_URL_POOL.mark_failure(base, cooldown_sec=30)

    if last_error and ("Connection reset" in str(last_error) or "104" in str(last_error)):
        logger.error("Ollama Embedding Error: wszystkie próby nieudane (Connection reset)")
        raise EmbeddingError("Embedding niedostępny — Ollama nie odpowiada (connection reset)")
    raise EmbeddingError(f"Błąd embeddingu Ollama: {last_error}") from last_error

def get_embeddings_batch(texts: list, batch_size: int = 6) -> list:
    """Batch embeddings z mniejszą równoległością (domyślnie 6 zamiast 8), żeby mniej obciążać Ollamę."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [None] * len(texts)

    def embed_one(idx_text):
        idx, text = idx_text
        return idx, get_embedding(text)

    with ThreadPoolExecutor(max_workers=batch_size) as ex:
        futures = {ex.submit(embed_one, (i, t)): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            try:
                idx, vec = fut.result()
                results[idx] = vec
            except EmbeddingError:
                raise
            except Exception as e:
                logger.error(f"Batch embedding error: {e}")
                raise EmbeddingError(f"Błąd batch embeddingu: {e}") from e

    return results

SEARCH_MODES = {
    "normal": {
        "label": "Standardowy",
        "system": (
            "Jesteś precyzyjnym asystentem analityczno-śledczym. Odpowiadaj zawsze po polsku, "
            "krótko, konkretnie i wyłącznie na podstawie dostarczonych dokumentów. "
            "Jeśli w dokumentach znajdują się liczby, kwoty, nazwy firm, nazwiska lub paragrafy, podaj je w pierwszej kolejności."
        ),
        "prompt_suffix": "Podaj zwięzłą syntezę dowodów:"
    },
    "detective": {
        "label": "Detektyw — briefing śledczy",
        "min_limit": 12,
        "max_per_file": 2,
        "system": (
            "Jesteś doświadczonym analitykiem śledczym (forensics dokumentów, zamówienia publiczne, finanse). "
            "Piszesz jak kolega z zespołu śledczego: konkretnie, po polsku, z odniesieniami do plików źródłowych. "
            "Porównujesz fakty MIĘDZY dokumentami — szukasz rozbieżności kwot, dat, stron umowy, numerów postępowań, "
            "brakujących załączników, podejrzanych zbieżności czasowych i powtarzających się podmiotów. "
            "Nie wymyślaj faktów: jeśli czegoś nie ma w kontekście, napisz [BRAK DOWODU] i co sprawdzić. "
            "Każde istotne znalezisko oznacz jednym tagiem: [ANOMALIA], [NIESPÓJNOŚĆ], [ROZBIEŻNOŚĆ], "
            "[PODEJRZANE], [WYMAGA SPRAWDZENIA]. "
            "Na końcu zawsze dodaj krótką sekcję z 2–4 pytaniami do dalszej analizy użytkownika."
        ),
        "prompt_suffix": (
            "Przygotuj briefing śledczy w podanym formacie sekcji. "
            "Priorytet: porównania między dokumentami i konkretne cytaty (plik + sens treści)."
        ),
    },
    "legal": {
        "label": "Prawny — przepisy",
        "system": (
            "Jesteś prawnikiem specjalizującym się w prawie zamówień publicznych i spółkach komunalnych. "
            "Identyfikuj każde odwołanie do ustaw, rozporządzeń i przepisów w dokumentach. "
            "Dla każdego przepisu oceń: (1) czy jest aktualny na dzień dokumentu, "
            "(2) czy rzeczywiście dotyczy tej organizacji / branży i kontekstu sprawy, "
            "(3) czy jest zastosowany prawidłowo w kontekście. "
            "Flaguj błędy: [PRZEPIS NIEAKTUALNY], [PRZEPIS NIEADEKWATNY], [BŁĘDNE ZASTOSOWANIE], [PRZEPIS NIEZGODNY]. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Oceń prawidłowość powołanych przepisów prawnych:"
    },
    "inconsistency": {
        "label": "Niespójności",
        "system": (
            "Jesteś audytorem dokumentacji. Szukasz SPRZECZNOŚCI i NIESPÓJNOŚCI w treści dokumentów. "
            "Gdzie ta sama liczba, fakt, data lub stwierdzenie pojawia się inaczej w różnych dokumentach? "
            "Format odpowiedzi: 'Dokument A twierdzi: [X]. Dokument B twierdzi: [Y]. SPRZECZNOŚĆ: [opis].' "
            "Wskazuj też wewnętrzne niespójności w jednym dokumencie. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Znajdź sprzeczności i niespójności między dokumentami:"
    },
    "extract": {
        "label": "Ekstrakcja danych",
        "system": (
            "Jesteś ekstrakatorem danych strukturalnych. Z dokumentów wyciągasz ustrukturyzowane fakty. "
            "Zwróć WYŁĄCZNIE tabelę Markdown z kolumnami: | Typ | Wartość | Dokument | Kontekst |. "
            "Typy: KWOTA, DATA, OSOBA, FIRMA, UMOWA, PARAGRAF, UCHWAŁA, KARA, PRZETARG, INNE. "
            "Każdy znaleziony fakt to osobny wiersz. Minimum 5 wierszy jeśli dane pozwalają. "
            "Odpowiadaj wyłącznie po polsku. Nie pisz nic poza tabelą."
        ),
        "prompt_suffix": "Wyciągnij ustrukturyzowane dane z dokumentów jako tabela Markdown:"
    }
}

def _embedding_query_for_mode(query_text: str, mode: str) -> str:
    """Zapytanie do embeddingu — bez historii rozmowy; detective lekko poszerza semantykę."""
    q = (query_text or "").strip()
    if mode != "detective" or not q:
        return q
    hint = (
        " niespójność anomalia rozbieżność kwota data umowa przetarg "
        "sprzeczność brak dokumentu podejrzany wzorzec"
    )
    return (q + hint)[:2400]


def _effective_search_limit(mode: str, limit: int) -> int:
    limit = min(max(int(limit or 5), 1), 20)
    if mode == "detective":
        floor = int(SEARCH_MODES["detective"].get("min_limit", 12))
        return max(limit, floor)
    return limit


def _diversify_contexts(raw: list, limit: int, max_per_file: int = 2) -> list:
    """Wybiera chunki z różnych plików — lepsze porównania między dokumentami."""
    if not raw or limit <= 0:
        return []
    per_file: dict[str, int] = {}
    out = []
    for item in raw:
        fname = item.get("file") or "Nieznany"
        if per_file.get(fname, 0) >= max_per_file:
            continue
        per_file[fname] = per_file.get(fname, 0) + 1
        out.append(item)
        if len(out) >= limit:
            break
    if len(out) < limit:
        seen = {(c.get("file"), (c.get("text") or "")[:80]) for c in out}
        for item in raw:
            key = (item.get("file"), (item.get("text") or "")[:80])
            if key in seen:
                continue
            out.append(item)
            seen.add(key)
            if len(out) >= limit:
                break
    return out


def _retrieve_search_contexts(
    client,
    query_text: str,
    limit: int,
    file_filter: str | None,
    mode: str,
) -> tuple[list, list]:
    """Wyszukiwanie wektorowe + opcjonalna dywersyfikacja plików (detective)."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    embed_q = _embedding_query_for_mode(query_text, mode)
    vector = get_embedding(embed_q)
    eff_limit = _effective_search_limit(mode, limit)
    fetch_limit = eff_limit
    max_per_file = 3
    if mode == "detective":
        max_per_file = int(SEARCH_MODES["detective"].get("max_per_file", 2))
        if not file_filter:
            fetch_limit = min(eff_limit * 3, 45)

    if file_filter:
        qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
        res = client.query_points(
            collection_name=ACTIVE_COLLECTION,
            query=vector,
            limit=fetch_limit,
            query_filter=qfilter,
        )
    else:
        res = client.query_points(
            collection_name=ACTIVE_COLLECTION,
            query=vector,
            limit=fetch_limit,
        )

    raw_contexts = []
    for point in res.points:
        p = point.payload
        raw_contexts.append({
            "file": p.get("file", ""),
            "text": p.get("text", ""),
            "score": float(point.score),
            "full_path": p.get("full_path", ""),
            "point_id": str(point.id),
        })

    if mode == "detective" and len(raw_contexts) > eff_limit:
        raw_contexts = _diversify_contexts(raw_contexts, eff_limit, max_per_file=max_per_file)

    llm_contexts = [{"file": c["file"], "text": c["text"]} for c in raw_contexts]
    results = [
        {
            "file": c.get("file") or "Nieznany",
            "score": f"{c.get('score', 0):.4f}",
            "point_id": c.get("point_id", ""),
            "snippet": _search_result_snippet(c.get("text", ""), query_text),
            "full_path": c.get("full_path", ""),
            "win_path": wsl_to_win(c.get("full_path", "")),
        }
        for c in raw_contexts
    ]
    return llm_contexts, results


def _build_search_prompt(
    query_text: str,
    contexts: list,
    mode: str,
    chat_context: str = "",
) -> tuple[str, str]:
    """Buduje prompt użytkownika i zwraca (prompt, system) — chat_context tylko do LLM."""
    cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])
    max_chunk = 1600 if mode == "detective" else 1400
    sanitized = [
        {"file": c.get("file", ""), "text": _sanitize_for_prompt(c.get("text", ""), max_chunk)}
        for c in contexts
    ]
    context_str = "\n\n".join(
        [f"[Dokument: {c['file']}]: {c['text']}" for c in sanitized]
    )
    chat_block = ""
    if chat_context and chat_context.strip():
        chat_block = (
            f"\n\nHISTORIA ROZMOWY (kontekst użytkownika — nie traktuj jako faktów, "
            f"tylko jako kierunek analizy):\n{_sanitize_for_prompt(chat_context.strip(), 2500)}\n"
        )

    if mode == "detective":
        prompt = (
            f"FRAGMENTY DOKUMENTÓW (jedyne źródło faktów):\n{context_str}\n"
            f"{chat_block}\n"
            f"PYTANIE / ZLECENIE ANALITYKA:\n{query_text}\n\n"
            f"{cfg['prompt_suffix']}\n\n"
            "FORMAT ODPOWIEDZI (nagłówki dokładnie tak):\n"
            "## Co wiemy z dokumentów\n"
            "(2–4 zdania: najważniejsze fakty z odniesieniem do plików)\n\n"
            "## Analiza śledcza\n"
            "(porównania między dokumentami; przy każdym znalezisku tag + plik + cytat/skrót)\n\n"
            "## Wnioski i ryzyka\n"
            "(co jest najbardziej podejrzane lub wymaga audytu)\n\n"
            "## Pytania do dalszej analizy\n"
            "(2–4 konkretne pytania, które użytkownik może zadać w kolejnym kroku)\n"
        )
    else:
        prompt = (
            f"KONTEKST Z DOKUMENTÓW:\n{context_str}\n"
            f"{chat_block}\n"
            f"ZAPYTANIE: {query_text}\n\n"
            f"{cfg['prompt_suffix']}"
        )
    return prompt, cfg["system"]


def generate_answer(
    query: str,
    contexts: list,
    mode: str = "normal",
    provider: str | None = None,
    model: str | None = None,
    chat_context: str = "",
) -> str:
    """Generuje odpowiedź używając wybranego providera (ollama lub openrouter)."""
    prompt, system = _build_search_prompt(query, contexts, mode, chat_context=chat_context)
    cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])

    try:
        result = call_llm(
            prompt=prompt,
            system=system,
            stream=False,
            provider=provider,
            model=model
        )
        if isinstance(result, dict):
            return result.get("response", str(result))
        return str(result)
    except Exception as e:
        prov = get_llm_provider(provider)
        if _is_rate_limit_error(e) and OPENROUTER_FALLBACK_TO_OLLAMA and prov in ("openrouter", "groq"):
            logger.warning(f"generate_answer: {prov} 429 → fallback do Ollama")
            try:
                result = call_llm(prompt, system, stream=False, provider="ollama", model=LLM_MODEL)
                if isinstance(result, dict):
                    return result.get("response", str(result))
                return str(result)
            except Exception as fb_e:
                return f"[RATE_LIMIT + Błąd fallback] {fb_e}"
        logger.error(f"LLM error in generate_answer ({prov}): {e}")
        if _is_rate_limit_error(e):
            return "[RATE_LIMIT] Przekroczono limit API chmury. Spróbuj później lub użyj Ollama."
        return f"Błąd syntezy LLM ({prov}): {e}"

def verify_answer(answer: str, contexts: list, query: str, provider: str | None = None, model: str | None = None) -> dict:
    """Krytyk — używa wybranego providera (call_llm)."""
    # Ochrona przed Prompt Injection w weryfikacji
    sanitized_contexts = [
        {"file": c.get("file", ""), "text": _sanitize_for_prompt(c.get("text", ""), 1000)}
        for c in contexts
    ]
    context_str = "\n\n".join([f"[{c['file']}]: {c['text']}" for c in sanitized_contexts])
    system = (
        "Jesteś rygorystycznym weryfikatorem faktów śledczych. Twoja rola to KRYTYCZNA OCENA odpowiedzi "
        "innego asystenta. Masz dostęp do oryginalnych dokumentów — to jedyne źródło prawdy. "
        "NIE ufasz odpowiedzi asystenta — sprawdzasz każde twierdzenie. "
        "Odpowiadaj wyłącznie po polsku. Bądź precyzyjny i bezlitosny wobec nieścisłości."
    )
    prompt = (
        f"ORYGINALNE DOKUMENTY (źródło prawdy):\n{context_str}\n\n"
        f"ZAPYTANIE UŻYTKOWNIKA: {query}\n\n"
        f"ODPOWIEDŹ ASYSTENTA DO WERYFIKACJI:\n{answer}\n\n"
        "Zadanie: sprawdź KAŻDE twierdzenie faktyczne w odpowiedzi asystenta.\n"
        "Format obowiązkowy — każde twierdzenie w osobnej linii:\n"
        "✓ [POTWIERDZONE] <twierdzenie> → <cytat z dokumentu>\n"
        "⚠ [CZĘŚCIOWE] <twierdzenie> → <co jest nieprecyzyjne>\n"
        "✗ [BRAK PODSTAW] <twierdzenie> → <czego brak w dokumentach>\n\n"
        "Na końcu jedna linia:\n"
        "WERDYKT: WIARYGODNA | CZĘŚCIOWO WIARYGODNA | ZAWIERA HALUCYNACJE\n"
        "UZASADNIENIE: <jedno zdanie>\n\n"
        "Weryfikacja:"
    )

    try:
        # Używamy dedykowanego modelu do weryfikacji (jeśli ustawiony), żeby nie dobić limitu tego samego modelu co główna odpowiedź
        effective_verify_model = model
        prov_verify = get_llm_provider(provider)
        if not effective_verify_model and prov_verify in ("openrouter", "groq"):
            effective_verify_model = _cloud_llm_settings(prov_verify)["verify_model"]
        result = call_llm(prompt=prompt, system=system, stream=False, provider=provider, model=effective_verify_model)
        raw = result.get("response", str(result)) if isinstance(result, dict) else str(result)

        # Wyciągnij werdykt
        verdict = "NIEOKREŚLONY"
        justification = ""
        for line in raw.splitlines():
            l = line.strip()
            if l.startswith("WERDYKT:"):
                verdict = l.replace("WERDYKT:", "").strip()
            if l.startswith("UZASADNIENIE:"):
                justification = l.replace("UZASADNIENIE:", "").strip()

        confirmed = raw.count("✓")
        partial    = raw.count("⚠")
        hallucin   = raw.count("✗")
        total = confirmed + partial + hallucin
        confidence_pct = round(confirmed / total * 100) if total > 0 else None

        return {
            "success": True,
            "raw": raw,
            "verdict": verdict,
            "justification": justification,
            "confirmed": confirmed,
            "partial": partial,
            "hallucinations": hallucin,
            "confidence_pct": confidence_pct
        }
    except urllib.error.URLError as e:
        logger.error(f"LLM connection error (verify_answer): {e}")
        return {"success": False, "error": "Błąd połączenia z weryfikatorem (Ollama)."}
    except Exception as e:
        logger.error(f"LLM error in verify_answer: {e}")
        return {"success": False, "error": str(e)}

@app.route('/verify', methods=['POST'])
def verify_endpoint():
    data = request.get_json()
    answer   = data.get('answer', '').strip()
    query    = data.get('query', '').strip()
    contexts = data.get('contexts', [])
    llm_provider = data.get('llm_provider')
    openrouter_model = _model_from_request(data, data.get('llm_provider'))
    if not answer or not query or not contexts:
        return jsonify({"success": False, "error": "Brak danych do weryfikacji"})
    # Przekazujemy provider, żeby weryfikacja też szła przez wybrany model (w tym osobny model_verify)
    result = verify_answer(answer, contexts, query, provider=llm_provider, model=openrouter_model)
    return jsonify(result)

def _safe_int_clamp(val, lo: int, hi: int, default: int = 1) -> int:
    try:
        return max(lo, min(hi, int(float(val))))
    except (TypeError, ValueError):
        return default


def highlight_backend(text: str, query: str) -> str:
    if not query: return text
    words = [w.strip() for w in query.split() if len(w.strip()) > 2]
    if not words: return text
    escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    for word in words:
        clean_word = re.sub(r'[.,\/#!$%\^&\*;:{}=\-_`~()]', '', word)
        root = clean_word[:-2] if len(clean_word) > 4 else clean_word
        if not root: continue
        try:
            pattern = re.compile(rf"({root}[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]*)", re.IGNORECASE)
            escaped = pattern.sub(r"<mark>\1</mark>", escaped)
        except re.error:
            continue
    return escaped


def _search_result_snippet(text: str, query: str, max_len: int = 220) -> str:
    """Krótki podgląd do listy wyników — pełny tekst przez /api/get_context."""
    plain = re.sub(r"<[^>]+>", "", text or "")
    plain = plain.replace("\n", " ").strip()
    if len(plain) <= max_len:
        return highlight_backend(plain, query)
    return highlight_backend(plain[:max_len].rsplit(" ", 1)[0] + "…", query)


def _mtime_in_range(path: Path, modified_after: str | None, modified_before: str | None) -> bool:
    if not modified_after and not modified_before:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if modified_after:
        try:
            ts = time.mktime(time.strptime(modified_after[:10], "%Y-%m-%d"))
            if mtime < ts:
                return False
        except ValueError:
            pass
    if modified_before:
        try:
            ts = time.mktime(time.strptime(modified_before[:10], "%Y-%m-%d")) + 86399
            if mtime > ts:
                return False
        except ValueError:
            pass
    return True


def _doc_modified_iso(doc: dict) -> str:
    meta = doc.get("metadata") or {}
    return (meta.get("modified") or "")[:10]


def _filter_documents_list(
    docs: list,
    ext_filter: list[str] | None = None,
    modified_after: str = "",
    modified_before: str = "",
) -> list:
    out = docs
    if ext_filter:
        allowed = {e.lower().lstrip(".") for e in ext_filter}
        out = [d for d in out if d.get("file", "").rsplit(".", 1)[-1].lower() in allowed]
    if modified_after:
        out = [d for d in out if _doc_modified_iso(d) >= modified_after[:10]]
    if modified_before:
        out = [d for d in out if _doc_modified_iso(d) <= modified_before[:10] or not _doc_modified_iso(d)]
    return out

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

def make_chunks(text: str) -> list:
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        # Zakończ na granicy zdania jeśli możliwe (ostatnia kropka/newline w ogonie)
        if end < len(text):
            for sep in ('. ', '.\n', '\n\n', '\n', ' '):
                cut = chunk.rfind(sep, CHUNK_SIZE // 2)
                if cut != -1:
                    chunk = chunk[:cut + len(sep)]
                    break
        if chunk.strip():
            chunks.append(chunk)
        start += max(len(chunk) - CHUNK_OVERLAP, step)
    return chunks

def _extract_xls(file_path: Path) -> str:
    """Parser starego formatu .xls — czyta też ukryte wiersze i kolumny."""
    try:
        import xlrd
        wb = xlrd.open_workbook(str(file_path), formatting_info=True)
        parts = []
        for sheet in wb.sheets():
            if sheet.nrows == 0:
                continue

            # Wykryj ukryte wiersze i kolumny
            hidden_rows = set()
            hidden_cols = set()
            try:
                for ri in range(sheet.nrows):
                    ri_obj = sheet.rowinfo_map.get(ri)
                    if ri_obj and ri_obj.hidden:
                        hidden_rows.add(ri)
            except Exception:
                pass
            try:
                for ci in range(sheet.ncols):
                    ci_obj = sheet.colinfo_map.get(ci)
                    if ci_obj and ci_obj.hidden:
                        hidden_cols.add(ci)
            except Exception:
                pass

            hidden_info = []
            if hidden_rows: hidden_info.append(f"{len(hidden_rows)} ukrytych wierszy")
            if hidden_cols: hidden_info.append(f"{len(hidden_cols)} ukrytych kolumn")

            lines = [f"\n=== Arkusz: {sheet.name}"
                     + (f" [UWAGA: {', '.join(hidden_info)}]" if hidden_info else "") + " ==="]

            # Nagłówki
            headers = []
            header_row = -1
            for ri in range(min(5, sheet.nrows)):
                row = [str(sheet.cell_value(ri, ci)).strip() for ci in range(sheet.ncols)]
                if len([v for v in row if v and v != '0.0']) >= 2:
                    headers = row
                    lines.append("Nagłówki: " + " | ".join(h for h in headers if h))
                    header_row = ri
                    break

            for ri in range(sheet.nrows):
                if ri == header_row:
                    continue
                is_hidden_row = ri in hidden_rows
                cells = []
                for ci in range(sheet.ncols):
                    val = sheet.cell_value(ri, ci)
                    if val == '' or val is None:
                        continue
                    ctype = sheet.cell_type(ri, ci)
                    if ctype == xlrd.XL_CELL_DATE:
                        try:
                            val = xlrd.xldate_as_datetime(val, wb.datemode).strftime('%Y-%m-%d')
                        except Exception:
                            pass
                    elif ctype == xlrd.XL_CELL_NUMBER and float(val) == int(val) and abs(val) < 1e12:
                        val = int(val)
                    hdr = headers[ci] if ci < len(headers) and headers[ci] else f"Kol{ci+1}"
                    hidden_col_mark = " [UKRYTA_KOL]" if ci in hidden_cols else ""
                    cells.append(f"{hdr}{hidden_col_mark}: {val}")

                if cells:
                    prefix = "[UKRYTY_WIERSZ] " if is_hidden_row else ""
                    lines.append(prefix + " | ".join(cells))

            parts.append("\n".join(lines))
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Błąd parsowania .xls {file_path.name}: {e}")
        return ""

def _extract_excel(file_path: Path) -> str:
    # Stary format — użyj xlrd
    if file_path.suffix.lower() == '.xls':
        return _extract_xls(file_path)
    parts = []
    # Dwa odczyty: raz z formułami, raz z wartościami (cache Excel)
    try:
        wb_vals = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    except Exception:
        wb_vals = None
    try:
        wb_form = openpyxl.load_workbook(file_path, data_only=False, read_only=True)
    except Exception:
        wb_form = None

    if not wb_vals and not wb_form:
        return ""

    sheet_names = (wb_vals or wb_form).sheetnames

    for sheet_name in sheet_names:
        ws_v = wb_vals[sheet_name]  if wb_vals and sheet_name in (wb_vals.sheetnames)  else None
        ws_f = wb_form[sheet_name]  if wb_form and sheet_name in (wb_form.sheetnames)  else None
        if not ws_v and not ws_f:
            continue

        rows_v = list(ws_v.iter_rows(values_only=True))        if ws_v else []
        rows_f = list(ws_f.iter_rows(values_only=False))       if ws_f else []
        if not rows_v and not rows_f:
            continue

        # Wykryj ukryte wiersze i kolumny (tylko bez read_only)
        hidden_rows = set()
        hidden_cols = set()
        try:
            ws_meta = openpyxl.load_workbook(file_path, data_only=True)[sheet_name]
            for ri_h, rd in ws_meta.row_dimensions.items():
                if rd.hidden:
                    hidden_rows.add(ri_h - 1)  # 0-based
            for col_letter, cd in ws_meta.column_dimensions.items():
                if cd.hidden:
                    from openpyxl.utils import column_index_from_string
                    hidden_cols.add(column_index_from_string(col_letter) - 1)
        except Exception:
            pass

        hidden_info = []
        if hidden_rows: hidden_info.append(f"{len(hidden_rows)} ukrytych wierszy")
        if hidden_cols: hidden_info.append(f"{len(hidden_cols)} ukrytych kolumn")

        # Nagłówki kolumn — pierwsza niepusta linia z wartościami tekstowymi
        headers = []
        header_row_idx = -1
        source_rows = rows_v or [list(r) for r in rows_f]
        for ri_h, row in enumerate(source_rows[:5]):
            if sum(1 for c in row if c is not None and str(c).strip()) >= 2:
                headers = [str(c).strip() if c is not None else f"Kol{i+1}"
                           for i, c in enumerate(row)]
                header_row_idx = ri_h
                break

        lines = [f"\n=== Arkusz: {sheet_name}"
                 + (f" [UWAGA: {', '.join(hidden_info)}]" if hidden_info else "") + " ==="]
        if headers:
            lines.append("Nagłówki: " + " | ".join(headers))

        for ri, row_v in enumerate(rows_v):
            if ri == header_row_idx:
                continue
            vals = [v for v in row_v if v is not None and str(v).strip()]
            if not vals:
                continue

            is_hidden = ri in hidden_rows
            cells = []
            for ci, val in enumerate(row_v):
                if val is None:
                    continue
                hdr = headers[ci] if ci < len(headers) else f"Kol{ci+1}"
                hidden_col_mark = " [UKRYTA_KOL]" if ci in hidden_cols else ""

                formula = None
                if rows_f and ri < len(rows_f):
                    rf_row = rows_f[ri]
                    if ci < len(rf_row):
                        fval = rf_row[ci].value if hasattr(rf_row[ci], 'value') else None
                        if isinstance(fval, str) and fval.startswith('='):
                            formula = fval

                if formula:
                    cells.append(f"{hdr}{hidden_col_mark}: {val} [wzór: {formula}]")
                else:
                    cells.append(f"{hdr}{hidden_col_mark}: {val}")

            if cells:
                prefix = "[UKRYTY_WIERSZ] " if is_hidden else ""
                lines.append(prefix + " | ".join(cells))

        # Jeśli data_only zwrócił same None (plik niezapisany przez Excel),
        # fallback: odczytaj formuły bezpośrednio
        data_lines = len([l for l in lines if l.startswith("Kol") or ": " in l])
        if data_lines == 0 and rows_f:
            for rf_row in rows_f:
                cells = []
                for ci, cell in enumerate(rf_row):
                    v = cell.value if hasattr(cell, 'value') else None
                    if v is None:
                        continue
                    hdr = headers[ci] if ci < len(headers) else f"Kol{ci+1}"
                    cells.append(f"{hdr}: {v}")
                if cells:
                    lines.append(" | ".join(cells))

        parts.append("\n".join(lines))

    if wb_vals:
        try: wb_vals.close()
        except Exception: pass
    if wb_form:
        try: wb_form.close()
        except Exception: pass

    return "\n\n".join(parts)


def extract_file_metadata(f_path: Path) -> dict:
    """Zbiera bogate metadane pliku (systemowe + formatowe) do celów śledczych."""
    meta = {
        "file_name": f_path.name,
        "full_path": str(f_path),
        "size_bytes": None,
        "size_human": None,
        "created": None,
        "modified": None,
        "accessed": None,
        "file_type": f_path.suffix.lower().lstrip('.'),
        "embedded": {}
    }

    try:
        stat = f_path.stat()
        meta["size_bytes"] = stat.st_size
        meta["size_human"] = f"{stat.st_size / 1024:.1f} KB" if stat.st_size < 10*1024*1024 else f"{stat.st_size / (1024*1024):.2f} MB"

        # Czas modyfikacji jest wiarygodny wszędzie
        meta["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))

        # Czas dostępu
        if hasattr(stat, "st_atime"):
            meta["accessed"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_atime))

        # Czas utworzenia — Windows ma st_ctime jako creation time, Unix ma jako change time
        if os.name == 'nt' and hasattr(stat, "st_ctime"):
            meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime))
        else:
            # Na Linux/WSL często nie mamy wiarygodnego created time z filesystemu
            meta["created"] = None

    except Exception as e:
        logger.warning(f"Błąd pobierania stat dla {f_path.name}: {e}")

    # === Metadane wbudowane w pliki ===
    ext = f_path.suffix.lower()

    # PDF
    if ext == '.pdf' and pdfplumber:
        try:
            with pdfplumber.open(f_path) as pdf:
                if pdf.metadata:
                    meta["embedded"]["pdf"] = {k: str(v) for k, v in pdf.metadata.items() if v}
        except Exception:
            pass

    # DOCX
    if ext == '.docx' and docx:
        try:
            d = docx.Document(f_path)
            core_props = d.core_properties
            embedded = {}
            if core_props.author: embedded["author"] = core_props.author
            if core_props.title: embedded["title"] = core_props.title
            if core_props.last_modified_by: embedded["last_modified_by"] = core_props.last_modified_by
            if core_props.created: embedded["created"] = str(core_props.created)
            if core_props.modified: embedded["modified"] = str(core_props.modified)
            if embedded:
                meta["embedded"]["docx"] = embedded
        except Exception:
            pass

    # XLSX / XLS
    if ext in ['.xlsx', '.xls'] and openpyxl:
        try:
            wb = openpyxl.load_workbook(f_path, data_only=True)
            props = wb.properties
            embedded = {}
            if props.creator: embedded["author"] = props.creator
            if props.title: embedded["title"] = props.title
            if props.lastModifiedBy: embedded["last_modified_by"] = props.lastModifiedBy
            if props.created: embedded["created"] = str(props.created)
            if props.modified: embedded["modified"] = str(props.modified)
            if embedded:
                meta["embedded"]["excel"] = embedded
            wb.close()
        except Exception:
            pass

    return meta


def extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    try:
        if ext in ['.md', '.txt']:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        elif ext == '.json':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return json.dumps(json.load(f), indent=2, ensure_ascii=False)
        elif ext == '.docx' and docx:
            return "\n".join([p.text for p in docx.Document(file_path).paragraphs])
        
        elif ext == '.pdf':
            text = ""
            # Próba 1: Zwykłe wyciągnięcie tekstu cyfrowego
            if pdfplumber:
                with pdfplumber.open(file_path) as pdf:
                    text = "\n".join([page.extract_text() or "" for page in pdf.pages])
            
            # Próba 2 (Fallback OCR): Jeżeli plik to skan (brak tekstu), uruchom Tesseract
            if len(text.strip()) < 20 and pytesseract and convert_from_path:
                logger.info(f"[OCR] Analiza wizualna pliku: {file_path.name}...")
                try:
                    pages = convert_from_path(file_path, dpi=200)
                    ocr_text = []
                    for page_img in pages:
                        page_text = pytesseract.image_to_string(page_img, lang=OCR_LANG)
                        ocr_text.append(page_text)
                    text = "\n\n".join(ocr_text)
                    logger.info(f"[OCR] Zakończono dla: {file_path.name} (Pobrano znaków: {len(text)})")
                except Exception as ocr_err:
                    logger.warning(f"Błąd OCR dla pliku {file_path.name}: {ocr_err}")
            
            return text

        # Obsługa obrazów bezpośrednio (jpg, png, tiff itp.) przez OCR
        elif ext in ['.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp'] and pytesseract:
            if convert_from_path:
                # pdf2image może obsłużyć niektóre obrazy, ale dla pewności używamy PIL jeśli dostępna
                try:
                    from PIL import Image
                    img = Image.open(file_path)
                    text = pytesseract.image_to_string(img, lang=OCR_LANG)
                    logger.info(f"[OCR] Przetworzono obraz: {file_path.name}")
                    return text
                except ImportError:
                    pass  # fallback poniżej
            # Fallback - spróbuj przez pdf2image jeśli PIL nie ma
            if convert_from_path:
                try:
                    pages = convert_from_path(file_path, dpi=200)
                    ocr_text = []
                    for page_img in pages:
                        page_text = pytesseract.image_to_string(page_img, lang=OCR_LANG)
                        ocr_text.append(page_text)
                    text = "\n\n".join(ocr_text)
                    logger.info(f"[OCR] Przetworzono obraz przez pdf2image: {file_path.name}")
                    return text
                except Exception as ocr_err:
                    logger.warning(f"Błąd OCR obrazu {file_path.name}: {ocr_err}")
            return ""

        elif ext in ['.xlsx', '.xls'] and openpyxl:
            return _extract_excel(file_path)
        elif ext == '.csv':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
    except Exception as e:
        logger.warning(f"Błąd parsowania pliku {file_path.name}: {e}")
    return ""

@app.route('/')
def index():
    return render_template("index.html", api_key_required=bool(APP_API_KEY))

@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        client = get_qdrant_client()
        info = client.get_collection(collection_name=ACTIVE_COLLECTION)
        return jsonify({
            "success": True,
            "vectors_count": getattr(info, 'vectors_count', None) or info.points_count,
            "points_count": info.points_count,
            "status": info.status,
            "active_collection": ACTIVE_COLLECTION
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/stats/storage', methods=['GET'])
def stats_storage():
    global ACTIVE_COLLECTION
    try:
        client = get_qdrant_client()
        cols = client.get_collections().collections
        result = []
        total_vectors = 0
        total_points  = 0

        for c in cols:
            info = client.get_collection(c.name)
            pts   = info.points_count or 0
            vsize = info.config.params.vectors.size if info.config.params.vectors else 768
            # Szacunek: wektory (float32) + payload (avg 900 B/chunk) + indeks HNSW (~20%)
            vec_bytes     = pts * vsize * 4
            payload_bytes = pts * 900
            index_bytes   = int(vec_bytes * 0.20)
            total_bytes   = vec_bytes + payload_bytes + index_bytes

            result.append({
                "name":    c.name,
                "points":  pts,
                "indexed": info.indexed_vectors_count or 0,
                "status":  str(info.status),
                "vector_size": vsize,
                "distance": str(info.config.params.vectors.distance) if info.config.params.vectors else "Cosine",
                "est_mb":  round(total_bytes / 1_048_576, 2),
                "active":  c.name == ACTIVE_COLLECTION
            })
            total_vectors += pts
            total_points  += pts

        # Free tier Qdrant Cloud: ~4 GB disk / 1 GB RAM
        FREE_DISK_MB = 4096
        used_mb = sum(r["est_mb"] for r in result)
        return jsonify({
            "success": True,
            "collections": result,
            "total_points": total_points,
            "used_mb": round(used_mb, 2),
            "free_disk_mb": FREE_DISK_MB,
            "used_pct": round(used_mb / FREE_DISK_MB * 100, 1),
            "active_collection": ACTIVE_COLLECTION,
            "max_collections": 1000
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/create', methods=['POST'])
def create_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name     = data.get('name', '').strip().replace(' ', '_')
    vec_size = int(data.get('vector_size', 768))
    distance = data.get('distance', 'Cosine').capitalize()
    switch   = data.get('switch_to', False)

    if not name:
        return jsonify({"success": False, "error": "Brak nazwy kolekcji"})
    if not name.replace('_','').replace('-','').isalnum():
        return jsonify({"success": False, "error": "Nazwa może zawierać tylko litery, cyfry, _ i -"})

    try:
        from qdrant_client.models import VectorParams, Distance
        dist_map = {"Cosine": Distance.COSINE, "Euclid": Distance.EUCLID, "Dot": Distance.DOT}
        dist = dist_map.get(distance, Distance.COSINE)

        client = get_qdrant_client()
        if client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' już istnieje"})

        client.create_collection(name, vectors_config=VectorParams(size=vec_size, distance=dist))

        if switch:
            ACTIVE_COLLECTION = name
            _persist_active_collection(name)
            _qdrant_client = None  # Reset połączenia po zmianie kolekcji
            _suggestions_cache["data"] = None; _docs_cache["data"] = None

        return jsonify({"success": True, "name": name, "switched": switch, "active": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/switch', methods=['POST'])
def switch_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    try:
        client = get_qdrant_client()
        if not client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' nie istnieje"})
        ACTIVE_COLLECTION = name
        _persist_active_collection(name)
        _qdrant_client = None  # Reset połączenia po zmianie kolekcji
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "active_collection": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/delete', methods=['POST'])
def delete_collection():
    global ACTIVE_COLLECTION, _qdrant_client
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    if name == ACTIVE_COLLECTION:
        return jsonify({"success": False, "error": "Nie można usunąć aktywnej kolekcji. Najpierw przełącz się na inną."})
    try:
        client = get_qdrant_client()
        client.delete_collection(name)
        _qdrant_client = None  # Reset połączenia po usunięciu kolekcji
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/browse', methods=['GET'])
def browse():
    raw = request.args.get('path', '').strip()
    show_all = request.args.get('all', '0') == '1'
    ext_filter = [e.lower().lstrip('.') for e in request.args.getlist('ext') if e.strip()]
    modified_after = request.args.get('modified_after', '').strip()
    modified_before = request.args.get('modified_before', '').strip()
    default_doc_exts = ['docx', 'pdf', 'xlsx', 'xls', 'csv', 'md', 'json', 'txt']
    allowed_exts = ext_filter if ext_filter else default_doc_exts

    if raw:
        p = Path(_normalize_browse_path(raw)).expanduser()
    else:
        p = _default_browse_path()

    if not p.exists() or not p.is_dir():
        parent = p.parent
        if parent.exists() and parent.is_dir() and _path_is_browsable(parent):
            p = parent
        else:
            p = _default_browse_path()

    try:
        p = p.resolve()
    except OSError:
        p = _default_browse_path()

    if not _path_is_browsable(p):
        roots_hint = ", ".join(str(r) for r in _resolve_allowed_roots()[:5])
        return jsonify({
            "success": False,
            "error": f"Ścieżka poza dozwolonymi katalogami (SEARCH_ROOTS). Dozwolone: {roots_hint}",
            "allowed_roots": [str(r) for r in _resolve_allowed_roots()],
        })

    try:
        entries = []
        total_children = 0
        permission_denied = 0

        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            total_children += 1
            try:
                if child.is_dir():
                    if not _path_is_browsable(child):
                        continue
                    entries.append({
                        "name": child.name,
                        "path": str(child),
                        "type": "dir",
                        "importable": _path_is_allowed(child),
                    })
                else:
                    ext = child.suffix.lower().lstrip('.')
                    is_doc = ext in allowed_exts
                    if show_all or is_doc:
                        if not _mtime_in_range(child, modified_after or None, modified_before or None):
                            continue
                        try:
                            st = child.stat()
                            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                            size_kb = round(st.st_size / 1024, 1)
                        except OSError:
                            mtime, size_kb = "", 0
                        entries.append({
                            "name": child.name,
                            "path": str(child),
                            "type": "file",
                            "ext": ext,
                            "modified": mtime,
                            "size_kb": size_kb,
                        })
            except PermissionError:
                permission_denied += 1

        is_empty = (total_children == 0) or (len(entries) == 0 and permission_denied == 0)
        has_hidden_or_other = (total_children > 0) and (len(entries) == 0) and (permission_denied == 0)

        return jsonify({
            "success": True,
            "current": str(p),
            "win_path": wsl_to_win(str(p)),
            "parent": str(p.parent) if p != p.parent and _path_is_browsable(p.parent) else None,
            "entries": entries,
            "is_empty": is_empty,
            "total_children": total_children,
            "permission_denied": permission_denied,
            "has_unlisted_items": has_hidden_or_other or permission_denied > 0,
            "show_all": show_all,
            "allowed_roots": [str(r) for r in _resolve_allowed_roots()],
            "filters": {
                "ext": allowed_exts if not show_all else [],
                "modified_after": modified_after or None,
                "modified_before": modified_before or None,
            },
        })
    except PermissionError:
        return jsonify({"success": False, "error": f"Brak uprawnień do odczytu: {p}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/browse/pick', methods=['POST'])
def browse_pick_folder():
    """Natywny dialog Windows — zwraca wybrany folder jako ścieżkę WSL."""
    data = request.get_json() or {}
    initial = (data.get("initial") or "").strip()
    try:
        if platform.system() == "Windows":
            return jsonify({
                "success": False,
                "error": "Dialog Windows działa z WSL — uruchom aplikację w WSL i otwórz UI w przeglądarce Windows.",
            }), 400
        wsl_path = _pick_windows_folder(initial)
        if not wsl_path:
            return jsonify({"success": False, "error": "Anulowano lub nie wybrano folderu"})
        p = Path(wsl_path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.is_dir():
            return jsonify({"success": False, "error": f"Folder nie istnieje: {wsl_path}"})
        if not _path_is_allowed(p):
            roots_hint = ", ".join(str(r) for r in _resolve_allowed_roots()[:5])
            return jsonify({
                "success": False,
                "error": f"Folder poza SEARCH_ROOTS. Dozwolone: {roots_hint}",
                "allowed_roots": [str(r) for r in _resolve_allowed_roots()],
            })
        return jsonify({
            "success": True,
            "path": str(p),
            "win_path": wsl_to_win(str(p)),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============================================================
# AUTOMATYCZNE MAPOWANIE DYSKÓW LOKALNYCH (dla zakładki Import)
# ============================================================

def _discover_local_drives():
    """Punkty startowe z SEARCH_ROOTS — tylko ścieżki, które da się realnie przeglądać."""
    return _discover_browse_roots()


@app.route('/api/drives')
def api_drives():
    try:
        drives = _discover_local_drives()
        default_path = str(_default_browse_path())
        for drive in drives:
            drive["win_path"] = wsl_to_win(drive.get("path", ""))
        return jsonify({
            "success": True,
            "platform": platform.system(),
            "drives": drives,
            "default_path": default_path,
            "default_win_path": wsl_to_win(default_path),
            "allowed_roots": [str(r) for r in _resolve_allowed_roots()],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ============================================================
# CIĘŻKIE OPERACJE — mutex + /tasks
# ============================================================

_heavy_tasks_lock = threading.Lock()
_active_heavy_task: dict | None = None
_heavy_task_history: list[dict] = []
_HEAVY_TASK_HISTORY_MAX = 8


def _heavy_task_snapshot() -> dict:
    with _heavy_tasks_lock:
        active = dict(_active_heavy_task) if _active_heavy_task else None
        history = [dict(t) for t in _heavy_task_history]
    return {"active": active, "history": history}


def _heavy_task_try_start(kind: str, label: str = "") -> tuple[bool, str | None, str | None]:
    """Zwraca (ok, task_id, komunikat_blokady)."""
    global _active_heavy_task
    task_id = hashlib.md5(f"{kind}:{time.time()}:{label}".encode()).hexdigest()[:12]
    with _heavy_tasks_lock:
        if _active_heavy_task and not _active_heavy_task.get("done"):
            busy = _active_heavy_task
            msg = (
                f"Inna operacja w toku: {busy.get('kind')} "
                f"({busy.get('label') or busy.get('id')}). Poczekaj na zakończenie."
            )
            return False, None, msg
        _active_heavy_task = {
            "id": task_id,
            "kind": kind,
            "label": (label or kind)[:200],
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
            "progress": 0,
            "total": 0,
            "message": "start",
            "done": False,
            "error": None,
        }
    return True, task_id, None


def _heavy_task_update(task_id: str | None, **fields):
    if not task_id:
        return
    with _heavy_tasks_lock:
        if not _active_heavy_task or _active_heavy_task.get("id") != task_id:
            return
        _active_heavy_task.update(fields)
        _active_heavy_task["updated_at"] = int(time.time())


def _heavy_task_finish(task_id: str | None, *, error: str | None = None):
    global _active_heavy_task
    if not task_id:
        return
    with _heavy_tasks_lock:
        if not _active_heavy_task or _active_heavy_task.get("id") != task_id:
            return
        _active_heavy_task["done"] = True
        _active_heavy_task["error"] = error
        _active_heavy_task["updated_at"] = int(time.time())
        finished = dict(_active_heavy_task)
        _heavy_task_history.insert(0, finished)
        del _heavy_task_history[_HEAVY_TASK_HISTORY_MAX:]
        _active_heavy_task = None


def _default_chat_model(provider: str | None = None) -> str:
    prov = get_llm_provider(provider)
    if prov == "groq":
        return GROQ_MODEL
    if prov == "openrouter":
        return OPENROUTER_MODEL
    return LLM_MODEL


@app.route('/tasks', methods=['GET'])
def tasks_status():
    """Status bieżącej ciężkiej operacji (import / wektoryzacja SQL)."""
    snap = _heavy_task_snapshot()
    busy = snap["active"] is not None and not snap["active"].get("done")
    return jsonify({"success": True, "busy": busy, **snap})


@app.route('/import/stream')
def import_stream():
    folder = request.args.get('folder', '').strip()
    if folder:
        folder = _normalize_browse_path(folder)
    exts = request.args.getlist('ext') or ['docx','pdf','xlsx','xls','csv','md','json']

    def generate():
        def sse(event, data):
            import json as _json
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        folder_path = Path(folder).expanduser()
        if not folder or not folder_path.exists():
            yield sse("error", {"msg": f"Ścieżka nie istnieje: {folder}"})
            return
        if not folder_path.is_dir() or not _path_is_allowed(folder_path):
            yield sse("error", {"msg": "Ścieżka niedozwolona lub poza SEARCH_ROOTS"})
            return

        files = _find_files_safe(folder_path.resolve(), exts)

        if not files:
            yield sse("done", {"count": 0, "chunks": 0, "skipped": 0, "msg": "Brak kompatybilnych plików."})
            return

        ok, task_id, block_msg = _heavy_task_try_start("import", str(folder_path))
        if not ok:
            yield sse("error", {"msg": block_msg, "blocked": True})
            return

        task_error = None
        try:
            yield sse("start", {"total": len(files), "task_id": task_id})

            qdrant = get_qdrant_client()
            imported = 0
            skipped = 0
            total_chunks = 0
            new_chunks = 0

            for i, f_path in enumerate(files):
                _heavy_task_update(task_id, progress=i + 1, total=len(files), message=f_path.name)
                try:
                    text = extract_text(f_path)
                    if not text or len(text.strip()) < 10:
                        skipped += 1
                        yield sse("skip", {"file": f_path.name, "reason": "pusty", "i": i+1, "total": len(files)})
                        continue

                    chunks = make_chunks(text)
                    file_new = 0

                    file_meta = extract_file_metadata(f_path)

                    chunk_ids = [hashlib.md5(c.encode('utf-8', errors='replace')).hexdigest() for c in chunks]
                    existing_ids = set()
                    for batch_start in range(0, len(chunk_ids), 100):
                        batch = chunk_ids[batch_start:batch_start+100]
                        found = qdrant.retrieve(collection_name=ACTIVE_COLLECTION, ids=batch,
                                               with_payload=False, with_vectors=False)
                        existing_ids.update(p.id for p in found)

                    new_chunks_data = [(cid, chunk) for cid, chunk in zip(chunk_ids, chunks)
                                       if cid not in existing_ids]
                    total_chunks += len(chunks)

                    BATCH = 6
                    embed_failed = False
                    for b in range(0, len(new_chunks_data), BATCH):
                        batch_items = new_chunks_data[b:b+BATCH]
                        batch_texts  = [item[1] for item in batch_items]
                        batch_ids    = [item[0] for item in batch_items]
                        try:
                            vectors = get_embeddings_batch(batch_texts, batch_size=BATCH)
                        except EmbeddingError as e:
                            skipped += 1
                            embed_failed = True
                            yield sse("skip", {"file": f_path.name, "reason": str(e)[:80], "i": i+1, "total": len(files)})
                            break

                        points  = [
                            PointStruct(
                                id=cid,
                                vector=vec,
                                payload={
                                    "file": f_path.name,
                                    "text": txt,
                                    "full_path": str(f_path),
                                    "metadata": file_meta
                                }
                            )
                            for cid, vec, txt in zip(batch_ids, vectors, batch_texts)
                            if vec and any(v != 0.0 for v in vec)
                        ]
                        if points:
                            qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                        new_chunks += len(points)
                        file_new  += len(points)

                    if embed_failed:
                        continue

                    imported += 1
                    yield sse("file", {
                        "file": f_path.name,
                        "chunks": len(chunks),
                        "new": file_new,
                        "i": i+1,
                        "total": len(files)
                    })
                    time.sleep(0.02)
                except Exception as e:
                    skipped += 1
                    yield sse("skip", {"file": f_path.name, "reason": str(e)[:80], "i": i+1, "total": len(files)})

            yield sse("done", {
                "count": imported,
                "chunks": total_chunks,
                "new_chunks": new_chunks,
                "skipped": skipped,
                "msg": f"Przetworzono {imported} plików · {new_chunks} nowych chunków · {total_chunks - new_chunks} duplikatów pominiętych"
            })
        except Exception as e:
            task_error = str(e)
            logger.exception("import_stream error")
            yield sse("error", {"msg": task_error})
        finally:
            _heavy_task_finish(task_id, error=task_error)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/collection/clear', methods=['POST'])
def clear_collection():
    try:
        client = get_qdrant_client()
        info = client.get_collection(ACTIVE_COLLECTION)
        count_before = info.points_count
        # Pobierz aktualny wymiar wektora zanim usuniesz kolekcję
        vsize = info.config.params.vectors.size if info.config.params.vectors else 768
        from qdrant_client.models import VectorParams, Distance
        client.delete_collection(ACTIVE_COLLECTION)
        client.create_collection(
            ACTIVE_COLLECTION,
            vectors_config=VectorParams(size=vsize, distance=Distance.COSINE)
        )
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "deleted": count_before})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/import', methods=['POST'])
def import_folder():
    data = request.get_json()
    return jsonify({"success": False, "error": "Użyj /import/stream (SSE)"})

def wsl_to_win(path: str) -> str:
    """Konwertuje ścieżkę WSL /mnt/c/... na Windows C:\\..."""
    if not path:
        return ""
    m = re.match(r'^/mnt/([a-zA-Z])(?:/(.*))?$', path)
    if m:
        drive = m.group(1).upper()
        rest = (m.group(2) or "").replace('/', '\\')
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return path


def open_file_safely(win_path: str) -> bool:
    """
    Bezpiecznie otwiera plik za pomocą domyślnej aplikacji systemowej.
    Unika shell injection (w przeciwieństwie do poprzedniego os.popen).
    """
    if not win_path:
        return False

    try:
        if platform.system() == "Windows":
            # Najbezpieczniejsza metoda na Windows
            os.startfile(win_path)
            logger.info(f"Otwarto plik: {win_path}")
            return True
        else:
            # Fallback dla Linux/macOS (przydatne przy testach z WSL)
            subprocess.run(["xdg-open", win_path], check=False)
            logger.info(f"Otwarto plik (xdg-open): {win_path}")
            return True
    except Exception as e:
        logger.warning(f"Nie udało się otworzyć pliku '{win_path}': {e}")
        return False

@app.route('/file/open', methods=['POST'])
def file_open():
    data = request.get_json()
    wsl_path = data.get('path', '').strip()
    if not wsl_path:
        return jsonify({"success": False, "error": "Brak ścieżki"})

    try:
        check_path = Path(wsl_path).expanduser().resolve()
    except OSError:
        return jsonify({"success": False, "error": "Nieprawidłowa ścieżka"})
    if not check_path.is_file() or not _path_is_allowed(check_path):
        return jsonify({"success": False, "error": "Plik niedostępny lub poza dozwolonymi katalogami"})

    win_path = wsl_to_win(wsl_path)
    if not win_path:
        return jsonify({"success": False, "error": "Nie można skonwertować ścieżki"})

    success = open_file_safely(win_path)

    if success:
        return jsonify({"success": True, "win_path": win_path})
    else:
        return jsonify({"success": False, "error": "Nie udało się otworzyć pliku"})

@app.route('/hybrid/stream', methods=['POST'])
def hybrid_stream():
    """Wyszukiwanie hybrydowe: RAG + SQL równolegle — SSE."""
    data       = request.get_json()
    query_text = data.get('query', '').strip()
    chat_context = (data.get('chat_context') or '').strip()
    conn_cfg   = data.get('conn', {})
    schema_str = data.get('schema', '')
    limit      = min(int(data.get('limit', 5)), 20)
    mode       = data.get('mode', 'normal')
    if mode not in SEARCH_MODES:
        mode = 'normal'
    file_filter= data.get('file_filter', None)
    llm_provider = data.get('llm_provider')
    openrouter_model = _model_from_request(data, data.get('llm_provider'))

    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"}), 400

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

        try:
            # 1. RAG — Qdrant query
            client = get_qdrant_client()
            try:
                rag_contexts, rag_results = _retrieve_search_contexts(
                    client, query_text, limit, file_filter, mode
                )
            except EmbeddingError as e:
                yield sse("error", {"error": str(e)})
                return

            yield sse("rag_results", {"results": rag_results, "contexts": rag_contexts})

            # 2. SQL — jeśli konfiguracja dostępna (z pełnymi guardami bezpieczeństwa)
            sql_data = {"success": False, "table": "", "columns": [], "rows": [], "sql": ""}
            sql_query = ""

            if conn_cfg and _sql_conn_configured(conn_cfg):
                try:
                    _require_sql_backend(conn_cfg)
                except RuntimeError as e:
                    sql_data["error"] = str(e)
                else:
                    try:
                        result = _run_text_to_sql_pipeline(
                            conn_cfg, query_text, schema_str,
                            fetch_limit=200, with_chart=False,
                        )
                        if result.get("success"):
                            sql_query = result.get("sql", "")
                            sql_data = {
                                "success": True,
                                "table": "wynik SQL",
                                "columns": result.get("columns", []),
                                "rows": result.get("rows", []),
                                "sql": sql_query[:120],
                                "total": result.get("total", 0),
                                "sql_corrections": result.get("sql_corrections", 0),
                            }
                        else:
                            sql_data["error"] = (result.get("error") or "Błąd SQL")[:200]
                            if result.get("sql"):
                                sql_data["sql"] = result["sql"][:120]
                    except Exception as e:
                        sql_data["error"] = str(e)[:200]

            yield sse("sql_results", {"sql_results": sql_data})

            # 3. LLM synteza — łączy RAG + SQL
            if not rag_contexts:
                yield sse("done", {"ai_answer": "Brak dokumentów w bazie RAG."})
                return

            rag_cap = 10 if mode == "detective" else 5
            rag_slice = rag_contexts[:rag_cap]
            prompt, system = _build_search_prompt(
                query_text, rag_slice, mode, chat_context=chat_context
            )
            if sql_data.get("success") and sql_data.get("rows"):
                rows_str = "\n".join([
                    " | ".join([f"{k}={v}" for k, v in zip(sql_data["columns"],
                             [r.get(col, "") for col in sql_data["columns"]])])
                    for r in sql_data["rows"][:10]
                ])
                prompt += f"\n\nDANE Z BAZY SQL (uzupełnienie — porównaj z dokumentami):\n{rows_str}\n"
                if mode == "detective":
                    prompt += (
                        "\nUwzględnij w sekcji Analiza śledcza rozbieżności między SQL a dokumentami, "
                        "jeśli występują.\n"
                    )
                else:
                    prompt += "Podaj: (1) co wynika z bazy danych, (2) co potwierdzają dokumenty, (3) wnioski.\n"

            # Streaming odpowiedzi LLM (obsługuje zarówno Ollama jak i OpenRouter)
            full_answer = ""

            try:
                for token in stream_llm_tokens(
                    prompt=prompt,
                    system=system,
                    provider=llm_provider,
                    model=openrouter_model,
                    task="search",
                ):
                    if isinstance(token, str) and token.startswith("[FALLBACK]"):
                        logger.info("LLM fallback (hybrid): %s", token[:120])
                        continue
                    if isinstance(token, str) and (token.startswith("[Błąd") or token.startswith("[RATE_LIMIT]")):
                        logger.warning(f"LLM stream error (hybrid): {token[:120]}")
                        yield sse("error", {"error": token, "provider": get_llm_provider(llm_provider)})
                        return
                    full_answer += token
                    yield sse("token", {"token": token})
            except Exception as e:
                logger.warning(f"LLM stream error: {e}")
                yield sse("error", {"error": f"Błąd streamingu LLM: {str(e)}"})

            yield sse("done", {"ai_answer": full_answer})

        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/search/stream', methods=['POST'])
def search_stream():
    """Wyszukiwanie z streamingiem LLM — wyniki natychmiast, odpowiedź słowo po słowie."""
    data       = request.get_json()
    query_text = data.get('query', '').strip()
    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"})
    chat_context = (data.get('chat_context') or '').strip()
    limit      = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode        = data.get('mode', 'normal')
    llm_provider = data.get('llm_provider')
    if mode not in SEARCH_MODES:
        mode = 'normal'

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

        try:
            client = get_qdrant_client()
            try:
                raw_contexts, results = _retrieve_search_contexts(
                    client, query_text, limit, file_filter, mode
                )
            except EmbeddingError as e:
                yield sse("error", {"error": str(e)})
                return

            # Wyślij wyniki od razu
            yield sse("results", {"results": results, "contexts": raw_contexts,
                                  "mode": mode, "mode_label": SEARCH_MODES[mode]["label"]})

            if not raw_contexts:
                yield sse("done", {"ai_answer": "Brak dokumentów."})
                return

            prompt, system = _build_search_prompt(
                query_text, raw_contexts, mode, chat_context=chat_context
            )

            # Streaming LLM — obsługuje Ollama i OpenRouter
            full_answer = ""
            openrouter_model = _model_from_request(data, data.get("llm_provider"))

            try:
                for token in stream_llm_tokens(
                    prompt=prompt,
                    system=system,
                    provider=llm_provider,
                    model=openrouter_model,
                    task="search",
                ):
                    if isinstance(token, str) and token.startswith("[FALLBACK]"):
                        logger.info("LLM fallback (search): %s", token[:120])
                        continue
                    if isinstance(token, str) and (token.startswith("[Błąd") or token.startswith("[RATE_LIMIT]")):
                        # Czysty błąd zamiast zaśmiecania odpowiedzi
                        logger.warning(f"LLM stream error (search): {token[:120]}")
                        yield sse("error", {"error": token, "provider": get_llm_provider(llm_provider)})
                        return
                    full_answer += token
                    yield sse("token", {"token": token})
            except Exception as e:
                logger.warning(f"LLM stream error w search_stream: {e}")
                yield sse("error", {"error": "Błąd streamingu modelu językowego."})

            yield sse("done", {"ai_answer": full_answer})

        except Exception as e:
            yield sse("error", {"error": str(e)})

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/search', methods=['POST'])
def search():
    data = request.get_json()
    query_text = data.get('query', '').strip()
    if not query_text: return jsonify({"success": False, "error": "Zapytanie puste"})
    chat_context = (data.get('chat_context') or '').strip()
    limit = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode = data.get('mode', 'normal')
    llm_provider = data.get('llm_provider')  # 'ollama' lub 'openrouter' z UI
    if mode not in SEARCH_MODES:
        mode = 'normal'

    try:
        client = get_qdrant_client()
        try:
            raw_contexts, results = _retrieve_search_contexts(
                client, query_text, limit, file_filter, mode
            )
        except EmbeddingError as e:
            return jsonify({"success": False, "error": str(e)})

        if file_filter and not raw_contexts:
            return jsonify({"success": True, "results": [], "ai_answer": "Brak dokumentów dla wybranego pliku."})

        openrouter_model = _model_from_request(data, data.get("llm_provider"))
        ai_answer = (
            generate_answer(
                query_text,
                raw_contexts,
                mode,
                provider=llm_provider,
                model=openrouter_model,
                chat_context=chat_context,
            )
            if raw_contexts
            else "Brak dokumentów."
        )
        return jsonify({
            "success": True,
            "results": results,
            "ai_answer": ai_answer,
            "mode": mode,
            "mode_label": SEARCH_MODES[mode]["label"],
            "contexts": raw_contexts  # potrzebne do weryfikacji po stronie klienta
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/analyze', methods=['POST'])
def analyze():
    """Gotowe raporty śledcze — uproszczony POST zwracający pole answer."""
    data = request.get_json() or {}
    query_text = (data.get('query') or '').strip()
    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"})
    chat_context = (data.get('chat_context') or '').strip()
    limit = min(int(data.get('limit', 10)), 20)
    file_filter = data.get('file_filter')
    mode = data.get('mode', 'normal')
    llm_provider = data.get('llm_provider')
    openrouter_model = _model_from_request(data, data.get('llm_provider'))
    if mode not in SEARCH_MODES:
        mode = 'normal'

    try:
        client = get_qdrant_client()
        try:
            raw_contexts, results = _retrieve_search_contexts(
                client, query_text, limit, file_filter, mode
            )
        except EmbeddingError as e:
            return jsonify({"success": False, "error": str(e)})

        answer = (
            generate_answer(
                query_text,
                raw_contexts,
                mode,
                provider=llm_provider,
                model=openrouter_model,
                chat_context=chat_context,
            )
            if raw_contexts
            else "Brak dokumentów w bazie."
        )
        return jsonify({
            "success": True,
            "answer": answer,
            "ai_answer": answer,
            "results": results,
            "mode": mode,
            "mode_label": SEARCH_MODES[mode]["label"],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/suggestions', methods=['GET'])
def get_suggestions():
    force = request.args.get('force', '0') == '1'
    now = time.time()
    if not force and _suggestions_cache["data"] and (now - _suggestions_cache["ts"]) < SUGGESTIONS_TTL:
        return jsonify({"success": True, "suggestions": _suggestions_cache["data"], "cached": True})
    try:
        client = get_qdrant_client()
        # Losowa próbka: pobierz ~300 chunków, wybierz 25 z różnych plików
        records, _ = client.scroll(
            collection_name=ACTIVE_COLLECTION,
            limit=300,
            offset=None,
            with_payload=["file", "text"],
            with_vectors=False
        )
        import random
        seen_files = set()
        sample = []
        random.shuffle(records)
        for r in records:
            fname = r.payload.get("file", "")
            if fname not in seen_files and len(r.payload.get("text", "")) > 100:
                seen_files.add(fname)
                sample.append(r.payload)
            if len(sample) >= 25:
                break

        if not sample:
            return jsonify({"success": True, "suggestions": [], "cached": False})

        context = "\n\n".join([
            f"[{p['file']}]: {p['text'][:600]}" for p in sample
        ])
        prompt = (
            "Na podstawie poniższych fragmentów dokumentów śledczych, wygeneruj dokładnie 8 konkretnych pytań analitycznych. "
            "Każde pytanie musi być zwięzłe (max 12 słów), dotyczyć konkretnych faktów, kwot, dat, nazwisk lub zdarzeń z tych dokumentów. "
            "Zwróć TYLKO listę 8 pytań, każde w osobnej linii, bez numeracji, bez komentarzy.\n\n"
            f"DOKUMENTY:\n{context}"
        )
        system_msg = "Jesteś analitykiem śledczym. Odpowiadasz wyłącznie po polsku. Zwracasz tylko listę pytań."
        prov = DEFAULT_LLM_PROVIDER
        result = call_llm(
            prompt, system_msg, stream=False,
            provider=prov, model=_default_chat_model(prov), max_tokens=800,
        )
        raw = result.get("response", "") if isinstance(result, dict) else str(result)

        lines = [l.strip().lstrip("-•·1234567890.). ") for l in raw.strip().splitlines()]
        suggestions = [l for l in lines if len(l) > 10][:8]

        _suggestions_cache["data"] = suggestions
        _suggestions_cache["ts"] = now
        return jsonify({"success": True, "suggestions": suggestions, "cached": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

NOISE_PATTERNS = [
    # Licencje i pliki prawne bibliotek
    r'^(GPL|LGPL|MIT|Apache License|OFL|OFL-\d|SIL Open Font|Architects Daughter SIL|LICENSE|COPYING|NOTICE)(\.\w+)?$',
    # Logi i pliki tymczasowe
    r'.*\.log$', r'^cat-log.*', r'^SaveAs_log.*', r'^ChangeLog.*',
    # Pliki bibliotek/fontów
    r'^OFL.*\.txt$', r'^OFL-FAQ.*',
    # Zaszyfrowane/tokenizowane nazwy URL (długie stringi base64/JWT z = lub +)
    r'^[A-Za-z0-9+/=]{40,}.*',
    # Dokumentacja techniczna niezwiązana ze sprawą (można rozszerzyć)
    r'^sss\..*',
]

import re as _re

def _is_noise(fname: str) -> str:
    """Zwraca powód detekcji szumu lub pusty string jeśli plik jest OK."""
    for pat in NOISE_PATTERNS:
        if _re.match(pat, fname, _re.IGNORECASE):
            return f"wzorzec: {pat[:40]}"
    # Zaszyfrowana nazwa — długa, zawiera = lub + ale nie jest normalną nazwą pliku
    if len(fname) > 60 and ('=' in fname or (fname.count('+') > 2)):
        return "zaszyfrowana nazwa (token URL)"
    return ""

@app.route('/documents/scan-noise', methods=['GET'])
def scan_noise():
    try:
        client = get_qdrant_client()
        file_chunks = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION, limit=250, offset=offset,
                with_payload=["file"], with_vectors=False
            )
            for r in records:
                fname = r.payload.get("file", "")
                if fname:
                    file_chunks[fname] = file_chunks.get(fname, 0) + 1
            if offset is None:
                break

        noise = []
        for fname, count in file_chunks.items():
            reason = _is_noise(fname)
            if reason:
                noise.append({"file": fname, "chunks": count, "reason": reason})

        noise.sort(key=lambda x: -x["chunks"])
        return jsonify({"success": True, "noise": noise, "total_chunks": sum(n["chunks"] for n in noise)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/documents/delete', methods=['POST'])
def delete_documents():
    data = request.get_json()
    files_to_delete = data.get('files', [])
    if not files_to_delete:
        return jsonify({"success": False, "error": "Brak listy plików"})
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        client = get_qdrant_client()
        # Skasuj wszystkie punkty gdzie payload.file znajduje się na liście do usunięcia
        client.delete(
            collection_name=ACTIVE_COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="file", match=MatchAny(any=files_to_delete))]
            )
        )
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "files_count": len(files_to_delete)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/documents', methods=['GET'])
def get_documents():
    force = request.args.get('force', '0') == '1'
    ext_filter = request.args.getlist('ext')
    modified_after = request.args.get('modified_after', '').strip()
    modified_before = request.args.get('modified_before', '').strip()
    now = time.time()
    if not force and not ext_filter and not modified_after and not modified_before \
            and _docs_cache["data"] and (now - _docs_cache["ts"]) < DOCS_CACHE_TTL:
        return jsonify({"success": True, "documents": _docs_cache["data"],
                        "total": len(_docs_cache["data"]), "cached": True})
    try:
        client = get_qdrant_client()
        file_chunks = {}
        file_paths  = {}
        file_meta   = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION,
                limit=250, offset=offset,
                with_payload=["file", "full_path", "metadata"],
                with_vectors=False
            )
            for r in records:
                fname = r.payload.get("file", "")
                if fname:
                    file_chunks[fname] = file_chunks.get(fname, 0) + 1
                    if fname not in file_paths and r.payload.get("full_path"):
                        file_paths[fname] = r.payload["full_path"]
                    if fname not in file_meta and r.payload.get("metadata"):
                        file_meta[fname] = r.payload["metadata"]
            if offset is None:
                break
        docs = sorted(
            [{
                "file": f,
                "chunks": c,
                "full_path": file_paths.get(f, ""),
                "metadata": file_meta.get(f)
            }
             for f, c in file_chunks.items()],
            key=lambda x: -x["chunks"]
        )
        _docs_cache["data"] = docs
        _docs_cache["ts"]   = now
        filtered = _filter_documents_list(docs, ext_filter or None, modified_after, modified_before)
        return jsonify({
            "success": True,
            "documents": filtered,
            "total": len(filtered),
            "total_unfiltered": len(docs),
            "cached": False,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/get_context', methods=['GET'])
def get_context():
    """Lazy-load fragmentu dokumentu (chunk z Qdrant lub skrót z pliku źródłowego)."""
    point_id = (request.args.get('point_id') or '').strip()
    file_name = (request.args.get('file') or '').strip()
    query = (request.args.get('query') or '').strip()
    source = (request.args.get('source') or 'chunk').strip().lower()

    if not point_id and not file_name:
        return jsonify({"success": False, "error": "Wymagany point_id lub file"}), 400

    try:
        client = get_qdrant_client()

        if point_id and source != 'file':
            records = client.retrieve(
                collection_name=ACTIVE_COLLECTION,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
            if records:
                p = records[0].payload or {}
                text = p.get("text", "")
                if query:
                    text = highlight_backend(text, query)
                else:
                    text = _escape_html_text(text)
                full_path = p.get("full_path", "")
                return jsonify({
                    "success": True,
                    "point_id": point_id,
                    "file": p.get("file", file_name),
                    "text": text,
                    "full_path": full_path,
                    "win_path": wsl_to_win(full_path),
                    "metadata": p.get("metadata"),
                    "source": "qdrant",
                })

        if file_name:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            if query and source == 'chunk':
                try:
                    vector = get_embedding(query)
                    res = client.query_points(
                        collection_name=ACTIVE_COLLECTION,
                        query=vector,
                        limit=1,
                        query_filter=Filter(
                            must=[FieldCondition(key="file", match=MatchValue(value=file_name))]
                        ),
                    )
                    if res.points:
                        p = res.points[0].payload or {}
                        text = highlight_backend(p.get("text", ""), query) if query else _escape_html_text(p.get("text", ""))
                        full_path = p.get("full_path", "")
                        return jsonify({
                            "success": True,
                            "point_id": str(res.points[0].id),
                            "file": file_name,
                            "text": text,
                            "full_path": full_path,
                            "win_path": wsl_to_win(full_path),
                            "metadata": p.get("metadata"),
                            "source": "qdrant_search",
                        })
                except EmbeddingError as e:
                    return jsonify({"success": False, "error": str(e)})

            scroll_filter = Filter(
                must=[FieldCondition(key="file", match=MatchValue(value=file_name))]
            )
            records, _ = client.scroll(
                collection_name=ACTIVE_COLLECTION,
                scroll_filter=scroll_filter,
                limit=1,
                with_payload=True,
                with_vectors=False,
            )
            if records:
                p = records[0].payload or {}
                text = highlight_backend(p.get("text", ""), query) if query else p.get("text", "")
                if not query:
                    text = _escape_html_text(text)
                full_path = p.get("full_path", "")
                return jsonify({
                    "success": True,
                    "point_id": str(records[0].id),
                    "file": file_name,
                    "text": text,
                    "full_path": full_path,
                    "win_path": wsl_to_win(full_path),
                    "metadata": p.get("metadata"),
                    "source": "qdrant_scroll",
                })

            path = _find_file_by_name_safe(file_name)
            if path and path.is_file() and _path_is_allowed(path):
                excerpt = extract_text(path)[:12000]
                if query:
                    excerpt = highlight_backend(excerpt, query)
                else:
                    excerpt = _escape_html_text(excerpt)
                return jsonify({
                    "success": True,
                    "file": file_name,
                    "text": excerpt,
                    "full_path": str(path),
                    "win_path": wsl_to_win(str(path)),
                    "metadata": extract_file_metadata(path),
                    "source": "file",
                    "truncated": len(excerpt) >= 12000,
                })

        return jsonify({"success": False, "error": "Nie znaleziono fragmentu dokumentu"}), 404
    except Exception as e:
        logger.exception("get_context")
        return jsonify({"success": False, "error": str(e)}), 500


def _load_file_chunks_for_compare(client, file_name: str, limit: int = 6) -> list[dict]:
    """Pobiera kilka chunków danego pliku z Qdrant do porównania LLM."""
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    file_name = (file_name or "").strip()
    if not file_name:
        return []

    try:
        records, _ = client.scroll(
            collection_name=ACTIVE_COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="file", match=MatchValue(value=file_name))]
            ),
            limit=limit,
            with_payload=["file", "text", "full_path", "metadata"],
            with_vectors=False,
        )
    except Exception:
        return []

    chunks: list[dict] = []
    for idx, record in enumerate(records or []):
        payload = record.payload or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        chunks.append({
            "index": idx + 1,
            "point_id": str(record.id),
            "file": payload.get("file", file_name),
            "text": text,
            "full_path": payload.get("full_path", ""),
            "metadata": payload.get("metadata"),
        })
    return chunks


def _infer_collection_profile() -> dict:
    """Prosta heurystyka profilu kolekcji na podstawie próbek chunków."""
    try:
        client = get_qdrant_client()
        records: list = []
        offset = None
        while len(records) < 400:
            batch, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION,
                limit=100,
                offset=offset,
                with_payload=["file", "text"],
                with_vectors=False,
            )
            records.extend(batch or [])
            if offset is None:
                break
        records = records[:400]
    except Exception as e:
        return {
            "success": False,
            "profile": "empty",
            "suggestion_mode": "normal",
            "suggestion_text": "Nie udało się odczytać kolekcji.",
            "error": str(e)[:200],
        }

    if not records:
        return {
            "success": True,
            "profile": "empty",
            "suggestion_mode": "normal",
            "suggestion_text": "",
            "points_sampled": 0,
        }

    total = len(records)
    numeric_hits = 0
    legal_hits = 0
    prose_hits = 0
    ext_counts: dict[str, int] = defaultdict(int)
    sample_files: list[str] = []

    legal_markers = (
        "ustawa", "rozporządzenie", "kodeks", "art.", "§", "paragraf",
        "zarządzenie", "uchwała", "wyrok", "sygn.", "dziennik ustaw",
    )

    for record in records:
        payload = record.payload or {}
        file_name = str(payload.get("file") or "").strip()
        text = str(payload.get("text") or "")

        if file_name:
            ext = Path(file_name).suffix.lower()
            ext_counts[ext] += 1
            if len(sample_files) < 6 and file_name not in sample_files:
                sample_files.append(file_name)

        lower = text.lower()
        if any(marker in lower for marker in legal_markers):
            legal_hits += 1

        digit_count = sum(ch.isdigit() for ch in text)
        if (
            file_name.lower().endswith((".xlsx", ".xls", ".csv", ".ods", ".tsv"))
            or digit_count >= max(8, len(text) // 10)
            or re.search(r"\b\d{1,3}(?:[ .]\d{3})*(?:[,.]\d+)?\b", text)
            or re.search(r"[%$€zł]", text, re.IGNORECASE)
        ):
            numeric_hits += 1

        if len(text) > 160 and re.search(r"\b[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{3,}\b", text):
            prose_hits += 1

    if legal_hits >= max(3, total // 4) and legal_hits >= numeric_hits:
        profile = "legal"
        suggestion_mode = "legal"
        suggestion_text = "Kolekcja wygląda na prawną lub regulacyjną. Domyślnie zacznij od trybu Prawny."
    elif numeric_hits >= max(3, total // 3):
        profile = "numerical"
        suggestion_mode = "extract"
        suggestion_text = "Kolekcja wygląda na tabelaryczną / liczbową. Domyślnie lepsza będzie Ekstrakcja danych."
    elif legal_hits and numeric_hits:
        profile = "mixed"
        suggestion_mode = "detective"
        suggestion_text = "Kolekcja miesza dane liczbowe i tekstowe. Domyślnie zacznij od trybu Detektyw."
    elif prose_hits >= max(3, total // 3):
        profile = "textual"
        suggestion_mode = "normal"
        suggestion_text = "Kolekcja wygląda na opisową. Domyślnie zacznij od trybu Standardowego."
    else:
        profile = "mixed"
        suggestion_mode = "detective"
        suggestion_text = "Kolekcja ma mieszany charakter. Domyślnie zacznij od trybu Detektyw."

    return {
        "success": True,
        "profile": profile,
        "suggestion_mode": suggestion_mode,
        "suggestion_text": suggestion_text,
        "points_sampled": total,
        "sample_files": sample_files,
        "ext_counts": dict(ext_counts),
        "numeric_hits": numeric_hits,
        "legal_hits": legal_hits,
        "prose_hits": prose_hits,
    }


@app.route('/api/collection/profile', methods=['GET'])
def collection_profile():
    return jsonify(_infer_collection_profile())


@app.route('/compare', methods=['POST'])
def compare_documents():
    """Porównuje dwa dokumenty przez LLM na podstawie ich chunków z Qdrant."""
    data = request.get_json() or {}
    file_a = (data.get("file_a") or "").strip()
    file_b = (data.get("file_b") or "").strip()
    focus = (data.get("focus") or "").strip()
    llm_provider = data.get("llm_provider")
    model = _model_from_request(data, llm_provider)

    if not file_a or not file_b:
        return jsonify({"success": False, "error": "Wybierz dwa pliki do porównania"}), 400
    if file_a == file_b:
        return jsonify({"success": False, "error": "Wybierz dwa różne pliki"}), 400

    try:
        client = get_qdrant_client()
        chunks_a = _load_file_chunks_for_compare(client, file_a, limit=6)
        chunks_b = _load_file_chunks_for_compare(client, file_b, limit=6)
        if not chunks_a:
            return jsonify({"success": False, "error": f"Brak chunków dla pliku: {file_a}"}), 404
        if not chunks_b:
            return jsonify({"success": False, "error": f"Brak chunków dla pliku: {file_b}"}), 404

        def _ctx(chunks: list[dict]) -> str:
            parts = []
            for chunk in chunks:
                parts.append(
                    f"[Chunk {chunk['index']}]\n"
                    f"{_sanitize_for_prompt(chunk['text'], 1200)}"
                )
            return "\n\n".join(parts)

        prompt = (
            f"Dokument A: {file_a}\n"
            f"{_ctx(chunks_a)}\n\n"
            f"Dokument B: {file_b}\n"
            f"{_ctx(chunks_b)}\n\n"
        )
        if focus:
            prompt += f"Skup się szczególnie na: {focus}\n\n"
        prompt += (
            "Porównaj dokumenty po polsku. Wypisz najważniejsze różnice i podobieństwa, "
            "szczególnie: strony, kwoty, daty, obowiązki, terminy, załączniki, sprzeczności i braki. "
            "Nie wymyślaj faktów. Jeśli czegoś nie ma w tekście, napisz, że brak danych."
        )

        system = (
            "Jesteś analitykiem porównawczym dokumentów. "
            "Odpowiadasz po polsku, konkretnie i bez markdownowych ozdobników. "
            "Jeśli są rozbieżności, wypisz je punktami."
        )

        result = call_llm(
            prompt=prompt,
            system=system,
            stream=False,
            provider=llm_provider,
            model=model,
            max_tokens=1800,
            temperature=0.2,
        )
        comparison = _llm_response_text(result)
        return jsonify({
            "success": True,
            "file_a": file_a,
            "file_b": file_b,
            "chunks_a": len(chunks_a),
            "chunks_b": len(chunks_b),
            "focus": focus,
            "comparison": comparison,
        })
    except Exception as e:
        logger.exception("compare_documents")
        return jsonify({"success": False, "error": str(e)[:300]}), 500

@app.route('/export/metadata_report', methods=['POST'])
def export_metadata_report_docx():
    """Generuje raport DOCX z metadanymi wybranych dokumentów (dla śledczych)."""
    data = request.get_json() or {}
    selected = data.get('files', [])  # lista obiektów z file, full_path, metadata

    if not selected:
        return jsonify({"success": False, "error": "Brak wybranych plików"}), 400

    try:
        import docx as _docx
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT

        doc = _docx.Document()

        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(11)

        # Nagłówek
        h = doc.add_heading('Raport Metadanych Wybranych Dokumentów', 0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph(f'Wygenerowano: {time.strftime("%d.%m.%Y %H:%M")}')
        doc.add_paragraph(f'Liczba plików: {len(selected)}')
        doc.add_paragraph()

        # Podsumowanie
        doc.add_heading('Podsumowanie', level=1)
        doc.add_paragraph('Poniżej znajdują się metadane systemowe oraz wbudowane dla wybranych dokumentów. '
                          'Dane pochodzą z oryginalnych plików w momencie importu.')

        # Dla każdego pliku
        for idx, f in enumerate(selected, 1):
            doc.add_heading(f'{idx}. {f.get("file", "Nieznany plik")}', level=1)

            meta = f.get('metadata') or {}

            # Metadane systemowe
            doc.add_heading('Metadane systemowe', level=2)
            table = doc.add_table(rows=1, cols=2)
            table.style = 'Table Grid'
            hdr_cells = table.rows[0].cells
            hdr_cells[0].text = 'Pole'
            hdr_cells[1].text = 'Wartość'

            for key in ['size_human', 'created', 'modified', 'accessed']:
                if meta.get(key):
                    row_cells = table.add_row().cells
                    row_cells[0].text = key.replace('_', ' ').title()
                    row_cells[1].text = str(meta[key])

            # Metadane wbudowane
            if meta.get('embedded'):
                doc.add_heading('Metadane wbudowane w plik', level=2)
                for fmt, emb in meta['embedded'].items():
                    p = doc.add_paragraph()
                    p.add_run(f'{fmt.upper()}: ').bold = True
                    p.add_run(str(emb))

            # Pełna ścieżka
            if meta.get('full_path'):
                p = doc.add_paragraph()
                p.add_run('Pełna ścieżka: ').bold = True
                p.add_run(meta['full_path'])

            doc.add_paragraph()  # odstęp

        # Stopka
        doc.add_paragraph('─' * 70)
        p = doc.add_paragraph()
        run = p.add_run('Wygenerowano przez AI Analiza Dokumentów | Narzędzie wspomagające analizę śledczą')
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x6c, 0x75, 0x7d)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        fname = f'raport_metadanych_{time.strftime("%Y%m%d_%H%M")}.docx'
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/export/docx', methods=['POST'])
def export_docx():
    data        = request.get_json()
    query       = data.get('query', '')
    ai_answer   = data.get('ai_answer', '')
    results     = data.get('results', [])
    mode_label  = data.get('mode_label', 'Standardowy')

    try:
        import docx as _docx
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = _docx.Document()

        # Styl dokumentu
        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(11)

        # Nagłówek
        h = doc.add_heading('Raport Śledczy — Analiza RAG', 0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph(f'Data: {time.strftime("%d.%m.%Y %H:%M")}   |   Tryb: {mode_label}')
        doc.add_paragraph(f'Kolekcja: {ACTIVE_COLLECTION}   |   Wyników: {len(results)}')
        doc.add_paragraph()

        # Zapytanie
        doc.add_heading('Zapytanie', level=1)
        p = doc.add_paragraph()
        p.add_run(query).bold = True

        # Odpowiedź LLM
        doc.add_heading('Synteza AI (Llama3)', level=1)
        for line in ai_answer.split('\n'):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(style='List Bullet' if line.startswith('-') else 'Normal')
            p.add_run(line.lstrip('- '))

        # Źródła
        doc.add_heading(f'Dokumenty źródłowe ({len(results)})', level=1)
        for i, r in enumerate(results, 1):
            doc.add_heading(f'{i}. {r.get("file","?")}', level=2)
            score_p = doc.add_paragraph()
            run = score_p.add_run(f'Dopasowanie: {float(r.get("score",0))*100:.1f}%')
            run.font.color.rgb = RGBColor(0x6c, 0x75, 0x7d)
            run.font.size = Pt(9)
            # Usuń tagi HTML z tekstu
            clean = re.sub(r'<[^>]+>', '', r.get('text', ''))
            doc.add_paragraph(clean[:1500])

            if r.get('win_path'):
                p = doc.add_paragraph()
                run = p.add_run(f'Ścieżka: {r["win_path"]}')
                run.font.color.rgb = RGBColor(0x0d, 0x6e, 0xfd)
                run.font.size = Pt(9)

            doc.add_paragraph()

        # Stopka
        doc.add_paragraph('─' * 60)
        doc.add_paragraph('Wygenerowano przez AI Analiza Dokumentów | Llama3 + Qdrant Cloud').runs[0].font.size = Pt(8)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        fname = f'raport_{time.strftime("%Y%m%d_%H%M")}.docx'
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


_NETWORK_SYSTEM = (
    "Jesteś ekspertem analityki śledczej. Twoim zadaniem jest wyciągnięcie jak najbogatszej sieci powiązań z dokumentów.\n\n"
    "ZASADY BEZWZGLĘDNE:\n"
    "1. Używaj WYŁĄCZNIE prawdziwych nazw, kwot, dat i faktów z dokumentów.\n"
    "2. NIGDY nie używaj placeholderów typu 'nazwa', 'firma', 'X', 'osoba A'.\n"
    "3. Każdy label musi być rzeczywistą wartością z tekstu.\n\n"
    "FORMAT JSON (zwracaj tylko poprawny JSON):\n"
    '{\n'
    '  "nodes": [ {"id": "jan_kowalski", "type": "osoba", "label": "Jan Kowalski"}, ... ],\n'
    '  "edges": [ {"source": "jan_kowalski", "target": "abc_spolka", "label": "prezes zarządu", '
    '"doc": "umowa_2023.pdf", "evidence": "Podpisał umowę 12.03.2023", "date": "2023-03-12", "strength": 3}, ... ]\n'
    '}\n\n'
    "Typy węzłów (type): osoba, firma, kwota, dokument, przetarg, umowa, inne\n\n"
    "Bogate typy relacji (label krawędzi) — używaj precyzyjnych:\n"
    "- prezes / członek zarządu / dyrektor\n"
    "- podpisał umowę / aneks\n"
    "- zapłacił / wystawił fakturę / otrzymał płatność\n"
    "- wygrał przetarg / złożył ofertę\n"
    "- zlecił / wykonał usługę\n"
    "- jest właścicielem / beneficjentem\n"
    "- zatwierdził / skontrolował\n\n"
    "W polu 'evidence' wstaw krótki, dosłowny cytat z dokumentu.\n"
    "Jeśli w tekście pojawia się data — dodaj ją w polu 'date' (YYYY-MM-DD lub YYYY-MM).\n"
    "W polu 'strength' (1-5) podaj siłę powiązania na podstawie dowodów w dokumencie.\n"
    "id: snake_case, bez polskich znaków, max 40 znaków."
)


def _aggregate_network_edges(raw_edges: list, seen_nodes: dict) -> list:
    """Grupuje krawędzie, sumuje siłę i zbiera dowody."""
    edge_groups = defaultdict(list)
    for e in raw_edges:
        src = str(e.get("source", "")).strip()
        tgt = str(e.get("target", "")).strip()
        if not src or not tgt or src not in seen_nodes or tgt not in seen_nodes:
            continue
        edge_groups[(src, tgt)].append({
            "label": (e.get("label") or "")[:40],
            "doc": (e.get("doc") or "")[:90],
            "evidence": (e.get("evidence") or "")[:180],
            "date": (e.get("date") or "")[:20],
            "strength": _safe_int_clamp(e.get("strength", 1), 1, 5, default=1),
        })

    clean_edges = []
    for (src, tgt), items in edge_groups.items():
        labels = [it["label"] for it in items if it["label"]]
        main_label = max(set(labels), key=labels.count) if labels else items[0]["label"]

        seen_ev = set()
        evidences = []
        for it in items:
            ev_key = (it["doc"], it["evidence"][:80])
            if ev_key not in seen_ev:
                seen_ev.add(ev_key)
                evidences.append({
                    "doc": it["doc"],
                    "evidence": it["evidence"],
                    "date": it["date"],
                })
            if len(evidences) >= 6:
                break

        total_strength = sum(it["strength"] for it in items) + (len(items) - 1)
        clean_edges.append({
            "source": src,
            "target": tgt,
            "label": main_label,
            "doc": items[0]["doc"],
            "evidence": items[0]["evidence"],
            "date": items[0]["date"],
            "strength": max(1, min(12, total_strength)),
            "evidence_count": len(evidences),
            "evidences": evidences,
        })

    clean_edges.sort(key=lambda e: e.get("strength", 1), reverse=True)
    return clean_edges


def _network_graph_stats(nodes: list, edges: list) -> dict:
    """Statystyki grafu bez dodatkowego wywołania LLM."""
    degree = defaultdict(int)
    for e in edges:
        degree[e["source"]] += e.get("strength", 1)
        degree[e["target"]] += e.get("strength", 1)

    type_counts = defaultdict(int)
    for n in nodes:
        type_counts[n.get("type", "inne")] += 1

    rel_counts = defaultdict(int)
    for e in edges:
        rel_counts[e.get("label") or "(bez etykiety)"] += 1

    dates = sorted(d for e in edges if (d := (e.get("date") or "").strip()) and d[0].isdigit())

    hubs = sorted(
        [{"id": n["id"], "label": n.get("label", n["id"]), "type": n.get("type", "inne"),
          "score": degree.get(n["id"], 0)}
         for n in nodes if degree.get(n["id"], 0) > 0],
        key=lambda h: h["score"],
        reverse=True,
    )[:8]

    top_relations = sorted(rel_counts.items(), key=lambda x: x[1], reverse=True)[:8]

    return {
        "entity_counts": dict(type_counts),
        "top_hubs": hubs,
        "top_relations": [{"label": k, "count": v} for k, v in top_relations],
        "date_span": {"min": dates[0], "max": dates[-1]} if dates else None,
        "strong_edges": sum(1 for e in edges if e.get("strength", 1) >= 3),
    }


def _network_ai_briefing(query: str, nodes: list, edges: list, stats: dict,
                         provider=None, model=None) -> str:
    """Krótki briefing śledczy na podstawie zbudowanego grafu."""
    if not nodes or not edges:
        return ""

    hub_lines = ", ".join(f"{h['label']} ({h['score']})" for h in stats.get("top_hubs", [])[:5])
    rel_lines = ", ".join(f"{r['label']}×{r['count']}" for r in stats.get("top_relations", [])[:5])
    sample_edges = edges[:12]
    edge_txt = "\n".join(
        f"- {e.get('source')} → {e.get('target')}: {e.get('label')} "
        f"(siła {e.get('strength', 1)}, {e.get('doc', '')})"
        for e in sample_edges
    )

    prompt = (
        f"ZAPYTANIE UŻYTKOWNIKA: {query}\n\n"
        f"GRAF: {len(nodes)} węzłów, {len(edges)} relacji.\n"
        f"Kluczowe węzły (huby): {hub_lines or 'brak'}.\n"
        f"Najczęstsze relacje: {rel_lines or 'brak'}.\n\n"
        f"Przykładowe krawędzie:\n{edge_txt}\n\n"
        "Napisz krótki briefing śledczy (max 6 punktów):\n"
        "1) główne podmioty i ich role,\n"
        "2) najsilniejsze powiązania finansowe/prawne,\n"
        "3) potencjalne rozbieżności lub luki [WYMAGA SPRAWDZENIA],\n"
        "4) 2-3 konkretne pytania do dalszej analizy.\n"
        "Tylko fakty z grafu — bez wymyślania."
    )
    system = (
        "Jesteś analitykiem śledczym. Pisz po polsku, zwięźle, punktami. "
        "Oznaczaj niepewności tagiem [WYMAGA SPRAWDZENIA]."
    )
    try:
        result = call_llm(
            prompt=prompt, system=system, stream=False,
            provider=provider, model=model, max_tokens=900,
        )
        return (result.get("response", "") if isinstance(result, dict) else str(result)).strip()
    except Exception as e:
        logger.warning(f"Briefing sieci powiązań: {e}")
        return ""


@app.route('/network', methods=['POST'])
def build_network():
    """
    Buduje sieć powiązań partiami (po 5 dokumentów).
    Zwraca SSE: progress → done (graf + statystyki + opcjonalny briefing AI).
    """
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    limit = min(int(data.get('limit', 10)), 20)
    llm_provider = data.get('llm_provider')
    openrouter_model = _model_from_request(data, data.get("llm_provider"))
    include_briefing = data.get("include_briefing", True)
    BATCH_SIZE = 5

    if not query:
        return jsonify({"success": False, "error": "Brak zapytania"})

    def generate():
        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        try:
            client = get_qdrant_client()
            try:
                vector = get_embedding(query)
            except EmbeddingError as e:
                yield sse("error", {"error": str(e)})
                return

            res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)
            all_contexts = [
                {"file": p.payload.get("file", ""), "text": p.payload.get("text", "")}
                for p in res.points
            ]

            if not all_contexts:
                yield sse("error", {"error": "Nie znaleziono dokumentów pasujących do zapytania"})
                return

            batches = [all_contexts[i:i + BATCH_SIZE] for i in range(0, len(all_contexts), BATCH_SIZE)]
            total = len(all_contexts)
            total_batches = len(batches)

            seen_nodes = {}
            raw_edges = []

            yield sse("progress", {
                "processed": 0,
                "total": total,
                "batch": 0,
                "total_batches": total_batches,
                "message": f"Znaleziono {total} fragmentów w {total_batches} partiach…",
            })

            for i, batch in enumerate(batches):
                yield sse("progress", {
                    "processed": min(i * BATCH_SIZE, total),
                    "total": total,
                    "batch": i + 1,
                    "total_batches": total_batches,
                    "message": f"Analiza partii {i + 1}/{total_batches} (LLM ekstrahuje encje)…",
                })

                batch_str = "\n\n".join(
                    f"[{c['file']}]: {_sanitize_for_prompt(c['text'], 800)}" for c in batch
                )
                prompt = (
                    f"DOKUMENTY (partia {i + 1}/{total_batches}):\n{batch_str}\n\n"
                    f"ZADANIE: {query}\n\n"
                    "Wyciągnij sieć powiązań używając PRAWDZIWYCH nazw, kwot i danych.\n"
                    "NIE używaj placeholderów. JSON:"
                )

                try:
                    result = call_llm(
                        prompt=prompt,
                        system=_NETWORK_SYSTEM,
                        stream=False,
                        provider=llm_provider,
                        model=openrouter_model,
                        max_tokens=2800,
                    )
                    raw = _llm_response_text(result)
                    graph = _extract_first_json_value(raw)
                    if not isinstance(graph, dict):
                        logger.warning("Network LLM returned non-object JSON: %s", type(graph).__name__)
                        continue

                    for n in graph.get("nodes", []):
                        nid = str(n.get("id", "")).strip()
                        if nid and nid not in seen_nodes:
                            seen_nodes[nid] = {
                                "id": nid,
                                "type": n.get("type", "inne"),
                                "label": (n.get("label") or nid)[:40],
                            }

                    for e in graph.get("edges", []):
                        raw_edges.append(e)

                except Exception as batch_err:
                    logger.warning(f"Błąd partii sieci {i + 1}: {batch_err}")
                    continue

            clean_nodes = list(seen_nodes.values())
            clean_edges = _aggregate_network_edges(raw_edges, seen_nodes)
            stats = _network_graph_stats(clean_nodes, clean_edges)

            briefing = ""
            if include_briefing and clean_nodes and clean_edges:
                yield sse("progress", {
                    "processed": total,
                    "total": total,
                    "batch": total_batches,
                    "total_batches": total_batches,
                    "message": "Generuję briefing śledczy AI…",
                })
                briefing = _network_ai_briefing(
                    query, clean_nodes, clean_edges, stats,
                    provider=llm_provider, model=openrouter_model,
                )

            yield sse("done", {
                "success": True,
                "nodes": clean_nodes,
                "edges": clean_edges,
                "query": query,
                "sources": len(all_contexts),
                "stats": stats,
                "briefing": briefing,
            })

        except Exception as e:
            logger.exception("Błąd build_network")
            yield sse("error", {"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/timeline', methods=['POST'])
def timeline_endpoint():
    """Oś czasu — ekstrahuje daty z dokumentów i buduje chronologię (SSE)."""
    data = request.get_json() or {}
    query_text = (data.get('query') or '').strip()
    limit = min(int(data.get('limit', 20)), 50)
    llm_provider = data.get('llm_provider')
    openrouter_model = _model_from_request(data, data.get('llm_provider'))

    def generate():
        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        try:
            client = get_qdrant_client()
            payloads = []

            # Pobieramy dokumenty — wg query lub ostatnie
            if query_text:
                yield sse("progress", {"message": "Wyszukiwanie dokumentów…"})
                try:
                    vector = get_embedding(query_text)
                    res = client.query_points(
                        collection_name=ACTIVE_COLLECTION,
                        query=vector,
                        limit=limit,
                        with_payload=True,
                        with_vectors=False
                    )
                    payloads = [p.payload for p in res.points]
                except Exception as e:
                    logger.warning(f"Błąd przy pobieraniu embeddingu: {e}")
                    payloads = []

            if not payloads:
                # Fallback — pobierz ostatnie dokumenty
                yield sse("progress", {"message": "Pobieranie ostatnich dokumentów…"})
                records, _ = client.scroll(
                    collection_name=ACTIVE_COLLECTION,
                    limit=limit,
                    with_payload=True,
                    with_vectors=False
                )
                payloads = [r.payload for r in records]

            if not payloads:
                yield sse("done", {
                    "success": True,
                    "timeline": [],
                    "message": "Brak dokumentów do analizy."
                })
                return

            # Pre-filtracja: szukamy dat za pomocą RegEx
            yield sse("progress", {"message": "Ekstrakcja dat (RegEx)…"})
            date_pattern = re.compile(
                r'\b(?:\d{1,4}[.\/-]\d{1,2}[.\/-]\d{1,4}|'
                r'\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+(?:19|20)\d{2})\b',
                re.IGNORECASE
            )

            date_chunks = []
            for p in payloads:
                text = p.get("text", "")
                if date_pattern.search(text):
                    date_chunks.append({
                        "file": p.get("file", "Nieznany"),
                        "text": text[:600]
                    })

            if not date_chunks:
                yield sse("done", {
                    "success": True,
                    "timeline": [],
                    "message": "Nie wykryto żadnych dat w badanych dokumentach."
                })
                return

            # Przygotowujemy kontekst — max 15 fragmentów
            context_str = "\n\n".join([
                f"[{c['file']}]: {c['text']}" for c in date_chunks[:15]
            ])

            system_msg = (
                "Jesteś śledczym analitykiem danych. Twoim zadaniem jest znalezienie wszystkich zdarzeń powiązanych z datami "
                "w podanym tekście i przypisanie do nich krótkich, obiektywnych opisów. "
                "Zwróć odpowiedź WYŁĄCZNIE jako poprawny JSON w formacie: "
                "[{\"date\": \"YYYY-MM-DD\", \"event\": \"opis zdarzenia\", \"entity\": \"powiązany podmiot (opcjonalnie)\", \"doc\": \"nazwa pliku\"}]. "
                "Jeśli brakuje dokładnego dnia, użyj YYYY-MM-01. Ważne: Zwróć sam JSON, bez żadnego tekstu pobocznego i bez bloku markdown ```json."
            )

            prompt = f"DOKUMENTY:\n{context_str}\n\nWyciągnij chronologię z dokumentów w formacie JSON:"

            yield sse("progress", {"message": "Analiza LLM…"})
            try:
                result = call_llm(
                    prompt=prompt,
                    system=system_msg,
                    stream=False,
                    provider=llm_provider,
                    model=openrouter_model,
                    max_tokens=1500
                )
                raw_resp = _llm_response_text(result)
            except Exception as llm_err:
                logger.exception(f"Timeline LLM error: {llm_err}")
                if _is_transient_llm_error(llm_err):
                    yield sse("error", {"error": "Błąd LLM: chwilowa niedostępność modelu. Spróbuj ponownie za chwilę."})
                else:
                    yield sse("error", {"error": f"Błąd LLM: {str(llm_err)[:200]}"})
                return

            # Parsowanie JSON'a
            try:
                parsed = _extract_first_json_value(raw_resp)
            except Exception:
                logger.warning(f"Timeline: brak JSON w odpowiedzi LLM: {raw_resp[:200]}")
                yield sse("error", {
                    "error": "LLM nie zwrócił poprawnego formatu JSON.",
                    "raw": raw_resp[:300]
                })
                return

            if isinstance(parsed, dict):
                parsed_timeline = parsed.get("timeline") or parsed.get("events") or parsed.get("items") or []
            else:
                parsed_timeline = parsed

            if not isinstance(parsed_timeline, list):
                yield sse("error", {
                    "error": "LLM zwrócił JSON w nieoczekiwanym formacie."
                })
                return

            parsed_timeline.sort(key=lambda x: x.get('date', '9999-99-99') if isinstance(x, dict) else '9999-99-99')
            yield sse("done", {
                "success": True,
                "timeline": parsed_timeline
            })

        except Exception as e:
            logger.exception("timeline_endpoint error")
            yield sse("error", {"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ============================================================
# /agents/swarm — rój LLM z przełącznikiem A/B/C
# ============================================================

SWARM_MODE_CONFIG = {
    "A": {
        "label": "A · Incognito",
        "description": "Każdy worker dostaje potasowane fragmenty i losowy model. Priorytet: prywatność.",
        "workers": 5,
        "max_chunks_per_worker": 4,
        "shuffle_chunks": True,
        "random_models": True,
        "mask_pii_default": True,
        "strict_incognito_default": True,
    },
    "B": {
        "label": "B · Szybkość",
        "description": "Najmniej narzutu, szybki model i maksymalny paralelizm. Priorytet: czas odpowiedzi.",
        "workers": 4,
        "max_chunks_per_worker": 5,
        "shuffle_chunks": False,
        "random_models": False,
        "mask_pii_default": False,
        "strict_incognito_default": False,
    },
    "C": {
        "label": "C · Jakość",
        "description": "Mniej workerów, mocniejsza synteza i lepsza zgodność między częściami. Priorytet: jakość.",
        "workers": 3,
        "max_chunks_per_worker": 6,
        "shuffle_chunks": False,
        "random_models": False,
        "mask_pii_default": False,
        "strict_incognito_default": False,
    },
}

SWARM_TASK_TEMPLATES = [
    {
        "id": "overpayments",
        "label": "Przepłaty / faktury",
        "hint": "Duplikaty kwot, rozbieżności NIP–kwota, fragmentacja zamówień",
        "query": (
            "Przeanalizuj fragmenty pod kątem przepłat, duplikatów faktur, rozbieżności kwot "
            "vs terminów płatności oraz podejrzanej fragmentacji zamówień. "
            "Wypisz konkretne kwoty, daty i kontrahentów z oznaczeniem ryzyka."
        ),
        "suggested_mode": "A",
    },
    {
        "id": "thesis",
        "label": "Praca dyplomowa / rozdział",
        "hint": "Spójność metodologii, luki w argumentacji, brakujące odwołania",
        "query": (
            "Oceń fragmenty pod kątem spójności metodologii, jakości argumentacji, "
            "brakujących odwołań i powtórzeń. Wskaż mocne strony, luki i sugestie poprawy "
            "dla recenzenta merytorycznego."
        ),
        "suggested_mode": "C",
    },
    {
        "id": "contracts",
        "label": "Umowy / klauzule",
        "hint": "Ryzyka prawne, niespójności między załącznikami",
        "query": (
            "Przeanalizuj fragmenty umów i aneksów: wskaż klauzule wysokiego ryzyka, "
            "niespójności między załącznikami, terminy, kary umowne i brakujące elementy. "
            "Zwróć checklistę ryzyk z priorytetem."
        ),
        "suggested_mode": "A",
    },
    {
        "id": "links",
        "label": "Powiązania osób–firm",
        "hint": "Kto występuje z kim i w jakim kontekście",
        "query": (
            "Na podstawie fragmentów wskaż powiązania między osobami, firmami i kwotami. "
            "Wypisz role, daty i kontekst współwystępowania. Zaznacz powiązania wymagające weryfikacji."
        ),
        "suggested_mode": "B",
    },
]


def _swarm_config(mode: str | None) -> dict:
    key = (mode or "A").strip().upper()
    return SWARM_MODE_CONFIG.get(key, SWARM_MODE_CONFIG["A"]) | {"key": key if key in SWARM_MODE_CONFIG else "A"}


def _swarm_assign_worker_models(mode_key: str, model_pool: list[str], worker_idx: int) -> list[str]:
    """Zwraca listę modeli do wypróbowania dla jednego workera (pierwszy = preferowany)."""
    pool = [m for m in (model_pool or []) if m] or [LLM_MODEL]
    key = (mode_key or "A").upper()
    if key == "A":
        # Incognito: jeden unikalny model na workera (pula jest już potasowana)
        return [pool[worker_idx % len(pool)]]
    if key == "B":
        return pool[:1]
    return pool[: min(2, len(pool))]


def _swarm_worker_params(mode_key: str) -> tuple[int, float]:
    key = (mode_key or "A").upper()
    if key == "B":
        return 1000, 0.2
    if key == "C":
        return 1800, 0.15
    return 1500, 0.15


def _swarm_synthesis_settings(mode_key: str, model_pool: list[str], *, strict_local: bool = False) -> tuple[list[str], int, float, str | None]:
    if strict_local:
        local = [m for m in (LLM_MODEL, "llama3", "llama3.1", "mistral", "qwen2.5") if m]
        return local[:3], 2000, 0.2, "ollama"
    pool = [m for m in (model_pool or []) if m] or [LLM_MODEL]
    key = (mode_key or "A").upper()
    if key == "B":
        return pool[:1], 1200, 0.2, None
    if key == "C":
        return pool[: min(4, len(pool))], 2800, 0.15, None
    return pool[: min(2, len(pool))], 1800, 0.2, None


def _swarm_privacy_flags(data: dict, config: dict, provider: str | None) -> tuple[bool, bool]:
    prov = get_llm_provider(provider)
    cloud_workers = prov != "ollama"
    if data.get("mask_pii") is None:
        mask_pii = bool(config.get("mask_pii_default")) and cloud_workers
    else:
        mask_pii = bool(data.get("mask_pii"))
    if data.get("strict_incognito") is None:
        strict_incognito = bool(config.get("strict_incognito_default")) and cloud_workers
    else:
        strict_incognito = bool(data.get("strict_incognito"))
    if strict_incognito and not cloud_workers:
        strict_incognito = False
    return mask_pii, strict_incognito


def _swarm_model_candidates(provider: str | None) -> list[str]:
    prov = get_llm_provider(provider)
    if prov == "groq":
        models = [
            GROQ_MODEL,
            GROQ_MODEL_VERIFY,
            "llama-3.1-8b-instant",
            "groq/compound-mini",
            "groq/compound",
            "llama-3.3-70b-versatile",
            "qwen/qwen3-32b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
        ]
    elif prov == "openrouter":
        models = [
            OPENROUTER_MODEL,
            OPENROUTER_MODEL_VERIFY,
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2.5-7b-instruct:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ]
    else:
        models = [
            LLM_MODEL,
            "llama3",
            "llama3.1",
            "mistral",
            "qwen2.5",
        ]

    out = []
    seen = set()
    for model in models:
        m = str(model or "").strip()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out or [LLM_MODEL]


def _swarm_ranked_models(provider: str | None, mode_key: str) -> list[str]:
    pool = _swarm_model_candidates(provider)
    key = (mode_key or "B").upper()

    if key == "A":
        import random
        shuffled = pool[:]
        random.shuffle(shuffled)
        return shuffled

    if key == "B":
        fast_order = [
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "mistralai/mistral-7b-instruct:free",
            "qwen/qwen-2.5-7b-instruct:free",
            "llama-3.1-8b-instant",
            "groq/compound-mini",
            LLM_MODEL,
        ]
        ranked = [m for m in fast_order if m in pool]
        ranked.extend([m for m in pool if m not in ranked])
        return ranked

    quality_order = [
        "meta-llama/llama-3.3-70b-instruct:free",
        "llama-3.3-70b-versatile",
        OPENROUTER_MODEL,
        GROQ_MODEL,
        OPENROUTER_MODEL_VERIFY,
        GROQ_MODEL_VERIFY,
        "groq/compound",
        "google/gemini-2.0-flash-exp:free",
        "mistralai/mistral-7b-instruct:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "llama-3.1-8b-instant",
        "groq/compound-mini",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        LLM_MODEL,
    ]
    ranked = [m for m in quality_order if m in pool]
    ranked.extend([m for m in pool if m not in ranked])
    return ranked


def _swarm_split_contexts(contexts: list[dict], workers: int, max_chunks_per_worker: int, shuffle_chunks: bool) -> list[list[dict]]:
    items = list(contexts or [])
    if shuffle_chunks:
        import random
        random.shuffle(items)
    limit = min(len(items), max(1, workers) * max(1, max_chunks_per_worker))
    items = items[:limit]
    groups = [[] for _ in range(max(1, workers))]
    for idx, item in enumerate(items):
        groups[idx % len(groups)].append(item)
    return [g for g in groups if g]


def _swarm_group_prompt(
    query: str,
    group: list[dict],
    group_idx: int,
    total_groups: int,
    mode_label: str,
    *,
    mask_pii: bool = False,
) -> str:
    parts = []
    for item in group:
        file_name = str(item.get("file") or "nieznany").strip()
        text, _stats = _swarm_prepare_text(str(item.get("text") or ""), max_len=1200, mask_pii=mask_pii)
        parts.append(f"[{file_name}]\n{text}")
    context = "\n\n".join(parts)
    safe_query, _ = _swarm_prepare_text(query, max_len=2000, mask_pii=mask_pii)
    return (
        f"TRYB ROJU: {mode_label}\n"
        f"PODZADANIE: część {group_idx + 1}/{total_groups}\n"
        f"PYTANIE UŻYTKOWNIKA: {safe_query}\n\n"
        "Dostajesz tylko wycinek dokumentów. Zwróć zwięzłą analizę w 4 sekcjach:\n"
        "1) fakty,\n2) osoby/podmioty,\n3) daty/kwoty/liczby,\n4) luki i pytania pomocnicze.\n"
        "Nie wymyślaj faktów spoza tekstu.\n\n"
        f"FRAGMENTY:\n{context}"
    )


def _swarm_synthesis_prompt(query: str, worker_outputs: list[dict], mode_label: str, mode_key: str = "A") -> str:
    blocks = []
    for out in worker_outputs:
        blocks.append(
            f"[Worker {out.get('worker_index') + 1} | {out.get('model')}]\n"
            f"{out.get('summary', '').strip()}"
        )
    joined = "\n\n".join(blocks)
    quality_hint = ""
    if (mode_key or "").upper() == "C":
        quality_hint = (
            "Tryb JAKOŚĆ: szczegółowo porównaj wnioski workerów, rozstrzygnij sprzeczności, "
            "wypisz najważniejsze dowody i ocenę wiarygodności.\n"
        )
    return (
        f"TRYB ROJU: {mode_label}\n"
        f"PYTANIE UŻYTKOWNIKA: {query}\n\n"
        f"{quality_hint}"
        "Masz syntezę wyników częściowych od wielu workerów. "
        "Połącz je w jedną spójną odpowiedź po polsku. "
        "Jeśli workerzy się różnią, wskaż to wprost i wybierz najbardziej wiarygodny wariant.\n"
        "Zwróć:\n"
        "- Krótkie podsumowanie\n"
        "- Najważniejsze fakty\n"
        "- Wnioski\n"
        "- 2-4 pytania dalsze\n\n"
        f"WYNIKI WORKERÓW:\n{joined}"
    )


def _swarm_call_with_fallback(prompt: str, system: str, provider: str | None, models: list[str],
                              max_tokens: int, temperature: float) -> tuple[str, str]:
    last_error = None
    for model in models or [None]:
        try:
            result = call_llm(
                prompt=prompt,
                system=system,
                stream=False,
                provider=provider,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return (model or "", _llm_response_text(result))
        except Exception as e:
            last_error = e
            continue
    raise RuntimeError(str(last_error) if last_error else "Błąd wywołania LLM")


@app.route('/api/swarm/modes', methods=['GET'])
def swarm_modes():
    """Opis trybów A/B/C roju LLM (zgodny z SWARM_MODE_CONFIG)."""
    modes = {}
    for key, cfg in SWARM_MODE_CONFIG.items():
        modes[key] = {
            "label": cfg.get("label"),
            "description": cfg.get("description"),
            "workers": cfg.get("workers"),
            "max_chunks_per_worker": cfg.get("max_chunks_per_worker"),
            "shuffle_chunks": cfg.get("shuffle_chunks"),
            "random_models": cfg.get("random_models"),
            "mask_pii_default": cfg.get("mask_pii_default"),
            "strict_incognito_default": cfg.get("strict_incognito_default"),
            "default_limit": cfg.get("workers", 4) * cfg.get("max_chunks_per_worker", 4),
        }
    return jsonify({
        "success": True,
        "modes": modes,
        "default": "A",
        "templates": SWARM_TASK_TEMPLATES,
    })


@app.route('/agents/swarm', methods=['POST'])
def agents_swarm():
    """Rój LLM: map-reduce z przełącznikiem A/B/C."""
    from concurrent.futures import ThreadPoolExecutor

    data = request.get_json() or {}
    query = (data.get("query") or "").strip()
    file_filter = data.get("file_filter")
    chat_context = (data.get("chat_context") or "").strip()
    provider = data.get("llm_provider")
    mode = (data.get("swarm_mode") or data.get("mode") or "A").strip().upper()
    config = _swarm_config(mode)
    search_limit = min(int(data.get("limit") or (config["workers"] * config["max_chunks_per_worker"])), 20)
    mask_pii, strict_incognito = _swarm_privacy_flags(data, config, provider)

    if not query:
        return jsonify({"success": False, "error": "Brak zapytania"}), 400

    def generate():
        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        try:
            client = get_qdrant_client()
            try:
                raw_contexts, results = _retrieve_search_contexts(
                    client, query, search_limit, file_filter, "detective"
                )
            except EmbeddingError as e:
                yield sse("error", {"error": str(e)})
                return

            if not raw_contexts:
                yield sse("error", {"error": "Brak dokumentów do analizy"})
                return

            groups = _swarm_split_contexts(
                raw_contexts,
                config["workers"],
                config["max_chunks_per_worker"],
                config["shuffle_chunks"],
            )
            if not groups:
                yield sse("error", {"error": "Nie udało się podzielić dokumentów na grupy"})
                return

            pii_stats: dict[str, int] = {}
            if mask_pii:
                for item in raw_contexts:
                    _, stats = _swarm_prepare_text(str(item.get("text") or ""), mask_pii=True)
                    _merge_pii_stats(pii_stats, stats)
                _, qstats = _swarm_prepare_text(query, max_len=5000, mask_pii=True)
                _merge_pii_stats(pii_stats, qstats)

            model_pool = _swarm_ranked_models(provider, config["key"])
            if not model_pool:
                model_pool = [LLM_MODEL]

            yield sse("start", {
                "success": True,
                "mode": config["key"],
                "mode_label": config["label"],
                "description": config["description"],
                "workers": len(groups),
                "sources": len(raw_contexts),
                "provider": get_llm_provider(provider),
                "shuffle_chunks": config.get("shuffle_chunks"),
                "random_models": config.get("random_models"),
                "max_chunks_per_worker": config.get("max_chunks_per_worker"),
                "mask_pii": mask_pii,
                "strict_incognito": strict_incognito,
                "pii_redactions": pii_stats,
            })

            worker_outputs = []
            worker_max_tokens, worker_temp = _swarm_worker_params(config["key"])

            with ThreadPoolExecutor(max_workers=len(groups)) as ex:
                futures = []
                for idx, group in enumerate(groups):
                    models = _swarm_assign_worker_models(config["key"], model_pool, idx)

                    prompt = _swarm_group_prompt(
                        query, group, idx, len(groups), config["label"], mask_pii=mask_pii,
                    )
                    system = (
                        "Jesteś jednym z workerów w roju LLM. "
                        "Analizujesz tylko otrzymany fragment dokumentów. "
                        "Odpowiadasz po polsku, zwięźle, bez markdownowych ozdobników."
                    )
                    futures.append((idx, group, models, ex.submit(
                        _swarm_call_with_fallback,
                        prompt,
                        system,
                        provider,
                        models,
                        worker_max_tokens,
                        worker_temp,
                    )))

                for idx, group, models, fut in futures:
                    try:
                        used_model, summary = fut.result()
                        worker_outputs.append({
                            "worker_index": idx,
                            "model": used_model or (models[0] if models else ""),
                            "summary": summary,
                            "chunks": len(group),
                        })
                        yield sse("worker", {
                            "worker_index": idx,
                            "workers": len(groups),
                            "model": used_model or (models[0] if models else ""),
                            "chunks": len(group),
                            "summary": summary,
                        })
                    except Exception as e:
                        worker_outputs.append({
                            "worker_index": idx,
                            "model": models[0] if models else "",
                            "summary": f"[Błąd workera: {str(e)[:200]}]",
                            "chunks": len(group),
                        })
                        yield sse("worker", {
                            "worker_index": idx,
                            "workers": len(groups),
                            "model": models[0] if models else "",
                            "chunks": len(group),
                            "error": str(e)[:200],
                        })

            worker_outputs.sort(key=lambda x: x["worker_index"])
            reduce_msg = "Syntezuję odpowiedzi workerów…"
            if strict_incognito:
                reduce_msg = "Synteza lokalna (Ollama) — strict incognito…"
            yield sse("reduce", {
                "message": reduce_msg,
                "workers": len(worker_outputs),
                "strict_incognito": strict_incognito,
            })

            synth_models, synth_max_tokens, synth_temp, synth_provider = _swarm_synthesis_settings(
                config["key"], model_pool, strict_local=strict_incognito,
            )
            synth_llm_provider = synth_provider or provider

            synth_prompt = _swarm_synthesis_prompt(
                query + (f"\n\nKontekst rozmowy:\n{chat_context}" if chat_context else ""),
                worker_outputs,
                config["label"],
                config["key"],
            )
            synth_system = (
                "Jesteś syntezatorem roju LLM. "
                "Masz połączyć częściowe analizy w jedną odpowiedź. "
                "Trzymaj się faktów i jasno zaznaczaj niepewności."
            )
            used_model, final_answer = _swarm_call_with_fallback(
                synth_prompt,
                synth_system,
                synth_llm_provider,
                synth_models,
                synth_max_tokens,
                synth_temp,
            )

            yield sse("done", {
                "success": True,
                "mode": config["key"],
                "mode_label": config["label"],
                "provider": get_llm_provider(provider),
                "synth_provider": get_llm_provider(synth_llm_provider),
                "used_model": used_model,
                "workers": worker_outputs,
                "answer": final_answer,
                "results": results,
                "sources": len(raw_contexts),
                "mask_pii": mask_pii,
                "strict_incognito": strict_incognito,
                "pii_redactions": pii_stats,
            })
        except Exception as e:
            logger.exception("agents_swarm error")
            yield sse("error", {"error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ============================================================
# ANALIZA (raporty śledcze + forensyka Excel)
# ============================================================

_EXCEL_ERR_MARKERS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!", "#N/A", "#NA")


def _fmt_excel_value(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1e9 or (0 < abs(v) < 0.0001):
            return f"{v:.6g}"
        return f"{v:,.4f}".replace(",", " ")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)[:120]


def _compress_int_ranges(nums: list[int]) -> str:
    if not nums:
        return ""
    nums = sorted(set(nums))
    parts = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}–{prev}")
        start = prev = n
    parts.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(parts)


def _excel_goal_seek_hint(stored: float) -> tuple[bool, str, int]:
    """Heurystyka śladu Goal Seek / Solver (nadmiarowa precyzja)."""
    if not isinstance(stored, float) or stored != stored:
        return False, "", 0
    text = f"{stored:.14f}".rstrip("0")
    if "." not in text:
        return False, "", 0
    decimals = len(text.split(".")[1])
    if decimals < 7:
        return False, "", decimals
    if abs(stored) < 1e-12:
        return False, "", decimals
    return (
        True,
        f"Wartość {_fmt_excel_value(stored)} ma {decimals} miejsc po przecinku — typowe dla Goal Seek / Solver.",
        decimals,
    )


def _excel_sum_range(ws_v, range_spec: str) -> tuple[float, int] | None:
    from openpyxl.utils import range_boundaries
    spec = range_spec.strip().replace("$", "")
    if ":" not in spec:
        return None
    try:
        min_col, min_row, max_col, max_row = range_boundaries(spec)
        total = 0.0
        count = 0
        for r in ws_v.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col):
            for c in r:
                if isinstance(c.value, (int, float)):
                    total += float(c.value)
                    count += 1
        return (total, count) if count else None
    except Exception:
        return None


def _excel_eval_simple_formula(formula: str, sheet_values: dict) -> float | None:
    """Proste formuły binarne: =A1+B1, =C3-D3, =E1*E2."""
    m = re.match(r"^=([A-Z]+\d+)\s*([+\-*/])\s*([A-Z]+\d+)$", formula.strip(), re.IGNORECASE)
    if not m:
        return None
    vals = []
    for coord in (m.group(1).upper(), m.group(3).upper()):
        v = sheet_values.get(coord)
        if not isinstance(v, (int, float)):
            return None
        vals.append(float(v))
    op = m.group(2)
    if op == "+":
        return vals[0] + vals[1]
    if op == "-":
        return vals[0] - vals[1]
    if op == "*":
        return vals[0] * vals[1]
    if op == "/" and vals[1] != 0:
        return vals[0] / vals[1]
    return None


def _excel_add_finding(findings: list, summary: dict, **kw) -> None:
    findings.append(kw)
    sev = kw.get("severity", "warning")
    cat = kw.get("category", "")
    if sev == "critical":
        summary["errors"] = summary.get("errors", 0) + 1
    if cat == "override":
        summary["overrides"] = summary.get("overrides", 0) + 1
    if cat == "goal_seek":
        summary["goal_seek"] = summary.get("goal_seek", 0) + 1
    if sev == "warning":
        summary["warnings"] = summary.get("warnings", 0) + 1
    if cat == "excel_error":
        summary["excel_errors"] = summary.get("excel_errors", 0) + 1
    if cat == "external_ref":
        summary["external_refs"] = summary.get("external_refs", 0) + 1


def _excel_forensics(file_path: Path) -> dict:
    """Analiza forensyczna Excel — formuły, cache, ukrycia, odwołania zewnętrzne."""
    findings: list[dict] = []
    summary: dict = {
        "total_formulas": 0,
        "errors": 0,
        "goal_seek": 0,
        "overrides": 0,
        "warnings": 0,
        "hidden_rows": 0,
        "hidden_cols": 0,
        "hidden_sheets": 0,
        "excel_errors": 0,
        "external_refs": 0,
        "critical": 0,
        "high": 0,
    }
    by_sheet: dict[str, dict] = {}

    try:
        wb_f = openpyxl.load_workbook(file_path, data_only=False)
        wb_v = openpyxl.load_workbook(file_path, data_only=True)
    except Exception as e:
        return {"success": False, "error": str(e)}

    props = wb_f.properties
    workbook_meta = {
        "creator": getattr(props, "creator", None) or "—",
        "last_modified_by": getattr(props, "lastModifiedBy", None) or "—",
        "created": str(props.created) if getattr(props, "created", None) else "—",
        "modified": str(props.modified) if getattr(props, "modified", None) else "—",
        "sheet_count": len(wb_f.sheetnames),
        "sheet_names": list(wb_f.sheetnames),
    }

    sheet_values: dict[str, dict] = {}
    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        sheet_values[sn] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    sheet_values[sn][cell.coordinate] = cell.value

    calc_values: dict[str, float] = {}
    calc_engine = "none"
    try:
        from xlcalculator import ModelCompiler, Evaluator
        compiler = ModelCompiler()
        model = compiler.read_and_parse_archive(str(file_path))
        evaluator = Evaluator(model)
        for sn in wb_f.sheetnames:
            ws_f = wb_f[sn]
            for row in ws_f.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        key = f"{sn}!{cell.coordinate}"
                        try:
                            val = evaluator.evaluate(key)
                            if isinstance(val, (int, float)):
                                calc_values[key] = float(val)
                        except Exception:
                            pass
        if calc_values:
            calc_engine = "xlcalculator"
    except Exception:
        calc_engine = "none"

    for sn in wb_f.sheetnames:
        by_sheet[sn] = {"formulas": 0, "findings": 0, "hidden_rows": 0, "hidden_cols": 0}
        ws_f = wb_f[sn]
        state = getattr(ws_f, "sheet_state", "visible") or "visible"
        if state in ("hidden", "veryHidden"):
            summary["hidden_sheets"] += 1
            _excel_add_finding(
                findings, summary,
                severity="high",
                category="hidden_sheet",
                sheet=sn,
                cell="(arkusz)",
                formula="",
                stored=None,
                calculated=None,
                issue=f"Ukryty arkusz: {sn}",
                detail=f"Stan arkusza: {state}. Dane mogą być niewidoczne w standardowym widoku.",
                recommendation="Odsłoń arkusz w Excelu lub uwzględnij go w analizie.",
            )

    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        hidden_rows = sorted(ri for ri, rd in ws.row_dimensions.items() if rd.hidden)
        hidden_cols = sorted(
            cl for cl, cd in ws.column_dimensions.items() if cd.hidden
        )

        if hidden_rows:
            summary["hidden_rows"] += len(hidden_rows)
            by_sheet.setdefault(sn, {})["hidden_rows"] = len(hidden_rows)
            samples = []
            for ri in hidden_rows[:12]:
                cells = []
                for cell in ws[ri]:
                    if cell.value is not None and str(cell.value).strip():
                        cells.append(f"{cell.column_letter}={_fmt_excel_value(cell.value)}")
                if cells:
                    samples.append(f"w.{ri}: " + ", ".join(cells[:6]))
            _excel_add_finding(
                findings, summary,
                severity="high",
                category="hidden_rows",
                sheet=sn,
                cell=f"wiersze {_compress_int_ranges(hidden_rows)}",
                formula="",
                stored=None,
                calculated=None,
                issue=f"{len(hidden_rows)} ukrytych wierszy",
                detail=(
                    f"Zakres wierszy: {_compress_int_ranges(hidden_rows)}. "
                    + ("Próbka danych: " + "; ".join(samples[:4]) if samples else "Wiersze bez danych liczbowych/tekstowych.")
                ),
                recommendation="Sprawdź, czy ukryte wiersze nie zawierają kwot pominiętych w sumach widocznych.",
            )

        if hidden_cols:
            summary["hidden_cols"] += len(hidden_cols)
            by_sheet.setdefault(sn, {})["hidden_cols"] = len(hidden_cols)
            _excel_add_finding(
                findings, summary,
                severity="high",
                category="hidden_cols",
                sheet=sn,
                cell=f"kolumny {', '.join(hidden_cols[:15])}{'…' if len(hidden_cols) > 15 else ''}",
                formula="",
                stored=None,
                calculated=None,
                issue=f"{len(hidden_cols)} ukrytych kolumn",
                detail=f"Kolumny: {', '.join(hidden_cols[:20])}. Mogą zawierać dane wyłączone z widoku.",
                recommendation="Zweryfikuj sumy i tabele przestawne pod kątem ukrytych kolumn.",
            )

    for sn in wb_f.sheetnames:
        if sn not in wb_v.sheetnames:
            continue
        ws_f = wb_f[sn]
        ws_v = wb_v[sn]
        flat_values = sheet_values.get(sn, {})

        for row in ws_v.iter_rows():
            for cell in row:
                val = cell.value
                if isinstance(val, str) and any(m in val.upper() for m in _EXCEL_ERR_MARKERS):
                    _excel_add_finding(
                        findings, summary,
                        severity="critical",
                        category="excel_error",
                        sheet=sn,
                        cell=cell.coordinate,
                        formula="",
                        stored=val,
                        calculated=None,
                        issue=f"Błąd Excel w komórce: {val}",
                        detail="Komórka zawiera kod błędu — formuła lub odwołanie jest nieprawidłowe.",
                        recommendation="Przejdź do komórki i napraw odwołanie lub dzielenie przez zero.",
                    )

        for row in ws_f.iter_rows():
            for cell_f in row:
                formula = cell_f.value
                if not isinstance(formula, str) or not formula.startswith("="):
                    continue

                summary["total_formulas"] += 1
                by_sheet.setdefault(sn, {})["formulas"] = by_sheet[sn].get("formulas", 0) + 1
                coord = cell_f.coordinate
                stored = flat_values.get(coord)
                calc_key = f"{sn}!{coord}"
                formula_short = formula if len(formula) <= 120 else formula[:117] + "…"

                if re.search(r"\[[^\]]+\]", formula):
                    _excel_add_finding(
                        findings, summary,
                        severity="warning",
                        category="external_ref",
                        sheet=sn,
                        cell=coord,
                        formula=formula_short,
                        stored=stored,
                        calculated=None,
                        issue="Odwołanie do zewnętrznego pliku",
                        detail="Formuła odwołuje się do innego skoroszytu — wynik zależy od pliku, którego tu nie weryfikujemy.",
                        recommendation="Otwórz powiązany plik i sprawdź, czy wartości są aktualne.",
                    )

                if stored is None:
                    _excel_add_finding(
                        findings, summary,
                        severity="warning",
                        category="no_cache",
                        sheet=sn,
                        cell=coord,
                        formula=formula_short,
                        stored=None,
                        calculated=None,
                        issue="Brak zapisanej wartości (cache)",
                        detail="Excel nie zapisał wyniku formuły (np. LibreOffice, import bez przeliczenia).",
                        recommendation="Otwórz plik w Excelu, Ctrl+Alt+F9 (przelicz) i zapisz ponownie.",
                    )
                    continue

                if isinstance(stored, float):
                    is_gs, gs_detail, dec = _excel_goal_seek_hint(stored)
                    if is_gs:
                        _excel_add_finding(
                            findings, summary,
                            severity="high",
                            category="goal_seek",
                            sheet=sn,
                            cell=coord,
                            formula=formula_short,
                            stored=stored,
                            calculated=None,
                            issue=f"Podejrzenie Goal Seek ({dec} miejsc dziesiętnych)",
                            detail=gs_detail,
                            recommendation="Sprawdź historię zmian komórki i zgodność z założeniami budżetu.",
                        )

                expected: float | None = None
                if calc_key in calc_values:
                    expected = calc_values[calc_key]
                else:
                    simple = _excel_eval_simple_formula(formula, flat_values)
                    if simple is not None:
                        expected = simple
                        calc_engine = calc_engine or "simple"

                if expected is not None and isinstance(stored, (int, float)):
                    try:
                        diff = abs(float(stored) - float(expected))
                        base = max(abs(float(expected)), 1e-9)
                        rel = diff / base
                        if diff > 0.01 and rel > 0.001:
                            _excel_add_finding(
                                findings, summary,
                                severity="critical",
                                category="override",
                                sheet=sn,
                                cell=coord,
                                formula=formula_short,
                                stored=stored,
                                calculated=round(float(expected), 6),
                                diff=round(diff, 4),
                                delta_pct=round(rel * 100, 2),
                                issue="Nadpisanie / rozbieżność cache vs formuła",
                                detail=(
                                    f"W komórce zapisano {_fmt_excel_value(stored)}, "
                                    f"a z formuły wynika {_fmt_excel_value(expected)} "
                                    f"(Δ={_fmt_excel_value(diff)}, {rel*100:.2f}%)."
                                ),
                                recommendation="Porównaj z wersją archiwalną; możliwe ręczne wklejenie wartości.",
                            )
                    except (TypeError, ValueError):
                        pass

                inner = formula.split("(", 1)
                if len(inner) == 2 and inner[0].upper() == "=SUM":
                    args = inner[1].rstrip(")").replace(";", ",")
                    if ":" in args and "!" not in args.split(":")[0]:
                        range_spec = args.split(",")[0].strip()
                        sum_result = _excel_sum_range(ws_v, range_spec)
                        if sum_result is not None and isinstance(stored, (int, float)):
                            total, _ = sum_result
                            diff = abs(float(stored) - total)
                            if diff > 0.02:
                                _excel_add_finding(
                                    findings, summary,
                                    severity="critical",
                                    category="sum_mismatch",
                                    sheet=sn,
                                    cell=coord,
                                    formula=formula_short,
                                    stored=stored,
                                    calculated=round(total, 4),
                                    diff=round(diff, 4),
                                    issue="Błąd sumowania SUM",
                                    detail=(
                                        f"Suma zakresu {range_spec} = {_fmt_excel_value(total)}, "
                                        f"w komórce {_fmt_excel_value(stored)} (Δ={_fmt_excel_value(diff)})."
                                    ),
                                    recommendation="Przelicz zakres ręcznie lub sprawdź ukryte wiersze w SUM.",
                                )

                m_avg = re.match(r"^=AVERAGE\(([^)]+)\)$", formula, re.IGNORECASE)
                if m_avg and isinstance(stored, (int, float)):
                    spec = m_avg.group(1).replace(";", ",").strip()
                    if ":" in spec and "!" not in spec:
                        sum_result = _excel_sum_range(ws_v, spec)
                        if sum_result is not None:
                            total, cnt = sum_result
                            avg = total / cnt
                            diff = abs(float(stored) - avg)
                            if diff > 0.05:
                                _excel_add_finding(
                                    findings, summary,
                                    severity="high",
                                    category="average_mismatch",
                                    sheet=sn,
                                    cell=coord,
                                    formula=formula_short,
                                    stored=stored,
                                    calculated=round(avg, 4),
                                    diff=round(diff, 4),
                                    issue="Błąd średniej AVERAGE",
                                    detail=(
                                        f"Średnia z zakresu ≈ {_fmt_excel_value(avg)}, "
                                        f"zapisano {_fmt_excel_value(stored)}."
                                    ),
                                    recommendation="Sprawdź puste komórki w zakresie — AVERAGE je pomija, SUM nie.",
                                )

    wb_f.close()
    wb_v.close()

    sev_order = {"critical": 0, "high": 1, "warning": 2}
    findings.sort(key=lambda x: (sev_order.get(x.get("severity"), 3), x.get("sheet", ""), x.get("cell", "")))

    for f in findings:
        if f.get("severity") == "critical":
            summary["critical"] += 1
        elif f.get("severity") == "high":
            summary["high"] += 1
        by_sheet.setdefault(f.get("sheet", "?"), {})["findings"] = (
            by_sheet.get(f.get("sheet", "?"), {}).get("findings", 0) + 1
        )

    total_findings = len(findings)
    max_show = 100
    truncated = total_findings > max_show
    if truncated:
        findings = findings[:max_show]

    penalty = summary["critical"] * 12 + summary["high"] * 5 + summary["warnings"] * 2
    confidence_pct = max(8, min(98, 95 - penalty)) if summary["total_formulas"] else 90
    if not findings:
        confidence_pct = 92

    return {
        "success": True,
        "file": file_path.name,
        "summary": summary,
        "findings": findings,
        "findings_total": total_findings,
        "findings_truncated": truncated,
        "workbook_meta": workbook_meta,
        "by_sheet": by_sheet,
        "calc_engine": calc_engine,
        "confidence_pct": confidence_pct,
    }


@app.route('/analyze/excel', methods=['POST'])
def analyze_excel():
    data      = request.get_json()
    win_path  = data.get('win_path', '')
    wsl_path  = data.get('wsl_path', '')
    fname     = data.get('file', '')

    # Ustal ścieżkę WSL
    path = None

    # 1. Bezpośrednia ścieżka WSL
    if wsl_path and Path(wsl_path).exists():
        path = Path(wsl_path)

    # 2. Ścieżka Windows → WSL
    if not path and win_path:
        m = re.match(r'^([A-Za-z]):\\(.*)', win_path)
        if m:
            p = Path(f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92),'/')}")
            if p.exists():
                path = p

    # 3. Szukaj po nazwie rekurencyjnie (bez shella)
    if not path and fname:
        path = _find_file_by_name_safe(fname)

    if not path:
        return jsonify({"success": False, "error": f"Nie znaleziono pliku '{fname}' — zaimportuj go ponownie z opcją śledzenia ścieżek (full_path)."})

    if openpyxl is None:
        return jsonify({"success": False, "error": "Brak biblioteki openpyxl — zainstaluj zależności Python."})

    try:
        path = path.expanduser().resolve()
    except OSError:
        return jsonify({"success": False, "error": "Nieprawidłowa ścieżka"})
    if not path.is_file() or not _path_is_allowed(path):
        return jsonify({"success": False, "error": "Plik niedostępny lub poza dozwolonymi katalogami (SEARCH_ROOTS)"})

    result = _excel_forensics(path)

    # Komentarz LLM przy istotnych znaleziskach
    notable = [
        f for f in result.get("findings", [])
        if f.get("severity") in ("critical", "high")
    ]
    if notable and result.get("success"):
        s = result.get("summary", {})
        meta = result.get("workbook_meta", {})
        ctx_lines = []
        for f in notable[:12]:
            extra = ""
            if f.get("calculated") is not None:
                extra = f" | oczekiwane: {f.get('calculated')}"
            if f.get("delta_pct") is not None:
                extra += f" | Δ%={f.get('delta_pct')}"
            ctx_lines.append(
                f"[{f.get('category','?')}] {f['sheet']}!{f['cell']}: {f['issue']}{extra} — {f.get('detail','')}"
            )
        prompt = (
            f"Plik: {path.name}\n"
            f"Autor: {meta.get('creator')} | Modyfikował: {meta.get('last_modified_by')} | "
            f"Data modyfikacji: {meta.get('modified')}\n"
            f"Formuły: {s.get('total_formulas', 0)} | Krytyczne: {s.get('critical', 0)} | "
            f"Wysokie: {s.get('high', 0)} | Goal Seek: {s.get('goal_seek', 0)} | "
            f"Silnik weryfikacji: {result.get('calc_engine', 'none')}\n\n"
            f"Znaleziska ({len(notable)} istotnych, pierwsze {min(12, len(notable))}):\n"
            + "\n".join(ctx_lines)
            + "\n\nOdpowiedz po polsku (max 12 zdań):\n"
            "1) Najpoważniejsze ryzyko śledcze/finansowe\n"
            "2) Manipulacja vs błąd techniczny\n"
            "3) Co sprawdzić dalej (konkretnie)"
        )
        system = (
            "Jesteś biegłym rewidentem śledczym. Analizujesz anomalie w Excelu. "
            "Pisz konkretnie, bez ogólników."
        )
        try:
            out = call_llm(prompt, system=system, stream=False, max_tokens=900, temperature=0.2)
            if isinstance(out, dict):
                result["llm_comment"] = out.get("response", str(out))
            else:
                result["llm_comment"] = str(out)[:4000]
        except Exception as e:
            result["llm_comment"] = f"(Błąd LLM: {e})"

    return jsonify(result)


# ============================================================

# ============================================================
# KOŃCOWA RECENZJA KODU CAŁEJ APLIKACJI
# Data: 01.06.2026
# Wykonane na prośbę użytkownika
# ============================================================
#
# Przegląd i poprawki wykonane w tej sesji:
#
# 1. Dodano nową zakładkę "Raporty śledcze" z 8 gotowymi szablonami
#    demonstracyjnymi możliwości analitycznych programu (umowy, przepływy,
#    osoby decyzyjne, przetargi, ryzyka, własność itp.).
#
# 2. Wprowadzono podstawowe przetwarzanie partiami w funkcji build_network()
#    (partie po 5 dokumentów) + informacja o liczbie przetworzonych partii.
#
# 3. Ulepszono komunikaty błędów w Sieci powiązań.
#
# 4. Dodano/ulepszono:
#    - Globalny licznik tokenów w topbarze
#    - Lepsze liczniki tokenów przy odpowiedziach LLM
#    - Bardziej widoczne paski postępu przy imporcie/wektoryzacji
#    - Toast po udanej aktualizacji z GitHub
#
# 5. Poprawiono skrypty pomocnicze (start, stop, restart-app.sh, install-user-service.sh)
#    z kolorowymi komunikatami i opcjonalnym git pull przy starcie.
#
# 6. Dodano zarządzanie App API Key w ustawieniach UI.
#
# 7. Zmieniono domyślny provider LLM na OpenRouter (oprócz importu/embeddings).
#
# 8. Dodano i udoskonalono system self-update w modalu diagnostycznym.
#
# 9. Zaktualizowano dokumentację (README.md, AGENTS.md, IMPROVEMENT_PLAN.md).
#
# 10. Przeprowadzono końcową pełną recenzję kodu i naprawiono pozostałości
#     po wcześniejszych agresywnych refaktoryzacjach.
#
# Stan na koniec sesji: aplikacja jest znacznie bardziej przyjazna,
# stabilna przy ciężkich analizach i lepiej przygotowana do codziennego
# użytku w trybie deweloperskim / near-production.
#
# Data zakończenia recenzji: 01 czerwca 2026
# ================================================================

# 2026-06-01 — Opcja A dla problemu z SQL config JSON parse errors (na żądanie użytkownika "A"):

# 2026-06-02 — Security remediation (z raportu przegląd PR #9–#18):
#   - Przywrócono _is_sql_safe, _validate_sql_table_refs, _get_sql_conn i powiązane
#     helpery SQL (usunięte w 3837d5a/ea77923). Bez nich hybrid_stream() z
#     LLM-generated SQL był narażony na injection (teraz fail-closed + walidacja).
#   - Dodano guard PYMSSQL_AVAILABLE w ścieżce hybrydowej.
#   - Drobne poprawki XSS w network error/evidence panel (escapeHtml).
# Najwyższy risk z audytu (latent SQL injection) — zaadresowany.
# - Całkowicie odłączono sqlLoadSavedConn() z window.onload oraz showTab w index.html
# - Zakładka SQL (#navItemSql) jest domyślnie ukryta (display:none)
# - ensureSqlTabVisibility() pokazuje ją automatycznie TYLKO gdy /sql/config ma has_config:true
# - Dodano przycisk w modalu diagnostycznym do ręcznego włączenia (pierwsza konfiguracja)
# - Dodano pełne stub endpointy dla wszystkich /sql/* wywoływanych z UI
# - Zaimplementowano brakujący /health (używa _check_* helpers + sql_available/configured)
#   → kolorowe kropki, staty topbara, bogaty modal diagnostyczny działają poprawnie
# - Dodano PYMSSQL_AVAILABLE + _load_sql_config
# Efekt: błąd "Unexpected token '<'" nigdy nie pojawia się dla użytkowników bez SQL.
# ================================================================

# ============================================================
# MODUŁ SQL — MS SQL Server + SQLite (Text-to-SQL + wektoryzacja do Qdrant)
# ============================================================

@app.route('/sql/config', methods=['GET'])
def sql_config_get():
    """Zwraca zapisaną konfigurację SQL lub informację, że jej nie ma."""
    try:
        cfg = _load_sql_config()
        if cfg and _sql_conn_configured(cfg):
            safe = _redact_sql_config(cfg)
            return jsonify({
                "success": True,
                "has_config": True,
                "config": {
                    "type": safe.get("type", "mssql"),
                    "server": safe.get("server", ""),
                    "port": safe.get("port", 1433),
                    "database": safe.get("database", ""),
                    "user": safe.get("user", ""),
                    "db_path": safe.get("db_path", ""),
                },
            })
        return jsonify({"success": True, "has_config": False})
    except Exception as e:
        return jsonify({"success": False, "has_config": False, "error": str(e)})


@app.route('/sql/config', methods=['POST'])
def sql_config_post():
    """Zapisuje konfigurację SQL (dla UI)."""
    try:
        data = request.get_json() or {}
        existing = _load_sql_config()
        for k in ["type", "server", "port", "database", "user", "db_path"]:
            if k in data:
                existing[k] = data[k]
        if data.get("password"):
            existing["password"] = data["password"]
        _save_sql_config(existing)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def _sql_text_columns(conn, table: str, dialect: str) -> list[str]:
    """Kolumny tekstowe/numeryczne — zwykły kursor (pymssql as_dict nie obsługuje metadanych)."""
    cur = conn.cursor()
    try:
        if dialect == "sqlite":
            cur.execute(f'PRAGMA table_info("{table}")')
            text_types = ("TEXT", "CHAR", "CLOB", "VARCHAR", "NVARCHAR", "INT", "INTEGER", "REAL", "FLOAT", "NUMERIC", "DECIMAL", "DATE", "DATETIME")
            return [r[1] for r in cur.fetchall() if any(t in (r[2] or "").upper() for t in text_types)]
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME=%s AND DATA_TYPE IN
            ('varchar','nvarchar','text','ntext','char','nchar','int','decimal','numeric','float','date','datetime')
            ORDER BY ORDINAL_POSITION
        """, (table,))
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()


def _sql_list_tables(conn, dialect: str) -> list[dict]:
    cur = conn.cursor()
    try:
        if dialect == "sqlite":
            cur.execute("""
                SELECT name, type FROM sqlite_master
                WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
            return [{"name": r[0], "type": r[1].upper()} for r in cur.fetchall()]
        cur.execute("""
            SELECT TABLE_NAME, TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE IN ('BASE TABLE','VIEW')
            ORDER BY TABLE_NAME
        """)
        return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
    finally:
        cur.close()


def _sql_table_count(conn, table: str, dialect: str) -> int:
    qtable = _sql_quote_ident(table, dialect)
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {qtable}")
        row = cur.fetchone()
        return int(row[0])
    finally:
        cur.close()


def _sql_fetch_table_rows(cur, table: str, columns: list[str], dialect: str, batch: int):
    col_list = ", ".join(_sql_quote_ident(c, dialect) for c in columns)
    qtable = _sql_quote_ident(table, dialect)
    cur.execute(f"SELECT {col_list} FROM {qtable}")
    while True:
        batch_rows = cur.fetchmany(batch)
        if not batch_rows:
            break
        for row in batch_rows:
            if isinstance(row, dict):
                yield row
            elif hasattr(row, "keys"):
                yield {k: row[k] for k in row.keys()}
            else:
                yield {columns[i]: row[i] for i in range(len(columns))}


def _sql_row_to_text(table: str, row: dict, label_col: str = "") -> str:
    label = str(row.get(label_col, "")) if label_col else ""
    parts = [f"{k}: {v}" for k, v in row.items() if v is not None and str(v).strip()]
    prefix = f"[{table}] {label}\n" if label else f"[{table}]\n"
    return prefix + " | ".join(parts)


@app.route('/sql/test', methods=['POST'])
def sql_test():
    """Test połączenia z bazą SQL (MS SQL Server lub SQLite)."""
    cfg = request.get_json() or {}
    try:
        if _sql_dialect(cfg) == "sqlite" and not _sql_conn_configured(cfg):
            return jsonify({
                "success": False,
                "error": "Podaj ścieżkę do pliku .db (np. /mnt/g/dane/eksport.db)",
            })
        _require_sql_backend(cfg)
        dialect = _sql_dialect(cfg)
        conn = _get_sql_conn(cfg, readonly=True)
        cur = conn.cursor()
        if dialect == "sqlite":
            cur.execute("SELECT sqlite_version()")
            version = f"SQLite {cur.fetchone()[0]}"
        else:
            cur.execute("SELECT @@VERSION")
            version = cur.fetchone()[0]
        tables = _sql_list_tables(conn, dialect)
        conn.close()
        return jsonify({"success": True, "version": version[:120], "tables": tables, "dialect": dialect})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/sql/schema', methods=['POST'])
def sql_schema():
    """Pobiera schemat wybranej tabeli."""
    data = request.get_json() or {}
    cfg = data.get("conn", {})
    table = data.get("table", "")
    if not _validate_sql_table_name(table):
        return jsonify({"success": False, "error": "Nieprawidłowa nazwa tabeli"})
    try:
        _require_sql_backend(cfg)
        dialect = _sql_dialect(cfg)
        conn = _get_sql_conn(cfg, readonly=True)
        cur = conn.cursor()
        if dialect == "sqlite":
            cur.execute(f'PRAGMA table_info("{table}")')
            cols = [
                {"name": r[1], "type": r[2], "max_len": None, "nullable": "YES" if r[3] == 0 else "NO"}
                for r in cur.fetchall()
            ]
        else:
            cur.execute("""
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
            """, (table,))
            cols = [
                {"name": r[0], "type": r[1], "max_len": r[2], "nullable": r[3]}
                for r in cur.fetchall()
            ]
        row_count = _sql_table_count(conn, table, dialect)
        conn.close()
        return jsonify({"success": True, "table": table, "columns": cols, "row_count": row_count})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/sql/ask', methods=['POST'])
def sql_ask():
    """Text-to-SQL: pytanie po polsku → LLM → SELECT → wyniki + interpretacja."""
    data = request.get_json() or {}
    cfg = data.get("conn", {})
    question = (data.get("question") or "").strip()
    schema = data.get("schema") or ""

    if not question:
        return jsonify({"success": False, "error": "Brak pytania"})

    try:
        _require_sql_backend(cfg)
        result = _run_text_to_sql_pipeline(
            cfg, question, schema, fetch_limit=500, with_chart=True,
        )
        if not result.get("success"):
            return jsonify(result)

        sql_query = result["sql"]
        result_rows = result["rows"]
        preview = "\n".join([
            ", ".join([f"{k}={v}" for k, v in r.items()])
            for r in result_rows[:10]
        ])
        prompt_interp = (
            f"Pytanie użytkownika: {question}\n"
            f"Zapytanie SQL: {sql_query}\n"
            f"Liczba wyników: {len(result_rows)}\n"
            f"Przykładowe wyniki:\n{preview}\n\n"
            "Odpowiedz po polsku: co wynika z tych danych? Podaj konkretne liczby i wnioski."
        )
        interp = call_llm(
            prompt_interp,
            "Jesteś analitykiem danych. Interpretujesz wyniki SQL po polsku.",
            stream=False,
            max_tokens=1500,
            temperature=0.2,
        )
        result["interpretation"] = _llm_response_text(interp)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "sql": ""})


@app.route('/sql/write', methods=['POST'])
def sql_write():
    """Generuje i wykonuje INSERT/UPDATE/DELETE po potwierdzeniu użytkownika (tylko MS SQL)."""
    data = request.get_json() or {}
    cfg = data.get("conn", {})
    if _sql_dialect(cfg) == "sqlite":
        return jsonify({
            "success": False,
            "error": "Tryb zapisu niedostępny dla SQLite — baza jest otwierana tylko do odczytu (mode=ro).",
        })
    if not PYMSSQL_AVAILABLE:
        return jsonify({"success": False, "error": "Moduł pymssql nie jest zainstalowany"})
    question = (data.get("question") or "").strip()
    schema = data.get("schema") or ""
    confirmed = bool(data.get("confirmed", False))
    sql_query = (data.get("sql") or "").strip()

    if not question and not sql_query:
        return jsonify({"success": False, "error": "Brak pytania lub zapytania SQL"})

    try:
        if not sql_query:
            effective_schema, _known = _resolve_sql_schema(cfg, schema)
            system_sql = (
                "Jesteś ekspertem T-SQL (MS SQL Server). "
                "Generujesz zapytania modyfikujące dane: INSERT, UPDATE, DELETE. "
                "Odpowiadasz WYŁĄCZNIE samym SQL — bez wyjaśnień, bez markdown."
            )
            prompt_sql = (
                f"SCHEMAT BAZY:\n{effective_schema}\n\n"
                f"ZADANIE: {question}\n\n"
                "Wygeneruj zapytanie T-SQL (INSERT/UPDATE/DELETE):"
            )
            gen = call_llm(prompt_sql, system_sql, stream=False, max_tokens=2000, temperature=0.1)
            sql_query = _clean_llm_sql(_llm_response_text(gen))

        first_word = sql_query.split()[0].upper() if sql_query.split() else ""
        allowed_write = ("INSERT", "UPDATE", "DELETE", "MERGE")
        if first_word not in allowed_write and first_word not in ("SELECT", "WITH"):
            return jsonify({"success": False, "error": f"Nieznane polecenie: {first_word}", "sql": sql_query})

        if not confirmed:
            impact_info = ""
            if first_word in ("UPDATE", "DELETE"):
                try:
                    conn_check = _get_sql_conn(cfg)
                    cur_check = conn_check.cursor()
                    if first_word == "UPDATE":
                        where_part = re.search(r"\bWHERE\b(.+?)(?:$)", sql_query, re.IGNORECASE | re.DOTALL)
                        table_part = re.search(r"UPDATE\s+\[?(\w+)\]?", sql_query, re.IGNORECASE)
                        if where_part and table_part:
                            count_sql = f"SELECT COUNT(*) FROM [{table_part.group(1)}] WHERE {where_part.group(1)}"
                            cur_check.execute(count_sql)
                            impact_info = f"Zmieni {cur_check.fetchone()[0]} wierszy"
                    elif first_word == "DELETE":
                        count_sql = sql_query.replace("DELETE", "SELECT COUNT(*)", 1)
                        cur_check.execute(count_sql)
                        impact_info = f"Usunie {cur_check.fetchone()[0]} wierszy"
                    conn_check.close()
                except Exception:
                    impact_info = "Nie udało się oszacować wpływu"

            return jsonify({
                "success": True,
                "preview": True,
                "sql": sql_query,
                "impact": impact_info,
                "first_word": first_word,
            })

        is_safe, safe_err = _is_sql_safe(sql_query, allowed_write)
        if not is_safe:
            return jsonify({"success": False, "error": safe_err or "Niedozwolone zapytanie SQL", "sql": sql_query})

        conn = _get_sql_conn(cfg)
        cur = conn.cursor()
        cur.execute(sql_query)
        rows_affected = cur.rowcount
        conn.commit()
        conn.close()
        logger.info("[SQL WRITE] %s | rows=%s | %s", first_word, rows_affected, sql_query[:100])

        return jsonify({
            "success": True,
            "executed": True,
            "sql": sql_query,
            "rows_affected": rows_affected,
            "message": f"Wykonano. Zmieniono/dodano {rows_affected} wierszy.",
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "sql": sql_query})


@app.route('/sql/vectorize', methods=['POST'])
def sql_vectorize():
    """Wektoryzuje dane z tabeli SQL do Qdrant (SSE)."""
    data = request.get_json() or {}
    cfg = data.get("conn", {})
    table = data.get("table", "")
    cols = data.get("columns") or []
    label_col = (data.get("label_col") or "").strip()

    if not _validate_sql_table_name(table):
        return jsonify({"success": False, "error": "Nieprawidłowa nazwa tabeli"}), 400

    def generate():
        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        task_id = None
        task_error = None
        try:
            _require_sql_backend(cfg)
            dialect = _sql_dialect(cfg)
            _ensure_collection_exists()

            ok, task_id, block_msg = _heavy_task_try_start("sql_vectorize", table)
            if not ok:
                yield sse("error", {"error": block_msg, "blocked": True})
                return

            conn = _get_sql_conn(cfg, readonly=True)
            data_cur = conn.cursor(as_dict=True) if dialect == "mssql" else conn.cursor()

            if cols:
                active_cols = list(cols)
            else:
                active_cols = _sql_text_columns(conn, table, dialect)
            if not active_cols:
                yield sse("error", {"error": f"Brak kolumn do wektoryzacji w tabeli {table}"})
                return

            total = _sql_table_count(conn, table, dialect)
            yield sse("start", {"start": True, "total": total, "table": table, "columns": active_cols, "task_id": task_id})

            qdrant = get_qdrant_client()
            done = 0
            rows_buf: list[str] = []
            BATCH = 50

            for row in _sql_fetch_table_rows(data_cur, table, active_cols, dialect, BATCH):
                rows_buf.append(_sql_row_to_text(table, row, label_col))
                if len(rows_buf) < BATCH:
                    continue

                vectors = get_embeddings_batch(rows_buf, batch_size=6)
                points = []
                for txt, vec in zip(rows_buf, vectors):
                    if not vec or not any(v != 0.0 for v in vec):
                        continue
                    cid = hashlib.md5(txt.encode("utf-8", errors="replace")).hexdigest()
                    points.append(PointStruct(
                        id=cid,
                        vector=vec,
                        payload={"file": f"[SQL] {table}", "text": txt, "full_path": "", "source": "sql", "sql_table": table},
                    ))
                if points:
                    qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                done += len(rows_buf)
                rows_buf = []
                pct = round(done / total * 100) if total else 0
                _heavy_task_update(task_id, progress=done, total=total, message=table)
                yield sse("progress", {"progress": True, "done": done, "total": total, "pct": pct})

            if rows_buf:
                vectors = get_embeddings_batch(rows_buf, batch_size=6)
                points = []
                for txt, vec in zip(rows_buf, vectors):
                    if not vec or not any(v != 0.0 for v in vec):
                        continue
                    cid = hashlib.md5(txt.encode("utf-8", errors="replace")).hexdigest()
                    points.append(PointStruct(
                        id=cid,
                        vector=vec,
                        payload={"file": f"[SQL] {table}", "text": txt, "full_path": "", "source": "sql", "sql_table": table},
                    ))
                if points:
                    qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                done += len(rows_buf)
                pct = round(done / total * 100) if total else 0
                _heavy_task_update(task_id, progress=done, total=total, message=table)
                yield sse("progress", {"progress": True, "done": done, "total": total, "pct": pct})

            conn.close()
            _docs_cache["data"] = None
            yield sse("done", {"done": True, "msg": f"Zwektoryzowano {done} wierszy z tabeli [{table}]"})
        except Exception as e:
            task_error = str(e)
            logger.exception("sql_vectorize error")
            yield sse("error", {"error": task_error})
        finally:
            _heavy_task_finish(task_id, error=task_error)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route('/sql/vectorize-all', methods=['POST'])
def sql_vectorize_all():
    """Wektoryzuje wszystkie tabele SQL do Qdrant (SSE)."""
    data = request.get_json() or {}
    cfg = data.get("conn", {})

    def generate():
        def sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        task_id = None
        task_error = None
        try:
            _require_sql_backend(cfg)
            dialect = _sql_dialect(cfg)
            _ensure_collection_exists()

            ok, task_id, block_msg = _heavy_task_try_start("sql_vectorize_all", "all_tables")
            if not ok:
                yield sse("error", {"error": block_msg, "blocked": True})
                return

            conn = _get_sql_conn(cfg, readonly=True)
            data_cur = conn.cursor(as_dict=True) if dialect == "mssql" else conn.cursor()

            tables = [t["name"] for t in _sql_list_tables(conn, dialect)]
            yield sse("start", {"start": True, "total_tables": len(tables), "tables": tables, "task_id": task_id})

            qdrant = get_qdrant_client()
            total_rows = 0

            for table_idx, table in enumerate(tables):
                _heavy_task_update(task_id, progress=table_idx + 1, total=len(tables), message=table)
                try:
                    yield sse("table_start", {
                        "table_start": True,
                        "table": table,
                        "table_idx": table_idx,
                        "total_tables": len(tables),
                    })

                    active_cols = _sql_text_columns(conn, table, dialect)
                    if not active_cols:
                        yield sse("table_done", {"table_done": True, "table": table, "rows": 0, "table_idx": table_idx})
                        continue

                    table_total = _sql_table_count(conn, table, dialect)
                    if table_total == 0:
                        yield sse("table_done", {"table_done": True, "table": table, "rows": 0, "table_idx": table_idx})
                        continue

                    table_done = 0
                    rows_buf: list[str] = []
                    BATCH = 50

                    for row in _sql_fetch_table_rows(data_cur, table, active_cols, dialect, BATCH):
                        rows_buf.append(_sql_row_to_text(table, row))
                        if len(rows_buf) < BATCH:
                            continue

                        vectors = get_embeddings_batch(rows_buf, batch_size=6)
                        points = []
                        for txt, vec in zip(rows_buf, vectors):
                            if not vec or not any(v != 0.0 for v in vec):
                                continue
                            cid = hashlib.md5(txt.encode("utf-8", errors="replace")).hexdigest()
                            points.append(PointStruct(
                                id=cid,
                                vector=vec,
                                payload={"file": f"[SQL] {table}", "text": txt, "full_path": "", "source": "sql", "sql_table": table},
                            ))
                        if points:
                            qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)

                        table_done += len(rows_buf)
                        total_rows += len(rows_buf)
                        rows_buf = []
                        pct = round(table_done / table_total * 100) if table_total else 0
                        yield sse("progress", {
                            "progress": True,
                            "table": table,
                            "done": table_done,
                            "total": table_total,
                            "pct": pct,
                        })

                    if rows_buf:
                        vectors = get_embeddings_batch(rows_buf, batch_size=6)
                        points = []
                        for txt, vec in zip(rows_buf, vectors):
                            if not vec or not any(v != 0.0 for v in vec):
                                continue
                            cid = hashlib.md5(txt.encode("utf-8", errors="replace")).hexdigest()
                            points.append(PointStruct(
                                id=cid,
                                vector=vec,
                                payload={"file": f"[SQL] {table}", "text": txt, "full_path": "", "source": "sql", "sql_table": table},
                            ))
                        if points:
                            qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                        table_done += len(rows_buf)
                        total_rows += len(rows_buf)

                    yield sse("table_done", {
                        "table_done": True,
                        "table": table,
                        "rows": table_done,
                        "table_idx": table_idx,
                    })
                except Exception as e:
                    yield sse("table_error", {"table_error": True, "table": table, "error": str(e)})

            conn.close()
            _docs_cache["data"] = None
            yield sse("done", {
                "done": True,
                "total_rows": total_rows,
                "total_tables": len(tables),
                "msg": f"Zwektoryzowano {total_rows} wierszy z {len(tables)} tabel",
            })
        except Exception as e:
            task_error = str(e)
            logger.exception("sql_vectorize_all error")
            yield sse("error", {"error": task_error})
        finally:
            _heavy_task_finish(task_id, error=task_error)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# /health — endpoint dla diagnostyki (kolorowe kropki, modal startowy,
# self-update, statusy). Używa istniejących _check_* helpers.
# ============================================================


@app.route('/health', methods=['GET'])
def health():
    """Zwraca bogaty status systemu dla UI (dashboard + modal diagnostyczny)."""
    light = request.args.get("light", "0") == "1"
    try:
        q = _check_qdrant_health()
        ocr = _ocr_health_status() if not light else {"available": False}
        parsers = _file_parsers_health() if not light else {}
        ollama_h = {"ok": True} if light else _check_ollama_health()
        or_h = {"ok": True} if light else _check_openrouter_health()
        groq_h = {"ok": True} if light else _check_groq_health()

        # LLM status — według aktywnego providera
        llm_ok = False
        llm_detail = ""
        if light:
            llm_ok = True
            llm_detail = "light"
        elif DEFAULT_LLM_PROVIDER == "groq":
            llm_ok = groq_h.get("ok", False)
            llm_detail = groq_h.get("error") or "Groq"
        elif DEFAULT_LLM_PROVIDER == "openrouter":
            llm_ok = or_h.get("ok", False)
            llm_detail = or_h.get("error") or "OpenRouter"
        else:
            llm_ok = ollama_h.get("ok", False)
            llm_detail = ollama_h.get("error") or "Ollama"

        # SQL
        sql_cfg = _load_sql_config()
        sql_configured = _sql_conn_configured(sql_cfg)
        sql_status = "ok" if (
            sql_configured and (_sql_dialect(sql_cfg) == "sqlite" or PYMSSQL_AVAILABLE)
        ) else ("warn" if sql_configured else "absent")

        # Wektory / kolekcja
        vectors = 0
        coll_name = ACTIVE_COLLECTION
        try:
            if q.get("ok") and q.get("active_collection_exists"):
                vectors = q.get("points_in_active", 0)
        except Exception:
            pass

        # Ogólny status
        if not q.get("ok", False):
            overall = "degraded"
        elif llm_ok:
            overall = "ok"
        else:
            overall = "warn"

        # Wersja (z git describe lub stałej)
        try:
            ver = _get_app_version()
        except Exception:
            ver = "dev"

        return jsonify({
            "success": True,
            "overall": overall,
            "version": ver,
            "llm_model": (
                GROQ_MODEL if DEFAULT_LLM_PROVIDER == "groq"
                else OPENROUTER_MODEL if DEFAULT_LLM_PROVIDER == "openrouter"
                else LLM_MODEL
            ),
            "timestamp": int(time.time()),

            # Połączenia (dla kropek i tabeli w modalu)
            "qdrant": q,
            "qdrant_status": "ok" if q.get("ok") else "error",
            "llm": {
                "ok": llm_ok,
                "detail": llm_detail,
                "kind": groq_h.get("kind") if DEFAULT_LLM_PROVIDER == "groq" else (
                    or_h.get("kind") if DEFAULT_LLM_PROVIDER == "openrouter" else (
                        ollama_h.get("kind") if DEFAULT_LLM_PROVIDER == "ollama" else None
                    )
                ),
            },
            "llm_status": "ok" if llm_ok else "warn",
            "llm_error": llm_detail if not llm_ok else None,
            "llm_provider": DEFAULT_LLM_PROVIDER,
            "embedding": {
                "ok": ollama_h.get("ok", False),  # embeddingi idą przez Ollama
                "model": "nomic-embed-text"
            },
            "ocr": ocr,
            "ocr_available": ocr.get("available", False),
            "file_parsers": parsers,

            # SQL (opcjonalny)
            "sql_available": PYMSSQL_AVAILABLE,
            "sql_configured": sql_configured,
            "sql_status": sql_status,

            # Aktywna kolekcja + wektory (dla topbara)
            "active_collection": {
                "name": coll_name,
                "points": vectors
            },
            "vectors_count": vectors,

            # Inne przydatne dla UI
            "provider": DEFAULT_LLM_PROVIDER,
            "app_api_key_set": bool(APP_API_KEY),
            "pools": {
                "groq": GROQ_KEY_POOL.stats(),
                "openrouter": OPENROUTER_KEY_POOL.stats(),
                "ollama": OLLAMA_URL_POOL.stats(),
            },
        })
    except Exception as e:
        logger.exception("health endpoint error")
        return jsonify({
            "success": False,
            "overall": "error",
            "error": str(e)[:200]
        }), 500


@app.route('/api/update/status', methods=['GET'])
def api_update_status():
    """Zwraca status aktualizacji — aktualna wersja, najnowsza, changelog."""
    if not _localhost_only():
        return jsonify({"success": False, "error": "Dostępne tylko z localhost"}), 403
    try:
        local_tag = _get_local_latest_tag()
        remote_tag = _get_remote_latest_tag()
        release_info = _get_latest_github_release() or {}

        if release_info.get("tag_name"):
            remote_tag = release_info["tag_name"]

        def _version_key(v: str) -> tuple:
            try:
                return tuple(int(x) for x in v.lstrip("v").split("."))
            except Exception:
                return (0, 0)

        update_available = bool(
            remote_tag and local_tag and _version_key(remote_tag) > _version_key(local_tag)
        )

        return jsonify({
            "success": True,
            "local_version": local_tag,
            "remote_version": remote_tag or local_tag,
            "current_version": local_tag,
            "latest_version": remote_tag or local_tag,
            "update_available": update_available,
            "release": release_info,
            "changelog": release_info.get("body", ""),
            "release_url": release_info.get("html_url"),
            "message": "Nowa wersja dostępna" if update_available else "Jesteś na najnowszej wersji",
        })
    except Exception as e:
        logger.exception("api_update_status error")
        return jsonify({"success": False, "error": str(e)[:200]}), 500


@app.route('/api/update/pull', methods=['POST'])
def api_update_pull():
    """Pobiera najnowszy kod z GitHub (git pull origin master)."""
    if not _localhost_only():
        return jsonify({"success": False, "error": "Dostępne tylko z localhost"}), 403
    try:
        result = _git_pull()
        ok = result.get("success", False)
        msg = result.get("stdout") or result.get("stderr") or (
            "Kod zaktualizowany. Zalecany restart aplikacji." if ok else "Błąd podczas aktualizacji."
        )
        return jsonify({
            "success": ok,
            "message": msg,
            "output": result.get("stdout", ""),
            "error": result.get("stderr", "") if not ok else "",
            "details": result,
        })
    except Exception as e:
        logger.exception("api_update_pull error")
        return jsonify({"success": False, "error": str(e)[:200]}), 500


@app.route('/api/update/restart', methods=['POST'])
def api_update_restart():
    """Restartuje aplikację (poprzez systemd --user service)."""
    if not _localhost_only():
        return jsonify({"success": False, "error": "Dostępne tylko z localhost"}), 403
    try:
        result = _try_restart_service()
        return jsonify({
            "success": result.get("success", False),
            "message": result.get("message", "Restart initiated"),
            "method": result.get("method", "unknown"),
        })
    except Exception as e:
        logger.exception("api_update_restart error")
        return jsonify({"success": False, "error": str(e)[:200]}), 500


def _localhost_only() -> bool:
    """True gdy żądanie pochodzi z localhost (self-update / service API)."""
    addr = (request.remote_addr or "").strip()
    if addr in ("127.0.0.1", "::1", "localhost"):
        return True
    if TRUST_PROXY:
        # Trust forwarded headers only when explicitly enabled.
        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd in ("127.0.0.1", "::1", "localhost")
    return False


def _systemd_service_state() -> str:
    """Stan usługi ai_analiza — najpierw user service, potem systemowa."""
    import shutil
    if not shutil.which("systemctl"):
        return "dev_mode"
    for cmd in (
        ["systemctl", "--user", "is-active", "ai_analiza"],
        ["systemctl", "is-active", "ai_analiza"],
    ):
        try:
            st = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            val = (st.stdout or "").strip()
            if val:
                return val
        except Exception:
            pass
    if os.environ.get("INVOCATION_ID"):
        return "active"
    return "unknown"


def _systemd_service_logs(limit: int = 30) -> str:
    for cmd in (
        ["journalctl", "--user", "-u", "ai_analiza", "-n", str(limit), "--no-pager", "--output=short"],
        ["journalctl", "-u", "ai_analiza", "-n", str(limit), "--no-pager", "--output=short"],
    ):
        try:
            logs = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if logs.stdout:
                return logs.stdout
        except Exception:
            pass
    return ""


@app.route('/api/service/status', methods=['GET'])
def service_status():
    """Status usługi systemd i ostatnie logi — tylko localhost."""
    if not _localhost_only():
        return jsonify({"success": False, "error": "Dostępne tylko z localhost"}), 403

    return jsonify({
        "success": True,
        "is_systemd": bool(os.environ.get("INVOCATION_ID")),
        "active": _systemd_service_state(),
        "logs": _systemd_service_logs(30),
    })


@app.route('/api/service/restart', methods=['POST'])
def service_restart():
    """Restart usługi ai_analiza — tylko localhost."""
    if not _localhost_only():
        return jsonify({"success": False, "error": "Dostępne tylko z localhost"}), 403

    def _do_restart():
        time.sleep(0.6)
        try:
            import shutil
            if shutil.which("systemctl"):
                for cmd in (
                    ["systemctl", "--user", "restart", "ai_analiza"],
                    ["systemctl", "restart", "ai_analiza"],
                ):
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                        if result.returncode == 0:
                            return
                    except Exception:
                        continue
            os._exit(0)
        except Exception:
            os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"success": True, "msg": "Restart zlecony"})
