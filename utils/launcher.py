from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import questionary
from accounts import Account, AccountStore, clean_domain, extract_domain_from_access_token
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
try:
    from patchright.sync_api import sync_playwright  # type: ignore[no-redef]
    BROWSER_BACKEND = "patchright"
except Exception:
    from playwright.sync_api import sync_playwright
    BROWSER_BACKEND = "playwright"
try:
    from playwright_stealth import stealth_sync
except Exception:
    stealth_sync = None
from questionary import Choice, Separator
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from zo_client import ZoClient

STATE_FILE = ROOT / "launcher_state.json"
LOG_FILE = ROOT / "proxy.log"
PID_FILE = ROOT / "proxy.pid"
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
API_BASE_URL = f"{PROXY_URL}/v1"

console = Console(highlight=False)

LANGS = {
    "ru": {
        "app_name": "ZoAPI",
        "subtitle": "локальный api для Zo Computer",
        "starting": "запуск локального api",
        "running": "локальный api запущен",
        "proxy_start": "Запускаю локальный API...",
        "proxy_on": "Онлайн",
        "proxy_off": "Офлайн",
        "accounts": "Аккаунты",
        "mode": "Режим",
        "active": "Активный",
        "actions": "Действия",
        "refresh": "Обновить статус",
        "accounts_menu": "Аккаунты",
        "setup_examples": "Ручная настройка",
        "docs": "Открыть документацию ZoAPI",
        "language": "Язык",
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
        "auth_body": "Откроется временный браузер.\n\n1. Войди в Zo.\n2. Если придёт письмо с verify-link — открой её в этом же окне браузера.\n3. JavaScript и редиректы включены.\n4. Как только появятся нужные cookie, окно закроется само.",
        "auth_fail": "Не удалось подготовить браузер Playwright.",
        "auth_browser_fail": "Ошибка браузерной авторизации",
        "browser_choice": "Браузер для входа",
        "browser_auto": "Авто (Chrome / Edge / Chromium)",
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
        "docs_opened": "Документация ZoAPI открыта в браузере.",
        "footer": "Стрелки: выбор • Enter: открыть • Локальный API работает, пока открыто это окно",
        "press_enter": "Нажми Enter, чтобы продолжить...",
        "lang_switch": "Сменить язык",
        "lang_ru": "Русский",
        "lang_en": "English",
        "table_title": "Аккаунты",
        "balance": "Баланс",
        "col_index": "#",
        "col_label": "Label",
        "col_email": "Email",
        "col_domain": "Domain",
        "col_ttl": "TTL",
        "col_state": "Статус",
        "state_ok": "Активен",
        "state_err": "Ошибка",
        "state_off": "Неактивен",
        "empty_accounts": "пока нет аккаунтов",
    },
    "en": {
        "app_name": "ZoAPI",
        "subtitle": "Local API for Zo Computer",
        "starting": "starting local api",
        "running": "API is running",
        "proxy_start": "Starting local API...",
        "proxy_on": "Online",
        "proxy_off": "Offline",
        "accounts": "Accounts",
        "mode": "Mode",
        "active": "Active",
        "actions": "Actions",
        "refresh": "Refresh status",
        "accounts_menu": "Accounts",
        "setup_examples": "Manual setup",
        "docs": "Open ZoAPI docs",
        "language": "Language",
        "exit": "Exit",
        "account_actions": "Account actions",
        "add_browser": "Add account via temporary browser",
        "switch_mode": "Switch mode",
        "set_active": "Set active account",
        "toggle": "Enable / disable account",
        "delete": "Delete account",
        "refresh_health": "Check login / balance / models",
        "back": "Back",
        "no_accounts": "no accounts",
        "install_chromium": "Installing temporary Chromium for auth...",
        "auth_title": "temporary browser sign-in",
        "auth_body": "A temporary browser window will open.\n\nSign into Zo there. If the email contains a verify link, open it in that same window. JavaScript and redirects stay enabled. As soon as the required cookies appear, the window closes automatically and the account is verified and saved.",
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
        "docs_opened": "ZoAPI docs opened in browser.",
        "footer": "Arrows: move • Enter: open • Local API stays up while this window is open",
        "press_enter": "Press Enter to continue...",
        "lang_switch": "Switch language",
        "lang_ru": "Russian",
        "lang_en": "English",
        "table_title": "Accounts",
        "balance": "Balance",
        "col_index": "#",
        "col_label": "Label",
        "col_email": "Email",
        "col_domain": "Domain",
        "col_ttl": "TTL",
        "col_state": "State",
        "state_ok": "Active",
        "state_err": "Error",
        "state_off": "Inactive",
        "empty_accounts": "no accounts yet",
    },
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_action": "refresh", "lang": "ru"}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("last_action", "refresh")
        data.setdefault("lang", "ru")
        return data
    except Exception:
        return {"last_action": "refresh", "lang": "ru"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def tr(state: dict, key: str, **kwargs: Any) -> str:
    lang = state.get("lang", "ru")
    return LANGS.get(lang, LANGS["ru"]).get(key, key).format(**kwargs)


def ui_style() -> questionary.Style:
    return questionary.Style(
        [
            ("qmark", "fg:#a78bfa bold"),
            ("question", "fg:#f5f3ff bold"),
            ("answer", "fg:#a78bfa bold"),
            ("pointer", "fg:#8b5cf6 bold"),
            ("highlighted", "fg:#faf5ff bg:#5b4a73 bold"),
            ("selected", "fg:#ddd6fe bold"),
            ("instruction", "fg:#d8b4fe"),
            ("separator", "fg:#b8a7d9"),
            ("disabled", "fg:#9f8fb8 italic"),
        ]
    )


def glyphs() -> dict[str, str]:
    if os.name == "nt":
        return {"ok": "[+]", "warn": "[!]", "err": "[x]", "run": ">"}
    return {"ok": "✓", "warn": "!", "err": "✕", "run": "›"}


def proxy_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _proxy_python_candidates() -> list[str]:
    cands: list[str] = []
    cands.append(sys.executable)
    if os.name == "nt":
        win = ROOT / ".venv" / "Scripts" / "python.exe"
        if win.exists():
            cands.append(str(win))
    else:
        unix = ROOT / ".venv" / "bin" / "python"
        if unix.exists():
            cands.append(str(unix))
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _spawn_proxy_with(python_exe: str) -> bool:
    try:
        logf = LOG_FILE.open("ab")
    except Exception:
        return False
    try:
        kwargs: dict[str, Any] = {
            "cwd": str(ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": logf,
            "stderr": logf,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([python_exe, str(ROOT / "proxy.py")], **kwargs)
    except Exception as e:
        try:
            LOG_FILE.write_bytes((f"\n[launcher] spawn failed for {python_exe}: {e}\n").encode("utf-8"))
        except Exception:
            pass
        return False
    finally:
        try:
            logf.close()
        except Exception:
            pass
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass
    for _ in range(40):
        if proc.poll() is not None:
            return False
        if proxy_running():
            return True
        time.sleep(0.1)
    return proxy_running()


def start_proxy() -> bool:
    if proxy_running():
        return True
    last_err: str = ""
    for py in _proxy_python_candidates():
        if _spawn_proxy_with(py):
            return True
        last_err = py
    if last_err:
        try:
            LOG_FILE.write_bytes((f"\n[launcher] all spawn attempts failed; last python={last_err}\n").encode("utf-8"))
        except Exception:
            pass
    return False


def detect_preferred_browser() -> tuple[dict[str, str], str]:
    candidates: list[tuple[dict[str, str], str]] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("PROGRAMFILES", "")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", "")
        win_paths = [
            ({"channel": "msedge"}, "Microsoft Edge"),
            ({"executable_path": os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe")}, "Microsoft Edge"),
            ({"executable_path": os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")}, "Microsoft Edge"),
            ({"executable_path": os.path.join(pfx86, "Microsoft", "Edge", "Application", "msedge.exe")}, "Microsoft Edge"),
            ({"channel": "chrome"}, "Google Chrome"),
            ({"executable_path": os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")}, "Google Chrome"),
            ({"executable_path": os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe")}, "Google Chrome"),
            ({"executable_path": os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe")}, "Google Chrome"),
        ]
        candidates.extend(win_paths)
    elif sys.platform == "darwin":
        candidates.extend([
            ({"channel": "chrome"}, "Google Chrome"),
            ({"channel": "msedge"}, "Microsoft Edge"),
            ({"executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}, "Google Chrome"),
            ({"executable_path": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"}, "Microsoft Edge"),
        ])
    else:
        if shutil.which("google-chrome"):
            candidates.append(({"executable_path": shutil.which("google-chrome") or ""}, "Google Chrome"))
        if shutil.which("microsoft-edge"):
            candidates.append(({"executable_path": shutil.which("microsoft-edge") or ""}, "Microsoft Edge"))
        candidates.extend([({"channel": "chrome"}, "Google Chrome"), ({"channel": "msedge"}, "Microsoft Edge")])
    for opts, label in candidates:
        path = opts.get("executable_path")
        if path and os.path.exists(path):
            return opts, label
        if opts.get("channel"):
            return opts, label
    return {}, "Chromium"


def browser_stealth_scripts(context) -> None:
    context.add_init_script("""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'ru-RU', 'ru'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
""")


def ensure_playwright_chromium(state: dict) -> bool:
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT, check=True)
        return True
    except Exception:
        return False


def fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds <= 0:
        return "0"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_balance(cents: int | None) -> str:
    return "?" if cents is None else f"${cents / 100:.2f}"


def clean_domain(domain: str) -> str:
    return domain.replace("\\", "").strip()


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
                acc.domain = clean_domain(acc.domain)
                store.mark_ok(acc.label)
            except Exception as e:
                store.mark_err(acc.label, str(e), max_streak=999)
    finally:
        await client.close()
        store.save()


async def fetch_account_health(account: Account) -> tuple[int | None, int]:
    client = ZoClient()
    try:
        models = await client.list_models(account)
        balance = await client.fetch_balance(account)
        return balance, len(models)
    finally:
        await client.close()


def header_panel(state: dict, running: bool) -> Panel:
    status = tr(state, "running") if running else tr(state, "starting")
    status_style = "#34d399" if running else "#fbbf24"
    group = Group(
        Align.center(Text(tr(state, "app_name"), style="bold #f5f3ff")),
        Align.center(Text(tr(state, "subtitle"), style="#ddd6fe")),
        Align.center(Text(status, style=f"bold {status_style}")),
    )
    return Panel(group, border_style="#b8a7d9", padding=(1, 2))


def status_style_and_text(state: dict, acc: Account) -> tuple[str, str]:
    if acc.disabled:
        return "#fb7185", tr(state, "state_off")
    if acc.error_streak:
        return "#fb7185", tr(state, "state_err")
    return "#34d399", tr(state, "state_ok")


def accounts_table(state: dict, store: AccountStore) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column(tr(state, "col_index"), width=4, justify="center")
    table.add_column(tr(state, "col_label"), width=16, overflow="fold")
    table.add_column(tr(state, "col_email"), width=30, overflow="fold")
    table.add_column(tr(state, "col_domain"), width=18, overflow="fold")
    table.add_column(tr(state, "col_ttl"), width=8, justify="center")
    table.add_column(tr(state, "balance"), width=12, justify="right")
    table.add_column(tr(state, "col_state"), width=12, overflow="fold")
    if not store.accounts:
        table.add_row("", "—", tr(state, "empty_accounts"), "—", "—", "—", "—")
        return table
    for idx, acc in enumerate(store.accounts, start=1):
        label = f"{acc.label}{' *' if acc.label == store.active_label else ''}"
        style, state_text = status_style_and_text(state, acc)
        table.add_row(
            str(idx),
            label,
            acc.email() or "?",
            clean_domain(acc.domain),
            fmt_ttl(acc.seconds_until_expiry()),
            fmt_balance(acc.balance_cents),
            Text(state_text, style=style),
        )
    return table


def bottom_bar(state: dict, store: AccountStore, proxy_ok: bool) -> Panel:
    usable = len(store.list_usable())
    total = len(store.accounts)
    mode = getattr(store, "mode", "fixed")
    active = store.active_label or "-"
    text = Text()
    text.append("API: ", style="bold #f5f3ff")
    text.append(tr(state, "proxy_on") if proxy_ok else tr(state, "proxy_off"), style="#34d399" if proxy_ok else "#fb7185")
    text.append(f"   {tr(state, 'accounts')}: {usable}/{total}", style="#a78bfa")
    text.append(f"   {tr(state, 'mode')}: {mode}", style="#a78bfa")
    text.append(f"   {tr(state, 'active')}: {active}", style="#a78bfa")
    text.append(f"   {tr(state, 'footer')}", style="#8b5cf6")
    return Panel(text, border_style="#b8a7d9", padding=(0, 1))


def draw_dashboard(state: dict, store: AccountStore, running: bool) -> None:
    console.clear()
    console.print(header_panel(state, running))
    console.print(Panel(accounts_table(state, store), title=tr(state, "table_title"), border_style="#b8a7d9", padding=(0, 1)))
    console.print(bottom_bar(state, store, running))


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
    console.print(Panel(tr(state, "manual_body"), title=tr(state, "manual_title"), border_style="#b8a7d9"))
    raw = prompt_text(state, tr(state, "cookie_header"), "") or ""
    access, refresh = extract_tokens_from_cookie(raw)
    if not access:
        console.print(f"[#f4b7b7]{glyphs()['err']} {tr(state, 'access_missing')}[/#f4b7b7]")
        pause(state)
        return
    domain_guess = extract_domain_from_access_token(access) or ""
    domain = prompt_text(state, tr(state, "domain"), domain_guess) or domain_guess
    if not domain:
        return
    label = prompt_text(state, tr(state, "label"), f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    acc = Account(label=label, domain=clean_domain(domain), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[#f4b7b7]{glyphs()['err']} {tr(state, 'verify_fail')}: {e}[/#f4b7b7]")
        pause(state)
        return
    acc.balance_cents = balance
    acc.balance_checked_at = time.time()
    store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
    console.print(Panel(f"{tr(state, 'saved')}\nLabel: {label}\n{tr(state, 'balance')}: {fmt_balance(balance)}\nModels: {models}", border_style="#b8a7d9"))
    pause(state)


def add_account_via_browser(state: dict, store: AccountStore) -> None:
    console.clear()
    console.print(Panel(tr(state, "auth_body"), title=tr(state, "auth_title"), border_style="#b8a7d9"))
    if not ensure_playwright_chromium(state):
        console.print(f"[#f4b7b7]{glyphs()['err']} {tr(state, 'auth_fail')}[/#f4b7b7]")
        pause(state)
        return
    captured: tuple[str, str, str] | None = None
    login_url = "https://www.zo.computer/signup?intent=login"
    cookie_urls = [
        "https://www.zo.computer",
        "https://zo.computer",
        "https://api.zo.computer",
        "https://auth.zo.computer",
    ]
    launch_opts, browser_name = detect_preferred_browser()
    try:
        with sync_playwright() as p:
            console.print(f"[#b8a7d9]{glyphs()['run']} Браузер: {browser_name}[/#b8a7d9]")
            browser = p.chromium.launch(
                headless=False,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-features=Translate,MediaRouter,OptimizationHints",
                ],
                **launch_opts,
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 860},
                ignore_https_errors=True,
                java_script_enabled=True,
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                ),
            )
            browser_stealth_scripts(context)
            page = context.new_page()
            if stealth_sync is not None:
                try:
                    stealth_sync(page)
                except Exception:
                    pass
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
            start = time.time()
            while time.time() - start < 900:
                try:
                    cookies = context.cookies(cookie_urls)
                except Exception:
                    cookies = context.cookies()
                access = next((c.get("value", "") for c in cookies if c.get("name") == "access_token"), "")
                refresh = next((c.get("value", "") for c in cookies if c.get("name") == "refresh_token"), "")
                if access and refresh:
                    captured = (access, refresh, clean_domain(extract_domain_from_access_token(access) or ""))
                    break
                try:
                    page.wait_for_timeout(700)
                except Exception:
                    break
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    except PlaywrightTimeoutError:
        pass
    except Exception as e:
        console.print(f"[#f4b7b7]{glyphs()['err']} {tr(state, 'auth_browser_fail')}: {e}[/#f4b7b7]")
        pause(state)
        return
    if not captured:
        console.print(f"[#e9d8a6]{glyphs()['warn']} {tr(state, 'cookies_timeout')}[/#e9d8a6]")
        if prompt_confirm(state, tr(state, "manual_fallback"), False):
            add_account_manual(state, store)
        return
    access, refresh, domain_guess = captured
    label = prompt_text(state, tr(state, "label"), f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    domain = prompt_text(state, tr(state, "domain"), domain_guess) or domain_guess
    if not domain:
        return
    acc = Account(label=label, domain=clean_domain(domain), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    console.print(f"[#b8a7d9]{glyphs()['run']} {tr(state, 'verify')}[/#b8a7d9]")
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[#f4b7b7]{glyphs()['err']} {tr(state, 'verify_fail')}: {e}[/#f4b7b7]")
        if prompt_confirm(state, tr(state, "save_anyway"), False):
            store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
        pause(state)
        return
    acc.balance_cents = balance
    acc.balance_checked_at = time.time()
    store.add(acc, make_active=not store.accounts or prompt_confirm(state, tr(state, "make_active"), True))
    console.print(Panel(f"{tr(state, 'saved')}\nLabel: {label}\nEmail: {acc.email() or '?'}\nDomain: {acc.domain}\n{tr(state, 'balance')}: {fmt_balance(balance)}\nModels: {models}", border_style="#b8a7d9"))
    pause(state)


def pick_account_label(state: dict, store: AccountStore, title: str) -> str | None:
    if not store.accounts:
        return None
    choices = [Choice(f"{idx}. {a.label:<12} {clean_domain(a.domain):<18} {a.email() or '?'}", a.label) for idx, a in enumerate(store.accounts, start=1)]
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
            console.print(f"[#b8a7d9]{glyphs()['run']} {tr(state, 'refreshing')}[/#b8a7d9]")
            asyncio.run(refresh_store_health(store))
            pause(state)


def show_setup_examples(state: dict) -> None:
    console.clear()
    console.print(Panel(tr(state, "manual_setup", api=API_BASE_URL, proxy=PROXY_URL), title=tr(state, "manual_setup_title"), border_style="#b8a7d9"))
    pause(state)


def open_docs(state: dict) -> None:
    webbrowser.open("https://github.com/UvenaliyS/ZoAPI/blob/main/docs.md")
    console.print(f"[#b8a7d9]{glyphs()['ok']} {tr(state, 'docs_opened')}[/#b8a7d9]")
    pause(state)


def switch_language(state: dict) -> None:
    lang = select_menu(
        state,
        tr(state, "lang_switch"),
        [Choice(tr(state, "lang_ru"), "ru"), Choice(tr(state, "lang_en"), "en")],
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
    console.print(f"[#b8a7d9]{glyphs()['run']} {tr(state, 'proxy_start')}[/#b8a7d9]")
    start_proxy()
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
            console.print(f"[#b8a7d9]{glyphs()['run']} {tr(state, 'refreshing')}[/#b8a7d9]")
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
