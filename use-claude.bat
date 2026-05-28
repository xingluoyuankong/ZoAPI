@echo off
REM Запускает Claude Code, направив его на локальный zo-claude-proxy.
setlocal

if "%PROXY_PORT%"=="" set PROXY_PORT=17878

if defined ANTHROPIC_API_KEY echo [warn] ANTHROPIC_API_KEY already set in this shell. Clearing it locally for Claude.
if defined ANTHROPIC_BASE_URL echo [warn] ANTHROPIC_BASE_URL already set in this shell. Overriding it for Claude.
reg query HKCU\Environment /v ANTHROPIC_API_KEY >nul 2>&1 && echo [warn] Persistent user env ANTHROPIC_API_KEY is set in Windows. This wrapper clears it only for the current session.
reg query HKCU\Environment /v ANTHROPIC_BASE_URL >nul 2>&1 && echo [warn] Persistent user env ANTHROPIC_BASE_URL is set in Windows. This wrapper overrides it only for the current session.

set "ANTHROPIC_BASE_URL=http://127.0.0.1:%PROXY_PORT%"
set "ANTHROPIC_AUTH_TOKEN=zo-proxy"
set "ANTHROPIC_API_KEY="
set "DISABLE_TELEMETRY=1"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"

claude %*
endlocal
