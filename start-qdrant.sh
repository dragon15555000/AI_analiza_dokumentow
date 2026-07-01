#!/bin/bash

# Startuje lokalnego Qdrant, jeśli dostępna jest binarka w .local/.
# Używane przez start/restart-app.sh oraz opcjonalnie przez systemd user service.

set -e

cd "$(dirname "$0")" || exit 1

QDRANT_BIN=""
for candidate in "./.local/qdrant" "./qdrant"; do
    if [ -x "$candidate" ]; then
        QDRANT_BIN="$candidate"
        break
    fi
done

if [ -z "$QDRANT_BIN" ]; then
    echo "Nie znaleziono binarki Qdrant w .local/qdrant ani ./qdrant" >&2
    exit 1
fi

if curl -sf http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
    echo "Qdrant już działa"
    exit 0
fi

mkdir -p qdrant_data
nohup "$QDRANT_BIN" >> qdrant.log 2>&1 &

for _ in $(seq 1 20); do
    if curl -sf http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
        echo "Qdrant uruchomiony"
        exit 0
    fi
    sleep 1
done

echo "Qdrant nie odpowiedział w 20s" >&2
exit 1
