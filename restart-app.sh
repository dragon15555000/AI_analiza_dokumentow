#!/bin/bash

# restart-app.sh
# Wygodny skrypt do restartu aplikacji AI Analiza Dokumentów
#
# Użycie:
#   ./restart-app.sh                 → nohup (prosty tryb)
#   ./restart-app.sh --user          → systemd user service (zalecane)
#   ./restart-app.sh --user --stop   → zatrzymaj user service
#   ./restart-app.sh --stop          → zatrzymaj nohup wersję

set -e

cd "$(dirname "$0")" || exit 1

USE_USER_SERVICE=false
ACTION=""

# Parsowanie argumentów
for arg in "$@"; do
    case $arg in
        --user)
            USE_USER_SERVICE=true
            ;;
        --stop)
            ACTION="stop"
            ;;
        start|stop|restart|status)
            ACTION="$arg"
            ;;
    esac
done

SERVICE_NAME="ai_analiza"

# ============================================
# TRYB: systemd --user (zalecany)
# ============================================
if [ "$USE_USER_SERVICE" = true ]; then
    echo "========================================"
    echo "   Restart (systemd user service) — zalecane"
    echo "========================================"

    if [ "$ACTION" = "stop" ] || [ "$1" = "--stop" ]; then
        echo "Zatrzymuję usługę użytkownika..."
        systemctl --user stop "$SERVICE_NAME" || true
        echo "✓ Usługa zatrzymana"
        exit 0
    fi

    if [ "$ACTION" = "status" ]; then
        systemctl --user status "$SERVICE_NAME" --no-pager
        exit 0
    fi

    if [ "$ACTION" = "logs" ]; then
        echo "Pokazuję logi (Ctrl+C aby wyjść)..."
        journalctl --user -u "$SERVICE_NAME" -f
        exit 0
    fi

    if [ "$ACTION" = "restart" ] || [ -z "$ACTION" ]; then
        echo "Restartuję usługę użytkownika..."
        systemctl --user restart "$SERVICE_NAME"
        sleep 1
        systemctl --user status "$SERVICE_NAME" --no-pager -l
        echo ""
        echo "Logi: ./restart-app.sh --user logs"
        echo "      lub: journalctl --user -u $SERVICE_NAME -f"
        exit 0
    fi

    # Domyślnie start
    echo "Uruchamiam usługę użytkownika..."
    systemctl --user start "$SERVICE_NAME"
    sleep 1
    systemctl --user status "$SERVICE_NAME" --no-pager -l
    echo ""
    echo "Logi: ./restart-app.sh --user logs"
    exit 0
fi

# ============================================
# TRYB: nohup (prosty / development)
# ============================================

echo "========================================"
echo "   Restart aplikacji AI Analiza Dokumentów (nohup)"
echo "========================================"

# 1. Zatrzymaj poprzednią instancję
echo ""
echo "[1/4] Zatrzymuję poprzednią instancję..."

ps aux | awk '/python.*app.py/ && !/awk/ {print $2}' | while read -r pid; do
    if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
        echo "  Zabijam proces PID: $pid"
        kill -9 "$pid" 2>/dev/null || true
    fi
done

sleep 2
echo "  ✓ Poprzednia instancja zatrzymana"

# 2. Opcjonalnie pobierz najnowszy kod
echo ""
read -p "[2/4] Pobrać najnowszy kod z GitHub? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  Pobieram zmiany..."
    git fetch origin
    git pull origin master
    echo "  ✓ Kod zaktualizowany do najnowszej wersji"
else
    echo "  Pominięto aktualizację kodu"
fi

# 3. Sprawdzenie środowiska
echo ""
if [ ! -f "venv/bin/python" ]; then
    echo "BŁĄD: Nie znaleziono środowiska wirtualnego (venv/bin/python)"
    echo "Najpierw utwórz je poleceniem:"
    echo "  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
fi

# 4. Uruchom aplikację w tle
echo "[3/4] Uruchamiam aplikację w tle..."

nohup ./venv/bin/python app.py > app.log 2>&1 &
PID=$!

sleep 3

if ps -p "$PID" > /dev/null 2>&1; then
    echo "  ✓ Aplikacja uruchomiona pomyślnie (PID: $PID)"
else
    echo "  ⚠️  Uruchomienie nie powiodło się."
    echo "     Sprawdź logi: tail -n 30 app.log"
    exit 1
fi

echo ""
echo "[4/4] Gotowe!"
echo ""
echo "   Adres:        http://127.0.0.1:5000"
echo "   Logi na żywo: tail -f app.log"
echo "   Zatrzymaj:    ./restart-app.sh --stop"
echo ""
echo "========================================"

# Obsługa --stop w trybie nohup
if [ "$ACTION" = "stop" ]; then
    echo "Zatrzymuję aplikację (nohup)..."
    ps aux | awk '/python.*app.py/ && !/awk/ {print $2}' | while read -r pid; do
        if [ -n "$pid" ]; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    echo "Zatrzymano."
fi

