#!/usr/bin/env bash
# Szybka walidacja produkcyjnego entry point (Waitress + wsgi:app).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== WSGI / Waitress checklist ==="

fail=0
ok() { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; fail=1; }

[[ -x "$ROOT/venv/bin/python" ]] || { bad "brak venv"; exit 1; }

"$ROOT/venv/bin/python" -m py_compile app.py wsgi.py && ok "py_compile app.py wsgi.py" || bad "py_compile"

"$ROOT/venv/bin/python" -c "from wsgi import app; assert app is not None" && ok "import wsgi:app" || bad "import wsgi:app"

"$ROOT/venv/bin/pip" show waitress >/dev/null 2>&1 && ok "pakiet waitress zainstalowany" || bad "brak waitress w venv"

[[ -f "$ROOT/ai_analiza-user.service" ]] && ok "ai_analiza-user.service istnieje" || bad "brak ai_analiza-user.service"

if grep -q 'wsgi:app' "$ROOT/ai_analiza-user.service" 2>/dev/null; then
  ok "user service wskazuje wsgi:app"
else
  bad "user service nie używa wsgi:app"
fi

if curl -sf --max-time 5 "http://127.0.0.1:${APP_PORT:-5000}/health?light=1" >/dev/null 2>&1; then
  ok "aplikacja odpowiada na /health?light=1"
else
  echo "  … aplikacja nie działa na :${APP_PORT:-5000} (pomiń jeśli celowo wyłączona)"
fi

echo ""
if [[ "$fail" -eq 0 ]]; then
  echo "WSGI OK — na maszynie docelowej: systemctl --user restart ai_analiza && journalctl --user -u ai_analiza -n 20"
  exit 0
fi
echo "WSGI checklist — wykryto problemy (patrz wyżej)"
exit 1
