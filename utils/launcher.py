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
STYLE = questionary.Style(
    [
        ("qmark", "fg:#60a5fa bold"),
        ("question", "bold fg:#e5e7eb"),
        ("answer", "fg:#34d399 bold"),
        ("pointer", "fg:#60a5fa bold"),
        ("highlighted", "fg:#f8fafc bg:#1e293b bold"),
        ("selected", "fg:#34d399 bold"),
        ("instruction", "fg:#94a3b8"),
        ("separator", "fg:#475569"),
        ("disabled", "fg:#64748b italic"),
    ]
)
console = Console(highlight=False)
ICONS = {
    "app": "[=]",
    "ok": "[+]",
    "warn": "[!]",
    "err": "[x]",
    "mode": "[~]",
    "acct": "[#]",
    "api": "[@]",
    "run": "[>]",
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_action": "refresh"}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_action": "refresh"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def app_header(status: str) -> None:
    title = Text()
    title.append("zo-claude-proxy", style="bold cyan")
    title.append("  local api hub", style="bold white")
    title.append(f"  {status}", style="dim")
    console.print(Panel(title, border_style="blue", padding=(0, 1)))


def status_bar(store: AccountStore, proxy_ok: bool) -> Panel:
    usable = len(store.list_usable())
    total = len(store.accounts)
    mode = getattr(store, "mode", "fixed")
    active = store.active_label or "-"
    bits = Text()
    bits.append(f"{ICONS['api']} proxy ", style="bold")
    bits.append("online" if proxy_ok else "offline", style="green" if proxy_ok else "red")
    bits.append("   ")
    bits.append(f"{ICONS['acct']} accounts ", style="bold")
    bits.append(f"{usable}/{total}", style="cyan")
    bits.append("   ")
    bits.append(f"{ICONS['mode']} mode ", style="bold")
    bits.append(mode, style="magenta")
    bits.append("   active ", style="bold")
    bits.append(active, style="yellow")
    return Panel(bits, border_style="blue", padding=(0, 1))


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


def ensure_playwright_chromium() -> bool:
    probe = [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]
    try:
        r = subprocess.run(probe, cwd=ROOT, capture_output=True, text=True, timeout=60)
        text = ((r.stdout or "") + (r.stderr or "")).lower()
        needs = (r.returncode != 0) or ("will download" in text) or ("not installed" in text)
    except Exception:
        needs = True
    if not needs:
        return True
    console.print(f"[cyan]{ICONS['run']} installing bundled Chromium for auth...[/cyan]")
    r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT)
    return r.returncode == 0


def fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return "expired"
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


def accounts_table(store: AccountStore) -> Table:
    table = Table(show_header=True, header_style="bold white", border_style="blue", expand=True)
    table.add_column("act", width=3, justify="center")
    table.add_column("label", style="green", min_width=8)
    table.add_column("email", style="white", min_width=18)
    table.add_column("domain", style="magenta", min_width=10)
    table.add_column("ttl", style="yellow", width=8)
    table.add_column("balance", style="cyan", width=10, justify="right")
    table.add_column("state", style="white", width=10)
    for acc in store.accounts:
        marker = "*" if acc.label == store.active_label else ""
        state = "off" if acc.disabled else ("err" if acc.error_streak else "ok")
        bal = "?" if acc.balance_cents is None else f"{acc.balance_cents}¢"
        table.add_row(marker, acc.label, acc.email() or "?", acc.domain, fmt_ttl(acc.seconds_until_expiry()), bal, state)
    if not store.accounts:
        table.add_row("", "—", "no accounts yet", "—", "—", "—", "—")
    return table


def api_panel() -> Panel:
    lines = Table.grid(padding=(0, 2))
    lines.add_column(style="cyan", width=22)
    lines.add_column(style="white")
    lines.add_row("Anthropic", "POST /v1/messages")
    lines.add_row("OpenAI", "POST /v1/chat/completions")
    lines.add_row("OpenAI", "POST /v1/responses")
    lines.add_row("OpenAI", "WS   /v1/responses")
    lines.add_row("Common", "GET  /v1/models   GET /health")
    lines.add_row("Base URL", API_BASE_URL)
    return Panel(lines, title="API", border_style="blue")


