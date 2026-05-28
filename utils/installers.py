"""
utils/installers.py — автоматическая прописка ZoAPI в Codex / Claude Code.

Что делает:
- Persistent env vars:
    * Windows: `setx NAME VALUE` (user scope) + текущий процесс.
    * macOS/Linux: блок `# >>> zoapi env >>> ... # <<< zoapi env <<<`
      в ~/.zshrc, ~/.bashrc, ~/.profile (идемпотентно, можно удалить).
- Codex CLI: ~/.codex/config.toml с провайдером `zoapi`
  (base_url = http://127.0.0.1:17878/v1, wire_api = responses).
- Claude Code: ~/.claude/settings.json с env-блоком
  (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY).

Всё можно откатить (uninstall_codex / uninstall_claude).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

try:
    from config import PROXY_PORT  # type: ignore
except Exception:
    PROXY_PORT = 17878

PROXY_HOST = "127.0.0.1"
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
OPENAI_BASE = f"{PROXY_URL}/v1"
DUMMY_KEY = "zo-proxy"

ENV_MARKER_START = "# >>> zoapi env >>>"
ENV_MARKER_END = "# <<< zoapi env <<<"
TOML_MARKER_START = "# >>> zoapi provider >>>"
TOML_MARKER_END = "# <<< zoapi provider <<<"


def is_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------


def _setx(name: str, value: str) -> tuple[bool, str]:
    """Windows: persist в user scope через setx и обновить текущий процесс."""
    try:
        res = subprocess.run(
            ["setx", name, value],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            shell=False,
        )
        ok = res.returncode == 0
        msg = ((res.stdout or "") + (res.stderr or "")).strip()
        if ok:
            os.environ[name] = value
        return ok, msg
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _reg_unset(name: str) -> tuple[bool, str]:
    """Windows: удалить user-scope env var через reg delete."""
    try:
        res = subprocess.run(
            ["reg", "delete", "HKCU\\Environment", "/F", "/V", name],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            shell=False,
        )
        os.environ.pop(name, None)
        return res.returncode == 0, ((res.stdout or "") + (res.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _unix_rc_files() -> list[Path]:
    home = Path.home()
    return [home / ".zshrc", home / ".bashrc", home / ".profile"]


_EXPORT_RE = re.compile(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"\s*$')


def _parse_existing_block(text: str) -> dict[str, str]:
    """Достать пары KEY=VALUE из существующего zoapi-блока."""
    s = text.find(ENV_MARKER_START)
    if s == -1:
        return {}
    e = text.find(ENV_MARKER_END, s)
    if e == -1:
        return {}
    block = text[s + len(ENV_MARKER_START) : e]
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = _EXPORT_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _strip_block(text: str, start_tag: str, end_tag: str) -> str:
    s = text.find(start_tag)
    if s == -1:
        return text
    e = text.find(end_tag, s)
    if e == -1:
        return text
    e_end = e + len(end_tag)
    # съесть один трейлинг \n если есть
    if e_end < len(text) and text[e_end] == "\n":
        e_end += 1
    return text[:s].rstrip() + ("\n" if text[:s].rstrip() else "") + text[e_end:].lstrip("\n")


def _write_unix_block(rc: Path, exports: dict[str, str]) -> None:
    rc.parent.mkdir(parents=True, exist_ok=True)
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    # мержим с тем, что уже было в блоке, чтобы не сносить чужие наши же ключи
    merged = {**_parse_existing_block(existing), **exports}
    cleaned = _strip_block(existing, ENV_MARKER_START, ENV_MARKER_END)
    lines = [ENV_MARKER_START]
    for k, v in merged.items():
        lines.append(f'export {k}="{v}"')
    lines.append(ENV_MARKER_END)
    block = "\n".join(lines)
    if cleaned.strip():
        new = cleaned.rstrip() + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    rc.write_text(new, encoding="utf-8")


def _remove_unix_keys(rc: Path, keys: Iterable[str]) -> None:
    """Удалить конкретные KEY из zoapi-блока (для частичного uninstall)."""
    if not rc.exists():
        return
    existing = rc.read_text(encoding="utf-8")
    parsed = _parse_existing_block(existing)
    if not parsed:
        return
    for k in keys:
        parsed.pop(k, None)
    cleaned = _strip_block(existing, ENV_MARKER_START, ENV_MARKER_END)
    if not parsed:
        # блок пуст — просто удалить
        if cleaned != existing:
            rc.write_text(cleaned, encoding="utf-8")
        return
    lines = [ENV_MARKER_START]
    for k, v in parsed.items():
        lines.append(f'export {k}="{v}"')
    lines.append(ENV_MARKER_END)
    block = "\n".join(lines)
    if cleaned.strip():
        new = cleaned.rstrip() + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    rc.write_text(new, encoding="utf-8")


def _remove_unix_block(rc: Path) -> None:
    if not rc.exists():
        return
    existing = rc.read_text(encoding="utf-8")
    cleaned = _strip_block(existing, ENV_MARKER_START, ENV_MARKER_END)
    if cleaned != existing:
        rc.write_text(cleaned, encoding="utf-8")


def set_env_vars(vars_: dict[str, str]) -> list[str]:
    """Прописать env vars так, чтобы они выжили перезагрузку терминала."""
    log: list[str] = []
    if is_windows():
        for k, v in vars_.items():
            ok, msg = _setx(k, v)
            log.append(
                f"setx {k}: {'ok' if ok else 'fail'}"
                + (f" ({msg})" if msg and not ok else "")
            )
    else:
        # Юникс: и в файлы, и в текущий процесс.
        for k, v in vars_.items():
            os.environ[k] = v
        rcs = _unix_rc_files()
        # обновляем только реально существующие или важные (.zshrc/.bashrc)
        # .profile создаём только если нет ни zshrc, ни bashrc.
        targets = [rc for rc in rcs if rc.name in (".zshrc", ".bashrc") and rc.exists()]
        if not targets:
            shell = os.environ.get("SHELL", "")
            if "zsh" in shell:
                targets = [Path.home() / ".zshrc"]
            else:
                targets = [Path.home() / ".bashrc"]
        for rc in targets:
            try:
                _write_unix_block(rc, vars_)
                log.append(f"updated {rc}")
            except Exception as e:  # noqa: BLE001
                log.append(f"failed {rc}: {e}")
    return log


def unset_env_vars(names: Iterable[str]) -> list[str]:
    log: list[str] = []
    names = list(names)
    if is_windows():
        for n in names:
            ok, _msg = _reg_unset(n)
            log.append(f"unset {n}: {'ok' if ok else 'fail'}")
    else:
        for n in names:
            os.environ.pop(n, None)
        for rc in _unix_rc_files():
            try:
                if rc.exists():
                    _remove_unix_keys(rc, names)
                    log.append(f"cleaned keys from {rc}")
            except Exception as e:  # noqa: BLE001
                log.append(f"failed {rc}: {e}")
    return log


# ---------------------------------------------------------------------------
# Codex CLI
# ---------------------------------------------------------------------------

CODEX_DIR = Path.home() / ".codex"
CODEX_CONFIG = CODEX_DIR / "config.toml"

CODEX_BLOCK = f"""{TOML_MARKER_START}
model_provider = "zoapi"

