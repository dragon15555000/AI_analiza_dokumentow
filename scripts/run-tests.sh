#!/usr/bin/env bash
# Szybkie testy integracyjne (bez pytest): składnia, /health, Groq API, opcjonalnie SSE search.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE_URL="${BASE_URL:-http://127.0.0.1:5000}"
SKIP_E2E=0
QUERY="${TEST_QUERY:-umowa}"

usage() {
  cat <<'EOF'
Użycie: ./scripts/run-tests.sh [opcje]

Testy integracyjne (wymaga działającej aplikacji na :5000):
  - py_compile app.py, wsgi.py
  - GET /health (Qdrant, embedding, LLM)
  - Groq chat API (compound-mini, llama-3.3-70b, compound)
  - GET /api/config/llm
  - POST /search/stream (SSE: results, token, done) — opcjonalnie

Opcje:
  --skip-e2e      Pomiń test /search/stream (wolniejszy)
  --base-url URL  Domyślnie http://127.0.0.1:5000
  -h, --help      Ta pomoc

Zmienne:
  TEST_QUERY      Zapytanie do search/stream (domyślnie: umowa)
  APP_API_KEY     Nadpisuje klucz z .env (nagłówek X-API-Key)

Przed testami: ./restart-app.sh --user   lub   ./venv/bin/python app.py
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-e2e) SKIP_E2E=1; shift ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Nieznana opcja: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Wczytaj pojedynczą zmienną z .env (bez source — unika złego formatu linii)
read_env() {
  local key="$1" default="${2:-}"
  if [[ -f "$ROOT/.env" ]]; then
    local line
    line="$(grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | head -1 || true)"
    if [[ -n "$line" ]]; then
      echo "$line" | sed "s/^${key}=//" | sed 's/[[:space:]]*#.*//' | tr -d '\r'
      return
    fi
  fi
  echo "$default"
}

if [[ ! -x "$ROOT/venv/bin/python" ]]; then
  echo "Brak venv — uruchom: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# Tylko znaki ASCII (unika 401 przez śmieci z komentarza w .env)
_raw_key="${APP_API_KEY:-$(read_env APP_API_KEY)}"
export APP_API_KEY="$(printf '%s' "$_raw_key" | tr -cd '[:alnum:]')"
export BASE_URL SKIP_E2E QUERY

exec "$ROOT/venv/bin/python" - <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
# cwd ustawione przez bash na ROOT repo
ROOT = Path.cwd()
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
SKIP_E2E = os.environ.get("SKIP_E2E", "0") == "1"
QUERY = os.environ.get("QUERY", "umowa")
API_KEY = os.environ.get("APP_API_KEY", "").strip()


def clean_key(value: str) -> str:
    """Usuwa znaki spoza ASCII (np. resztki komentarza z .env)."""
    return "".join(c for c in value if ord(c) < 128).strip()


