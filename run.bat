@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Python not found. Install Python 3.10+ and ensure "python" works in PATH.
    exit /b 1
  )
  ".venv\Scripts\pip.exe" install -q -r requirements.txt
)

".venv\Scripts\python.exe" launcher.py %*
endlocal
