#!/bin/bash
# Prosty skrypt pomocniczy do migracji na waitress

set -e

cd "$(dirname "$0")"

echo "=== Migracja na produkcyjny serwer (waitress) ==="

if [ ! -d "venv" ]; then
    echo "Błąd: Nie znaleziono folderu venv"
    exit 1
fi

echo "1. Instaluję waitress..."
source venv/bin/activate
pip install waitress

echo "2. Kopiuję i konfiguruję serwis systemd..."
CURRENT_USER=${SUDO_USER:-$(whoami)}
CURRENT_DIR=$(pwd)
sed -e "s|{{USER}}|${CURRENT_USER}|g" \
    -e "s|{{WORKDIR}}|${CURRENT_DIR}|g" \
    ai_analiza.service | sudo tee /etc/systemd/system/ai_analiza.service > /dev/null

echo "3. Przeładowuję systemd..."
sudo systemctl daemon-reload

echo "4. Włączam i startuję serwis..."
sudo systemctl enable --now ai_analiza

echo "5. Status:"
sudo systemctl status ai_analiza --no-pager

echo ""
echo "Gotowe."
echo "Logi na żywo: sudo journalctl -u ai_analiza -f"
