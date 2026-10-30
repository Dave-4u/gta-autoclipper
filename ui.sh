#!/usr/bin/env bash
# GTA AutoClipper — local Gradio web UI
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "[ui] creating .venv…"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -U pip
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "[ui] ensuring dependencies…"
pip install -r requirements.txt -q

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found. Install ffmpeg and re-run."
  exit 1
fi

echo "[ui] opening http://127.0.0.1:7860 …"
exec python -m gta_autoclipper --ui --host 127.0.0.1 --port 7860
