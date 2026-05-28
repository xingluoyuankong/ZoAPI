from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from accounts import Account, AccountStore, extract_domain_from_access_token, extract_tokens_from_cookie
from zo_client import ZoClient

STATE_FILE = ROOT / "launcher_state.json"
PROXY_PORT = 17878
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
LOG_FILE = ROOT / "proxy.log"
CLIENTS = [
    ("claude", "Claude Code", "claude"),
    ("codex", "Codex", "codex"),
    ("opencode", "OpenCode", "opencode"),
    ("hermes", "Hermes", "hermes"),
]
STYLE = questionary.Style(
    [
        ("qmark", "fg:#60a5fa bold"),
        ("question", "bold fg:#e5e7eb"),
        ("answer", "fg:#34d399 bold"),
        ("pointer", "fg:#60a5fa bold"),
        ("highlighted", "fg:#ffffff bg:#1f2937 bold"),
        ("selected", "fg:#34d399 bold"),
        ("instruction", "fg:#6b7280"),
        ("separator", "fg:#4b5563"),
        ("disabled", "fg:#6b7280 italic"),
    ]
)
console = Console(highlight=False)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_client": "claude"}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_client": "claude"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def hr() -> None:
    console.print("[dim]─[/dim]" * 30)


def app_header(subtitle: str = "") -> None:
    title = Text("zo-claude-proxy", style="bold cyan")
    if subtitle:
        title.append(f"  {subtitle}", style="dim")
    console.print(Panel(Align.left(title), border_style="blue", padding=(0, 1)))


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
    for _ in range(50):
        time.sleep(0.2)
        if proxy_running():
            return True
    return False


def ensure_playwright_chromium() -> bool:
    probe = [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]
    try:
        r = subprocess.run(probe, cwd=ROOT, capture_output=True, text=True, timeout=60)
        text = (r.stdout or "") + (r.stderr or "")
        needs_install = (r.returncode != 0) or ("will download" in text.lower()) or ("not installed" in text.lower())
    except Exception:
        needs_install = True
    if not needs_install:
        return True
    console.print("[cyan][+] Ставлю Chromium для встроенной авторизации ...[/cyan]")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT)
    return r.returncode == 0


def _fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return "expired"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def _fetch_account_health(account: Account) -> tuple[int | None, int | None]:
    client = ZoClient()
    try:
        models = await client.list_models(account)
        balance = await client.fetch_balance(account)
        return balance, len(models)
    finally:
        await client.close()


def render_accounts(store: AccountStore) -> None:
    mode = getattr(store, "mode", "fixed")
    subtitle = f"accounts  mode={mode}"
    app_header(subtitle)
    if not store.accounts:
        console.print(Panel("[dim]No accounts yet. Add one from the menu below.[/dim]", border_style="yellow"))
        return
    table = Table(show_header=True, header_style="bold white", border_style="blue")
    table.add_column("active", style="cyan", width=6)
    table.add_column("label", style="green")
    table.add_column("email", style="white")
    table.add_column("domain", style="magenta")
    table.add_column("ttl", style="yellow", width=8)
    table.add_column("state", style="white", width=10)
    for acc in store.accounts:
        marker = "*" if acc.label == store.active_label else ""
        state = "off" if acc.disabled else ("err" if acc.error_streak else "ok")
        table.add_row(marker, acc.label, acc.email() or "?", acc.domain, _fmt_ttl(acc.seconds_until_expiry()), state)
    console.print(table)


def client_status(bin_name: str) -> str:
    return "ready" if shutil.which(bin_name) else "missing"


def summary_line(store: AccountStore) -> str:
    usable = len(store.list_usable())
    total = len(store.accounts)
    mode = getattr(store, "mode", "fixed")
    active = store.active_label or "-"
    return f"accounts {usable}/{total} usable   mode {mode}   active {active}   proxy {PROXY_URL}"


def select_menu(message: str, choices: list[Any], default: str | None = None):
    return questionary.select(
        message,
        choices=choices,
        default=default,
        style=STYLE,
        qmark=">",
        pointer="❯",
        instruction="↑↓ move • Enter select",
    ).ask()


def prompt_text(message: str, default: str = "") -> str | None:
    return questionary.text(message, default=default, style=STYLE, qmark="> ").ask()


def prompt_confirm(message: str, default: bool = True) -> bool:
    value = questionary.confirm(message, default=default, style=STYLE, qmark="> ").ask()
    return bool(value)


