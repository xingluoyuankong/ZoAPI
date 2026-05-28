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


def _broadcast_env_change() -> None:
    """Windows: WM_SETTINGCHANGE, чтобы запущенные процессы видели новые env vars."""
    if not is_windows():
        return
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def _setx(name: str, value: str) -> tuple[bool, str]:
    """Windows: persist в user scope, plus best-effort machine scope, plus
    broadcast WM_SETTINGCHANGE так чтобы новые процессы подхватили без перезагрузки."""
    try:
        # user scope
        u = subprocess.run(
            ["setx", name, value],
            capture_output=True, text=True, timeout=20, check=False, shell=False,
        )
        ok = u.returncode == 0
        msg = ((u.stdout or "") + (u.stderr or "")).strip()
        # machine scope — best effort, обычно без админ-прав сфейлит, это ок
        subprocess.run(
            ["setx", "/M", name, value],
            capture_output=True, text=True, timeout=20, check=False, shell=False,
        )
        if ok:
            os.environ[name] = value
        _broadcast_env_change()
        return ok, msg
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _reg_unset(name: str) -> tuple[bool, str]:
    """Windows: удалить env var из HKCU и (best-effort) HKLM."""
    ok = False
    msgs: list[str] = []
    try:
        u = subprocess.run(
            ["reg", "delete", "HKCU\\Environment", "/F", "/V", name],
            capture_output=True, text=True, timeout=20, check=False, shell=False,
        )
        if u.returncode == 0:
            ok = True
        else:
            msgs.append((u.stdout or "") + (u.stderr or ""))
    except Exception as e:  # noqa: BLE001
        msgs.append(str(e))
    try:
        subprocess.run(
            [
                "reg", "delete",
                "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment",
                "/F", "/V", name,
            ],
            capture_output=True, text=True, timeout=20, check=False, shell=False,
        )
    except Exception:
        pass
    os.environ.pop(name, None)
    _broadcast_env_change()
    return ok, " ".join(m.strip() for m in msgs if m).strip()


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


def _strip_unix_stray_export(rc: Path, key: str) -> None:
    """Убрать любые `export KEY=...` или `KEY=...` строки В РАЗНЫХ местах rc-файла,
    КРОМЕ нашего marker-блока — туда ключ положит set_env_vars."""
    if not rc.exists():
        return
    text = rc.read_text(encoding="utf-8")
    s = text.find(ENV_MARKER_START)
    e = text.find(ENV_MARKER_END, s) + len(ENV_MARKER_END) if s != -1 else -1
    if s != -1 and e != -1:
        before, block, after = text[:s], text[s:e], text[e:]
    else:
        before, block, after = text, "", ""
    pat = re.compile(
        rf'^\s*(?:export\s+)?{re.escape(key)}\s*=.*$',
        re.MULTILINE,
    )
    new_before = pat.sub("", before)
    new_after = pat.sub("", after)
    # уберём двойные пустые строки, не более одной подряд
    new_before = re.sub(r'\n{3,}', '\n\n', new_before)
    new_after = re.sub(r'\n{3,}', '\n\n', new_after)
    new_text = new_before + block + new_after
    if new_text != text:
        rc.write_text(new_text, encoding="utf-8")


