#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import urllib.request
import re
import os
import hashlib
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file
import io
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

app = Flask(__name__)

QDRANT_URL        = os.environ["QDRANT_URL"]
QDRANT_KEY        = os.environ["QDRANT_KEY"]
ACTIVE_COLLECTION = os.environ.get("ACTIVE_COLLECTION", "mzk_documents")
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

# ---- Cache embeddingów (SHA256 → wektor, plik JSON) ----
_EMBED_CACHE_PATH = Path(__file__).parent / "embedding_cache.json"
_embed_cache: dict = {}
_embed_cache_dirty = 0

def _load_embed_cache():
    global _embed_cache
    if _EMBED_CACHE_PATH.exists():
        try:
            _embed_cache = json.loads(_EMBED_CACHE_PATH.read_text())
            print(f"Załadowano cache embeddingów: {len(_embed_cache)} wpisów")
        except Exception:
            _embed_cache = {}

def _save_embed_cache():
    try:
        _EMBED_CACHE_PATH.write_text(json.dumps(_embed_cache))
    except Exception as e:
        print(f"⚠️ Błąd zapisu cache: {e}")

_load_embed_cache()

def get_embedding(text: str) -> list:
    import hashlib as _hl
    key = _hl.sha256(text[:1500].encode('utf-8', errors='replace')).hexdigest()
    if key in _embed_cache:
        return _embed_cache[key]

    url = OLLAMA_URL + "/api/embeddings"
    payload = {"model": "nomic-embed-text", "prompt": text[:1500]}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            vec = json.loads(r.read().decode("utf-8"))["embedding"]
        _embed_cache[key] = vec
        global _embed_cache_dirty
        _embed_cache_dirty += 1
        if _embed_cache_dirty >= 50:   # zapisuj co 50 nowych wpisów
            _save_embed_cache()
            _embed_cache_dirty = 0
        return vec
    except Exception as e:
        print(f"⚠️ Ollama Embedding Error: {e}")
        return [0.0] * 768

def get_embeddings_batch(texts: list, batch_size: int = 8) -> list:
    """Batch embeddings — wysyła kilka tekstów równolegle, ~5x szybszy import."""
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
            except Exception as e:
                print(f"⚠️ Batch embedding error: {e}")
                results[futures[fut]] = [0.0] * 768

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
        "label": "Detektyw — anomalie",
        "system": (
            "Jesteś analitykiem śledczym specjalizującym się w wykrywaniu nadużyć finansowych i korupcji. "
            "Szukasz ANOMALII, NIESPÓJNOŚCI i PODEJRZANYCH WZORCÓW między dokumentami. "
            "Porównaj dane z różnych źródeł. Wskazuj konkretne rozbieżności: różne kwoty dla tej samej pozycji, "
            "sprzeczne daty, podejrzane zbieżności, brakujące dokumenty. "
            "Każde znalezisko oznacz: [ANOMALIA], [NIESPÓJNOŚĆ], [PODEJRZANE], [WYMAGA SPRAWDZENIA]. "
            "Odpowiadaj wyłącznie po polsku."
        ),
        "prompt_suffix": "Wskaż anomalie, niespójności i miejsca wymagające sprawdzenia:"
    },
    "legal": {
        "label": "Prawny — przepisy",
        "system": (
            "Jesteś prawnikiem specjalizującym się w prawie zamówień publicznych i spółkach komunalnych. "
            "Identyfikuj każde odwołanie do ustaw, rozporządzeń i przepisów w dokumentach. "
            "Dla każdego przepisu oceń: (1) czy jest aktualny na dzień dokumentu, "
            "(2) czy rzeczywiście dotyczy MZK / transportu publicznego / spółek komunalnych w Polsce, "
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

def generate_answer(query: str, contexts: list, mode: str = "normal") -> str:
    url = OLLAMA_URL + "/api/generate"
    context_str = "\n\n".join([f"[Dokument: {c['file']}]: {c['text'][:1400]}" for c in contexts])
    cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])
    prompt = f"KONTEKST Z DOKUMENTÓW:\n{context_str}\n\nZAPYTANIE: {query}\n\n{cfg['prompt_suffix']}"
    payload = {"model": "llama3", "prompt": prompt, "system": cfg["system"], "stream": False, "options": {"num_ctx": 8192}}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.loads(r.read().decode("utf-8"))["response"]
    except Exception as e:
        return f"Błąd syntezy LLM: {e}"