def add_account_manual(store: AccountStore) -> None:
    app_header("manual cookie fallback")
    console.print("[dim]Paste full Cookie header from a Zo /ask request.[/dim]")
    raw = prompt_text("Cookie header:", "") or ""
    access, refresh = extract_tokens_from_cookie(raw)
    if not access:
        console.print("[red][!] access_token not found.[/red]")
        return
    domain = prompt_text("Workspace domain:", extract_domain_from_access_token(access) or "") or ""
    if not domain:
        return
    label = prompt_text("Label:", f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    balance, models = asyncio.run(_fetch_account_health(acc))
    store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
    console.print(f"[green][+] Added {label}[/green]  [dim]balance={balance if balance is not None else '?'}¢  models={models or '?'}[/dim]")


def add_account_via_browser(store: AccountStore) -> None:
    app_header("browser sign-in")
    console.print(Panel(
        "A fresh temporary Chromium will open.\n\n"
        "Log into Zo there. As soon as access_token + refresh_token appear,\n"
        "the browser closes automatically, the account is verified, and balance/models are checked.\n\n"
        "This browser profile is temporary and is deleted right after capture.",
        border_style="cyan",
    ))
    if not ensure_playwright_chromium():
        console.print("[red][!] Could not install Chromium for Playwright.[/red]")
        return
    captured: tuple[str, str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="zo-proxy-browser-") as tmp:
        user_data_dir = Path(tmp) / "profile"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=False,
                    viewport={"width": 1360, "height": 900},
                )
                page = browser.new_page()
                page.goto("https://zo.computer", wait_until="domcontentloaded")
                start = time.time()
                while time.time() - start < 600:
                    cookies = browser.cookies(["https://zo.computer", "https://api.zo.computer"])
                    access = next((c.get("value", "") for c in cookies if c.get("name") == "access_token"), "")
                    refresh = next((c.get("value", "") for c in cookies if c.get("name") == "refresh_token"), "")
                    if access and refresh:
                        domain = extract_domain_from_access_token(access) or ""
                        captured = (access, refresh, domain)
                        break
                    page.wait_for_timeout(1200)
                browser.close()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            console.print(f"[red][!] Browser auth failed: {e}[/red]")
            return
    if not captured:
        console.print("[yellow][!] Cookies were not captured in time.[/yellow]")
        if prompt_confirm("Use manual cookie fallback?", False):
            add_account_manual(store)
        return
    access, refresh, domain_guess = captured
    suggested_label = f"acc{len(store.accounts)+1}"
    label = prompt_text("Label:", suggested_label) or suggested_label
    domain = prompt_text("Workspace domain:", domain_guess) or domain_guess
    if not domain:
        console.print("[red][!] Workspace domain is required.[/red]")
        return
    if any(a.label == label for a in store.accounts) and not prompt_confirm(f"Overwrite existing label '{label}'?", False):
        return
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    console.print("[cyan][+] Verifying account ...[/cyan]")
    try:
        balance, models = asyncio.run(_fetch_account_health(acc))
    except Exception as e:
        console.print(f"[red][!] Login cookies captured, but verification failed: {e}[/red]")
        if prompt_confirm("Save account anyway?", False):
            store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
        return
    store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
    balance_text = f"{balance}¢" if balance is not None else "?"
    console.print(Panel(f"[green]Added {label}[/green]\nemail: {acc.email() or '?'}\ndomain: {acc.domain}\nbalance: {balance_text}\nmodels: {models}", border_style="green"))


async def ping_all(store: AccountStore) -> None:
    rows = []
    client = ZoClient()
    try:
        for acc in store.accounts:
            try:
                models = await client.list_models(acc)
                bal = await client.fetch_balance(acc)
                store.mark_ok(acc.label)
                rows.append((acc.label, acc.email() or "?", "ok", str(len(models)), str(bal) if bal is not None else "?"))
            except Exception as e:
                store.mark_err(acc.label, str(e), max_streak=999)
                rows.append((acc.label, acc.email() or "?", type(e).__name__, "-", "-"))
    finally:
        await client.close()
    table = Table(show_header=True, header_style="bold white", border_style="blue")
    table.add_column("label", style="green")
    table.add_column("email")
    table.add_column("status")
    table.add_column("models", justify="right")
    table.add_column("balance¢", justify="right")
    for row in rows:
        table.add_row(*row)
    console.print(table)


def pick_account_label(store: AccountStore, title: str) -> str | None:
    if not store.accounts:
        return None
    choices = [Choice(f"{'*' if a.label==store.active_label else ' '}  {a.label}   {a.email() or '?'}   [{('off' if a.disabled else 'ok')} ]", a.label) for a in store.accounts]
    choices += [Separator(), Choice("Back", None)]
    return select_menu(title, choices)


