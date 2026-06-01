#!/bin/bash

# install-user-service.sh
# Automatyczna instalacja usługi systemd użytkownika dla AI Analiza Dokumentów.
#
# Użycie:
#   ./install-user-service.sh
#
# Skrypt:
# - kopiuje ai_analiza-user.service do ~/.config/systemd/user/
# - przeładowuje systemd użytkownika
# - włącza i uruchamia usługę
# - wyświetla status

set -e

SERVICE_NAME="ai_analiza"
SOURCE_SERVICE_FILE="ai_analiza-user.service"
TARGET_DIR="$HOME/.config/systemd/user"
TARGET_FILE="$TARGET_DIR/$SERVICE_NAME.service"

echo "========================================"
echo "  Instalacja usługi użytkownika systemd"
echo "  AI Analiza Dokumentów"
echo "========================================"
echo ""

# Sprawdzenie czy plik źródłowy istnieje
if [ ! -f "$SOURCE_SERVICE_FILE" ]; then
    echo "BŁĄD: Nie znaleziono pliku $SOURCE_SERVICE_FILE w bieżącym katalogu."
    echo "Uruchom ten skrypt z głównego katalogu projektu."
    exit 1
fi

# Utwórz katalog docelowy jeśli nie istnieje
echo "[1/5] Tworzenie katalogu użytkownika systemd..."
mkdir -p "$TARGET_DIR"
echo "      ✓ $TARGET_DIR"

# Skopiuj plik usługi
echo ""
echo "[2/5] Kopiowanie pliku usługi..."
cp "$SOURCE_SERVICE_FILE" "$TARGET_FILE"
echo "      ✓ Skopiowano do $TARGET_FILE"

# Przeładuj systemd użytkownika
echo ""
echo "[3/5] Przeładowywanie systemd użytkownika..."
systemctl --user daemon-reload
echo "      ✓ Daemon przeładowany"

# Włącz usługę (autostart przy logowaniu)
echo ""
echo "[4/5] Włączanie usługi..."
systemctl --user enable "$SERVICE_NAME" >/dev/null
echo "      ✓ Usługa włączona (autostart)"

# Uruchom / zrestartuj usługę
echo ""
echo "[5/5] Uruchamianie usługi..."
systemctl --user restart "$SERVICE_NAME"
echo "      ✓ Usługa uruchomiona"

# Pokaż status
echo ""
echo "========================================"
echo "  Status usługi"
echo "========================================"
systemctl --user status "$SERVICE_NAME" --no-pager -l

echo ""
echo "✓ Instalacja zakończona sukcesem!"
echo ""
echo "Przydatne komendy:"
echo "  ./restart-app.sh --user logs          # logi na żywo"
echo "  journalctl --user -u $SERVICE_NAME -f # alternatywa"
echo "  systemctl --user status $SERVICE_NAME"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  systemctl --user stop $SERVICE_NAME"
echo ""
echo "Aby usługa startowała nawet po wylogowaniu z systemu:"
echo "  loginctl enable-linger $USER"
echo ""
echo "========================================"
