from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import questionary
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from questionary import Choice, Separator
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from accounts import Account, AccountStore, extract_domain_from_access_token, extract_tokens_from_cookie
from zo_client import ZoClient

STATE_FILE = ROOT / "launcher_state.json"
PROXY_PORT = 17878
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
API_BASE_URL = f"{PROXY_URL}/v1"
LOG_FILE = ROOT / "proxy.log"

console = Console(highlight=False)

LANGS = {
    "ru": {
        "boot": "запуск",
        "proxy_start": "Запускаю локальный API...",
        "proxy_fail": "Не удалось поднять локальный API.",
        "running": "локальный api работает",
        "starting": "запуск локального api",
        "proxy_on": "онлайн",
        "proxy_off": "офлайн",
        "accounts": "аккаунты",
        "mode": "режим",
        "active": "активный",
        "actions": "Действия",
        "refresh": "Обновить статус",
        "accounts_menu": "Аккаунты",
        "setup_examples": "Показать ручную настройку",
        "docs": "Открыть доки Zo API",
        "exit": "Выход",
        "account_actions": "Действия с аккаунтами",
        "add_browser": "Добавить аккаунт через временный браузер",
        "switch_mode": "Переключить режим",
        "set_active": "Сделать аккаунт активным",
        "toggle": "Включить / отключить аккаунт",
        "delete": "Удалить аккаунт",
        "refresh_health": "Проверить логин / баланс / модели",
        "back": "Назад",
        "no_accounts": "нет аккаунтов",
        "install_chromium": "Ставлю временный Chromium для авторизации...",
        "auth_title": "временный браузер для входа",
        "auth_body": "Откроется отдельный чистый Chromium.\n\nВойди в Zo там. Как только появятся нужные cookies, окно закроется само, затем я проверю логин, баланс и модели и сохраню аккаунт.",
        "auth_fail": "Не удалось поставить Chromium Playwright.",
        "auth_browser_fail": "Ошибка браузерной авторизации",
        "cookies_timeout": "Не успел поймать cookies после входа.",
        "manual_fallback": "Перейти к ручному варианту?",
        "label": "Метка аккаунта",
        "domain": "Домен workspace",
        "make_active": "Сделать активным?",
        "verify": "Проверяю логин, баланс и модели...",
        "save_anyway": "Сохранить всё равно?",
        "saved": "Аккаунт сохранён",
        "manual_title": "ручной резервный вариант",
        "manual_body": "Вставь полный Cookie header из запроса Zo /ask.",
        "cookie_header": "Cookie header",
        "access_missing": "Не найден access_token.",
        "verify_fail": "Проверка не прошла",
        "choose_active": "Выбери активный аккаунт",
        "choose_account": "Выбери аккаунт",
        "delete_which": "Какой аккаунт удалить?",
        "delete_confirm": "Удалить аккаунт '{label}'?",
        "refreshing": "Обновляю статус аккаунтов...",
        "manual_setup_title": "как подключить приложения вручную",
        "manual_setup": "OpenAI-compatible приложения:\n  Base URL: {api}\n  API key:  zo-proxy\n\nAnthropic-compatible приложения:\n  Base URL: {proxy}\n  API key / token: zo-proxy\n  endpoint: /v1/messages",
        "docs_opened": "Документация Zo API открыта в браузере.",
        "footer": "Стрелки: выбор • Enter: открыть • Локальный API работает, пока открыто это окно",
        "api_title": "api роуты",
        "client_title": "ручная настройка",
        "api_common": "Общее",
        "base_url": "База URL",
        "state_ok": "ok",
        "state_err": "ошибка",
        "state_off": "выкл",
        "empty_accounts": "пока нет аккаунтов",
        "language": "Язык",
        "lang_switch": "Сменить язык: русский / English",
        "lang_ru": "Русский",
        "lang_en": "English",
        "press_enter": "Нажми Enter, чтобы продолжить...",
        "yes": "Да",
        "no": "Нет",
        "app_name": "ZoAPI",
        "subtitle": "локальный api для Zo Computer",
        "balance": "Баланс",
    },
    "en": {
        "boot": "booting",
        "proxy_start": "Starting local API...",
        "proxy_fail": "Failed to start local API.",
        "running": "local api is running",
        "starting": "starting local api",
        "proxy_on": "online",
        "proxy_off": "offline",
        "accounts": "accounts",
        "mode": "mode",
        "active": "active",
        "actions": "Actions",
        "refresh": "Refresh status",
        "accounts_menu": "Accounts",
        "setup_examples": "Show manual setup",
        "docs": "Open Zo API docs",
        "exit": "Exit",
        "account_actions": "Account actions",
        "add_browser": "Add account via temporary browser",
        "switch_mode": "Switch mode",
        "set_active": "Set active account",
        "toggle": "Enable / disable account",
        "delete": "Delete account",
        "refresh_health": "Refresh login / balance / models",
        "back": "Back",
        "no_accounts": "no accounts",
        "install_chromium": "Installing bundled Chromium for auth...",
        "auth_title": "temporary browser sign-in",
        "auth_body": "A fresh temporary Chromium will open.\n\nSign into Zo there. As soon as the required cookies appear, the window closes automatically, then login, balance and models are verified and the account is saved.",
        "auth_fail": "Could not install Playwright Chromium.",
        "auth_browser_fail": "Browser auth failed",
        "cookies_timeout": "Timed out waiting for cookies.",
        "manual_fallback": "Use manual fallback?",
        "label": "Account label",
        "domain": "Workspace domain",
        "make_active": "Make active?",
        "verify": "Checking login, balance and models...",
        "save_anyway": "Save anyway?",
        "saved": "Account saved",
        "manual_title": "manual fallback",
        "manual_body": "Paste the full Cookie header from a Zo /ask request.",
        "cookie_header": "Cookie header",
        "access_missing": "access_token not found.",
        "verify_fail": "Verification failed",
        "choose_active": "Choose active account",
        "choose_account": "Choose account",
        "delete_which": "Delete which account?",
        "delete_confirm": "Delete account '{label}'?",
        "refreshing": "Refreshing account status...",
        "manual_setup_title": "manual client setup",
        "manual_setup": "OpenAI-compatible apps:\n  Base URL: {api}\n  API key:  zo-proxy\n\nAnthropic-compatible apps:\n  Base URL: {proxy}\n  API key / token: zo-proxy\n  endpoint: /v1/messages",
        "docs_opened": "Zo API docs opened in browser.",
        "footer": "Arrows: move • Enter: open • Local API stays up while this window is open",
        "api_title": "api routes",
        "client_title": "manual setup",
        "api_common": "Common",
        "base_url": "Base URL",
        "state_ok": "ok",
        "state_err": "error",
        "state_off": "off",
        "empty_accounts": "no accounts yet",
        "language": "Language",
        "lang_switch": "Switch language: Russian / English",
        "lang_ru": "Russian",
        "lang_en": "English",
        "press_enter": "Press Enter to continue...",
        "yes": "Yes",
        "no": "No",
        "app_name": "ZoAPI",
        "subtitle": "local api for Zo Computer",
        "balance": "Balance",
    },
}

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_action": "refresh", "lang": "ru"}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"last_action": "refresh", "lang": "ru"}
        data.setdefault("last_action", "refresh")
        data.setdefault("lang", "ru")
        return data
    except Exception:
        return {"last_action": "refresh", "lang": "ru"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def tr(state: dict, key: str, **kwargs: Any) -> str:
    lang = state.get("lang", "ru")
    text = LANGS.get(lang, LANGS["ru"]).get(key, key)
    return text.format(**kwargs)


def ui_style() -> questionary.Style:
    return questionary.Style(
        [
            ("qmark", "fg:#94a3b8 bold"),
            ("question", "bold fg:#f0fdf4"),
            ("answer", "fg:#bbf7d0 bold"),
            ("pointer", "fg:#86efac bold"),
            ("highlighted", "fg:#f0fdf4 bg:#365314 bold"),
            ("selected", "fg:#d9f99d bold"),
            ("instruction", "fg:#d1fae5"),
            ("separator", "fg:#86efac"),
            ("disabled", "fg:#6b7280 italic"),
        ]
    )


def glyphs() -> dict[str, str]:
    if os.name == "nt":
        return {"ok": "[+]", "warn": "[!]", "err": "[x]", "run": ">", "dot": "*"}
    return {"ok": "✓", "warn": "!", "err": "✕", "run": "›", "dot": "•"}


def proxy_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def start_proxy() -> bool:
    if proxy_running():
        return True
    cmd = [sys.executable, "proxy.py"]
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200
        subprocess.Popen(cmd, cwd=ROOT, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        with LOG_FILE.open("ab") as f:
            subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=f, start_new_session=True)
    for _ in range(60):
        time.sleep(0.2)
        if proxy_running():
            return True
    return False


def ensure_playwright_chromium(state: dict) -> bool:
    probe = [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]
    try:
        r = subprocess.run(probe, cwd=ROOT, capture_output=True, text=True, timeout=60)
        text = ((r.stdout or "") + (r.stderr or "")).lower()
        needs = (r.returncode != 0) or ("will download" in text) or ("not installed" in text)
    except Exception:
        needs = True
    if not needs:
        return True
    console.print(f"[green]{glyphs()['run']} {tr(state, 'install_chromium')}[/green]")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT)
    return r.returncode == 0


def fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return "0"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def fetch_account_health(account: Account) -> tuple[int | None, int | None]:
    client = ZoClient()
    try:
        models = await client.list_models(account)
        balance = await client.fetch_balance(account)
        return balance, len(models)
    finally:
        await client.close()


async def refresh_store_health(store: AccountStore) -> None:
    client = ZoClient()
    try:
        for acc in store.accounts:
            try:
                models = await client.list_models(acc)
                balance = await client.fetch_balance(acc)
                acc.balance_cents = balance
                acc.balance_checked_at = time.time()
                acc.last_err = None
                acc.error_streak = 0
                store.mark_ok(acc.label)
            except Exception as e:
                store.mark_err(acc.label, str(e), max_streak=999)
    finally:
        await client.close()
        store.save()


def header_panel(state: dict, proxy_ok: bool) -> Panel:
    title = Text(tr(state, "app_name"), style="bold green")
    subtitle = Text(tr(state, "subtitle"), style="bold white")
    status = Text(tr(state, "running") if proxy_ok else tr(state, "starting"), style="green" if proxy_ok else "yellow")
    group = Group(Align.center(title), Align.center(subtitle), Align.center(status))
    return Panel(group, border_style="green", padding=(1, 2))


def accounts_table(state: dict, store: AccountStore) -> Table:
    table = Table(show_header=True, header_style="bold white", border_style="green", expand=True)
    table.add_column("*", width=3, justify="center")
    table.add_column("label", style="green", min_width=10)
    table.add_column("email", style="white", min_width=20)
    table.add_column("domain", style="bright_green", min_width=12)
    table.add_column("ttl", style="yellow", width=7)
    table.add_column(tr(state, "balance"), style="white", width=10, justify="right")
    table.add_column("state", style="white", width=10)
    if not store.accounts:
        table.add_row("", "—", tr(state, "empty_accounts"), "—", "—", "—", "—")
        return table
    for acc in store.accounts:
        marker = glyphs()["dot"] if acc.label == store.active_label else ""
        status = tr(state, "state_off") if acc.disabled else (tr(state, "state_err") if acc.error_streak else tr(state, "state_ok"))
        bal = "?" if acc.balance_cents is None else f"${acc.balance_cents / 100:.2f}"
        table.add_row(marker, acc.label, acc.email() or "?", acc.domain, fmt_ttl(acc.seconds_until_expiry()), bal, status)
    return table


