"""
Тонкая обёртка для совместимости.

Меню теперь живёт в launcher.py (запускается через run.bat / run.sh).
Этот скрипт оставлен для:

    python setup.py --check    # exit 0/1 — есть ли валидный аккаунт
    python setup.py --list     # печать таблицы

Запуск без флагов — открывает то же меню аккаунтов, что и в лончере,
чтобы старые хабиты вроде `python setup.py` тоже работали.
"""

from __future__ import annotations

import argparse
import sys

from accounts import AccountStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 0 если есть валидный аккаунт, иначе 1",
    )
    ap.add_argument(
        "--list", action="store_true", help="распечатать таблицу и выйти"
    )
    args = ap.parse_args()

    store = AccountStore()

    if args.check:
        return 0 if store.list_usable() else 1

    if args.list:
        from launcher import _render_accounts

        print(_render_accounts(store))
        return 0

    from launcher import accounts_menu

    accounts_menu(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