def verify_answer(answer: str, contexts: list, query: str) -> dict:
    """Krytyk: weryfikuje każde twierdzenie odpowiedzi względem źródłowych dokumentów."""
    url = OLLAMA_URL + "/api/generate"
    context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:1000]}" for c in contexts])
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
    payload = {"model": "llama3", "prompt": prompt, "system": system, "stream": False, "options": {"num_ctx": 8192}}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=240) as r:
            raw = json.loads(r.read().decode("utf-8"))["response"]

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
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route('/verify', methods=['POST'])
def verify_endpoint():
    data = request.get_json()
    answer   = data.get('answer', '').strip()
    query    = data.get('query', '').strip()
    contexts = data.get('contexts', [])
    if not answer or not query or not contexts:
        return jsonify({"success": False, "error": "Brak danych do weryfikacji"})
    result = verify_answer(answer, contexts, query)
    return jsonify(result)

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
        except: continue
    return escaped

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
        print(f"⚠️ Błąd parsowania .xls {file_path.name}: {e}")
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
        elif ext == '.pdf' and pdfplumber:
            with pdfplumber.open(file_path) as pdf:
                return "\n".join([page.extract_text() or "" for page in pdf.pages])
        elif ext in ['.xlsx', '.xls'] and openpyxl:
            return _extract_excel(file_path)
        elif ext == '.csv':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
    except Exception as e:
        print(f"⚠️ Błąd parsowania pliku {file_path.name}: {e}")
    return ""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/stats', methods=['GET'])
def get_stats():
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
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
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
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
    global ACTIVE_COLLECTION
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

        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        if client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' już istnieje"})

        client.create_collection(name, vectors_config=VectorParams(size=vec_size, distance=dist))

        if switch:
            ACTIVE_COLLECTION = name
            _suggestions_cache["data"] = None; _docs_cache["data"] = None

        return jsonify({"success": True, "name": name, "switched": switch, "active": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/switch', methods=['POST'])
def switch_collection():
    global ACTIVE_COLLECTION
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        if not client.collection_exists(name):
            return jsonify({"success": False, "error": f"Kolekcja '{name}' nie istnieje"})
        ACTIVE_COLLECTION = name
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "active_collection": ACTIVE_COLLECTION})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/collections/delete', methods=['POST'])
def delete_collection():
    global ACTIVE_COLLECTION
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Brak nazwy"})
    if name == ACTIVE_COLLECTION:
        return jsonify({"success": False, "error": "Nie można usunąć aktywnej kolekcji. Najpierw przełącz się na inną."})
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        client.delete_collection(name)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/browse', methods=['GET'])
def browse():
    raw = request.args.get('path', '/mnt').strip()
    p = Path(raw)
    if not p.exists() or not p.is_dir():
        parent = p.parent
        if parent.exists() and parent.is_dir():
            p = parent
        else:
            p = Path('/mnt')
    try:
        entries = []
        for child in sorted(p.iterdir()):
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child), "type": "dir"})
                elif child.suffix.lower() in ['.docx','.pdf','.xlsx','.xls','.csv','.md','.json','.txt']:
                    entries.append({"name": child.name, "path": str(child), "type": "file"})
            except PermissionError:
                pass
        return jsonify({"success": True, "current": str(p), "parent": str(p.parent) if p != p.parent else None, "entries": entries})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/import/stream')
def import_stream():
    folder = request.args.get('folder', '').strip()
    exts = request.args.getlist('ext') or ['docx','pdf','xlsx','xls','csv','md','json']

    def generate():
        def sse(event, data):
            import json as _json
            return f"event: {event}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        folder_path = Path(folder)
        if not folder or not folder_path.exists():
            yield sse("error", {"msg": f"Ścieżka nie istnieje: {folder}"})
            return

        ext_pattern = " ".join([f'-name "*.{e}"' for e in exts])
        or_pattern = " -o ".join([f'-name "*.{e}"' for e in exts])
        cmd = f'find "{folder_path}" -type f \\( {or_pattern} \\)'
        files = [Path(l.strip()) for l in os.popen(cmd).readlines() if l.strip()]

        if not files:
            yield sse("done", {"count": 0, "chunks": 0, "skipped": 0, "msg": "Brak kompatybilnych plików."})
            return

        yield sse("start", {"total": len(files)})

        qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        imported = 0
        skipped = 0
        total_chunks = 0
        new_chunks = 0

        for i, f_path in enumerate(files):
            try:
                text = extract_text(f_path)
                if not text or len(text.strip()) < 10:
                    skipped += 1
                    yield sse("skip", {"file": f_path.name, "reason": "pusty", "i": i+1, "total": len(files)})
                    continue

                chunks = make_chunks(text)
                file_new = 0

                # Deduplikacja — sprawdź które chunki już są w bazie
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

                # Batch embeddings — 8 równolegle
                BATCH = 8
                for b in range(0, len(new_chunks_data), BATCH):
                    batch_items = new_chunks_data[b:b+BATCH]
                    batch_texts  = [item[1] for item in batch_items]
                    batch_ids    = [item[0] for item in batch_items]
                    vectors = get_embeddings_batch(batch_texts, batch_size=BATCH)
                    points  = [
                        PointStruct(id=cid, vector=vec,
                                    payload={"file": f_path.name, "text": txt, "full_path": str(f_path)})
                        for cid, vec, txt in zip(batch_ids, vectors, batch_texts)
                        if vec and any(v != 0.0 for v in vec)
                    ]
                    if points:
                        qdrant.upsert(collection_name=ACTIVE_COLLECTION, points=points)
                    new_chunks += len(points)
                    file_new  += len(points)

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

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/collection/clear', methods=['POST'])
def clear_collection():
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        info = client.get_collection(ACTIVE_COLLECTION)
        count_before = info.points_count
        from qdrant_client.models import VectorParams, Distance
        client.delete_collection(ACTIVE_COLLECTION)
        client.create_collection(
            ACTIVE_COLLECTION,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE)
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
    """Konwertuje sciezke WSL /mnt/g/... na Windows G:\\..."""
    if not path:
        return ""
    import re
    m = re.match(r'^/mnt/([a-zA-Z])/(.*)', path)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace('/', '\\')
        return f"{drive}:\\{rest}"
    return path

