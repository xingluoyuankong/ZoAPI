#!/usr/bin/env bash
# Запускает Claude Code, направив его на локальный zo-claude-proxy.
set -euo pipefail

PORT="${PROXY_PORT:-17878}"

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "[warn] ANTHROPIC_API_KEY already set in this shell. Clearing it locally for Claude."
fi
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  echo "[warn] ANTHROPIC_BASE_URL already set in this shell. Overriding it for Claude."
fi

export ANTHROPIC_BASE_URL="http://127.0.0.1:${PORT}"
export ANTHROPIC_AUTH_TOKEN="zo-proxy"      # любая непустая строка
export ANTHROPIC_API_KEY=""                  # должна быть пустой, иначе CLI пойдёт в Anthropic
export DISABLE_TELEMETRY="1"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"

# по умолчанию sonnet → MODEL_MAP в .env разрулит в anthropic:claude-opus-4-7
exec claude "$@"
