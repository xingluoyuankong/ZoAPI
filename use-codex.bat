@echo off
setlocal
cd /d "%~dp0"

if "%PROXY_PORT%"=="" set PROXY_PORT=17878
set "BASE_URL=http://127.0.0.1:%PROXY_PORT%/v1"

if "%CODEX_HOME%"=="" set "CODEX_HOME=%CD%\.codex-home"
if not exist "%CODEX_HOME%" mkdir "%CODEX_HOME%"

> "%CODEX_HOME%\config.toml" (
  echo openai_base_url = "%BASE_URL%"
  echo model = "gpt-5.3-codex"
)

set "OPENAI_API_KEY=zo-proxy"
set "OPENAI_BASE_URL=%BASE_URL%"

codex %*
endlocal
