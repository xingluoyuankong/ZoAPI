#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "                ZoAPI"
echo "       ====================="
echo

if [ ! -x .venv/bin/python ]; then
  echo "[!] Окружение не найдено. Сначала запусти setup.sh"
  exit 1
fi

VPY="./.venv/bin/python"
if ! "$VPY" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, playwright_stealth, patchright
PY
then
  echo "[!] Похоже, зависимости не поставлены до конца. Запусти setup.sh"
  exit 1
fi

exec "$VPY" utils/launcher.py "$@"
