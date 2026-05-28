#!/usr/bin/env bash
set -euo pipefail

PORT="${PROXY_PORT:-17878}"
BASE_URL="http://127.0.0.1:${PORT}/v1"

export OPENAI_API_KEY="zo-proxy"
export OPENAI_BASE_URL="$BASE_URL"
export HERMES_MODEL="${HERMES_MODEL:-gpt-5.5}"

exec hermes "$@"
