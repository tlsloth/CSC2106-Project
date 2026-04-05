#!/bin/bash

# ------------------------------------------------------------
# Upload pico_mpr_bridge project to Raspberry Pi Pico W
# Requires: mpremote installed and Pico W connected via USB
# Run this script from inside the pico_mpr_bridge folder
# ------------------------------------------------------------

set -e

echo ""
echo "[1/6] Checking mpremote..."
if ! command -v mpremote &> /dev/null; then
    echo "ERROR: mpremote not found."
    echo "Install with: pip install mpremote"
    exit 1
fi

PORT="$1"
if [ -z "$PORT" ]; then
    echo -n "[2/6] Enter Pico W port (e.g. /dev/tty.usbmodem101 or leave blank for auto): "
    read PORT
fi

if [ -z "$PORT" ]; then
    MP_CONN="connect auto"
    echo "[2/6] No port given — using auto-detect..."
else
    MP_CONN="connect $PORT"
    echo "[2/6] Using port: $PORT"
fi

echo "[2/6] Checking Pico W connection..."
if ! mpremote $MP_CONN fs ls > /dev/null 2>&1; then
    echo "ERROR: Cannot connect to Pico W."
    echo "1) Confirm Pico W is connected and MicroPython is flashed."
    echo "2) Check available ports with: ls /dev/tty.usb* or ls /dev/ttyACM*"
    echo "3) Retry with: ./upload_to_pico.sh /dev/tty.usbmodemXXXX"
    exit 1
fi

# Use script location as project root
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

echo "[3/6] Verifying project structure..."
for required in "main.py" "config.py" "core" "interfaces" "utils" "lib"; do
    if [ ! -e "$required" ]; then
        echo "ERROR: '$required' not found in $PROJECT_DIR"
        exit 1
    fi
done

echo "[3/6] Uploading top-level Python files..."
for f in *.py; do
    echo "  - $f"
    mpremote $MP_CONN fs cp "$f" ":$f"
done

echo "[4/6] Uploading folders..."
for folder in core interfaces utils lib; do
    echo "  - $folder/"
    mpremote $MP_CONN fs cp -r "$folder" ":"
done

echo "[5/6] Resetting Pico W..."
mpremote $MP_CONN reset

echo "[6/6] Done."
echo "Upload complete. Pico W reset triggered."
echo ""
echo "Tip: To view logs:"
echo "  mpremote connect auto repl"
echo "  or"
echo "  mpremote $MP_CONN repl"
echo ""