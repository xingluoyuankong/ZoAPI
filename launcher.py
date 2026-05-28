from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_client": "claude"}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_client": "claude"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def run_accounts_menu() -> None:
    subprocess.run([sys.executable, "setup.py"], cwd=ROOT)


def env_for_client(client_id: str) -> dict[str, str]:
    env = os.environ.copy()
    if client_id == "claude":
        if env.get("ANTHROPIC_API_KEY"):
            print("[warn] ANTHROPIC_API_KEY already set in this shell. Clearing locally.")
        if env.get("ANTHROPIC_BASE_URL"):
            print("[warn] ANTHROPIC_BASE_URL already set in this shell. Overriding locally.")
        env["ANTHROPIC_BASE_URL"] = PROXY_URL
        env["ANTHROPIC_AUTH_TOKEN"] = "zo-proxy"
        env["ANTHROPIC_API_KEY"] = ""
        env["DISABLE_TELEMETRY"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    elif client_id == "codex":
        codex_home = ROOT / ".codex-home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "config.toml").write_text(
            f'openai_base_url = "{PROXY_URL}/v1"\nmodel = "gpt-5.3-codex"\n',
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
                            "claude-sonnet-4-6": {"name": "Claude Sonnet 4.6 via Zo"},
                            "claude-opus-4-7": {"name": "Claude Opus 4.7 via Zo"},
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


def choose_client(state: dict) -> str | None:
    default = state.get("last_client", "claude")
    mapping = {str(i + 1): client_id for i, (client_id, _, _) in enumerate(CLIENTS)}
    while True:
        print("\n=== zo-claude-proxy ===")
        print(f"Proxy: {PROXY_URL}")
        print("\nКого запускать:")
        for i, (client_id, title, bin_name) in enumerate(CLIENTS, start=1):
            mark = "*" if client_id == default else " "
            status = "ok" if shutil.which(bin_name) else "не найден"
            print(f"  {i}. [{mark}] {title:<12} ({status})")
        print("  5. Аккаунты / ротация")
        print("  q. Выход")
        raw = input(f"\nВыбор [Enter={default}]: ").strip().lower()
        if raw == "":
            return default
        if raw == "q":
            return None
        if raw == "5":
            run_accounts_menu()
            continue
        if raw in mapping:
            state["last_client"] = mapping[raw]
            save_state(state)
            return mapping[raw]
        for client_id, title, bin_name in CLIENTS:
            if raw in (client_id, title.lower(), bin_name.lower()):
                state["last_client"] = client_id
                save_state(state)
                return client_id
        print("[!] Не понял выбор.")


def main() -> int:
    state = load_state()
    client_id = choose_client(state)
    if not client_id:
        return 0
    record = next(item for item in CLIENTS if item[0] == client_id)
    title, bin_name = record[1], record[2]
    exe = shutil.which(bin_name)
    if not exe:
        print(f"[!] {title} не найден в PATH.")
        return 1
    if not start_proxy():
        print("[!] Не удалось поднять локальный прокси.")
        return 1
    print(f"[+] Прокси готов: {PROXY_URL}")
    print(f"[+] Запускаю {title}...")
    env = env_for_client(client_id)
    raise SystemExit(subprocess.call([exe, *sys.argv[1:]], cwd=ROOT, env=env))


if __name__ == "__main__":
    raise SystemExit(main())
