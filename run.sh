#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Virtualenv non trovato. Esegui prima ./setup.sh" >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

exec python main.py "$@"
