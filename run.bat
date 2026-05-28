@echo off
chcp 65001 > nul
setlocal EnableExtensions
cd /d "%~dp0"

title ZoAPI

echo.
echo                 ZoAPI
echo        =====================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [!] Окружение не найдено.
  echo     Сначала запусти setup.bat
  echo.
  pause
  exit /b 1
)

set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, patchright" >nul 2>nul
if errorlevel 1 (
  echo [!] Похоже, зависимости не поставлены до конца.
  echo     Запусти setup.bat
  echo.
  pause
  exit /b 1
)

"%VPY%" utils\launcher.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [!] Приложение завершилось с кодом %EXITCODE%.
  echo.
  pause
)
exit /b %EXITCODE%
