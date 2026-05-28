"""
Интерактивная настройка аккаунтов для zo-claude-proxy.

Запускать:
    python setup.py            # интерактивное меню
    python setup.py --check    # просто проверить что есть валидный аккаунт; exit 0/1
    python setup.py --list     # вывести таблицу аккаунтов и выйти

Меню умеет:
 - показать список с E-mail / доменом / TTL access_token / streak ошибок
 - добавить аккаунт через вставку Cookie-хедера из DevTools
 - удалить аккаунт
 - переключить активный
 - проверить (ping /models/available) каждый аккаунт
 - вкл/выкл аккаунт
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time

from accounts import (
    Account,
    AccountStore,
    extract_domain_from_access_token,
    extract_tokens_from_cookie,
)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return f"истёк {-seconds // 60} мин назад"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def render_table(store: AccountStore) -> str:
    if not store.accounts:
        return "  (нет аккаунтов — добавь первый через [a])"
    rows = []
    rows.append(f"  {'#':<3}{'active':<8}{'label':<14}{'email':<28}{'domain':<14}{'TTL':<14}{'streak':<8}{'state':<10}")
    rows.append("  " + "-" * 100)
    for i, a in enumerate(store.accounts):
        marker = "→" if a.label == store.active_label else " "
        email = a.email() or "?"
        ttl = _fmt_ttl(a.seconds_until_expiry())
        state = "off" if a.disabled else ("err" if a.error_streak else "ok")
        rows.append(
            f"  {i:<3}{marker:<8}{a.label:<14}{email:<28}{a.domain:<14}{ttl:<14}{a.error_streak:<8}{state:<10}"
        )
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# add account
# ---------------------------------------------------------------------------


COOKIE_INSTRUCTIONS = """\
Достать cookies из браузера:

  1. Открой свой Zo workspace (например https://uvenaliy.zo.computer)
  2. F12 → Network → отправь любое сообщение в чат
  3. Найди запрос POST /ask
  4. Headers → Request Headers → найди строку 'cookie: ...'
  5. Скопируй её ВСЮ (правой кнопкой → Copy value)

Можно вставить только две куки (access_token=...; refresh_token=...) —
этого достаточно. Лишние куки безвредны.
"""


def add_account_interactive(store: AccountStore) -> None:
    print("\n" + COOKIE_INSTRUCTIONS)
    print("Вставь Cookie-хедер (можно многострочно, заверши пустой строкой):\n")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    cookie_raw = " ".join(lines)
    access, refresh = extract_tokens_from_cookie(cookie_raw)
    if not access:
        print("\n[!] Не нашёл access_token в Cookie. Попробуй снова.")
        return
    if not refresh:
        print("\n[?] refresh_token не найден. Auto-refresh работать не будет — окей?")
        if input("    продолжить без refresh_token? [y/N]: ").strip().lower() != "y":
            return

    domain = extract_domain_from_access_token(access) or ""
    domain_in = input(f"\nДомен на zo.computer [{domain}]: ").strip() or domain
    if not domain_in:
        print("[!] Домен обязателен. Отменено.")
        return

    suggested_label = f"acc{len(store.accounts) + 1}"
    label = input(f"label (короткое имя для этого аккаунта) [{suggested_label}]: ").strip() or suggested_label

    if any(a.label == label for a in store.accounts):
        if input(f"[?] аккаунт '{label}' уже есть, перезаписать? [y/N]: ").strip().lower() != "y":
            return

    acc = Account(
        label=label,
        domain=domain_in,
        access_token=access,
        refresh_token=refresh,
        added_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    make_active = not store.accounts or input("сделать активным? [Y/n]: ").strip().lower() != "n"
    store.add(acc, make_active=make_active)
    print(f"\n[+] Добавлен '{label}' (email={acc.email() or '?'}, TTL≈{_fmt_ttl(acc.seconds_until_expiry())})")


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


async def ping_all(store: AccountStore) -> None:
    from zo_client import ZoClient
    client = ZoClient()
    try:
        for a in store.accounts:
            print(f"  [{a.label}] ping... ", end="", flush=True)
            try:
                models = await client.list_models(a)
                print(f"OK ({len(models)} моделей)")
                store.mark_ok(a.label)
            except Exception as e:
                print(f"FAIL — {type(e).__name__}: {e}")
                store.mark_err(a.label, str(e), max_streak=999)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


def menu(store: AccountStore) -> None:
    while True:
        print("\n=== zo-claude-proxy: аккаунты ===")
        print(render_table(store))
        print(
            "\nКоманды:\n"
            "  a       — добавить аккаунт\n"
            "  s N     — сделать активным аккаунт #N\n"
            "  r N     — удалить аккаунт #N\n"
            "  t       — пингануть все аккаунты\n"
            "  d N     — отключить (disable) аккаунт #N\n"
            "  e N     — включить (enable) аккаунт #N\n"
            "  q       — выход\n"
        )
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        parts = raw.split()
        cmd = parts[0].lower()

        if cmd == "q":
            return
        if cmd == "a":
            add_account_interactive(store)
            continue
        if cmd == "t":
            asyncio.run(ping_all(store))
            continue
        if cmd in ("s", "r", "d", "e") and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1])
            if not (0 <= i < len(store.accounts)):
                print(f"[!] нет #{i}")
                continue
            label = store.accounts[i].label
            if cmd == "s":
                store.set_active(label)
                print(f"[+] активный: {label}")
            elif cmd == "r":
                if input(f"удалить '{label}'? [y/N]: ").strip().lower() == "y":
                    store.remove(label)
            elif cmd == "d":
                store.disable(label, "disabled by user")
                print(f"[+] {label} отключён")
            elif cmd == "e":
                store.enable(label)
                print(f"[+] {label} включён")
            continue

        print("[?] неизвестная команда")


# ---------------------------------------------------------------------------
# entrypoints
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 0 если есть валидный аккаунт, иначе 1")
    ap.add_argument("--list", action="store_true", help="распечатать таблицу и выйти")
    args = ap.parse_args()

    store = AccountStore()

    if args.check:
        ok = bool(store.list_usable())
        return 0 if ok else 1

    if args.list:
        print(render_table(store))
        return 0

    menu(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
