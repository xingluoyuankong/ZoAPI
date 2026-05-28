@echo off
setlocal

if "%PROXY_PORT%"=="" set PROXY_PORT=17878
set "BASE_URL=http://127.0.0.1:%PROXY_PORT%/v1"

set "OPENAI_API_KEY=zo-proxy"
set "OPENAI_BASE_URL=%BASE_URL%"
if "%HERMES_MODEL%"=="" set "HERMES_MODEL=gpt-5.5"

hermes %*
endlocal