def set_env_vars(vars_: dict[str, str]) -> list[str]:
    """Прописать env vars так, чтобы они выжили перезагрузку терминала
    И не было стейла от старых установок / ручных правок."""
    log: list[str] = []
    if is_windows():
        for k, v in vars_.items():
            ok, msg = _setx(k, v)
            log.append(
                f"setx {k}: {'ok' if ok else 'fail'}"
                + (f" ({msg})" if msg and not ok else "")
            )
    else:
        # Сначала добить любые стейл-объявления вне нашего блока
        for rc in _unix_rc_files():
            for k in vars_.keys():
                _strip_unix_stray_export(rc, k)
        # текущий процесс
        for k, v in vars_.items():
            os.environ[k] = v
        # наш блок
        for rc in _unix_rc_files():
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
    """Прописать Codex CLI: env + ~/.codex/config.toml. Жёстко: убивает любой
    стейл от старых версий, включая stray `model_provider=...` в корне и
    оставшийся вручную `[model_providers.zoapi]` БЕЗ маркеров."""
    log = set_env_vars(
        {
            "OPENAI_API_KEY": DUMMY_KEY,
            "OPENAI_BASE_URL": OPENAI_BASE,
        }
    )
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    existing = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""

    # 1) убрать наши прошлые блоки (с маркерами)
    cleaned = _strip_block(existing, TOML_TOP_START, TOML_TOP_END)
    cleaned = _strip_block(cleaned, TOML_MARKER_START, TOML_MARKER_END)
    # 2) убрать любой свободно стоящий top-level `model_provider = ...`
    cleaned = _strip_top_level_key(cleaned, "model_provider").rstrip()
    # 3) добить любую `[model_providers.zoapi]` таблицу БЕЗ маркеров (старые ручные правки)
    cleaned = re.sub(
        r'(?ms)^\[\s*model_providers\.zoapi\s*\][^\[]*?(?=^\[|\Z)',
        '',
        cleaned,
    ).rstrip()

    # 4) вставить TOP-блок перед первой [...] секцией (или в начало, если её нет)
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
    """Прописать Claude Code: env + ~/.claude/settings.json. Жёстко: удаляет
    ANTHROPIC_API_KEY изо всех мест (env vars + settings.json env block +
    stray rc-строки) чтобы не было Auth-conflict у юзеров со старой установкой."""
    log = set_env_vars(
        {
            "ANTHROPIC_BASE_URL": PROXY_URL,
            "ANTHROPIC_AUTH_TOKEN": DUMMY_KEY,
        }
    )
    # API_KEY чистим везде
    log.extend(unset_env_vars(["ANTHROPIC_API_KEY"]))
    if not is_windows():
        for rc in _unix_rc_files():
            _strip_unix_stray_export(rc, "ANTHROPIC_API_KEY")

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
    # Также чистим возможный apiKeyHelper, который перебивает наш token
    data.pop("apiKeyHelper", None)
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
        # Anthropic — Opus
        "claude-opus-4-8": {"name": "Claude Opus 4.8"},
        "claude-opus-4-8-thinking": {"name": "Claude Opus 4.8 (Thinking)"},
        "claude-opus-4-7": {"name": "Claude Opus 4.7"},
        "claude-opus-4-7-thinking": {"name": "Claude Opus 4.7 (Thinking)"},
        "claude-opus-4-6": {"name": "Claude Opus 4.6"},
        "claude-opus-4-5": {"name": "Claude Opus 4.5"},
        "claude-opus-4-1": {"name": "Claude Opus 4.1"},
        "claude-opus-4": {"name": "Claude Opus 4"},
        "claude-3-opus-latest": {"name": "Claude 3 Opus"},
        # Anthropic — Sonnet
        "claude-sonnet-4-6": {"name": "Claude Sonnet 4.6"},
        "claude-sonnet-4-6-thinking": {"name": "Claude Sonnet 4.6 (Thinking)"},
        "claude-sonnet-4-5": {"name": "Claude Sonnet 4.5"},
        "claude-sonnet-4-5-thinking": {"name": "Claude Sonnet 4.5 (Thinking)"},
        "claude-sonnet-4": {"name": "Claude Sonnet 4"},
        "claude-3-7-sonnet-latest": {"name": "Claude 3.7 Sonnet"},
        "claude-3-5-sonnet-latest": {"name": "Claude 3.5 Sonnet"},
        # Anthropic — Haiku
        "claude-haiku-4-6": {"name": "Claude Haiku 4.6"},
        "claude-haiku-4-5": {"name": "Claude Haiku 4.5"},
        "claude-3-5-haiku-latest": {"name": "Claude 3.5 Haiku"},
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
        # Google
        "gemini-3.0-pro": {"name": "Gemini 3.0 Pro"},
        "gemini-2.5-pro": {"name": "Gemini 2.5 Pro"},
        "gemini-2.5-flash": {"name": "Gemini 2.5 Flash"},
        # xAI
        "grok-4": {"name": "Grok 4"},
        "grok-3": {"name": "Grok 3"},
        # DeepSeek
        "deepseek-v3": {"name": "DeepSeek V3"},
        "deepseek-r1": {"name": "DeepSeek R1"},
        # Other
        "kimi-k2": {"name": "Kimi K2"},
        "qwen-3-coder": {"name": "Qwen 3 Coder"},
        "llama-3.3": {"name": "Llama 3.3"},
    },
}


def install_opencode() -> list[str]:
    """Прописать OpenCode: ЖЁСТКО перезаписать provider.zoapi на свежий
    каталог. Других провайдеров (openrouter / anthropic / ...) НЕ трогаем."""
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

    data.setdefault("$schema", "https://opencode.ai/config.json")

    provider = data.get("provider")
    if not isinstance(provider, dict):
        provider = {}
    # ЖЁСТКАЯ ПЕРЕЗАПИСЬ — старый zoapi может содержать устаревшие/битые поля,
    # мерж их сохранил бы. Целиком меняем на свежий каталог.
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
