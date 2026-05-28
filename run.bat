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
  echo [!] Okruzhenie ne naydeno. Snachala zapusti setup.bat
  echo.
  pause
  exit /b 1
)

set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, patchright" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
  echo [!] Zavisimosti ne polnostyu ustanovleny. Zapusti setup.bat
  echo.
  pause
  exit /b 1
)

"%VPY%" utils\launcher.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo [!] Prilozhenie zavershilos s kodom %EXITCODE%.
  echo.
  pause
)
exit /b %EXITCODE%