@app.route('/file/open', methods=['POST'])
def file_open():
    data = request.get_json()
    wsl_path = data.get('path', '').strip()
    if not wsl_path:
        return jsonify({"success": False, "error": "Brak ścieżki"})
    win_path = wsl_to_win(wsl_path)
    if not win_path:
        return jsonify({"success": False, "error": "Nie można skonwertować ścieżki"})
    try:
        os.popen(f'cmd.exe /c start "" "{win_path}"')
        return jsonify({"success": True, "win_path": win_path})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/search/stream', methods=['POST'])
def search_stream():
    """Wyszukiwanie z streamingiem LLM — wyniki natychmiast, odpowiedź słowo po słowie."""
    data       = request.get_json()
    query_text = data.get('query', '').strip()
    if not query_text:
        return jsonify({"success": False, "error": "Zapytanie puste"})
    limit      = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode        = data.get('mode', 'normal')
    if mode not in SEARCH_MODES:
        mode = 'normal'

    def generate():
        def sse(event, d):
            return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"

        try:
            client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
            vector = get_embedding(query_text)

            if file_filter:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector,
                                          limit=limit, query_filter=qfilter)
            else:
                res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

            raw_contexts = []
            results = []
            for point in res.points:
                p = point.payload
                raw_contexts.append({"file": p.get("file",""), "text": p.get("text","")})
                results.append({
                    "file": p.get("file","Nieznany"),
                    "score": f"{point.score:.4f}",
                    "text": highlight_backend(p.get("text",""), query_text),
                    "full_path": p.get("full_path",""),
                    "win_path": wsl_to_win(p.get("full_path",""))
                })

            # Wyślij wyniki od razu
            yield sse("results", {"results": results, "contexts": raw_contexts,
                                  "mode": mode, "mode_label": SEARCH_MODES[mode]["label"]})

            if not raw_contexts:
                yield sse("done", {"ai_answer": "Brak dokumentów."})
                return

            # Streaming LLM
            cfg = SEARCH_MODES.get(mode, SEARCH_MODES["normal"])
            context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:1400]}" for c in raw_contexts])
            prompt = f"KONTEKST:\n{context_str}\n\nZAPYTANIE: {query_text}\n\n{cfg['prompt_suffix']}"
            payload = {"model": "llama3", "prompt": prompt, "system": cfg["system"], "stream": True, "options": {"num_ctx": 8192}}

            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST"
            )
            full_answer = ""
            with urllib.request.urlopen(req, timeout=240) as r:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk_data = json.loads(line)
                        token = chunk_data.get("response", "")
                        if token:
                            full_answer += token
                            yield sse("token", {"token": token})
                        if chunk_data.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue

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
    limit = min(int(data.get('limit', 5)), 20)
    file_filter = data.get('file_filter', None)
    mode = data.get('mode', 'normal')
    if mode not in SEARCH_MODES:
        mode = 'normal'

    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        vector = get_embedding(query_text)

        if file_filter:
            # Używa indeksu keyword na polu 'file' — szybkie, bez skanowania całości
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=file_filter))])
            res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector,
                                      limit=limit, query_filter=qfilter)
            if not res.points:
                return jsonify({"success": True, "results": [], "ai_answer": "Brak dokumentów dla wybranego pliku."})
        else:
            res = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

        raw_contexts = []
        results = []
        for point in res.points:
            p = point.payload
            raw_contexts.append({"file": p.get("file", "Nieznany"), "text": p.get("text", "")})
            full_path = p.get("full_path", "")
            results.append({
                "file": p.get("file", "Nieznany"),
                "score": f"{point.score:.4f}",
                "text": highlight_backend(p.get("text", ""), query_text),
                "full_path": full_path,
                "win_path": wsl_to_win(full_path)
            })

        ai_answer = generate_answer(query_text, raw_contexts, mode) if raw_contexts else "Brak dokumentów."
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

