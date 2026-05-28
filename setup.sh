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
  PYTHON_EXE="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_EXE="python"
else
  echo "[!] Python 3.10+ не найден."
  exit 1
fi

if ! "$PYTHON_EXE" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  echo "[!] Нужен Python 3.10 или новее. Сейчас: $($PYTHON_EXE --version 2>&1)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[+] Создаю виртуальное окружение..."
  "$PYTHON_EXE" -m venv .venv
else
  echo "[+] Виртуальное окружение уже есть."
fi

VPY="./.venv/bin/python"

echo "[+] Обновляю pip / setuptools / wheel..."
"$VPY" -m pip install --quiet --upgrade pip setuptools wheel

echo "[+] Устанавливаю зависимости проекта..."
"$VPY" -m pip uninstall -y playwright-stealth >/dev/null 2>&1 || true
"$VPY" -m pip install -r requirements.txt

echo "[+] Проверяю Python-модули..."
"$VPY" -c "import fastapi, uvicorn, httpx, pydantic, questionary, rich, playwright, patchright; print('ok')"

echo "[+] Ставлю браузер Chromium для Playwright..."
"$VPY" -m playwright install chromium
"$VPY" -m patchright install chromium >/dev/null 2>&1 || true

echo
echo "[+] Готово. Сейчас запущу ZoAPI..."
echo
exec ./run.sh
