@echo off
chcp 65001 > nul
setlocal EnableExtensions
cd /d "%~dp0"

title ZoAPI setup

echo.
echo                 ZoAPI
echo        =====================
echo.
echo  Первичная установка окружения
echo.

set "PYTHON_EXE="

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=py -3"
)

if "%PYTHON_EXE%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
  )
)

if "%PYTHON_EXE%"=="" (
  echo [!] Python 3.10+ не найден.
  echo     Установи Python и повтори запуск.
  echo.
  pause
  exit /b 1
)

%PYTHON_EXE% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo [!] Нужен Python 3.10 или новее.
  %PYTHON_EXE% --version
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [+] Создаю виртуальное окружение...
  %PYTHON_EXE% -m venv .venv
  if errorlevel 1 (
    echo [!] Не удалось создать .venv
    echo.
    pause
    exit /b 1
  )
) else (
  echo [+] Виртуальное окружение уже есть.
)

set "VPY=.venv\Scripts\python.exe"

echo [+] Обновляю pip / setuptools / wheel...
"%VPY%" -m pip install --quiet --upgrade pip setuptools wheel
if errorlevel 1 (
  echo [!] Не удалось обновить pip / setuptools / wheel
  echo.
  pause
  exit /b 1
)

echo [+] Устанавливаю зависимости проекта...
"%VPY%" -m pip uninstall -y playwright-stealth >nul 2>nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [!] Не удалось поставить зависимости.
  echo.
  pause
  exit /b 1
)

echo [+] Проверяю Python-модули...
"%VPY%" -c "import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, patchright; print('ok')"
if errorlevel 1 (
  echo [!] Проверка импортов не прошла.
  echo.
  pause
  exit /b 1
)

echo [+] Ставлю браузер Chromium для Playwright...
"%VPY%" -m playwright install chromium
if errorlevel 1 (
  echo [!] Не удалось установить Chromium для Playwright.
  echo.
  pause
  exit /b 1
)

echo [+] Ставлю браузер Chromium для patchright...
"%VPY%" -m patchright install chromium
if errorlevel 1 (
  echo [!] Не удалось установить Chromium для patchright. Продолжаю.
)

echo.
echo [+] Готово. Сейчас запущу ZoAPI...
echo.
call run.bat
exit /b %ERRORLEVEL%
