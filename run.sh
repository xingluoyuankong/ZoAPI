#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  zo-claude-proxy"
echo "  ==============="
echo

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "[!] Python 3.10+ не найден. Установи Python и повтори запуск."
  exit 1
fi

if ! "$PYTHON_CMD" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "[!] Нужен Python 3.10 или новее. Сейчас: $($PYTHON_CMD --version 2>&1)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[+] Создаю виртуальное окружение .venv ..."
  "$PYTHON_CMD" -m venv .venv
fi

VPY="./.venv/bin/python"

if ! "$VPY" - <<'PY' >/dev/null 2>&1
import fastapi, uvicorn, httpx, pydantic, questionary, browser_cookie3
PY
then
  echo "[+] Ставлю/обновляю зависимости проекта ..."
  "$VPY" -m pip install --quiet -r requirements.txt
fi

exec "$VPY" utils/launcher.py "$@"
