@echo off
REM Запускает zo-claude-proxy на Windows.
REM Создаёт venv в .\.venv, ставит зависимости, поднимает прокси.
setlocal

cd /d "%~dp0"

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo Создан .env из .env.example - открой и впиши ZO_API_KEY
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Создаю venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Не нашёл python в PATH. Поставь Python 3.10+ и проверь "python --version".
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\pip.exe" install -r requirements.txt
)

".venv\Scripts\python.exe" proxy.py
endlocal
