@echo off
REM AutoClipper — Cutroom local web desk (Windows)
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install Python 3.10+ from python.org and tick "Add to PATH".
  pause
  exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo ERROR: ffmpeg not found. Install from https://www.gyan.dev/ffmpeg/builds/ and add to PATH.
  pause
  exit /b 1
)

if not exist .venv (
  echo [ui] creating .venv...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -U pip
) else (
  call .venv\Scripts\activate.bat
)

echo [ui] ensuring dependencies...
python -m pip install -r requirements.txt -q

echo [ui] opening Cutroom at http://127.0.0.1:7860 ...
python -m gta_autoclipper --ui --host 127.0.0.1 --port 7860
pause