_suggestions_cache = {"data": None, "ts": 0}
SUGGESTIONS_TTL = 1800  # 30 minut

@app.route('/suggestions', methods=['GET'])
def get_suggestions():
    force = request.args.get('force', '0') == '1'
    now = time.time()
    if not force and _suggestions_cache["data"] and (now - _suggestions_cache["ts"]) < SUGGESTIONS_TTL:
        return jsonify({"success": True, "suggestions": _suggestions_cache["data"], "cached": True})
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
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

        context = "\n\n".join([
            f"[{p['file']}]: {p['text'][:600]}" for p in sample
        ])
        prompt = (
            "Na podstawie poniższych fragmentów dokumentów śledczych, wygeneruj dokładnie 8 konkretnych pytań analitycznych. "
            "Każde pytanie musi być zwięzłe (max 12 słów), dotyczyć konkretnych faktów, kwot, dat, nazwisk lub zdarzeń z tych dokumentów. "
            "Zwróć TYLKO listę 8 pytań, każde w osobnej linii, bez numeracji, bez komentarzy.\n\n"
            f"DOKUMENTY:\n{context}"
        )
        url = OLLAMA_URL + "/api/generate"
        payload = {"model": "llama3", "prompt": prompt, "stream": False,
                   "system": "Jesteś analitykiem śledczym. Odpowiadasz wyłącznie po polsku. Zwracasz tylko listę pytań.",
                   "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = json.loads(r.read().decode("utf-8"))["response"]

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
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
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
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        files_set = set(files_to_delete)
        ids_to_delete = []
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION, limit=250, offset=offset,
                with_payload=["file"], with_vectors=False
            )
            for r in records:
                if r.payload.get("file") in files_set:
                    ids_to_delete.append(r.id)
            if offset is None:
                break

        for i in range(0, len(ids_to_delete), 100):
            client.delete(collection_name=ACTIVE_COLLECTION,
                          points_selector=ids_to_delete[i:i+100])
        _suggestions_cache["data"] = None; _docs_cache["data"] = None
        return jsonify({"success": True, "deleted_chunks": len(ids_to_delete)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

_docs_cache = {"data": None, "ts": 0}
DOCS_CACHE_TTL = 300  # 5 minut

@app.route('/documents', methods=['GET'])
def get_documents():
    force = request.args.get('force', '0') == '1'
    now = time.time()
    if not force and _docs_cache["data"] and (now - _docs_cache["ts"]) < DOCS_CACHE_TTL:
        return jsonify({"success": True, "documents": _docs_cache["data"],
                        "total": len(_docs_cache["data"]), "cached": True})
    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        file_chunks = {}
        file_paths  = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=ACTIVE_COLLECTION,
                limit=250, offset=offset,
                with_payload=["file", "full_path"],
                with_vectors=False
            )
            for r in records:
                fname = r.payload.get("file", "")
                if fname:
                    file_chunks[fname] = file_chunks.get(fname, 0) + 1
                    if fname not in file_paths and r.payload.get("full_path"):
                        file_paths[fname] = r.payload["full_path"]
            if offset is None:
                break
        docs = sorted(
            [{"file": f, "chunks": c, "full_path": file_paths.get(f, "")}
             for f, c in file_chunks.items()],
            key=lambda x: -x["chunks"]
        )
        _docs_cache["data"] = docs
        _docs_cache["ts"]   = now
        return jsonify({"success": True, "documents": docs, "total": len(docs), "cached": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

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
        doc.add_paragraph('Wygenerowano przez MZK RAG System | Llama3 + Qdrant Cloud').runs[0].font.size = Pt(8)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)

        fname = f'raport_{time.strftime("%Y%m%d_%H%M")}.docx'
        return send_file(buf, as_attachment=True, download_name=fname,
                         mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/network', methods=['POST'])
def build_network():
    data    = request.get_json()
    query   = data.get('query', '').strip()
    limit   = min(int(data.get('limit', 10)), 20)

    if not query:
        return jsonify({"success": False, "error": "Brak zapytania"})

    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        vector = get_embedding(query)
        res    = client.query_points(collection_name=ACTIVE_COLLECTION, query=vector, limit=limit)

        contexts = [{"file": p.payload.get("file",""), "text": p.payload.get("text","")} for p in res.points]
        context_str = "\n\n".join([f"[{c['file']}]: {c['text'][:800]}" for c in contexts])

        system = (
            "Jesteś ekspertem śledczym ds. wykrywania korupcji i powiązań finansowych. "
            "Z dokumentów wyciągasz SIEĆ POWIĄZAŃ składającą się z węzłów i krawędzi. "
            "\n\nTYPY WĘZŁÓW:"
            "\n- osoba: imię i nazwisko (np. 'Jan Kowalski')"
            "\n- firma: nazwa firmy lub instytucji (np. 'REFUNDA Sp. z o.o.', 'MZK Gorzów')"
            "\n- kwota: kwota pieniężna z kontekstem (np. '92 247 144 zł rekompensata')"
            "\n- dokument: umowa, uchwała, przetarg, raport (np. 'Umowa nr 12/2023')"
            "\n- inne: daty, adresy, inne ważne encje"
            "\n\nTYPY RELACJI (label krawędzi) — bądź precyzyjny:"
            "\n- przepływ finansowy: 'zapłacił Xzł', 'przelał Xzł', 'faktura Xzł'"
            "\n- zatrudnienie: 'prezes', 'dyrektor', 'pracownik'"
            "\n- własność/udziały: 'właściciel', 'udziałowiec', 'wspólnik'"
            "\n- kontrakt: 'podpisał umowę', 'zlecił', 'wykonawca'"
            "\n- decyzja: 'zatwierdził', 'podpisał', 'uchwalił', 'anulował'"
            "\n- powiązanie osobiste: 'znajomy', 'rodzina', 'współpracownik'"
            "\n- przetarg: 'wygrał przetarg', 'złożył ofertę', 'wykluczony'"
            "\n\nZwróć WYŁĄCZNIE poprawny JSON (bez komentarzy, bez markdown):\n"
            '{"nodes":[{"id":"unikalny_id","type":"osoba|firma|kwota|dokument|inne","label":"wyswietlana nazwa"}],'
            '"edges":[{"source":"id_zrodla","target":"id_celu","label":"typ relacji"}]}'
            "\n\nWAŻNE: id musi być unikalny i taki sam w nodes i edges. Bez polskich znaków w id."
        )
        prompt = (
            f"DOKUMENTY ŚLEDCZE:\n{context_str}\n\n"
            f"ZAPYTANIE: {query}\n\n"
            "Wyciągnij kompletną sieć powiązań: osoby, firmy, kwoty, przepływy finansowe, decyzje.\n"
            "Skup się na: kto komu płacił, kto co podpisał, kto z kim jest powiązany.\n"
            "Zwróć JSON:"
        )
        payload = {"model": "llama3", "prompt": prompt, "system": system, "stream": False, "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = json.loads(r.read().decode("utf-8"))["response"]

        # Wyciągnij JSON z odpowiedzi
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            return jsonify({"success": False, "error": "LLM nie zwrócił JSON", "raw": raw})

        graph = json.loads(json_match.group())

        # Walidacja i deduplikacja
        seen_nodes = {}
        clean_nodes = []
        for n in graph.get("nodes", []):
            nid = str(n.get("id","")).strip()
            if nid and nid not in seen_nodes:
                seen_nodes[nid] = True
                clean_nodes.append({
                    "id": nid,
                    "type": n.get("type","inne"),
                    "label": n.get("label", nid)[:40]
                })

        seen_edges = set()
        clean_edges = []
        for e in graph.get("edges", []):
            src = str(e.get("source","")).strip()
            tgt = str(e.get("target","")).strip()
            key = f"{src}|{tgt}"
            if src and tgt and src in seen_nodes and tgt in seen_nodes and key not in seen_edges:
                seen_edges.add(key)
                clean_edges.append({
                    "source": src,
                    "target": tgt,
                    "label": e.get("label","")[:30]
                })

        return jsonify({
            "success": True,
            "nodes": clean_nodes,
            "edges": clean_edges,
            "query": query,
            "sources": len(contexts)
        })
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"Błąd parsowania JSON: {e}", "raw": raw[:500]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _excel_forensics(file_path: Path) -> dict:
    """Pełna analiza forensyczna pliku Excel — szuka błędów rachunkowych i manipulacji."""
    findings = []
    summary  = {"total_formulas": 0, "errors": 0, "goal_seek": 0, "overrides": 0,
                "warnings": 0, "hidden_rows": 0, "hidden_cols": 0}

    try:
        wb_f = openpyxl.load_workbook(file_path, data_only=False)
        wb_v = openpyxl.load_workbook(file_path, data_only=True)
    except Exception as e:
        return {"success": False, "error": str(e)}

    # --- Wykryj ukryte wiersze i kolumny we wszystkich arkuszach ---
    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        sheet_hidden_rows = [ri for ri, rd in ws.row_dimensions.items() if rd.hidden]
        sheet_hidden_cols = [cl for cl, cd in ws.column_dimensions.items() if cd.hidden]

        if sheet_hidden_rows:
            summary["hidden_rows"] += len(sheet_hidden_rows)
            # Zbierz wartości z ukrytych wierszy
            hidden_data = []
            for ri in sheet_hidden_rows[:20]:  # max 20 ukrytych wierszy w raporcie
                row_cells = []
                for cell in ws[ri]:
                    if cell.value is not None and str(cell.value).strip():
                        row_cells.append(f"{cell.column_letter}: {cell.value}")
                if row_cells:
                    hidden_data.append(f"wiersz {ri}: " + " | ".join(row_cells[:8]))
            findings.append({
                "severity": "high",
                "sheet": sn, "cell": f"wiersze {sheet_hidden_rows[:5]}{'...' if len(sheet_hidden_rows)>5 else ''}",
                "formula": "",
                "stored": None,
                "calculated": None,
                "issue": f"UKRYTE WIERSZE: {len(sheet_hidden_rows)} wierszy w arkuszu '{sn}'",
                "detail": (f"Znaleziono {len(sheet_hidden_rows)} ukrytych wierszy. "
                           + ("Dane w nich: " + "; ".join(hidden_data[:3]) if hidden_data
                              else "Wiersze są puste."))
            })

        if sheet_hidden_cols:
            summary["hidden_cols"] += len(sheet_hidden_cols)
            findings.append({
                "severity": "high",
                "sheet": sn, "cell": f"kolumny {sheet_hidden_cols[:5]}",
                "formula": "",
                "stored": None,
                "calculated": None,
                "issue": f"UKRYTE KOLUMNY: {len(sheet_hidden_cols)} kolumn w arkuszu '{sn}'",
                "detail": f"Kolumny {', '.join(sheet_hidden_cols[:10])} są ukryte — mogą zawierać dane pominięte w analizie."
            })

    # Zbierz wszystkie wartości arkusza do słownika dla cross-check
    sheet_values = {}
    for sn in wb_v.sheetnames:
        ws = wb_v[sn]
        sheet_values[sn] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    sheet_values[sn][cell.coordinate] = cell.value

    # --- Próba ewaluacji przez xlcalculator ---
    calc_values = {}
    try:
        from xlcalculator import ModelCompiler, Evaluator
        compiler  = ModelCompiler()
        model     = compiler.read_and_parse_archive(str(file_path))
        evaluator = Evaluator(model)
        for sn in wb_f.sheetnames:
            ws_f = wb_f[sn]
            for row in ws_f.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith('='):
                        key = f"{sn}!{cell.coordinate}"
                        try:
                            val = evaluator.evaluate(key)
                            calc_values[key] = val
                        except Exception:
                            pass
    except Exception:
        pass  # xlcalculator niedostępny lub błąd parsowania

    # --- Analiza komórek ---
    for sn in wb_f.sheetnames:
        ws_f = wb_f[sn]
        ws_v = wb_v[sn]

        for row in ws_f.iter_rows():
            for cell_f in row:
                formula = cell_f.value
                if not isinstance(formula, str) or not formula.startswith('='):
                    continue

                summary["total_formulas"] += 1
                coord   = cell_f.coordinate
                stored  = sheet_values.get(sn, {}).get(coord)
                calc_key = f"{sn}!{coord}"

                # 1. Brak wartości — plik niezapisany przez Excel (np. LibreOffice)
                if stored is None:
                    findings.append({
                        "severity": "warning",
                        "sheet": sn, "cell": coord,
                        "formula": formula[:80],
                        "stored": None,
                        "calculated": None,
                        "issue": "Brak zapisanej wartości",
                        "detail": "Plik nie był przeliczony przez Excel lub zapisany przez LibreOffice. Wartości formuł mogą być błędne."
                    })
                    summary["warnings"] += 1
                    continue

                # 2. Ślad Goal Seek — ekstremalna precyzja dziesiętna
                if isinstance(stored, float):
                    s_str     = repr(stored)
                    after_dot = s_str.split('.')[-1] if '.' in s_str else ''
                    if len(after_dot) > 9:
                        findings.append({
                            "severity": "high",
                            "sheet": sn, "cell": coord,
                            "formula": formula[:80],
                            "stored": stored,
                            "calculated": None,
                            "issue": f"Ślad Goal Seek ({len(after_dot)} miejsc po przecinku)",
                            "detail": f"Wartość {stored} ma {len(after_dot)} miejsc po przecinku — charakterystyczne dla narzędzia Goal Seek (wsteczne dobieranie wartości)."
                        })
                        summary["goal_seek"] += 1

                # 3. Niezgodność formuły z wartością wyliczoną przez xlcalculator
                if calc_key in calc_values:
                    calc_val = calc_values[calc_key]
                    if calc_val is not None and stored is not None:
                        try:
                            diff = abs(float(stored) - float(calc_val))
                            rel  = diff / max(abs(float(calc_val)), 1e-9)
                            if rel > 0.001 and diff > 0.01:  # >0.1% rozbieżność i >1 grosz
                                findings.append({
                                    "severity": "critical",
                                    "sheet": sn, "cell": coord,
                                    "formula": formula[:80],
                                    "stored": stored,
                                    "calculated": round(float(calc_val), 6),
                                    "diff": round(diff, 2),
                                    "issue": f"NIEZGODNOŚĆ: zapisano {stored}, formuła daje {round(float(calc_val),2)}",
                                    "detail": f"Różnica: {round(diff,2)} ({round(rel*100,2)}%). Możliwa ręczna modyfikacja."
                                })
                                summary["overrides"] += 1
                                summary["errors"] += 1
                        except (ValueError, TypeError):
                            pass

                # 4. Prosta weryfikacja SUM — parsuj zakres i sumuj ręcznie
                m_sum = re.match(r'^=SUM\(([A-Z]+\d+):([A-Z]+\d+)\)$', formula, re.IGNORECASE)
                if m_sum and isinstance(stored, (int, float)):
                    try:
                        from openpyxl.utils import range_boundaries
                        min_col, min_row, max_col, max_row = range_boundaries(f"{m_sum.group(1)}:{m_sum.group(2)}")
                        total = 0.0
                        for r in ws_v.iter_rows(min_row=min_row, max_row=max_row,
                                                min_col=min_col, max_col=max_col):
                            for c in r:
                                if isinstance(c.value, (int, float)):
                                    total += c.value
                        diff = abs(stored - total)
                        if diff > 0.02:
                            findings.append({
                                "severity": "critical",
                                "sheet": sn, "cell": coord,
                                "formula": formula[:80],
                                "stored": stored,
                                "calculated": round(total, 2),
                                "diff": round(diff, 2),
                                "issue": f"BŁĄD SUM: zapisano {stored}, rzeczywista suma = {round(total,2)}",
                                "detail": f"Różnica {round(diff,2)} zł. Formuła {formula} nie zgadza się z sumą zakresu."
                            })
                            summary["errors"] += 1
                    except Exception:
                        pass

    wb_f.close()
    wb_v.close()

    # Posortuj: najpierw critical, potem high, potem warning
    sev_order = {"critical": 0, "high": 1, "warning": 2}
    findings.sort(key=lambda x: sev_order.get(x["severity"], 3))

    return {
        "success": True,
        "file": file_path.name,
        "summary": summary,
        "findings": findings
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

    # 3. Szukaj po nazwie rekurencyjnie w /mnt/g i /mnt/c/Users
    if not path and fname:
        search_roots = ["/mnt/g", "/mnt/c/Users/Marcin/Documents", "/mnt/c/Users/Marcin/Desktop"]
        for root in search_roots:
            rp = Path(root)
            if not rp.exists():
                continue
            # find przez system — szybciej niż Python glob
            result = os.popen(f'find "{root}" -type f -name "{fname}" 2>/dev/null | head -1').read().strip()
            if result and Path(result).exists():
                path = Path(result)
                break

    if not path:
        return jsonify({"success": False, "error": f"Nie znaleziono pliku '{fname}' — zaimportuj go ponownie z opcją śledzenia ścieżek (full_path)."})

    result = _excel_forensics(path)

    # Jeśli są krytyczne błędy — poproś LLM o komentarz
    critical = [f for f in result.get("findings", []) if f["severity"] == "critical"]
    if critical and result.get("success"):
        ctx = "\n".join([f"[{f['sheet']}!{f['cell']}] {f['issue']} | {f['detail']}" for f in critical[:8]])
        prompt = (
            f"Plik Excel: {path.name}\n"
            f"Znaleziono {len(critical)} krytycznych niezgodności w formułach:\n{ctx}\n\n"
            "Oceń: czy to błąd rachunkowy, celowa manipulacja, czy błąd techniczny? "
            "Co to oznacza w kontekście śledztwa finansowego? Odpowiedz po polsku, krótko."
        )
        system = "Jesteś biegłym rewidentem śledczym. Analizujesz anomalie w plikach Excel pod kątem fałszowania dokumentów finansowych."
        payload = {"model": "llama3", "prompt": prompt, "system": system, "stream": False, "options": {"num_ctx": 8192}}
        try:
            req = urllib.request.Request(
                OLLAMA_URL + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                result["llm_comment"] = json.loads(r.read().decode("utf-8"))["response"]
        except Exception as e:
            result["llm_comment"] = f"(Błąd LLM: {e})"

    return jsonify(result)


@app.route('/compare', methods=['POST'])
def compare_documents():
    """Porównanie dwóch konkretnych plików — pobiera wszystkie ich chunki i wysyła do LLM."""
    data   = request.get_json()
    file_a = data.get('file_a', '').strip()
    file_b = data.get('file_b', '').strip()
    focus  = data.get('focus', 'Porównaj te dokumenty — wskaż różnice, sprzeczności i podobieństwa.').strip()

    if not file_a or not file_b:
        return jsonify({"success": False, "error": "Podaj dwa pliki do porównania"})

    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_KEY)
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        def get_chunks(fname, max_chunks=12):
            qfilter = Filter(must=[FieldCondition(key="file", match=MatchValue(value=fname))])
            # Wektor zerowy = pobierz dowolne chunki (scroll z filtrem)
            records, _ = client.scroll(
                collection_name=ACTIVE_COLLECTION, limit=max_chunks,
                query_filter=qfilter, with_payload=["text"], with_vectors=False
            )
            return [r.payload.get("text", "") for r in records]

        chunks_a = get_chunks(file_a)
        chunks_b = get_chunks(file_b)

        if not chunks_a:
            return jsonify({"success": False, "error": f"Brak danych dla pliku: {file_a}"})
        if not chunks_b:
            return jsonify({"success": False, "error": f"Brak danych dla pliku: {file_b}"})

        text_a = "\n\n".join(chunks_a[:10])[:4000]
        text_b = "\n\n".join(chunks_b[:10])[:4000]

        system = (
            "Jesteś analitykiem śledczym porównującym dokumenty. "
            "Wskazujesz różnice, sprzeczności, brakujące informacje i podejrzane rozbieżności. "
            "Odpowiadaj zawsze po polsku, konkretnie, z cytatami."
        )
        prompt = (
            f"DOKUMENT A: {file_a}\n{text_a}\n\n"
            f"---\n\n"
            f"DOKUMENT B: {file_b}\n{text_b}\n\n"
            f"ZADANIE: {focus}\n\n"
            "Porównanie (wskaż konkretne różnice z cytatami):"
        )
        payload = {"model": "llama3", "prompt": prompt, "system": system,
                   "stream": False, "options": {"num_ctx": 8192}}
        req = urllib.request.Request(
            OLLAMA_URL + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            answer = json.loads(r.read().decode("utf-8"))["response"]

        return jsonify({
            "success": True,
            "file_a": file_a, "chunks_a": len(chunks_a),
            "file_b": file_b, "chunks_b": len(chunks_b),
            "comparison": answer
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/export/csv', methods=['POST'])
def export_csv():
    """Konwertuje tabelę Markdown z trybu extract → plik CSV."""
    data     = request.get_json()
    md_table = data.get('table', '')
    if not md_table:
        return jsonify({"success": False, "error": "Brak tabeli"}), 400
    try:
        import csv, io as _io
        lines = [l.strip() for l in md_table.splitlines() if l.strip()]
        rows  = []
        for line in lines:
            if re.match(r'^\s*\|[-| ]+\|\s*$', line):
                continue  # separator
            if line.startswith('|'):
                cells = [c.strip() for c in line.split('|')[1:-1]]
                rows.append(cells)

        buf = _io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow(row)

        output = buf.getvalue().encode('utf-8-sig')  # BOM dla Excela
        fname  = f"ekstrakcja_{time.strftime('%Y%m%d_%H%M')}.csv"
        return send_file(
            _io.BytesIO(output), as_attachment=True,
            download_name=fname, mimetype='text/csv; charset=utf-8'
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/cache/stats', methods=['GET'])
def cache_stats():
    _save_embed_cache()
    size_mb = round(_EMBED_CACHE_PATH.stat().st_size / 1_048_576, 2) if _EMBED_CACHE_PATH.exists() else 0
    return jsonify({"entries": len(_embed_cache), "size_mb": size_mb})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