[model_providers.zoapi]
name = "ZoAPI"
base_url = "{OPENAI_BASE}"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
{TOML_MARKER_END}"""


def install_codex() -> list[str]:
    """Прописать Codex CLI: env + ~/.codex/config.toml."""
    log = set_env_vars(
        {
            "OPENAI_API_KEY": DUMMY_KEY,
            "OPENAI_BASE_URL": OPENAI_BASE,
        }
    )
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    cleaned = _strip_block(existing, TOML_MARKER_START, TOML_MARKER_END)
    # Убрать любой свободно стоящий model_provider = ... вне нашего блока,
    # чтобы он не перебивал наш.
    cleaned_lines: list[str] = []
    for line in cleaned.splitlines():
        s = line.strip()
        if s.startswith("model_provider") and "=" in s:
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).rstrip()

    if cleaned:
        new = cleaned + "\n\n" + CODEX_BLOCK + "\n"
    else:
        new = CODEX_BLOCK + "\n"
    CODEX_CONFIG.write_text(new, encoding="utf-8")
    log.append(f"wrote {CODEX_CONFIG}")
    return log


def uninstall_codex() -> list[str]:
    log = unset_env_vars(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
    if CODEX_CONFIG.exists():
        try:
            existing = CODEX_CONFIG.read_text(encoding="utf-8")
            cleaned = _strip_block(existing, TOML_MARKER_START, TOML_MARKER_END)
            if cleaned != existing:
                CODEX_CONFIG.write_text(cleaned, encoding="utf-8")
                log.append(f"cleaned {CODEX_CONFIG}")
        except Exception as e:  # noqa: BLE001
            log.append(f"failed to clean {CODEX_CONFIG}: {e}")
    return log


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"


def install_claude() -> list[str]:
    """Прописать Claude Code: env + ~/.claude/settings.json (поле env)."""
    log = set_env_vars(
        {
            "ANTHROPIC_BASE_URL": PROXY_URL,
            "ANTHROPIC_AUTH_TOKEN": DUMMY_KEY,
            "ANTHROPIC_API_KEY": DUMMY_KEY,
        }
    )
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    env["ANTHROPIC_BASE_URL"] = PROXY_URL
    env["ANTHROPIC_AUTH_TOKEN"] = DUMMY_KEY
    env["ANTHROPIC_API_KEY"] = DUMMY_KEY
    data["env"] = env
    CLAUDE_SETTINGS.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.append(f"wrote {CLAUDE_SETTINGS}")
    return log


def uninstall_claude() -> list[str]:
    log = unset_env_vars(
        ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"]
    )
    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
            env = data.get("env") or {}
            if isinstance(env, dict):
                for k in (
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_API_KEY",
                ):
                    env.pop(k, None)
                if env:
                    data["env"] = env
                else:
                    data.pop("env", None)
            if data:
                CLAUDE_SETTINGS.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            else:
                CLAUDE_SETTINGS.unlink()
            log.append(f"cleaned {CLAUDE_SETTINGS}")
        except Exception as e:  # noqa: BLE001
            log.append(f"failed to clean {CLAUDE_SETTINGS}: {e}")
    return log


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def install_both() -> list[str]:
    return install_codex() + install_claude()


def uninstall_both() -> list[str]:
    return uninstall_codex() + uninstall_claude()


def status() -> dict[str, dict[str, str | bool]]:
    """Снимок текущей конфигурации (для UI)."""

    def has(name: str) -> bool:
        return bool(os.environ.get(name))

    codex_cfg_ok = False
    if CODEX_CONFIG.exists():
        try:
            txt = CODEX_CONFIG.read_text(encoding="utf-8")
            codex_cfg_ok = TOML_MARKER_START in txt
        except Exception:
            codex_cfg_ok = False

    claude_cfg_ok = False
    if CLAUDE_SETTINGS.exists():
        try:
            data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) or {}
            env = (data.get("env") or {}) if isinstance(data, dict) else {}
            claude_cfg_ok = isinstance(env, dict) and env.get("ANTHROPIC_BASE_URL") == PROXY_URL
        except Exception:
            claude_cfg_ok = False

    return {
        "codex": {
            "config": codex_cfg_ok,
            "OPENAI_API_KEY": has("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": has("OPENAI_BASE_URL"),
            "config_path": str(CODEX_CONFIG),
        },
        "claude": {
            "config": claude_cfg_ok,
            "ANTHROPIC_BASE_URL": has("ANTHROPIC_BASE_URL"),
            "ANTHROPIC_AUTH_TOKEN": has("ANTHROPIC_AUTH_TOKEN"),
            "ANTHROPIC_API_KEY": has("ANTHROPIC_API_KEY"),
            "config_path": str(CLAUDE_SETTINGS),
        },
    }
