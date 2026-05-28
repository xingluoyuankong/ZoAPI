@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if "%PROXY_PORT%"=="" set PROXY_PORT=17878
set "BASE_URL=http://127.0.0.1:%PROXY_PORT%/v1"
set "OPENAI_API_KEY=zo-proxy"
set "OPENAI_BASE_URL=%BASE_URL%"

set "OPENCODE_CONFIG_CONTENT={""$schema"":""https://opencode.ai/config.json"",""provider"":{""zo"":{""npm"":""@ai-sdk/openai-compatible"",""name"":""Zo Proxy"",""options"":{""baseURL"":""%BASE_URL%"",""apiKey"":""{env:OPENAI_API_KEY}""},""models"":{""gpt-5.3-codex"":{""name"":""GPT-5.3 Codex via Zo""},""gpt-5.5"":{""name"":""GPT-5.5 via Zo""},""gpt-5.4-mini"":{""name"":""GPT-5.4 Mini via Zo""},""claude-sonnet-4-6"":{""name"":""Claude Sonnet 4.6 via Zo""},""claude-opus-4-7"":{""name"":""Claude Opus 4.7 via Zo""},""gemini-3.1-pro-preview"":{""name"":""Gemini 3.1 Pro via Zo""}}}},""enabled_providers"": [""zo""],""model"":""zo/gpt-5.3-codex"",""small_model"":""zo/gpt-5.4-mini"",""autoupdate"":false}"

opencode %*
endlocal
