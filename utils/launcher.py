"""
zo-claude-proxy — единый TUI-лончер.

Запуск:
    run.bat       (Windows)
    ./run.sh      (macOS / Linux)

Стрелки ↑↓ + Enter. Запоминает последний выбор клиента.

Возможности:
 - стартует локальный прокси (если ещё не поднят)
 - запускает Claude Code / Codex / OpenCode / Hermes с правильным окружением
 - регистрация Zo-аккаунта через браузер (cookies подсасываются
   автоматически из Chrome / Edge / Firefox / Brave / Opera / Vivaldi /
   Chromium / Safari через browser-cookie3)
 - выбор активного аккаунта или режим ротации
"""

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
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import questionary
from questionary import Choice, Separator

from accounts import (
    Account,
    AccountStore,
    extract_domain_from_access_token,
    extract_tokens_from_cookie,
)

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

# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_client": "claude"}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_client": "claude"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# proxy lifecycle
# ---------------------------------------------------------------------------


def proxy_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def start_proxy() -> bool:
    if proxy_running():
        return True
    python = sys.executable
    cmd = [python, "proxy.py"]
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
        subprocess.Popen(
            cmd,
            cwd=ROOT,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        with LOG_FILE.open("ab") as f:
            subprocess.Popen(
                cmd, cwd=ROOT, stdout=f, stderr=f, start_new_session=True
            )
    for _ in range(50):
        time.sleep(0.2)
        if proxy_running():
            return True
    return False


# ---------------------------------------------------------------------------
# browser-based cookie capture
# ---------------------------------------------------------------------------


ZO_HOME = "https://zo.computer/"


def _browser_loaders() -> list[tuple[str, Any]]:
    try:
        import browser_cookie3 as bc3  # type: ignore
    except Exception:
        return []
    names = [
        "chrome",
        "edge",
        "brave",
        "opera",
        "chromium",
        "vivaldi",
        "firefox",
        "librewolf",
        "safari",
    ]
    out: list[tuple[str, Any]] = []
    for n in names:
        loader = getattr(bc3, n, None)
        if callable(loader):
            out.append((n, loader))
    return out


def try_detect_cookies() -> tuple[str, str, str] | None:
    """
    Перебирает установленные браузеры и забирает access_token + refresh_token
    с домена .zo.computer. Возвращает (access, refresh, domain) или None.
    """
    access = ""
    refresh = ""
    for name, loader in _browser_loaders():
        try:
            jar = loader(domain_name="zo.computer")
        except Exception:
            continue
        for cookie in jar:
            host = (cookie.domain or "").lstrip(".").lower()
            if not host.endswith("zo.computer"):
                continue
            if cookie.name == "access_token" and not access:
                access = cookie.value
            elif cookie.name == "refresh_token" and not refresh:
                refresh = cookie.value
        if access:
            break
    if not access:
        return None
    if refresh:
        refresh = unquote(refresh)
    domain = extract_domain_from_access_token(access) or ""
    return access, refresh, domain


def add_account_via_browser(store: AccountStore) -> None:
    questionary.print(
        "\n[step 1] Сейчас открою браузер. Залогинься в Zo Computer "
        "(или просто убедись, что уже залогинен) и вернись сюда.",
        style="fg:#888888",
    )
    try:
        webbrowser.open(ZO_HOME)
    except Exception:
        questionary.print(
            f"[!] Не получилось открыть браузер автоматически. Открой вручную: {ZO_HOME}",
            style="fg:#cc6666",
        )

    questionary.text(
        "Когда залогинился — нажми Enter, чтобы я забрал cookies:",
        default="",
    ).ask()

    detected = try_detect_cookies()
    if not detected:
        questionary.print(
            "[!] Не нашёл cookies в браузерах через browser-cookie3.",
            style="fg:#cc6666",
        )
        if sys.platform == "darwin":
            questionary.print(
                "    На macOS Chrome просит keychain access — иногда система "
                "это блокирует. Попробуй Firefox или вставь cookie вручную.",
                style="fg:#888888",
            )
        if questionary.confirm(
            "Вставить Cookie-хедер вручную?", default=False
        ).ask():
            add_account_manual(store)
        return

    access, refresh, jwt_domain = detected
    suggested_label = f"acc{len(store.accounts) + 1}"
    suggested_domain = jwt_domain or ""

    questionary.print(
        f"[+] Нашёл сессию: домен={suggested_domain or '?'}, "
        f"refresh_token={'есть' if refresh else 'нет'}",
        style="fg:#88cc88",
    )

    domain = questionary.text(
        "Домен на zo.computer",
        default=suggested_domain,
    ).ask()
    if not domain:
        questionary.print("[!] Отменено.", style="fg:#cc6666")
        return

    label = questionary.text(
        "Короткий label для этого аккаунта",
        default=suggested_label,
    ).ask() or suggested_label

    if any(a.label == label for a in store.accounts):
        if not questionary.confirm(
            f"Аккаунт '{label}' уже есть — перезаписать?", default=False
        ).ask():
            return

    acc = Account(
        label=label,
        domain=domain.strip(),
        access_token=access,
        refresh_token=refresh,
        added_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    make_active = not store.accounts or questionary.confirm(
        "Сделать активным?", default=True
    ).ask()
    store.add(acc, make_active=make_active)
    questionary.print(
        f"[+] Добавлен '{label}' ({acc.email() or '?'})",
        style="fg:#88cc88",
    )


def add_account_manual(store: AccountStore) -> None:
    questionary.print(
        "Открой DevTools → Network → POST /ask → скопируй request header 'cookie: ...'.",
        style="fg:#888888",
    )
    raw = questionary.text("Вставь Cookie-хедер целиком:").ask() or ""
    access, refresh = extract_tokens_from_cookie(raw)
    if not access:
        questionary.print("[!] access_token не найден.", style="fg:#cc6666")
        return
    domain = extract_domain_from_access_token(access) or ""
    domain = (
        questionary.text("Домен на zo.computer", default=domain).ask() or domain
    )
    if not domain:
        return
    label = questionary.text(
        "Label", default=f"acc{len(store.accounts) + 1}"
    ).ask() or f"acc{len(store.accounts) + 1}"
    acc = Account(
        label=label,
        domain=domain.strip(),
        access_token=access,
        refresh_token=refresh,
        added_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    store.add(acc, make_active=True)
    questionary.print(f"[+] Добавлен '{label}'.", style="fg:#88cc88")


# ---------------------------------------------------------------------------
# accounts menu (questionary)
# ---------------------------------------------------------------------------


def _fmt_ttl(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return f"истёк"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def _render_accounts(store: AccountStore) -> str:
    if not store.accounts:
        return "  (пусто — добавь первый аккаунт)"
    mode = getattr(store, "mode", "fixed")
    head = f"  режим: {mode}\n"
    head += (
        f"  {'active':<7}{'#':<3}{'label':<14}{'email':<28}"
        f"{'domain':<14}{'TTL':<10}{'state':<8}\n"
    )
    head += "  " + "-" * 84 + "\n"
    rows = []
    for i, a in enumerate(store.accounts):
        marker = "★" if a.label == store.active_label else " "
        ttl = _fmt_ttl(a.seconds_until_expiry())
        state = "off" if a.disabled else ("err" if a.error_streak else "ok")
        rows.append(
            f"  {marker:<7}{i:<3}{a.label:<14}{(a.email() or '?'):<28}"
            f"{a.domain:<14}{ttl:<10}{state:<8}"
        )
    return head + "\n".join(rows)


def _pick_account(store: AccountStore, prompt: str) -> Account | None:
    if not store.accounts:
        return None
    choices = []
    for i, a in enumerate(store.accounts):
        marker = "★" if a.label == store.active_label else " "
        state = "off" if a.disabled else ("err" if a.error_streak else "ok")
        title = f"{marker} #{i}  {a.label}  ({a.email() or '?'})  [{state}]"
        choices.append(Choice(title=title, value=a.label))
    choices.append(Separator())
    choices.append(Choice(title="← отмена", value=None))
    return questionary.select(prompt, choices=choices).ask()


async def _ping_all(store: AccountStore) -> None:
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


def accounts_menu(store: AccountStore) -> None:
    while True:
        print("\n=== zo-claude-proxy: аккаунты ===")
        print(_render_accounts(store))

        mode = getattr(store, "mode", "fixed")
        next_mode = "rotation" if mode == "fixed" else "fixed"

        action = questionary.select(
            "Что делаем?",
            choices=[
                Choice(title="➕ Добавить аккаунт через браузер", value="add"),
                Choice(
                    title=f"⇄ Переключить режим (сейчас {mode} → {next_mode})",
                    value="mode",
                ),
                Choice(
                    title="★ Сделать аккаунт активным",
                    value="set_active",
                    disabled=None if store.accounts else "нет аккаунтов",
                ),
                Choice(
                    title="⊘ Отключить / включить аккаунт",
                    value="toggle",
                    disabled=None if store.accounts else "нет аккаунтов",
                ),
                Choice(
                    title="🗑 Удалить аккаунт",
                    value="delete",
                    disabled=None if store.accounts else "нет аккаунтов",
                ),
                Choice(
                    title="📡 Пингануть все",
                    value="ping",
                    disabled=None if store.accounts else "нет аккаунтов",
                ),
                Separator(),
                Choice(title="← Назад", value="back"),
            ],
            instruction="(↑↓ выбор, Enter — подтвердить)",
        ).ask()

        if action in (None, "back"):
            return
        if action == "add":
            add_account_via_browser(store)
        elif action == "mode":
            store.set_mode(next_mode)
            questionary.print(f"[+] режим: {next_mode}", style="fg:#88cc88")
        elif action == "set_active":
            label = _pick_account(store, "Кого сделать активным?")
            if label:
                store.set_active(label)
                questionary.print(f"[+] активный: {label}", style="fg:#88cc88")
        elif action == "toggle":
            label = _pick_account(store, "Кого включить/отключить?")
            if not label:
                continue
            acc = store.get(label)
            if not acc:
                continue
            if acc.disabled:
                store.enable(label)
                questionary.print(f"[+] {label} включён", style="fg:#88cc88")
            else:
                store.disable(label, "disabled from menu")
                questionary.print(f"[+] {label} отключён", style="fg:#cc6666")
        elif action == "delete":
            label = _pick_account(store, "Какой аккаунт удалить?")
            if not label:
                continue
            if questionary.confirm(
                f"Точно удалить '{label}'?", default=False
            ).ask():
                store.remove(label)
                questionary.print(f"[+] удалён {label}", style="fg:#cc6666")
        elif action == "ping":
            asyncio.run(_ping_all(store))


# ---------------------------------------------------------------------------
# main launcher menu
# ---------------------------------------------------------------------------


def _client_status(bin_name: str) -> str:
    return "ok" if shutil.which(bin_name) else "не установлен"


def main_menu(state: dict, store: AccountStore) -> str | None:
    last = state.get("last_client", "claude")
    n_accs = len(store.accounts)
    mode = getattr(store, "mode", "fixed")
    active = store.active_label or "—"

    choices: list[Any] = []
    for cid, title, bin_name in CLIENTS:
        status = _client_status(bin_name)
        suffix = "" if status == "ok" else f"  ({status})"
        choices.append(Choice(title=f"▶ {title}{suffix}", value=cid))
    choices.append(Separator())
    accounts_label = (
        f"⚙ Аккаунты  ({n_accs} шт, режим={mode}, активный={active})"
    )
    choices.append(Choice(title=accounts_label, value="accounts"))
    choices.append(Choice(title="✕ Выход", value="exit"))

    default = last if any(
        isinstance(c, Choice) and c.value == last for c in choices
    ) else CLIENTS[0][0]

    return questionary.select(
        "zo-claude-proxy — что запускаем?",
        choices=choices,
        default=default,
        instruction="(↑↓ выбор, Enter — подтвердить)",
        qmark="·",
    ).ask()


# ---------------------------------------------------------------------------
# environment per client
# ---------------------------------------------------------------------------


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
        (codex_home / "config.toml").write_text(
            f'openai_base_url = "{PROXY_URL}/v1"\n'
            'model = "gpt-5.3-codex"\n',
            encoding="utf-8",
        )
        env["CODEX_HOME"] = str(codex_home)
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
    elif client_id == "opencode":
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "zo": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Zo Proxy",
                        "options": {
                            "baseURL": f"{PROXY_URL}/v1",
                            "apiKey": "{env:OPENAI_API_KEY}",
                        },
                        "models": {
                            "gpt-5.5": {"name": "GPT-5.5 via Zo"},
                            "gpt-5.3-codex": {"name": "GPT-5.3 Codex via Zo"},
                            "claude-sonnet-4-6": {
                                "name": "Claude Sonnet 4.6 via Zo"
                            },
                            "claude-opus-4-7": {
                                "name": "Claude Opus 4.7 via Zo"
                            },
                        },
                    }
                },
                "model": "zo/gpt-5.3-codex",
            },
            ensure_ascii=False,
        )
    elif client_id == "hermes":
        env["OPENAI_BASE_URL"] = f"{PROXY_URL}/v1"
        env["OPENAI_API_KEY"] = "zo-proxy"
        env.setdefault("HERMES_MODEL", "gpt-5.5")
    return env


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    state = load_state()
    store = AccountStore()

    while True:
        choice = main_menu(state, store)
        if choice in (None, "exit"):
            return 0
        if choice == "accounts":
            accounts_menu(store)
            store.load()  # перечитать на случай ручных правок
            continue
        # клиент
        if not store.list_usable():
            questionary.print(
                "Нет валидного аккаунта — открою аккаунты, добавь сначала.",
                style="fg:#cc9966",
            )
            accounts_menu(store)
            if not store.list_usable():
                continue
        state["last_client"] = choice
        save_state(state)
        record = next(item for item in CLIENTS if item[0] == choice)
        title, bin_name = record[1], record[2]
        exe = shutil.which(bin_name)
        if not exe:
            questionary.print(
                f"[!] {title} не найден в PATH.",
                style="fg:#cc6666",
            )
            continue
        if not start_proxy():
            questionary.print(
                "[!] Не удалось поднять локальный прокси.",
                style="fg:#cc6666",
            )
            continue
        questionary.print(
            f"[+] Прокси готов: {PROXY_URL}", style="fg:#88cc88"
        )
        questionary.print(
            f"[+] Запускаю {title}...", style="fg:#88cc88"
        )
        env = env_for_client(choice)
        return subprocess.call([exe, *sys.argv[1:]], cwd=ROOT, env=env)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