def setup_panel() -> Panel:
    body = Table.grid(expand=True)
    body.add_column(style="white")
    body.add_row("Use these settings in your app:")
    body.add_row("")
    body.add_row(f"Base URL: {API_BASE_URL}")
    body.add_row("API key:  zo-proxy")
    body.add_row("Examples: gpt-5.3-codex, gpt-5.5, claude-sonnet-4-6, claude-opus-4-7")
    return Panel(body, title="Manual client setup", border_style="blue")


def dashboard(store: AccountStore, proxy_ok: bool) -> None:
    console.clear()
    app_header("running automatically" if proxy_ok else "starting proxy")
    console.print(status_bar(store, proxy_ok))
    console.print(accounts_table(store))
    console.print(Panel(Group(api_panel(), setup_panel()), border_style="blue", title="Routes + config"))
    footer = Text()
    footer.append("↑↓ move  Enter select", style="dim")
    footer.append("   local API stays up while this window is open", style="dim")
    console.print(Panel(footer, border_style="blue", padding=(0, 1)))


def select_menu(message: str, choices: list[Any], default: str | None = None):
    return questionary.select(
        message,
        choices=choices,
        default=default,
        style=STYLE,
        qmark=">",
        pointer=">",
        instruction="↑↓ move • Enter select",
    ).ask()


def prompt_text(message: str, default: str = "") -> str | None:
    return questionary.text(message, default=default, style=STYLE, qmark="> ").ask()


def prompt_confirm(message: str, default: bool = True) -> bool:
    value = questionary.confirm(message, default=default, style=STYLE, qmark="> ").ask()
    return bool(value)


def pause() -> None:
    console.print("[dim]Press Enter to continue...[/dim]")
    input()


