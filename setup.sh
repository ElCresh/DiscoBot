#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Errore: '$PYTHON_BIN' non trovato. Installa Python 3 o esporta PYTHON_BIN." >&2
    exit 1
fi

if ! command -v vlc >/dev/null 2>&1 && ! ldconfig -p 2>/dev/null | grep -q libvlc; then
    echo "Attenzione: libvlc non sembra installato. Su Debian/Ubuntu: sudo apt install vlc libvlc-dev" >&2
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creo virtualenv in $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "Creato .env da .env.example — modifica i valori se necessario."
fi

mkdir -p soundfonts

echo "Setup completato. Avvia con ./run.sh"
