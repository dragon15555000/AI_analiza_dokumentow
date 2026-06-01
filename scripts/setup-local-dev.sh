#!/usr/bin/env bash
# Jednorazowa konfiguracja lokalnego środowiska dev (Linux x86_64 / amd64).
# Nie uruchamia serwisów — tylko venv, .env, binarka Qdrant.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

QDRANT_VERSION="${QDRANT_VERSION:-v1.13.6}"
INSTALL_SYSTEM=0
PULL_MODELS=0
SKIP_QDRANT=0

usage() {
  cat <<'EOF'
Użycie: ./scripts/setup-local-dev.sh [opcje]

Przygotowuje lokalne dev bez Qdrant Cloud:
  - venv + pip install -r requirements.txt
  - .env (jeśli brak) z lokalnym Qdrant i Ollama
  - binarka Qdrant w .local/ (Linux x86_64)

Opcje:
  --system-deps   Zainstaluj pakiety apt: python3.X-venv (auto-detect), zstd, curl (wymaga sudo)
  --pull-models   Pobierz modele Ollama: nomic-embed-text, llama3 (wymaga działającego ollama)
  --skip-qdrant   Nie pobieraj binarki Qdrant
  -h, --help      Ta pomoc

Po skrypcie uruchom ręcznie (osobne terminale / tmux):
  1. ollama serve
  2. .local/qdrant          (z katalogu repo)
  3. set -a && source .env && set +a && ./venv/bin/python app.py

Sprawdzenie: curl -s http://127.0.0.1:5000/health

Więcej: README.md (sekcja „Dev lokalny”) i AGENTS.md
EOF
}

log() { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system-deps) INSTALL_SYSTEM=1 ;;
    --pull-models) PULL_MODELS=1 ;;
    --skip-qdrant) SKIP_QDRANT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) warn "Nieznana opcja: $1"; usage; exit 1 ;;
  esac
  shift
done

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) QDRANT_ASSET="qdrant-x86_64-unknown-linux-gnu.tar.gz" ;;
  *)
    warn "Architektura $ARCH: automatyczne pobranie Qdrant nieobsługiwane. Użyj Qdrant Cloud w .env lub binarkę z https://github.com/qdrant/qdrant/releases"
    SKIP_QDRANT=1
    ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  warn "Brak python3. Uruchom ponownie z --system-deps lub zainstaluj Python 3.10+."
  exit 1
fi

# Wykryj dokładną wersję Pythona (np. 3.12, 3.13)
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log "Python $PY_VER wykryty."

if [[ "$INSTALL_SYSTEM" -eq 1 ]]; then
  log "Instalacja pakietów systemowych (sudo)..."
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  # Próbuj wersjonowany pakiet venv, potem generyczny python3-venv
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "python${PY_VER}-venv" zstd curl ca-certificates 2>/dev/null || \
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv zstd curl ca-certificates
fi

log "Tworzenie venv i instalacja zależności Python..."
# Sprawdź czy venv działa; jeśli nie — podpowiedz komendę
if ! python3 -m venv --without-pip /tmp/_venv_test 2>/dev/null; then
  warn "python3 -m venv nie działa. Zainstaluj: sudo apt install python${PY_VER}-venv"
  warn "Lub uruchom skrypt z flagą --system-deps"
  exit 1
fi
rm -rf /tmp/_venv_test
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

log "Sprawdzenie składni (py_compile)..."
./venv/bin/python -m py_compile app.py wsgi.py

if [[ ! -f .env ]]; then
  log "Tworzenie .env dla stacku lokalnego..."
  cat > .env <<'ENVEOF'
# Wygenerowane przez scripts/setup-local-dev.sh — dev lokalny
QDRANT_URL=http://127.0.0.1:6333
QDRANT_KEY=dev-local-key
ACTIVE_COLLECTION=dokumenty
OLLAMA_URL=http://127.0.0.1:11434
LLM_MODEL=llama3:latest
ENVEOF
  warn "Utworzono .env (lokalny Qdrant + Ollama). Dla produkcji skopiuj z .env.example i ustaw Qdrant Cloud."
else
  log "Plik .env już istnieje — pomijam (nie nadpisuję)."
fi

if [[ "$SKIP_QDRANT" -eq 0 ]]; then
  mkdir -p .local
  if [[ -x .local/qdrant ]]; then
    log "Qdrant: .local/qdrant już jest — pomijam pobieranie."
  else
    log "Pobieranie Qdrant ${QDRANT_VERSION} (${QDRANT_ASSET})..."
    curl -fsSL -o .local/qdrant.tar.gz \
      "https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/${QDRANT_ASSET}"
    tar -xzf .local/qdrant.tar.gz -C .local
    rm -f .local/qdrant.tar.gz
    chmod +x .local/qdrant
    log "Qdrant zainstalowany: .local/qdrant"
  fi
fi

if [[ "$PULL_MODELS" -eq 1 ]]; then
  if command -v ollama >/dev/null 2>&1; then
    log "Pobieranie modeli Ollama (może potrwać)..."
    ollama pull nomic-embed-text
    ollama pull llama3
  else
    warn "Ollama nie znaleziona w PATH — pomijam --pull-models. Zainstaluj z https://ollama.ai/"
  fi
fi

cat <<EOF

Gotowe (katalog: $ROOT).

Następne kroki:
  1. Terminal A:  ollama serve
  2. Terminal B:  cd $ROOT/.local && ./qdrant
  3. Terminal C:  cd $ROOT && set -a && source .env && set +a && ./venv/bin/python app.py

  Modele (jeśli jeszcze nie masz):  ollama pull nomic-embed-text && ollama pull llama3
  Lub uruchom skrypt z flagą:        ./scripts/setup-local-dev.sh --pull-models

  Przeglądarka: http://localhost:5000
  Health:       curl -s http://127.0.0.1:5000/health

  Pierwsza kolekcja (API):
    curl -s -X POST http://127.0.0.1:5000/collections/create \\
      -H 'Content-Type: application/json' \\
      -d '{"name":"dokumenty","vector_size":768,"switch_to":true}'

  Import przykładu:
    curl -sN 'http://127.0.0.1:5000/import/stream?folder=$ROOT/sample_docs&ext=txt'

EOF