def clean_key_list(values) -> list[str]:
    """Normalizuje listę kluczy API i usuwa puste / zduplikowane wpisy."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = clean_key(str(value or ""))
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def is_groq_key(value: str) -> bool:
    return value.startswith("gsk_") and len(value) > 20

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{mark}: {name}{suffix}")


def headers() -> dict:
    h: dict = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def safe_detail(text: str, limit: int = 100) -> str:
    return (text or "")[:limit].encode("ascii", errors="replace").decode()


def wait_for_app(max_attempts: int = 12, pause: float = 2.0) -> bool:
    """Czeka aż /health odpowie (np. po restarcie usługi)."""
    for i in range(1, max_attempts + 1):
        try:
            r = requests.get(f"{BASE}/health", headers=headers(), timeout=10)
            if r.status_code == 200 and r.json().get("success"):
                return True
        except requests.RequestException:
            pass
        if i < max_attempts:
            print(f"  … czekam na aplikację ({i}/{max_attempts})")
            import time
            time.sleep(pause)
    return False


if not wait_for_app():
    check("aplikacja dostępna", False, f"brak odpowiedzi z {BASE}/health")
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{'=' * 42}\nWynik: {len(results) - failed}/{len(results)} PASS (przerwano)")
    sys.exit(1)

# 1. Składnia
rc = subprocess.run(
    [str(ROOT / "venv/bin/python"), "-m", "py_compile", "app.py", "wsgi.py"],
    capture_output=True,
    cwd=ROOT,
)
check("py_compile app.py wsgi.py", rc.returncode == 0, rc.stderr.decode()[:120] if rc.returncode else "OK")

# 2. Health
try:
    r = requests.get(f"{BASE}/health", headers=headers(), timeout=25)
    if r.status_code == 401:
        check("/health", False, "401 — ustaw APP_API_KEY lub wpisz klucz w sessionStorage UI")
    else:
        h = r.json()
        check("/health", h.get("success") and h.get("overall") == "ok", str(h.get("overall", r.status_code)))
        check("  qdrant", h.get("qdrant", {}).get("ok"), f"{h.get('vectors_count', '?')} wektorów")
        emb = h.get("embedding", {})
        check("  embedding (Ollama)", emb.get("ok"), emb.get("model", ""))
        llm = h.get("llm", {})
        check("  llm", llm.get("ok"), safe_detail(llm.get("detail", "")))
        prov = h.get("provider", "")
        check("  provider", prov in ("groq", "openrouter", "ollama"), prov)
except requests.RequestException as e:
    check("/health", False, str(e)[:100])

# 2b. Tasks (mutex ciężkich operacji)
try:
    t = requests.get(f"{BASE}/tasks", headers=headers(), timeout=10)
    if t.status_code == 401:
        check("/tasks", False, "401")
    else:
        td = t.json()
        check("/tasks", td.get("success") is True, "busy=" + str(td.get("busy")))
except requests.RequestException as e:
    check("/tasks", False, str(e)[:80])

# 3. Groq API
groq_keys: list[str] = []
llm_cfg = ROOT / ".llm_config.json"
if llm_cfg.exists():
    try:
        cfg = json.loads(llm_cfg.read_text(encoding="utf-8"))
        groq_keys.extend(clean_key_list(cfg.get("groq_keys")))
        single_candidate = clean_key(cfg.get("groq_key") or "")
        if single_candidate:
            groq_keys.append(single_candidate)
    except Exception:
        pass
if not groq_keys and (ROOT / ".env").exists():
    env_candidates = []
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("GROQ_API_KEYS=") or line.startswith("GROQ_API_KEY="):
            _, raw_value = line.split("=", 1)
            env_candidates.extend(clean_key_list(raw_value.replace(";", ",").split(",")))
    groq_keys.extend(env_candidates)

groq_keys = [key for key in groq_keys if is_groq_key(key)]
if not groq_keys:
    check("Groq API", False, "brak poprawnego GROQ_API_KEY/GROQ_API_KEYS w .llm_config.json lub .env")

if groq_keys:
    url = "https://api.groq.com/openai/v1/chat/completions"
    groq_key = groq_keys[0]
    if len(groq_keys) > 1:
        print(f"  … wykryto {len(groq_keys)} kluczy Groq, testuję pierwszy poprawny")
    for model in ("groq/compound-mini", "llama-3.3-70b-versatile", "groq/compound"):
        try:
            gr = requests.post(
                url,
                headers={"Authorization": f"Bearer {groq_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 8,
                },
                timeout=60,
            )
            if gr.status_code == 200:
                check(f"Groq API {model}", True)
            else:
                err = (gr.json().get("error") or {}).get("message", gr.text[:80])
                check(f"Groq API {model}", False, err[:100])
        except requests.RequestException as e:
            check(f"Groq API {model}", False, str(e)[:80])

# 4. Config LLM
try:
    c = requests.get(f"{BASE}/api/config/llm", headers=headers(), timeout=10)
    if c.status_code == 401:
        check("/api/config/llm", False, "401")
    else:
        d = c.json()
        check("/api/config/llm", d.get("success"), f"provider={d.get('provider')}")
except requests.RequestException as e:
    check("/api/config/llm", False, str(e)[:80])

# 5. SSE search/stream (z ponowieniem przy chwilowym reset połączenia)
if not SKIP_E2E:
    import time

    events: list[str] = []
    err = None
    stream_ok = False
    hdr = {**headers(), "Content-Type": "application/json"}
    body = {
        "query": QUERY,
        "limit": 2,
        "mode": "normal",
        "llm_provider": "groq",
        "groq_model": "groq/compound-mini",
    }
    for attempt in range(1, 4):
        events = []
        err = None
        try:
            time.sleep(0.5 if attempt > 1 else 0)
            with requests.post(
                f"{BASE}/search/stream",
                headers=hdr,
                json=body,
                stream=True,
                timeout=120,
            ) as resp:
                if resp.status_code != 200:
                    if attempt < 3:
                        continue
                    check("/search/stream HTTP", False, str(resp.status_code))
                    break
                current_event = None
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw.startswith("event: "):
                        current_event = raw[7:].strip()
                    elif raw.startswith("data: ") and current_event:
                        events.append(current_event)
                        if current_event == "error":
                            try:
                                err = json.loads(raw[6:]).get("error", raw[6:][:80])
                            except Exception:
                                err = raw[6:][:80]
                            break
                        if current_event in ("done", "error"):
                            break
                        if len(events) > 800:
                            break
                stream_ok = True
                break
        except requests.RequestException as e:
            if attempt < 3:
                print(f"  … ponawiam /search/stream ({attempt}/3): {safe_detail(str(e), 60)}")
                continue
            check("/search/stream", False, safe_detail(str(e), 100))
            stream_ok = False
            break

    if stream_ok:
        check("/search/stream HTTP", True)
        check("  SSE: results", "results" in events)
        check("  SSE: token", "token" in events, safe_detail(str(err or "")))
        check("  SSE: done", "done" in events, safe_detail(str(err or "")))
else:
    print("SKIP: /search/stream (--skip-e2e)")

failed = sum(1 for _, ok, _ in results if not ok)
passed = len(results) - failed
print(f"\n{'=' * 42}")
print(f"Wynik: {passed}/{len(results)} PASS")
if failed:
    print("Nieudane:")
    for name, ok, detail in results:
        if not ok:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
sys.exit(1 if failed else 0)
PY
