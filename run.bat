@echo off
REM GTA AutoClipper launcher (Windows)
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
  echo [run] creating .venv...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -U pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

python -m gta_autoclipper %*
