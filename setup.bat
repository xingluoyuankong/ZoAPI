@echo off
chcp 65001 > nul
setlocal EnableExtensions
cd /d "%~dp0"

title ZoAPI setup

echo.
echo                 ZoAPI
echo        =====================
echo.
echo  Pervichnaya ustanovka okruzheniya
echo.

set "PYTHON_EXE="

where py >nul 2>nul
if %ERRORLEVEL%==0 set "PYTHON_EXE=py -3"

if not defined PYTHON_EXE (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
  echo [!] Python ne nayden v PATH. Ustanovi Python 3.12 ili 3.13.
  echo.
  pause
  exit /b 1
)

echo [+] Python: %PYTHON_EXE%

if not exist ".venv\Scripts\python.exe" (
  echo [+] Sozdayu virtualnoe okruzhenie .venv ...
  %PYTHON_EXE% -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [!] Ne udalos sozdat .venv. Proverki Python.
    echo.
    pause
    exit /b 1
  )
)

set "VPY=.venv\Scripts\python.exe"

echo [+] Obnovlyayu pip / setuptools / wheel ...
"%VPY%" -m pip install --quiet --upgrade pip setuptools wheel

echo [+] Stavlyu zavisimosti iz requirements.txt ...
"%VPY%" -m pip uninstall -y playwright-stealth >nul 2>nul
"%VPY%" -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo [!] Ne udalos postavit zavisimosti.
  echo     Esli upalo na pydantic-core / pyo3 — Python slishkom svezhiy.
  echo     Postav Python 3.12 ili 3.13 i povtori setup.bat.
  echo.
  pause
  exit /b 1
)

echo [+] Proveryayu importy ...
"%VPY%" -c "import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, patchright"
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo [!] Ne vse importy proshli. Otkroy log vyshe.
  echo.
  pause
  exit /b 1
)

echo [+] Skachivayu Chromium dlya Playwright ...
"%VPY%" -m playwright install chromium
"%VPY%" -m patchright install chromium >nul 2>nul

echo.
echo [+] Gotovo. Seychas zapuschu ZoAPI...
echo.
call run.bat
exit /b %ERRORLEVEL%
