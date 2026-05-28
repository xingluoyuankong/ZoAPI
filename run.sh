#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

if ! ./.venv/bin/python setup.py --check; then
  echo "No usable Zo account. Opening setup wizard..."
  ./.venv/bin/python setup.py
  if ! ./.venv/bin/python setup.py --check; then
    echo "Still no account. Exiting."
    exit 1
  fi
fi

exec ./.venv/bin/python proxy.py
