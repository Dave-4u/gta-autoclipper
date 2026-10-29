#!/usr/bin/env bash
# GTA AutoClipper launcher (Linux / macOS / Git Bash)
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  echo "[run] creating .venv…"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -U pip
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "[run] ensuring dependencies…"
pip install -r requirements.txt -q

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found. Install ffmpeg and re-run."
  exit 1
fi

exec python -m gta_autoclipper "$@"