def api_panel(state: dict) -> Panel:
    lines = Table.grid(padding=(0, 2))
    lines.add_column(style="green", width=18)
    lines.add_column(style="white")
    lines.add_row("Anthropic", "POST /v1/messages")
    lines.add_row("OpenAI", "POST /v1/chat/completions")
    lines.add_row("OpenAI", "POST /v1/responses")
    lines.add_row("OpenAI", "WS   /v1/responses")
    lines.add_row(tr(state, "api_common"), "GET /v1/models   GET /health")
    lines.add_row(tr(state, "base_url"), API_BASE_URL)
    return Panel(lines, title=tr(state, "api_title"), border_style="green")


def setup_panel(state: dict) -> Panel:
    body = Text(tr(state, "manual_setup", api=API_BASE_URL, proxy=PROXY_URL), style="white")
    return Panel(body, title=tr(state, "client_title"), border_style="green")


def bottom_bar(state: dict, store: AccountStore, proxy_ok: bool) -> Panel:
    usable = len(store.list_usable())
    total = len(store.accounts)
    mode = getattr(store, "mode", "fixed")
    active = store.active_label or "-"
    text = Text()
    text.append(f"API: ", style="bold white")
    text.append(tr(state, "proxy_on") if proxy_ok else tr(state, "proxy_off"), style="green" if proxy_ok else "red")
    text.append("   ")
    text.append(f"{tr(state, 'accounts')}: {usable}/{total}", style="white")
    text.append("   ")
    text.append(f"{tr(state, 'mode')}: {mode}", style="white")
    text.append("   ")
    text.append(f"{tr(state, 'active')}: {active}", style="white")
    text.append("   ")
    text.append(tr(state, "footer"), style="green")
    return Panel(text, border_style="green", padding=(0, 1))