def add_account_manual(store: AccountStore) -> None:
    console.clear()
    app_header("manual cookie fallback")
    console.print(Panel("Paste full Cookie header from a Zo /ask request.", border_style="yellow"))
    raw = prompt_text("Cookie header:", "") or ""
    access, refresh = extract_tokens_from_cookie(raw)
    if not access:
        console.print(f"[red]{ICONS['err']} access_token not found.[/red]")
        pause()
        return
    domain = prompt_text("Workspace domain:", extract_domain_from_access_token(access) or "") or ""
    if not domain:
        return
    label = prompt_text("Label:", f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[red]{ICONS['err']} verification failed: {e}[/red]")
        pause()
        return
    store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
    console.print(Panel(f"Saved {label}\nbalance: {balance if balance is not None else '?'}¢\nmodels: {models}", border_style="green"))
    pause()


def add_account_via_browser(store: AccountStore) -> None:
    console.clear()
    app_header("temporary browser auth")
    console.print(Panel(
        "A fresh temporary Playwright Chromium will open.\n\n"
        "Log into Zo there. As soon as access_token + refresh_token appear,\n"
        "the browser closes automatically, then the account is verified,\n"
        "balance/models are fetched, and the account is saved.\n\n"
        "Nothing is read from your normal Chrome/Edge/Firefox profile.",
        border_style="cyan",
    ))
    if not ensure_playwright_chromium():
        console.print(f"[red]{ICONS['err']} could not install Playwright Chromium.[/red]")
        pause()
        return
    captured: tuple[str, str, str] | None = None
    with tempfile.TemporaryDirectory(prefix="zo-proxy-browser-") as tmp:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir=tmp,
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
                        captured = (access, refresh, extract_domain_from_access_token(access) or "")
                        break
                    page.wait_for_timeout(1000)
                browser.close()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            console.print(f"[red]{ICONS['err']} browser auth failed: {e}[/red]")
            pause()
            return
    if not captured:
        console.print(f"[yellow]{ICONS['warn']} cookies were not captured in time.[/yellow]")
        if prompt_confirm("Use manual cookie fallback?", False):
            add_account_manual(store)
        return
    access, refresh, domain_guess = captured
    label = prompt_text("Label:", f"acc{len(store.accounts)+1}") or f"acc{len(store.accounts)+1}"
    domain = prompt_text("Workspace domain:", domain_guess) or domain_guess
    if not domain:
        console.print(f"[red]{ICONS['err']} workspace domain is required.[/red]")
        pause()
        return
    acc = Account(label=label, domain=domain.strip(), access_token=access, refresh_token=refresh, added_at=dt.datetime.now(dt.timezone.utc).isoformat())
    console.print(f"[cyan]{ICONS['run']} verifying login, balance, and models ...[/cyan]")
    try:
        balance, models = asyncio.run(fetch_account_health(acc))
    except Exception as e:
        console.print(f"[red]{ICONS['err']} cookies captured, but verification failed: {e}[/red]")
        if prompt_confirm("Save anyway?", False):
            store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
        pause()
        return
    acc.balance_cents = balance
    acc.balance_checked_at = time.time()
    store.add(acc, make_active=not store.accounts or prompt_confirm("Make active?", True))
    console.print(Panel(
        f"{ICONS['ok']} added {label}\nemail: {acc.email() or '?'}\ndomain: {acc.domain}\nbalance: {balance if balance is not None else '?'}¢\nmodels: {models}",
        border_style="green",
    ))
    pause()


def pick_account_label(store: AccountStore, title: str) -> str | None:
    if not store.accounts:
        return None
    choices = [Choice(f"{'*' if a.label == store.active_label else ' '}  {a.label:<10}  {a.email() or '?':<28}  {a.domain}", a.label) for a in store.accounts]
    choices += [Separator(), Choice("Back", None)]
    return select_menu(title, choices)


def accounts_menu(store: AccountStore) -> None:
    while True:
        console.clear()
        dashboard(store, proxy_running())
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
                Choice("Refresh login / balance / models", "refresh", disabled=None if store.accounts else "no accounts"),
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
        elif action == "refresh":
            console.print(f"[cyan]{ICONS['run']} refreshing login / balances / models ...[/cyan]")
            asyncio.run(refresh_store_health(store))
            pause()


def copy_setup_examples() -> None:
    console.clear()
    app_header("manual setup examples")
    console.print(Panel(
        f"Base URL: {API_BASE_URL}\nAPI key:  zo-proxy\n\nOpenCode / Codex app / other OpenAI-compatible app:\n  use OpenAI-compatible mode\n  set Base URL to {API_BASE_URL}\n  set API key to zo-proxy\n\nClaude Code / Anthropic-compatible app:\n  Base URL: {PROXY_URL}\n  Auth token / API key: zo-proxy\n  endpoint: /v1/messages",
        border_style="green",
    ))
    pause()


def open_docs() -> None:
    webbrowser.open("https://docs.zocomputer.com/api")
    console.print(f"[green]{ICONS['ok']} opened Zo API docs in your browser.[/green]")
    pause()


def main() -> int:
    state = load_state()
    store = AccountStore()
    console.clear()
    app_header("booting")
    console.print(f"[cyan]{ICONS['run']} starting local proxy on {PROXY_URL} ...[/cyan]")
    if not start_proxy():
        console.print(f"[red]{ICONS['err']} failed to start local proxy.[/red]")
        return 1
    if store.accounts:
        try:
            asyncio.run(refresh_store_health(store))
        except Exception:
            pass
    while True:
        dashboard(store, proxy_running())
        choice = select_menu(
            "Actions",
            [
                Choice("Refresh status", "refresh"),
                Choice("Accounts", "accounts"),
                Choice("Show manual setup examples", "setup"),
                Choice("Open Zo API docs", "docs"),
                Separator(),
                Choice("Exit", "exit"),
            ],
            default=state.get("last_action", "refresh"),
        )
        if choice in (None, "exit"):
            return 0
        state["last_action"] = choice
        save_state(state)
        if choice == "refresh":
            console.print(f"[cyan]{ICONS['run']} refreshing status ...[/cyan]")
            if store.accounts:
                asyncio.run(refresh_store_health(store))
        elif choice == "accounts":
            accounts_menu(store)
            store.load()
        elif choice == "setup":
            copy_setup_examples()
        elif choice == "docs":
            open_docs()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
