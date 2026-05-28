@echo off
REM Запускает Claude Code, направив его на локальный zo-claude-proxy.
setlocal

if "%PROXY_PORT%"=="" set PROXY_PORT=17878

set "ANTHROPIC_BASE_URL=http://127.0.0.1:%PROXY_PORT%"
set "ANTHROPIC_AUTH_TOKEN=zo-proxy"
set "ANTHROPIC_API_KEY="
set "DISABLE_TELEMETRY=1"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"

claude %*
endlocal