def draw_dashboard(state: dict, store: AccountStore, proxy_ok: bool) -> None:
    console.clear()
    console.print(header_panel(state, proxy_ok))
    console.print(accounts_table(state, store))
    console.print(api_panel(state))
    console.print(setup_panel(state))
    console.print(bottom_bar(state, store, proxy_ok))


def select_menu(state: dict, message: str, choices: list[Any], default: str | None = None):
    return questionary.select(
        message,
        choices=choices,
        default=default,
        style=ui_style(),
        qmark=glyphs()["run"],
        pointer=glyphs()["run"],
        instruction="↑↓ • Enter",
    ).ask()


def prompt_text(state: dict, message: str, default: str = "") -> str | None:
    return questionary.text(message, default=default, style=ui_style(), qmark=glyphs()["run"] + " ").ask()


def prompt_confirm(state: dict, message: str, default: bool = True) -> bool:
    value = questionary.confirm(message, default=default, style=ui_style(), qmark=glyphs()["run"] + " ").ask()
    return bool(value)


def pause(state: dict) -> None:
    console.print(f"[dim]{tr(state, 'press_enter')}[/dim]")
    input()


def add_account_manual(state: dict, store: AccountStore) -> None:
    console.clear()
    console.print(Panel(tr(state, "manual_body"), title=tr(state, "manual_title"), border_style="yellow"))
    raw = prompt_text(state, tr(state, "cookie_header"), "") or ""
    access, refresh = extract_tokens_from_cookie(raw)
    if not access:
        console.print(f"[red]{glyphs()['err']} {tr(state, 'access_missing')}[/red]")
        pause(state)
        return
    domain_guess = extract_domain_from_access_token(access) or ""
    domain = prompt_text(state, tr(state, "domain"), domain_guess) or domain_guess
    if not domain:
        return
    label = prompt_text(state, tr(state, "label"), f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[red]{glyphs()['err']} {tr(state, 'verify_fail')}: {e}[/red]")
        pause(state)
        return
    acc.balance_cents = balance
    acc.balance_checked_at = time.time()
    store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
    bal_text = "?" if balance is None else f"${balance / 100:.2f}"
    console.print(Panel(f"{tr(state, 'saved')}\nlabel: {label}\n{tr(state, 'balance')}: {bal_text}\nmodels: {models}", border_style="green"))
    pause(state)


def add_account_via_browser(state: dict, store: AccountStore) -> None:
    console.clear()
    console.print(Panel(tr(state, "auth_body"), title=tr(state, "auth_title"), border_style="green"))
    if not ensure_playwright_chromium(state):
        console.print(f"[red]{glyphs()['err']} {tr(state, 'auth_fail')}[/red]")
        pause(state)
        return
    captured: tuple[str, str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="zoapi-browser-") as tmp:
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch_persistent_context(
                        user_data_dir=tmp,
                        headless=False,
                        viewport={"width": 1360, "height": 900},
                    )
                except Exception as e:
                    if "Executable doesn't exist" in str(e) or "Executable doesn\'t exist" in str(e):
                        if not ensure_playwright_chromium(state):
                            raise
                        browser = p.chromium.launch_persistent_context(
                            user_data_dir=tmp,
                            headless=False,
                            viewport={"width": 1360, "height": 900},
                        )
                    else:
                        raise
                page = browser.new_page()
                page.goto("https://zo.computer", wait_until="domcontentloaded")
                start = time.time()
                while time.time() - start < 600:
                    cookies = browser.cookies(["https://zo.computer", "https://api.zo.computer"])
                    access = next((c.get("value", "") for c in cookies if c.get("name") == "access_token"), "")
                    refresh = next((c.get("value", "") for c in cookies if c.get("name") == "refresh_token"), "")
                    if access and refresh:
                        captured = (access, refresh, extract_domain_from_access_token(access) or "")
                        break
                    page.wait_for_timeout(900)
                browser.close()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            console.print(f"[red]{glyphs()['err']} {tr(state, 'auth_browser_fail')}: {e}[/red]")
            pause(state)
            return
    if not captured:
        console.print(f"[yellow]{glyphs()['warn']} {tr(state, 'cookies_timeout')}[/yellow]")
        if prompt_confirm(state, tr(state, "manual_fallback"), False):
            add_account_manual(state, store)
        return
    access, refresh, domain_guess = captured
    label = prompt_text(state, tr(state, "label"), f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    domain = prompt_text(state, tr(state, "domain"), domain_guess) or domain_guess
    if not domain:
        return
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    console.print(f"[green]{glyphs()['run']} {tr(state, 'verify')}[/green]")
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[red]{glyphs()['err']} {tr(state, 'verify_fail')}: {e}[/red]")
        if prompt_confirm(state, tr(state, "save_anyway"), False):
            store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
        pause(state)
        return
    acc.balance_cents = balance
    acc.balance_checked_at = time.time()
    store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
    bal_text = "?" if balance is None else f"${balance / 100:.2f}"
    console.print(Panel(f"{tr(state, 'saved')}\nlabel: {label}\nemail: {acc.email() or '?'}\ndomain: {acc.domain}\n{tr(state, 'balance')}: {bal_text}\nmodels: {models}", border_style="green"))
    pause(state)


def pick_account_label(state: dict, store: AccountStore, title: str) -> str | None:
    if not store.accounts:
        return None
    choices = [Choice(f"{'*' if a.label == store.active_label else ' '}  {a.label:<10}  {a.email() or '?':<28}  {a.domain}", a.label) for a in store.accounts]
    choices += [Separator(), Choice(tr(state, "back"), None)]
    return select_menu(state, title, choices)


def accounts_menu(state: dict, store: AccountStore) -> None:
    while True:
        draw_dashboard(state, store, proxy_running())
        mode = getattr(store, "mode", "fixed")
        next_mode = "rotation" if mode == "fixed" else "fixed"
        action = select_menu(
            state,
            tr(state, "account_actions"),
            [
                Choice(tr(state, "add_browser"), "add"),
                Choice(f"{tr(state, 'switch_mode')}: {mode} -> {next_mode}", "mode"),
                Choice(tr(state, "set_active"), "active", disabled=None if store.accounts else tr(state, "no_accounts")),
                Choice(tr(state, "toggle"), "toggle", disabled=None if store.accounts else tr(state, "no_accounts")),
                Choice(tr(state, "delete"), "delete", disabled=None if store.accounts else tr(state, "no_accounts")),
                Choice(tr(state, "refresh_health"), "refresh", disabled=None if store.accounts else tr(state, "no_accounts")),
                Separator(),
                Choice(tr(state, "back"), "back"),
            ],
        )
        if action in (None, "back"):
            return
        if action == "add":
            add_account_via_browser(state, store)
        elif action == "mode":
            store.set_mode(next_mode)
        elif action == "active":
            label = pick_account_label(state, store, tr(state, "choose_active"))
            if label:
                store.set_active(label)
        elif action == "toggle":
            label = pick_account_label(state, store, tr(state, "choose_account"))
            if label:
                acc = store.get(label)
                if acc and acc.disabled:
                    store.enable(label)
                elif acc:
                    store.disable(label, "disabled from launcher")
        elif action == "delete":
            label = pick_account_label(state, store, tr(state, "delete_which"))
            if label and prompt_confirm(state, tr(state, "delete_confirm", label=label), False):
                store.remove(label)
        elif action == "refresh":
            console.print(f"[green]{glyphs()['run']} {tr(state, 'refreshing')}[/green]")
            asyncio.run(refresh_store_health(store))
            pause(state)


def show_setup_examples(state: dict) -> None:
    console.clear()
    console.print(Panel(tr(state, "manual_setup", api=API_BASE_URL, proxy=PROXY_URL), title=tr(state, "manual_setup_title"), border_style="green"))
    pause(state)


def open_docs(state: dict) -> None:
    webbrowser.open("https://docs.zocomputer.com/api")
    console.print(f"[green]{glyphs()['ok']} {tr(state, 'docs_opened')}[/green]")
    pause(state)


def switch_language(state: dict) -> None:
    lang = select_menu(
        state,
        tr(state, "lang_switch"),
        [
            Choice(tr(state, "lang_ru"), "ru"),
            Choice(tr(state, "lang_en"), "en"),
        ],
        default=state.get("lang", "ru"),
    )
    if lang in ("ru", "en"):
        state["lang"] = lang
        save_state(state)


def main() -> int:
    state = load_state()
    store = AccountStore()
    console.clear()
    console.print(header_panel(state, False))
    console.print(f"[green]{glyphs()['run']} {tr(state, 'proxy_start')}[/green]")
    if not start_proxy():
        console.print(f"[red]{glyphs()['err']} {tr(state, 'proxy_fail')}[/red]")
        return 1
    if store.accounts:
        try:
            asyncio.run(refresh_store_health(store))
        except Exception:
            pass
    while True:
        draw_dashboard(state, store, proxy_running())
        choice = select_menu(
            state,
            tr(state, "actions"),
            [
                Choice(tr(state, "refresh"), "refresh"),
                Choice(tr(state, "accounts_menu"), "accounts"),
                Choice(tr(state, "setup_examples"), "setup"),
                Choice(tr(state, "language"), "lang"),
                Choice(tr(state, "docs"), "docs"),
                Separator(),
                Choice(tr(state, "exit"), "exit"),
            ],
            default=state.get("last_action", "refresh"),
        )
        if choice in (None, "exit"):
            return 0
        state["last_action"] = choice
        save_state(state)
        if choice == "refresh":
            console.print(f"[green]{glyphs()['run']} {tr(state, 'refreshing')}[/green]")
            if store.accounts:
                asyncio.run(refresh_store_health(store))
        elif choice == "accounts":
            accounts_menu(state, store)
            store.load()
        elif choice == "setup":
            show_setup_examples(state)
        elif choice == "lang":
            switch_language(state)
        elif choice == "docs":
            open_docs(state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
