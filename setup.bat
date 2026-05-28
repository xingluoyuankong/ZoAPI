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
if "%PYTHON_CM