def accounts_menu(store: AccountStore) -> None:
    while True:
        console.clear()
        render_accounts(store)
        mode = getattr(store, "mode", "fixed")
        next_mode = "rotation" if mode == "fixed" else "fixed"
        action = select_menu(
            "Account actions",
            [
                Choice("Add account via temporary browser", "add"),
                Choice(f"Switch mode: {mode} -> {next_mode}", "mode"),
                Choice("Set active account", "active", disabled=None if store.accounts else "no accounts"),
                Choice("Enable / disable account", "toggle", disabled=None if store.accounts else "no accounts"),
                Choice("Delete account", "delete", disabled=None if store.accounts else "no accounts"),
                Choice("Ping and check balances", "ping", disabled=None if store.accounts else "no accounts"),
                Separator(),
                Choice("Back", "back"),
            ],
        )
        if action in (None, "back"):
            return
        if action == "add":
            add_account_via_browser(store)
        elif action == "mode":
            store.set_mode(next_mode)
        elif action == "active":
            label = pick_account_label(store, "Choose active account")
            if label:
                store.set_active(label)
        elif action == "toggle":
            label = pick_account_label(store, "Choose account")
            if label:
                acc = store.get(label)
                if acc and acc.disabled:
                    store.enable(label)
                elif acc:
                    store.disable(label, "disabled from launcher")
        elif action == "delete":
            label = pick_account_label(store, "Delete which account?")
            if label and prompt_confirm(f"Delete '{label}'?", False):
                store.remove(label)
        elif action == "ping":
            asyncio.run(ping_all(store))
        if action != "ping":
            console.print("[dim]Press Enter to continue...[/dim]")
            input()


def env_for_client(client_id: str) -> dict[str, str]:
    env = os.environ.copy()
    if client_id == "claude":
        env["ANTHROPIC_BASE_URL"] = PROXY_URL
        env["ANTHROPIC_AUTH_TOKEN"] = "zo-proxy"
        env["ANTHROPIC_API_KEY"] = ""
        env["DISABLE_TELEMETRY"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    elif client_id == "codex":
        codex_home = ROOT / ".codex-home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "config.toml").write_text(f'openai_base_url = "{PROXY_URL}/v1"\nmodel = "gpt-5.3-codex"\n', encoding="utf-8")
        env["CODEX_HOME"] = str(codex_home)
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
    elif client_id == "opencode":
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "zo": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Zo Proxy",
                    "options": {"baseURL": f"{PROXY_URL}/v1", "apiKey": "{env:OPENAI_API_KEY}"},
                    "models": {
                        "gpt-5.5": {"name": "GPT-5.5 via Zo"},
                        "gpt-5.3-codex": {"name": "GPT-5.3 Codex via Zo"},
                        "claude-sonnet-4-6": {"name": "Claude Sonnet 4.6 via Zo"},
                        "claude-opus-4-7": {"name": "Claude Opus 4.7 via Zo"},
                    },
                }
            },
            "model": "zo/gpt-5.3-codex",
        }, ensure_ascii=False)
    elif client_id == "hermes":
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
        env.setdefault("HERMES_MODEL", "gpt-5.5")
    return env


def main_menu(state: dict, store: AccountStore) -> str | None:
    console.clear()
    app_header(summary_line(store))
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right", style="dim")
    for cid, title, bin_name in CLIENTS:
        grid.add_row(title, client_status(bin_name))
    console.print(Panel(grid, title="Clients", border_style="blue"))
    last = state.get("last_client", "claude")
    choices: list[Any] = [Choice(title, cid) for cid, title, _ in CLIENTS]
    choices += [Separator(), Choice("Accounts", "accounts"), Choice("Exit", "exit")]
    return select_menu("What do you want to launch?", choices, default=last)


def main() -> int:
    state = load_state()
    store = AccountStore()
    while True:
        choice = main_menu(state, store)
        if choice in (None, "exit"):
            return 0
        if choice == "accounts":
            accounts_menu(store)
            store.load()
            continue
        if not store.list_usable():
            console.print("[yellow][!] No usable account yet. Open Accounts first.[/yellow]")
            time.sleep(1)
            accounts_menu(store)
            if not store.list_usable():
                continue
        state["last_client"] = choice
        save_state(state)
        record = next(item for item in CLIENTS if item[0] == choice)
        title, bin_name = record[1], record[2]
        exe = shutil.which(bin_name)
        if not exe:
            console.print(f"[red][!] {title} not found in PATH.[/red]")
            time.sleep(1.2)
            continue
        if not start_proxy():
            console.print("[red][!] Failed to start local proxy.[/red]")
            return 1
        console.print(Panel(f"[green]Proxy ready[/green]\n{PROXY_URL}\n\nLaunching [bold]{title}[/bold] ...", border_style="green"))
        env = env_for_client(choice)
        return subprocess.call([exe, *sys.argv[1:]], cwd=ROOT, env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
