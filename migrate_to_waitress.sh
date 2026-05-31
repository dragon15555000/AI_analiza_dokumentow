#!/bin/bash
# Prosty skrypt pomocniczy do migracji na waitress

set -e

echo "=== Migracja na produkcyjny serwer (waitress) ==="

if [ ! -d "venv" ]; then
    echo "Błąd: Nie znaleziono folderu venv"
    exit 1
fi

echo "1. Instaluję waitress..."
source venv/bin/activate
pip install waitress

echo "2. Kopiuję serwis systemd..."
sudo cp mzk_web.service /etc/systemd/system/

echo "3. Przeładowuję systemd..."
sudo systemctl daemon-reload

echo "4. Włączam i startuję serwis..."
sudo systemctl enable --now mzk_web

echo "5. Status:"
sudo systemctl status mzk_web --no-pager

echo ""
echo "Gotowe."
echo "Logi na żywo: sudo journalctl -u mzk_web -f"
