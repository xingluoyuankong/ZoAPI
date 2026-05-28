"""
utils/installers.py — автоматическая прописка ZoAPI в Codex / Claude Code / OpenCode.

Что делает:
- Persistent env vars:
    * Windows: `setx NAME VALUE` (user scope) + текущий процесс.
    * macOS/Linux: блок `# >>> zoapi env >>> ... # <<< zoapi env <<<`
      в ~/.zshrc, ~/.bashrc, ~/.profile (идемпотентно, можно удалить).
- Codex CLI: ~/.codex/config.toml — корневой ключ `model_provider = "zoapi"`
  и таблица `[model_providers.zoapi]` с маркерами; чужие настройки не трогаем.
- Claude Code: ~/.claude/settings.json с env-блоком
  (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN). ANTHROPIC_API_KEY НЕ ставим:
  Claude Code ругается «Auth conflict» если выставлены обе переменные.
- OpenCode: ~/.config/opencode/opencode.json — мержим только `provider.zoapi`,
  остальные провайдеры и настройки сохраняются.

Всё можно откатить (uninstall_codex / uninstall_claude / uninstall_opencode).
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
TOML_TOP_START = "# >>> zoapi top >>>"
TOML_TOP_END = "# <<< zoapi top <<<"


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

CODEX_TOP_BLOCK = f"""{TOML_TOP_START}
model_provider = "zoapi"
{TOML_TOP_END}"""

CODEX_SECTION_BLOCK = f"""{TOML_MARKER_START}
[model_providers.zoapi]
name = "ZoAPI"
base_url = "{OPENAI_BASE}"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
{TOML_MARKER_END}"""


def _strip_top_level_key(text: str, key: str) -> str:
    """Удалить строки `<key> = ...`, встречающиеся ДО первой `[...]` секции."""
    out: list[str] = []
    seen_section = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not seen_section and stripped.startswith("["):
            seen_section = True
        if not seen_section and re.match(rf"\s*{re.escape(key)}\s*=", line):
            continue
        out.append(line)
    return "\n".join(out)


def install_codex() -> list[str]:
    """Прописать Codex CLI: env + ~/.codex/config.toml.

    Корневой ключ `model_provider = "zoapi"` всегда оказывается в корне TOML
    (вставляется до первой `[...]` секции), таблица провайдера — в конце файла.
    """
    log = set_env_vars(
        {
            "OPENAI_API_KEY": DUMMY_KEY,
            "OPENAI_BASE_URL": OPENAI_BASE,
        }
    )
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""

    # 1) убрать наши прошлые блоки
    cleaned = _strip_block(existing, TOML_TOP_START, TOML_TOP_END)
    cleaned = _strip_block(cleaned, TOML_MARKER_START, TOML_MARKER_END)
    # 2) убрать любой свободно стоящий top-level `model_provider = ...`
    cleaned = _strip_top_level_key(cleaned, "model_provider").rstrip()

    # 3) вставить TOP-блок перед первой [...] секцией (или в начало, если её нет)
    lines = cleaned.splitlines()
    section_idx = next(
        (i for i, l in enumerate(lines) if l.lstrip().startswith("[")),
        len(lines),
    )
    head = "\n".join(lines[:section_idx]).rstrip()
    tail = "\n".join(lines[section_idx:]).strip("\n")

    pieces: list[str] = []
    if head:
        pieces.append(head)
    pieces.append(CODEX_TOP_BLOCK)
    if tail:
        pieces.append(tail)
    pieces.append(CODEX_SECTION_BLOCK)

    new = "\n\n".join(pieces).rstrip() + "\n"
    CODEX_CONFIG.write_text(new, encoding="utf-8")
    log.append(f"wrote {CODEX_CONFIG}")
    return log


def uninstall_codex() -> list[str]:
    log = unset_env_vars(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
    if CODEX_CONFIG.exists():
        try:
            existing = CODEX_CONFIG.read_text(encoding="utf-8")
            cleaned = _strip_block(existing, TOML_TOP_START, TOML_TOP_END)
            cleaned = _strip_block(cleaned, TOML_MARKER_START, TOML_MARKER_END)
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
        }
    )
    # Claude Code ругается «Auth conflict», если выставлены и ANTHROPIC_AUTH_TOKEN,
    # и ANTHROPIC_API_KEY. Старые версии лаунчера ставили обе — чистим API_KEY.
    log.extend(unset_env_vars(["ANTHROPIC_API_KEY"]))
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
    env.pop("ANTHROPIC_API_KEY", None)
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
# OpenCode
# ---------------------------------------------------------------------------

OPENCODE_DIR = Path.home() / ".config" / "opencode"
OPENCODE_CONFIG = OPENCODE_DIR / "opencode.json"

OPENCODE_PROVIDER_KEY = "zoapi"
OPENCODE_PROVIDER = {
    "npm": "@ai-sdk/openai-compatible",
    "name": "ZoAPI",
    "options": {
        "baseURL": OPENAI_BASE,
        "apiKey": DUMMY_KEY,
    },
    "models": {
        # OpenAI
        "gpt-5": {"name": "GPT-5"},
        "gpt-5-pro": {"name": "GPT-5 Pro"},
        "gpt-5-codex": {"name": "GPT-5 Codex"},
        "gpt-5-mini": {"name": "GPT-5 Mini"},
        "gpt-5-nano": {"name": "GPT-5 Nano"},
        "gpt-4o": {"name": "GPT-4o"},
        "gpt-4o-mini": {"name": "GPT-4o Mini"},
        "o3": {"name": "o3"},
        "o3-pro": {"name": "o3 Pro"},
        "o3-mini": {"name": "o3 Mini"},
        "o4-mini": {"name": "o4 Mini"},
        "o1": {"name": "o1"},
        "o1-preview": {"name": "o1 Preview"},
        # Anthropic
        "claude-opus-4-8": {"name": "Claude Opus 4.8"},
        "claude-opus-4-8-thinking": {"name": "Claude Opus 4.8 (Thinking)"},
        "claude-opus-4-7": {"name": "Claude Opus 4.7"},
        "claude-opus-4-5": {"name": "Claude Opus 4.5"},
        "claude-opus-4-1": {"name": "Claude Opus 4.1"},
        "claude-opus-4": {"name": "Claude Opus 4"},
        "claude-sonnet-4-6": {"name": "Claude Sonnet 4.6"},
        "claude-sonnet-4-6-thinking": {"name": "Claude Sonnet 4.6 (Thinking)"},
        "claude-sonnet-4-5": {"name": "Claude Sonnet 4.5"},
        "claude-sonnet-4-5-extended-thinking": {"name": "Claude Sonnet 4.5 (Extended)"},
        "claude-sonnet-4": {"name": "Claude Sonnet 4"},
        "claude-haiku-4-6": {"name": "Claude Haiku 4.6"},
        "claude-haiku-4-5": {"name": "Claude Haiku 4.5"},
        "claude-3-7-sonnet": {"name": "Claude 3.7 Sonnet"},
        "claude-3-5-sonnet": {"name": "Claude 3.5 Sonnet"},
        "claude-3-5-haiku": {"name": "Claude 3.5 Haiku"},
        "claude-3-opus": {"name": "Claude 3 Opus"},
        # Google
        "gemini-3.0-pro": {"name": "Gemini 3.0 Pro"},
        "gemini-3.0-flash": {"name": "Gemini 3.0 Flash"},
        "gemini-3.0-pro-thinking": {"name": "Gemini 3.0 Pro Thinking"},
        "gemini-2.5-pro": {"name": "Gemini 2.5 Pro"},
        "gemini-2.5-flash": {"name": "Gemini 2.5 Flash"},
        "gemini-2.0-pro": {"name": "Gemini 2.0 Pro"},
        "gemini-2.0-flash": {"name": "Gemini 2.0 Flash"},
        # xAI
        "grok-4": {"name": "Grok 4"},
        "grok-3": {"name": "Grok 3"},
        "grok-2": {"name": "Grok 2"},
        # DeepSeek
        "deepseek-v3": {"name": "DeepSeek V3"},
        "deepseek-r1": {"name": "DeepSeek R1"},
        # Meta
        "llama-4": {"name": "Llama 4"},
        "llama-3.3-70b": {"name": "Llama 3.3 70B"},
    },
}


def install_opencode() -> list[str]:
    """Прописать OpenCode: добавить провайдера `zoapi` в opencode.json
    БЕЗ удаления остальных провайдеров / настроек.
    """
    log: list[str] = []
    OPENCODE_DIR.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if OPENCODE_CONFIG.exists():
        try:
            raw = OPENCODE_CONFIG.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception as e:  # noqa: BLE001
            log.append(f"warn: existing {OPENCODE_CONFIG} not valid JSON ({e}); will rewrite")
            data = {}

    # schema подставляем только если отсутствует — не перетираем чужой
    data.setdefault("$schema", "https://opencode.ai/config.json")

    provider = data.get("provider")
    if not isinstance(provider, dict):
        provider = {}
    # МЕРЖ: правим только наш ключ, остальные провайдеры не трогаем
    provider[OPENCODE_PROVIDER_KEY] = OPENCODE_PROVIDER
    data["provider"] = provider

    OPENCODE_CONFIG.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.append(f"wrote {OPENCODE_CONFIG} (provider.{OPENCODE_PROVIDER_KEY})")
    return log


def uninstall_opencode() -> list[str]:
    log: list[str] = []
    if not OPENCODE_CONFIG.exists():
        return log
    try:
        raw = OPENCODE_CONFIG.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return log
        provider = data.get("provider")
        if isinstance(provider, dict) and OPENCODE_PROVIDER_KEY in provider:
            provider.pop(OPENCODE_PROVIDER_KEY, None)
            if provider:
                data["provider"] = provider
            else:
                data.pop("provider", None)
            OPENCODE_CONFIG.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            log.append(f"removed provider.{OPENCODE_PROVIDER_KEY} from {OPENCODE_CONFIG}")
    except Exception as e:  # noqa: BLE001
        log.append(f"failed to clean {OPENCODE_CONFIG}: {e}")
    return log


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def install_both() -> list[str]:
    return install_codex() + install_claude() + install_opencode()


def uninstall_both() -> list[str]:
    return uninstall_codex() + uninstall_claude() + uninstall_opencode()


def status() -> dict[str, dict[str, str | bool]]:
    """Снимок текущей конфигурации (для UI)."""

    def has(name: str) -> bool:
        return bool(os.environ.get(name))

    codex_cfg_ok = False
    if CODEX_CONFIG.exists():
        try:
            txt = CODEX_CONFIG.read_text(encoding="utf-8")
            codex_cfg_ok = TOML_MARKER_START in txt and TOML_TOP_START in txt
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

    opencode_cfg_ok = False
    if OPENCODE_CONFIG.exists():
        try:
            data = json.loads(OPENCODE_CONFIG.read_text(encoding="utf-8")) or {}
            prov = (data.get("provider") or {}) if isinstance(data, dict) else {}
            opencode_cfg_ok = isinstance(prov, dict) and OPENCODE_PROVIDER_KEY in prov
        except Exception:
            opencode_cfg_ok = False

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
        "opencode": {
            "config": opencode_cfg_ok,
            "config_path": str(OPENCODE_CONFIG),
        },
    }
