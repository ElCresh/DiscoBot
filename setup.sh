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

mkdir -p soundfonts vendor

# Cloudflared (per il tunnel pubblico gestito da DiscoBot)
CLOUDFLARED="vendor/cloudflared"
if [ -x "$CLOUDFLARED" ] && "$CLOUDFLARED" --version >/dev/null 2>&1; then
    echo "cloudflared gia' presente: $($CLOUDFLARED --version 2>/dev/null | head -1)"
else
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    ASSET=""
    EXTRACT="binary"
    case "$OS-$ARCH" in
        Linux-x86_64)   ASSET="cloudflared-linux-amd64" ;;
        Linux-aarch64)  ASSET="cloudflared-linux-arm64" ;;
        Linux-armv7l)   ASSET="cloudflared-linux-arm" ;;
        Darwin-x86_64)  ASSET="cloudflared-darwin-amd64.tgz"; EXTRACT="tgz" ;;
        Darwin-arm64)   ASSET="cloudflared-darwin-arm64.tgz"; EXTRACT="tgz" ;;
        *)
            echo "Attenzione: piattaforma non riconosciuta ($OS $ARCH). Tunnel pubblico non disponibile finche' non scarichi cloudflared manualmente in $CLOUDFLARED" >&2
            ASSET=""
            ;;
    esac
    if [ -n "$ASSET" ]; then
        URL="https://github.com/cloudflare/cloudflared/releases/latest/download/$ASSET"
        echo "Scarico cloudflared ($ASSET)..."
        if [ "$EXTRACT" = "tgz" ]; then
            curl -fsSL "$URL" -o /tmp/cloudflared.tgz && \
                tar -xzf /tmp/cloudflared.tgz -C vendor/ && \
                rm -f /tmp/cloudflared.tgz
        else
            curl -fsSL "$URL" -o "$CLOUDFLARED"
        fi
        if [ -f "$CLOUDFLARED" ]; then
            chmod +x "$CLOUDFLARED"
            echo "cloudflared installato: $($CLOUDFLARED --version 2>/dev/null | head -1)"
        else
            echo "Attenzione: download cloudflared fallito" >&2
        fi
    fi
fi

echo "Setup completato. Avvia con ./run.sh"
