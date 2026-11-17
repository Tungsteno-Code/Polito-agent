#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Setup PoliTO Agent & Sbobinatore (Fedora Linux) ==="

echo "[1/4] Installazione pacchetti di sistema tramite dnf..."
DNF_PKGS=(
  pandoc
  texlive-scheme-medium
  texlive-xetex
  texlive-collection-fontsrecommended
)
if ! command -v ffmpeg >/dev/null 2>&1; then
  DNF_PKGS+=(ffmpeg-free)
fi
sudo dnf install -y "${DNF_PKGS[@]}"

echo "[2/4] Creazione/aggiornamento virtual environment Python (.venv)..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[3/4] Installazione browser Chromium per Playwright..."
.venv/bin/playwright install chromium

echo "[4/4] Configurazione file .env e symlink CLI..."
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Creato .env da .env.example (compila le tue chiavi/credenziali)."
else
  echo ".env già esistente: preservato senza sovrascritture."
fi

chmod +x "$SCRIPT_DIR/polito" "$SCRIPT_DIR/sbobina.py" "$SCRIPT_DIR/setup.sh"
mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/polito" "$HOME/.local/bin/polito"

echo "=== Setup completato! Prova con: polito --help ==="
