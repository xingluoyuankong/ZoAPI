#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PROXY_PORT:-17878}"
BASE_URL="http://127.0.0.1:${PORT}/v1"
CODEX_HOME_DIR="${CODEX_HOME:-$PWD/.codex-home}"
mkdir -p "$CODEX_HOME_DIR"

cat > "$CODEX_HOME_DIR/config.toml" <<EOF
openai_base_url = "$BASE_URL"
model = "gpt-5.3-codex"
EOF

export CODEX_HOME="$CODEX_HOME_DIR"
export OPENAI_API_KEY="zo-proxy"
export OPENAI_BASE_URL="$BASE_URL"

exec codex "$@"
