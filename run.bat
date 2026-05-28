@echo off
chcp 65001 > nul
setlocal EnableExtensions
cd /d "%~dp0"

title zo-claude-proxy

echo.
echo   zo-claude-proxy
echo   ===============
echo.

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if "%PYTHON_CMD%"=="" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)

if "%PYTHON_CMD%"=="" (
  echo [!] Python 3.10+ не найден.
  echo     Установи Python с https://www.python.org/downloads/windows/
  echo     В установщике поставь галочку "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [!] Нужен Python 3.10 или новее.
  %PYTHON_CMD% --version
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [+] Создаю виртуальное окружение .venv ...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 (
    echo [!] Не удалось создать .venv.
    echo.
    pause
    exit /b 1
  )
)

set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import fastapi, uvicorn, httpx, pydantic, questionary, browser_cookie3" >nul 2>nul
if errorlevel 1 (
  echo [+] Ставлю/обновляю зависимости проекта ...
  "%VPY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo [!] Не удалось установить зависимости.
    echo.
    pause
    exit /b 1
  )
)

"%VPY%" utils\launcher.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [!] Лончер завершился с ошибкой %EXITCODE%.
  echo.
  pause
)

exit /b %EXITCODE%
