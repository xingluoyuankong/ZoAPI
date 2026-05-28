#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "                ZoAPI"
echo "       ====================="
echo
echo "  Первичная установка окружения"
echo

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "[!] Python 3.10+ не найден."
  exit 1
fi

if ! "$PYTHON_CMD" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "[!] Ну