#!/usr/bin/env bash
# Запускает прокси. Использует venv в ./.venv если он есть.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Создан .env из .env.example — открой и впиши ZO_API_KEY"
    exit 1
  fi
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

exec ./.venv/bin/python proxy.py
