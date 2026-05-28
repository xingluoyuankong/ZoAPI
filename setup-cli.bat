@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  ".venv\Scripts\pip.exe" install -q -r requirements.txt
)

".venv\Scripts\python.exe" setup.py %*
endlocal
