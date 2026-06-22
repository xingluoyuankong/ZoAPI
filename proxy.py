"""
zo-claude-proxy — основной HTTP сервер.

Anthropic /v1/messages → Zo /ask с автоматическим выбором аккаунта,
ротацией при ошибках и переводом стрима в формат Claude Code.

Эндпоинты:
  POST /v1/messages          — Anthropic-совместимый chat completions
  GET  /v1/models            — список моделей в Anthropic-формате
  GET  /v1/admin/accounts    — статус всех аккаунтов
  POST /v1/admin/active      — сменить активный аккаунт ({"label": "main"})
  GET  /health               — пинг

Запуск:
  python proxy.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

import config
import runtime
from accounts import Account, AccountStore
from anthropic_sse import AnthropicStreamTranslator, sse
from mapper import build_q_from_messages, get_conversation_id, get_messages_delta
from zo_client import (
    ZoAuthError,
    ZoBadRequest,
    ZoClient,
    ZoForbidden,
    ZoServerError,
)
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# config / logging
# ---------------------------------------------------------------------------

PROXY_PORT = int(getattr(config, "PROXY_PORT", 17878))
LOG_LEVEL = getattr(config, "LOG_LEVEL", "INFO").upper()
MAX_ERRORS_BEFORE_ROTATE = int(getattr(config, "MAX_ERRORS_BEFORE_ROTATE", 3))
ZO_DEFAULT_MODEL = getattr(config, "ZO_DEFAULT_MODEL", "zo:anthropic/claude-opus-4-7")
MODEL_MAP: dict[str, str] = dict(getattr(config, "MODEL_MAP", {}) or {})
HIDE_THINKING = bool(getattr(config, "HIDE_THINKING", False))
EXPANDED_PATHS: list[str] = list(getattr(config, "EXPANDED_PATHS", []) or [])

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zo-proxy")


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------

STORE = AccountStore()
ZO = ZoClient()
CONVO_CACHE: dict[str, str] = {}  # ключ "label::convo_seed" -> zo conversation_id

# --- Conversation State (from zo-proxy-public) ---
# Maps client_convo_id -> {account_label: zo_conversation_id}
_CONVO_ZO_IDS: dict[str, dict[str, str]] = {}
# Maps client_convo_id -> list of message hashes (for backtracking detection)
_CONVO_HISTORY: dict[str, list[str]] = {}

# Кэш моделей (по аккаунту) на 5 минут.
_MODELS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_MODELS_TTL = 300.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _humanize(message: str) -> str:
    return message.replace("\n", " ").strip()[:400] or "unknown"


def _anthropic_err(status: int, message: str) -> JSONResponse:
    err_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        422: "invalid_request_error",
        429: "rate_limit_error",
        500: "api_error",
        502: "api_error",
        503: "overloaded_error",
        504: "api_error",
        529: "overloaded_error",
    }.get(status, "api_error")
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": err_type, "message": _humanize(message)}},
    )


def _openai_err(status: int, message: str) -> JSONResponse:
    err_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        409: "conflict_error",
        413: "request_too_large",
        422: "invalid_request_error",
        429: "rate_limit_error",
        500: "server_error",
        502: "server_error",
        503: "server_error",
        504: "server_error",
    }.get(status, "server_error")
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": _humanize(message),
                "type": err_type,
                "param": None,
                "code": status,
            }
        },
    )


def _error_for_path(path: str, status: int, message: str) -> JSONResponse:
    if path.startswith("/v1/chat/completions") or path.startswith("/v1/responses"):
        return _openai_err(status, message)
    return _anthropic_err(status, message)


def _stringify_content(content: Any) -> str:
    """Anthropic content может быть строкой или массивом блоков."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            name = block.get("name", "?")
            tid = block.get("id") or block.get("tool_use_id") or "?"
            args = block.get("input", {})
            parts.append(
                f'\n<zo:call name="{name}" id="{tid}">{json.dumps(args, ensure_ascii=False)}</zo:call>\n'
            )
        elif t == "tool_result":
            content_inner = block.get("content")
            tid = block.get("tool_use_id", "?")
            if isinstance(content_inner, list):
                text = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content_inner
                )
            else:
                text = str(content_inner)
            parts.append(f'\n<zo:result id="{tid}">{text}</zo:result>\n')
        elif t == "image":
            parts.append("[image attachment elided]")
        elif t == "thinking":
            pass
        else:
            parts.append(str(block))
    return "".join(parts)


def _stringify_openai_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        t = block.get("type")
        if t in ("text", "input_text", "output_text"):
            parts.append(block.get("text", ""))
        elif t in ("image_url", "input_image"):
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            parts.append(f"[image attachment elided: {image_url or 'inline'}]")
        elif t in ("tool_call", "function_call"):
            name = block.get("name") or block.get("function", {}).get("name") or "tool"
            tid = block.get("id") or block.get("call_id") or block.get("tool_call_id") or "?"
            args = block.get("arguments") or block.get("input") or {}
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            parts.append(f'\n<zo:call name="{name}" id="{tid}">{args}</zo:call>\n')
        elif t in ("tool_result", "function_call_output"):
            call_id = block.get("call_id") or block.get("tool_call_id") or "?"
            output = block.get("output") or block.get("content") or ""
            if isinstance(output, list):
                output = "\n".join(_stringify_openai_content([item]) for item in output)
            parts.append(f'\n<zo:result id="{call_id}">{output}</zo:result>\n')
        else:
            parts.append(str(block))
    return "".join(parts)


def _extract_client_tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    """Достаёт имена тулов из request.tools (поддерживает Anthropic и OpenAI форматы)."""
    if not tools:
        return []
    names: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or t.get("function", {}).get("name")
        if name:
            names.append(name)
    return names


def _persona(acc: "Account | None" = None) -> str | None:
    """Возвращает persona_id для запроса.

    Приоритет:
      1) XML-mode persona из кэша ZoClient (создаётся ensure_xml_mode_active)
      2) runtime.json persona_id — ручной override через TUI
      3) None — XML-mode not ready yet
    """
    if acc is not None:
        # Кэш из zo_client
        pid = ZO._persona_cache.get(acc.label)
        if pid:
            return pid
        # Legacy bridge_persona_id
        if getattr(acc, "bridge_persona_id", None):
            return acc.bridge_persona_id
    try:
        pid = runtime.get_persona_id().strip() if hasattr(runtime, "get_persona_id") else ""
        return pid or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Message flattening — делегируем в mapper.py
#
# mapper.build_q_from_messages() форматирует тулы клиента как XML-описание
# и собирает system + conversation + tools в единую строку `q` для Zo /ask.
# XML-mode persona на стороне Zo убирает серверные тулы — модель вынуждена
# использовать <zo:call> теги.
# ---------------------------------------------------------------------------


def _flatten_messages(
    system: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    messages_subset: list[dict] | None = None,
) -> str:
    """Anthropic messages → q string для Zo /ask."""
    # Конвертируем Anthropic-формат сообщений в generic формат для mapper
    generic_msgs: list[dict] = []
    if system:
        generic_msgs.append({"role": "system", "content": system})
    for m in (messages_subset if messages_subset is not None else messages):
        role = m.get("role", "user")
        content = m.get("content")
        msg: dict[str, Any] = {"role": role, "content": content}
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
        generic_msgs.append(msg)

    if messages_subset is not None:
        # Delta mode — no system/tools
        return build_q_from_messages(generic_msgs, None, messages_subset=generic_msgs)
    return build_q_from_messages(generic_msgs, tools)


def _flatten_openai_messages(
    instructions: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    messages_subset: list[dict] | None = None,
) -> str:
    """OpenAI messages → q string для Zo /ask."""
    generic_msgs: list[dict] = []
    if instructions:
        generic_msgs.append({"role": "system", "content": instructions})
    for m in (messages_subset if messages_subset is not None else messages):
        role = (m.get("role") or "user").lower()
        if role == "developer":
            role = "system"
        content = m.get("content")
        msg: dict[str, Any] = {"role": role, "content": content}
        if m.get("tool_calls"):
            msg["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            msg["tool_call_id"] = m["tool_call_id"]
        generic_msgs.append(msg)

    if messages_subset is not None:
        return build_q_from_messages(generic_msgs, None, messages_subset=generic_msgs)
    return build_q_from_messages(generic_msgs, tools)


def _responses_input_to_messages(body: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    instructions = body.get("instructions")
    tools = body.get("tools") or []
    raw_input = body.get("input")
    messages: list[dict[str, Any]] = []

    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, dict):
        messages.append({
            "role": raw_input.get("role") or "user",
            "content": raw_input.get("content") or raw_input,
        })
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                itype = item.get("type")
                if itype in (None, "message"):
                    messages.append({
                        "role": item.get("role") or "user",
                        "content": item.get("content") or "",
                    })
                elif itype in ("function_call_output", "tool_result"):
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "function_call_output",
                            "call_id": item.get("call_id") or item.get("tool_call_id"),
                            "output": item.get("output") or item.get("content") or "",
                        }],
                    })
                else:
                    messages.append({
                        "role": item.get("role") or "user",
                        "content": [item],
                    })
            else:
                messages.append({"role": "user", "content": str(item)})

    return instructions, messages, tools


def _convo_key(account_label: str, system: str | None, first_user_msg: str) -> str:
    """
    Стабильный ключ для кэширования conversation_id. Меняется когда
    Claude Code стартует новый тред.
    """
    import hashlib
    h = hashlib.sha256()
    h.update((system or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(first_user_msg[:2048].encode("utf-8"))
    return f"{account_label}::{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# Conversation State (Omniroute-style delta + backtracking detection)
# ---------------------------------------------------------------------------


def _get_zo_convo_id(client_convo_id: str, label: str) -> str | None:
    """Получить Zo-side conversation_id для этого аккаунта в этом чате."""
    return _CONVO_ZO_IDS.get(client_convo_id, {}).get(label)


def _set_zo_convo_id(client_convo_id: str, label: str, zo_convo_id: str) -> None:
    """Сохранить маппинг client → zo conversation."""
    if client_convo_id not in _CONVO_ZO_IDS:
        _CONVO_ZO_IDS[client_convo_id] = {}
    _CONVO_ZO_IDS[client_convo_id][label] = zo_convo_id


def _check_history(client_convo_id: str, history_msgs: list[dict]) -> bool:
    """Проверяет что история сообщений монотонно растёт.
    Если нет (бэктрекинг/правка) — сбрасывает маппинг и возвращает False."""
    import hashlib as _hl

    msg_hashes: list[str] = []
    for msg in history_msgs:
        payload = {"role": msg.get("role"), "content": msg.get("content")}
        if msg.get("tool_calls"):
            payload["tool_calls"] = msg.get("tool_calls")
        if msg.get("tool_call_id"):
            payload["tool_call_id"] = msg.get("tool_call_id")
        s = json.dumps(payload, sort_keys=True)
        msg_hashes.append(_hl.md5(s.encode("utf-8")).hexdigest())

    old_hashes = _CONVO_HISTORY.get(client_convo_id)
    if old_hashes is not None:
        is_prefix = (
            len(old_hashes) <= len(msg_hashes)
            and msg_hashes[: len(old_hashes)] == old_hashes
        )
        if not is_prefix:
            # Backtrack — сброс
            _CONVO_ZO_IDS.pop(client_convo_id, None)
            _CONVO_HISTORY[client_convo_id] = msg_hashes
            return False
    _CONVO_HISTORY[client_convo_id] = msg_hashes
    return True


# ---------------------------------------------------------------------------
# Динамический список моделей Zo (для fallback на ближайшую доступную)
# ---------------------------------------------------------------------------

_AVAILABLE_IDS: set[str] = set()
_AVAILABLE_REFRESH_TS: float = 0.0
_AVAILABLE_TTL: float = 300.0
_AVAILABLE_LOCK = asyncio.Lock()


async def _refresh_available_models(force: bool = False) -> set[str]:
    """Тянем /models/available у Zo, кэшируем _AVAILABLE_TTL секунд.

    Если нет аккаунтов или сеть упала — оставляем прежний снимок (или пустой
    set, тогда fallback просто отключается)."""
    global _AVAILABLE_REFRESH_TS
    now = time.monotonic()
    if not force and _AVAILABLE_IDS and (now - _AVAILABLE_REFRESH_TS) < _AVAILABLE_TTL:
        return _AVAILABLE_IDS
    async with _AVAILABLE_LOCK:
        if not force and _AVAILABLE_IDS and (now - _AVAILABLE_REFRESH_TS) < _AVAILABLE_TTL:
            return _AVAILABLE_IDS
        a = _pick_account()
        if not a:
            return _AVAILABLE_IDS
        try:
            models = await ZO.list_models(a)
        except Exception as e:  # noqa: BLE001
            log.warning("refresh_available_models failed: %s", e)
            return _AVAILABLE_IDS
        ids = {m.get("model_name") for m in models if isinstance(m, dict) and m.get("model_name")}
        if ids:
            _AVAILABLE_IDS.clear()
            _AVAILABLE_IDS.update(ids)
            _AVAILABLE_REFRESH_TS = now
            log.info("refreshed Zo model list: %d models", len(_AVAILABLE_IDS))
    return _AVAILABLE_IDS


_FAMILY_RE = re.compile(r"^(?P<vendor>zo:[^/]+/)(?P<family>[a-z]+(?:-[a-z]+)*)(?P<rest>(?:-\d+)*.*)?$")


def _model_family(zo_id: str) -> str:
    """zo:anthropic/claude-opus-4-8           -> zo:anthropic/claude-opus
    zo:openai/gpt-5.5                      -> zo:openai/gpt
    zo:anthropic/claude-sonnet-4-6-thinking -> zo:anthropic/claude-sonnet
    Берёт word-сегменты с начала до первой цифры."""
    if "/" not in zo_id:
        return zo_id
    vendor, name = zo_id.split("/", 1)
    parts = name.split("-")
    fam_parts = []
    for p in parts:
        # пока встречаем не-цифровой и не "version" сегмент — добавляем
        if any(ch.isdigit() for ch in p):
            break
        fam_parts.append(p)
    if not fam_parts:
        fam_parts = [parts[0]]
    return f"{vendor}/{'-'.join(fam_parts)}"


def _version_key(zo_id: str) -> tuple:
    """Ключ сортировки: набор всех чисел в id, по убыванию идёт «новее»."""
    nums = re.findall(r"\d+", zo_id)
    return tuple(int(n) for n in nums)


# Хардкод-фоллбеки на случай если /models/ не подгрузился (например пустой
# _AVAILABLE_IDS из-за сетевой ошибки): известные «следующие» версии
# раутятся на предыдущие что есть в Zo. Когда Zo добавит — это станет
# no-op.
HARDCODED_FALLBACKS: dict[str, str] = {
    "zo:anthropic/claude-opus-4-8":           "zo:anthropic/claude-opus-4-7",
    "zo:anthropic/claude-opus-4-8-thinking":  "zo:anthropic/claude-opus-4-7-thinking",
    "zo:anthropic/claude-sonnet-4-7":         "zo:anthropic/claude-sonnet-4-6",
    "zo:anthropic/claude-haiku-4-7":          "zo:anthropic/claude-haiku-4-6",
}


def _fallback_in_family(upstream: str) -> str:
    """Если upstream есть у Zo — возвращаем как есть.  Иначе ищем
    ближайшую модель той же семьи (например opus 4-8 -> opus 4-7),
    либо хоть что-то от того же vendor.  Если и этого нет —
    отдаём upstream как есть, пусть Zo сам бросит 4xx."""
    if upstream in HARDCODED_FALLBACKS:
        return HARDCODED_FALLBACKS[upstream]
    if not _AVAILABLE_IDS or upstream in _AVAILABLE_IDS:
        return upstream
    family = _model_family(upstream)
    same_family = [m for m in _AVAILABLE_IDS if _model_family(m) == family]
    if same_family:
        same_family.sort(key=_version_key, reverse=True)
        return same_family[0]
    if "/" in upstream:
        vendor_prefix = upstream.split("/", 1)[0] + "/"
        same_vendor = [m for m in _AVAILABLE_IDS if m.startswith(vendor_prefix)]
        if same_vendor:
            same_vendor.sort(key=_version_key, reverse=True)
            return same_vendor[0]
    return upstream


def _resolve_model(requested: str | None) -> str:
    forced = runtime.get_force_model().strip() if hasattr(runtime, "get_force_model") else ""
    name = (forced or (requested or "")).strip()
    if not name:
        return ZO_DEFAULT_MODEL
    if name.startswith("zo:"):
        return _fallback_in_family(name)

    # 1) Точные алиасы (короткие имена) — case-insensitive.
    low = name.lower()
    for needle, target in MODEL_MAP.items():
        if needle.lower() == low:
            return _fallback_in_family(target)

    # 2) Умная маршрутизация по префиксу.
    if name.startswith("claude"):
        return _fallback_in_family(f"zo:anthropic/{name}")
    if name.startswith("gpt-") or name.startswith("o1") or name.startswith("o3") or name.startswith("o4") or name.startswith("codex"):
        return _fallback_in_family(f"zo:openai/{name}")
    if name.startswith("gemini"):
        return _fallback_in_family(f"zo:google/{name}")
    if name.startswith("grok"):
        return _fallback_in_family(f"zo:xai/{name}")
    if name.startswith("deepseek"):
        return _fallback_in_family(f"zo:deepseek/{name}")
    if name.startswith("llama"):
        return _fallback_in_family(f"zo:meta/{name}")
    if name.startswith("qwen"):
        return _fallback_in_family(f"zo:alibaba/{name}")
    if name.startswith("kimi"):
        return _fallback_in_family(f"zo:moonshot/{name}")
    if name.startswith("glm"):
        return _fallback_in_family(f"zo:zai/{name}")
    if name.startswith("minimax"):
        return _fallback_in_family(f"zo:minimax/{name}")

    return _fallback_in_family(ZO_DEFAULT_MODEL)


async def _get_models_for(account: Account) -> list[dict[str, Any]]:
    now = time.monotonic()
    cached = _MODELS_CACHE.get(account.label)
    if cached and now - cached[0] < _MODELS_TTL:
        return cached[1]
    models = await ZO.list_models(account)
    _MODELS_CACHE[account.label] = (now, models)
    return models


# ---------------------------------------------------------------------------
# account selection / rotation
# ---------------------------------------------------------------------------


def _pick_account() -> Account | None:
    STORE.load()  # перечитать с диска (на случай если setup.py поменял)
    a = STORE.get_active()
    if a and a.is_usable():
        return a
    # fallback: первый usable
    for c in STORE.accounts:
        if c.is_usable():
            STORE.set_active(c.label)
            return c
    return None


def _rotate_on_error(account: Account, err: Exception) -> Account | None:
    """
    Регистрирует ошибку у account и, если streak >= MAX, переключается на
    следующего. Возвращает новый active (или тот же, если ротация не нужна).
    """
    should = STORE.mark_err(account.label, str(err), MAX_ERRORS_BEFORE_ROTATE)
    if not should:
        return account
    nxt = STORE.rotate_after_error(account.label)
    if nxt:
        log.warning(
            "Ротация: %s -> %s (после %d ошибок: %s)",
            account.label,
            nxt.label,
            account.error_streak,
            _humanize(str(err)),
        )
        return nxt
    log.warning("Ротация невозможна — других аккаунтов нет.")
    return None


def _force_rotate(account: Account, err: Exception) -> Account | None:
    """
    Принудительная ротация без ожидания streak — для фатальных ошибок
    конкретного аккаунта (401/403). Помечает аккаунт ошибкой и сразу
    переключает active.
    """
    STORE.mark_err(account.label, str(err), max_streak=1)  # для статистики
    nxt = STORE.rotate_after_error(account.label)
    if nxt:
        log.warning(
            "Force-ротация: %s -> %s (фатальная ошибка: %s)",
            account.label,
            nxt.label,
            _humanize(str(err)),
        )
        return nxt
    log.warning("Force-ротация невозможна — других аккаунтов нет.")
    return None


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await ZO.close()


app = FastAPI(title="zo-claude-proxy", lifespan=lifespan)

# /auth и /auth/save — браузерный фолбэк для добавления аккаунта когда
# Playwright/Patchright недоступен (Docker, headless-окружения и т.п.).
try:
    import auth_setup
    auth_setup.install(app, STORE, ZO)
except Exception as e:  # noqa: BLE001
    log.warning("auth_setup install failed: %s", e)


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
    return _error_for_path(request.url.path, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _val_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_for_path(request.url.path, 400, f"Bad request body: {exc.errors()[:3]}")


# ------------------------- health & admin -------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    a = _pick_account()
    return {
        "ok": a is not None,
        "active": a.label if a else None,
        "accounts": len(STORE.accounts),
        "usable": len(STORE.list_usable()),
        "default_model": ZO_DEFAULT_MODEL,
    }


@app.get("/v1/admin/accounts")
async def admin_accounts() -> dict[str, Any]:
    return {
        "active": STORE.active_label,
        "accounts": [
            {
                "label": a.label,
                "domain": a.domain,
                "email": a.email(),
                "ttl_seconds": a.seconds_until_expiry(),
                "error_streak": a.error_streak,
                "last_err": a.last_err,
                "disabled": a.disabled,
                "bridge_persona_id": a.bridge_persona_id,
                "balance_cents": a.balance_cents,
            }
            for a in STORE.accounts
        ],
    }


@app.post("/v1/admin/bootstrap")
async def admin_bootstrap() -> dict[str, Any]:
    """Принудительно прогоняет XML persona bootstrap + возвращает
    результаты по каждому аккаунту."""
    results: list[dict[str, Any]] = []
    for a in STORE.accounts:
        if not a.is_usable():
            results.append({"label": a.label, "ok": False, "reason": "not usable"})
            continue
        if a.is_api_key():
            results.append({"label": a.label, "ok": True, "reason": "zo_sk_ (no persona needed)"})
            continue
        try:
            pid = await ZO.ensure_xml_mode_active(a)
            try:
                active = await ZO.get_active_personas(a)
            except Exception:
                active = None
            results.append({
                "label": a.label,
                "ok": bool(pid),
                "bridge_persona_id": pid,
                "active_main": (active or {}).get("main") if isinstance(active, dict) else None,
                "api_key_id": a.api_key_id,
                "api_key_prefix": (a.api_key[:14] + "…") if a.api_key else None,
                "api_key_present": bool(a.api_key),
            })
        except Exception as e:  # noqa: BLE001
            results.append({"label": a.label, "ok": False, "error": str(e)})
    STORE.save()
    return {"results": results}


@app.post("/v1/admin/api-key/recreate")
async def admin_api_key_recreate(req: Request) -> dict[str, Any]:
    body: dict[str, Any] = {}
    try:
        body = await req.json()
    except Exception:
        pass
    label = (body or {}).get("label") or STORE.active_label
    acc = next((a for a in STORE.accounts if a.label == label), None)
    if not acc:
        raise HTTPException(status_code=404, detail=f"no account labeled {label}")
    import time as _t
    name = f"zoapi-bridge-{int(_t.time())}"
    try:
        created = await ZO.create_api_key(acc, name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"create_api_key failed: {e}")
    if not created or not created.get("key"):
        raise HTTPException(status_code=502, detail="create_api_key returned no key")
    acc.api_key = created.get("key")
    acc.api_key_id = created.get("id")
    STORE.save()
    return {
        "ok": True,
        "label": label,
        "api_key_id": acc.api_key_id,
        "api_key_prefix": (acc.api_key[:14] + "…") if acc.api_key else None,
        "name": name,
    }


@app.post("/v1/admin/active")
async def admin_set_active(req: Request) -> dict[str, Any]:
    body = await req.json()
    label = (body or {}).get("label")
    if not label or not STORE.set_active(label):
        raise HTTPException(status_code=400, detail=f"unknown label: {label}")
    return {"ok": True, "active": label}




@app.on_event("startup")
async def _startup_warm_models() -> None:
    import time as _time

    # --- models cache ---
    try:
        await _refresh_available_models(force=True)
    except Exception as e:  # noqa: BLE001
        log.warning("startup model refresh failed: %s", e)

    async def _models_loop() -> None:
        while True:
            await asyncio.sleep(_AVAILABLE_TTL)
            try:
                await _refresh_available_models(force=True)
            except Exception:
                pass

    asyncio.create_task(_models_loop())

    # --- XML-mode persona bootstrap (scopes=[] — серверные тулы Zo отключены) ---
    async def _xml_persona_bootstrap() -> None:
        for acc in STORE.accounts:
            if not acc.is_usable():
                continue
            try:
                pid = await ZO.ensure_xml_mode_active(acc)
                if pid:
                    acc.bridge_persona_id = pid
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] XML persona bootstrap failed: %s", acc.label, e)
        STORE.save()

    try:
        await _xml_persona_bootstrap()
    except Exception as e:  # noqa: BLE001
        log.warning("startup XML persona bootstrap failed: %s", e)

    async def _persona_loop() -> None:
        while True:
            await asyncio.sleep(120)
            try:
                await _xml_persona_bootstrap()
            except Exception as e:  # noqa: BLE001
                log.warning("XML persona periodic bootstrap failed: %s", e)

    asyncio.create_task(_persona_loop())

    # --- balance polling каждые 60s по всем usable-аккаунтам ---
    async def _balances_loop() -> None:
        while True:
            for acc in list(STORE.accounts):
                if not acc.is_usable():
                    continue
                try:
                    cents = await ZO.fetch_balance(acc)
                    if cents is not None:
                        acc.balance_cents = cents
                        acc.balance_checked_at = _time.time()
                except Exception:
                    pass
            try:
                STORE.save()
            except Exception:
                pass
            await asyncio.sleep(60)

    asyncio.create_task(_balances_loop())

# ------------------------- models -------------------------


ANTHROPIC_CATALOG: list[dict[str, Any]] = [
    {"id": "claude-opus-4-7", "display_name": "Claude Opus 4.7", "summary": "Most capable for complex work"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "summary": "Best for everyday tasks"},
]


def _catalog_entry(m: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": m["id"],
        "object": "model",
        "type": "model",
        "display_name": m.get("display_name") or m["id"],
        "created": 1704067200,
        "created_at": "2024-01-01T00:00:00Z",
        "owned_by": "anthropic",
    }
    if m.get("summary"):
        out["summary"] = m["summary"]
    return out


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    data = [_catalog_entry(m) for m in ANTHROPIC_CATALOG]
    return {
        "object": "list",
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
    }


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str) -> dict[str, Any]:
    for m in ANTHROPIC_CATALOG:
        if m["id"] == model_id:
            return _catalog_entry(m)
    raise HTTPException(status_code=404, detail=f"model not found: {model_id}")


# ------------------------- /v1/messages -------------------------


@app.post("/v1/messages")
async def messages(req: Request) -> Any:
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    model_req = body.get("model")
    zo_model = _resolve_model(model_req)
    msgs: list[dict[str, Any]] = body.get("messages") or []
    system = body.get("system")
    if isinstance(system, list):
        system = "\n".join(
            (b.get("text", "") if isinstance(b, dict) else str(b)) for b in system
        )
    tools = body.get("tools") or []
    stream = bool(body.get("stream", False))
    client_tool_names = _extract_client_tool_names(tools)

    # Anthropic extended-thinking: {"thinking": {"type": "enabled", "budget_tokens": N}}
    thinking_cfg = body.get("thinking") or {}
    enable_thinking = (
        isinstance(thinking_cfg, dict) and thinking_cfg.get("type") == "enabled"
    )

    if not msgs:
        raise HTTPException(status_code=400, detail="messages is empty")

    flat = _flatten_messages(system, msgs, tools)
    first_user = next(
        (_stringify_content(m.get("content")) for m in msgs if m.get("role") == "user"),
        "",
    )

    log.info(
        "POST /v1/messages: model=%s -> %s, msgs=%d, tools=%d, stream=%s",
        model_req,
        zo_model,
        len(msgs),
        len(tools),
        stream,
    )

    if stream:
        return StreamingResponse(
            _do_stream(flat, zo_model, system, first_user, model_req or "claude", client_tool_names, enable_thinking),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )

    return await _do_nonstream(flat, zo_model, system, first_user, model_req or "claude", client_tool_names)


@app.post("/v1/chat/completions")
async def chat_completions(req: Request) -> Any:
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    model_req = body.get("model")
    zo_model = _resolve_model(model_req)
    msgs: list[dict[str, Any]] = body.get("messages") or []
    tools = body.get("tools") or []
    stream = bool(body.get("stream", False))
    client_tool_names = _extract_client_tool_names(tools)

    instructions_parts: list[str] = []
    for m in msgs:
        if (m.get("role") or "").lower() in ("system", "developer"):
            instructions_parts.append(_stringify_openai_content(m.get("content")))
    instructions = "\n\n".join(p for p in instructions_parts if p.strip()) or None
    flat = _flatten_openai_messages(instructions, msgs, tools)
    first_user = next(
        (_stringify_openai_content(m.get("content")) for m in msgs if (m.get("role") or "").lower() == "user"),
        "",
    )

    log.info(
        "POST /v1/chat/completions: model=%s -> %s, msgs=%d, tools=%d, stream=%s",
        model_req,
        zo_model,
        len(msgs),
        len(tools),
        stream,
    )

    if stream:
        return StreamingResponse(
            _do_openai_chat_stream(flat, zo_model, instructions, first_user, model_req or "gpt-5", client_tool_names),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    return await _do_openai_chat_nonstream(flat, zo_model, instructions, first_user, model_req or "gpt-5", client_tool_names)


@app.post("/v1/responses")
async def responses_api(req: Request) -> Any:
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    model_req = body.get("model")
    zo_model = _resolve_model(model_req)
    instructions, msgs, tools = _responses_input_to_messages(body)
    flat = _flatten_openai_messages(instructions, msgs, tools)
    first_user = next(
        (_stringify_openai_content(m.get("content")) for m in msgs if (m.get("role") or "").lower() == "user"),
        "",
    )
    stream = bool(body.get("stream", False))
    client_tool_names = _extract_client_tool_names(tools)

    log.info(
        "POST /v1/responses: model=%s -> %s, msgs=%d, tools=%d, stream=%s",
        model_req,
        zo_model,
        len(msgs),
        len(tools),
        stream,
    )

    if stream:
        return StreamingResponse(
            _do_responses_stream(flat, zo_model, instructions, first_user, model_req or "gpt-5", client_tool_names),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    return await _do_responses_nonstream(flat, zo_model, instructions, first_user, model_req or "gpt-5", client_tool_names)


@app.websocket("/v1/responses")
async def responses_socket(ws: WebSocket) -> None:
    await ws.accept()
    while True:
        try:
            msg = await ws.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            await ws.send_json({
                "type": "error",
                "code": "400",
                "message": "Invalid websocket JSON payload",
                "param": None,
                "event_id": uuid.uuid4().hex,
            })
            continue

        if not isinstance(msg, dict):
            await ws.send_json({
                "type": "error",
                "code": "400",
                "message": "Websocket payload must be an object",
                "param": None,
                "event_id": uuid.uuid4().hex,
            })
            continue

        body = msg.get("response") if msg.get("type") == "response.create" and isinstance(msg.get("response"), dict) else msg
        try:
            await _do_responses_websocket(ws, body)
        except WebSocketDisconnect:
            return
        except Exception as e:
            await ws.send_json({
                "type": "error",
                "code": "500",
                "message": f"proxy error: {e}",
                "param": None,
                "event_id": uuid.uuid4().hex,
            })


async def _do_responses_websocket(
    ws: WebSocket,
    body: dict[str, Any],
) -> None:
    from openai_sse import ResponsesApiTranslator

    model_req = body.get("model")
    zo_model = _resolve_model(model_req)
    instructions, msgs, tools = _responses_input_to_messages(body)
    flat = _flatten_openai_messages(instructions, msgs, tools)
    first_user = next(
        (_stringify_openai_content(m.get("content")) for m in msgs if (m.get("role") or "").lower() == "user"),
        "",
    )
    client_tool_names = _extract_client_tool_names(tools)

    translator = ResponsesApiTranslator(model=model_req or "gpt-5", client_tool_names=client_tool_names)
    started = False
    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            for payload in translator.error_events(401, "No usable Zo account. Run setup.py."):
                await ws.send_json(payload)
            return
        if acc.label in attempts:
            for payload in translator.error_events(502, f"All accounts failed: {attempts}"):
                await ws.send_json(payload)
            return
        attempts.append(acc.label)
        convo_key = _convo_key(acc.label, instructions, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available
        try:
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if not started:
                    for payload in translator.start_events():
                        await ws.send_json(payload)
                    started = True
                if conv_header:
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                for payload in translator.feed_events(ev_name, data):
                    await ws.send_json(payload)
            for payload in translator.finish_events():
                await ws.send_json(payload)
            STORE.mark_ok(acc.label)
            return
        except ZoAuthError as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for payload in translator.error_events(401, f"Zo auth: {e}"):
                await ws.send_json(payload)
            return
        except ZoForbidden as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for payload in translator.error_events(403, f"Zo: {e}"):
                await ws.send_json(payload)
            return
        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            new = _rotate_on_error(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for payload in translator.error_events(502, f"Zo error: {e}"):
                await ws.send_json(payload)
            return
        except Exception as e:
            log.exception("[%s] responses websocket unexpected", acc.label)
            for payload in translator.error_events(500, f"proxy error: {e}"):
                await ws.send_json(payload)
            return


async def _do_openai_chat_stream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    openai_model_name: str,
    client_tool_names: list[str] | None = None,
) -> AsyncIterator[bytes]:
    from openai_sse import ChatCompletionsTranslator

    translator = ChatCompletionsTranslator(model=openai_model_name, client_tool_names=client_tool_names)
    started = False
    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            for chunk in translator.error(401, "No usable Zo account. Run setup.py."):
                yield chunk.encode("utf-8")
            return
        if acc.label in attempts:
            for chunk in translator.error(502, f"All accounts failed: {attempts}"):
                yield chunk.encode("utf-8")
            return
        attempts.append(acc.label)
        convo_key = _convo_key(acc.label, system, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available
        try:
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                for chunk in translator.feed(ev_name, data):
                    yield chunk.encode("utf-8")
            for chunk in translator.finish():
                yield chunk.encode("utf-8")
            STORE.mark_ok(acc.label)
            return
        except ZoAuthError as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(401, f"Zo auth: {e}"):
                yield chunk.encode("utf-8")
            return
        except ZoForbidden as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(403, f"Zo: {e}"):
                yield chunk.encode("utf-8")
            return
        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            new = _rotate_on_error(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(502, f"Zo error: {e}"):
                yield chunk.encode("utf-8")
            return
        except Exception as e:
            log.exception("[%s] openai stream unexpected", acc.label)
            for chunk in translator.error(500, f"proxy error: {e}"):
                yield chunk.encode("utf-8")
            return


async def _do_openai_chat_nonstream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    openai_model_name: str,
    client_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    from openai_sse import build_openai_nonstream

    text = await _collect_text_response(flat_input, zo_model, system, first_user, client_tool_names)
    return build_openai_nonstream(openai_model_name, text)


async def _do_responses_stream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    openai_model_name: str,
    client_tool_names: list[str] | None = None,
) -> AsyncIterator[bytes]:
    from openai_sse import ResponsesApiTranslator

    translator = ResponsesApiTranslator(model=openai_model_name, client_tool_names=client_tool_names)
    started = False
    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            for chunk in translator.error(401, "No usable Zo account. Run setup.py."):
                yield chunk.encode("utf-8")
            return
        if acc.label in attempts:
            for chunk in translator.error(502, f"All accounts failed: {attempts}"):
                yield chunk.encode("utf-8")
            return
        attempts.append(acc.label)
        convo_key = _convo_key(acc.label, system, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available
        try:
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                for chunk in translator.feed(ev_name, data):
                    yield chunk.encode("utf-8")
            for chunk in translator.finish():
                yield chunk.encode("utf-8")
            STORE.mark_ok(acc.label)
            return
        except ZoAuthError as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(401, f"Zo auth: {e}"):
                yield chunk.encode("utf-8")
            return
        except ZoForbidden as e:
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(403, f"Zo: {e}"):
                yield chunk.encode("utf-8")
            return
        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            new = _rotate_on_error(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(502, f"Zo error: {e}"):
                yield chunk.encode("utf-8")
            return
        except Exception as e:
            log.exception("[%s] responses stream unexpected", acc.label)
            for chunk in translator.error(500, f"proxy error: {e}"):
                yield chunk.encode("utf-8")
            return


async def _collect_text_response(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    client_tool_names: list[str] | None = None,
) -> str:
    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            raise HTTPException(status_code=401, detail="No usable Zo account. Run setup.py.")
        if acc.label in attempts:
            raise HTTPException(status_code=502, detail=f"All accounts failed: {attempts}")
        attempts.append(acc.label)
        convo_key = _convo_key(acc.label, system, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available
        try:
            text_acc: list[str] = []
            new_conv: str | None = None
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if conv_header and not new_conv:
                    new_conv = conv_header
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                if ev_name == "PartDeltaEvent":
                    delta = data.get("delta") or {}
                    if delta.get("part_delta_kind") in ("text", "thinking"):
                        text_acc.append(delta.get("content_delta") or "")
                elif ev_name == "PartStartEvent":
                    part = data.get("part") or {}
                    if part.get("part_kind") in ("text", "thinking"):
                        text_acc.append(part.get("content") or "")
            if new_conv:
                pass  # CONVO_CACHE disabled for multi-user isolation
            text = "".join(text_acc).strip() or "(empty response)"
            STORE.mark_ok(acc.label)
            return text
        except ZoAuthError as e:
            new = _force_rotate(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=401, detail=str(e))
        except ZoForbidden as e:
            new = _force_rotate(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=403, detail=str(e))
        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            new = _rotate_on_error(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=502, detail=str(e))


async def _do_responses_nonstream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    openai_model_name: str,
    client_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    from openai_sse import build_responses_nonstream

    text = await _collect_text_response(flat_input, zo_model, system, first_user, client_tool_names)
    return build_responses_nonstream(openai_model_name, text)


# ---------------------------------------------------------------------------
# streaming worker (с ротацией)
# ---------------------------------------------------------------------------


async def _do_stream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    anthropic_model_name: str,
    client_tool_names: list[str] | None = None,
    enable_thinking: bool = False,
) -> AsyncIterator[bytes]:
    translator = AnthropicStreamTranslator(model=anthropic_model_name, client_tool_names=client_tool_names, enable_thinking=enable_thinking)
    # message_start пошлём только когда успешно получим хоть один event от Zo,
    # чтобы при ошибке мы могли отдать чистый error без message_start.
    started = False

    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            for chunk in translator.error(401, "No usable Zo account. Run setup.py."):
                yield chunk.encode("utf-8")
            return
        if acc.label in attempts:
            for chunk in translator.error(502, f"All accounts failed: {attempts}"):
                yield chunk.encode("utf-8")
            return
        attempts.append(acc.label)

        convo_key = _convo_key(acc.label, system, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available

        try:
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                for chunk in translator.feed(ev_name, data):
                    yield chunk.encode("utf-8")

            for chunk in translator.finish():
                yield chunk.encode("utf-8")
            STORE.mark_ok(acc.label)
            return

        except ZoAuthError as e:
            log.warning("[%s] auth: %s — force-ротация", acc.label, e)
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(401, f"Zo auth: {e}"):
                yield chunk.encode("utf-8")
            return

        except ZoForbidden as e:
            log.warning("[%s] 403: %s — force-ротация", acc.label, e)
            new = _force_rotate(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(403, f"Zo: {e}"):
                yield chunk.encode("utf-8")
            return

        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            log.warning("[%s] zo error: %s", acc.label, e)
            new = _rotate_on_error(acc, e)
            if new and not started and new.label != acc.label:
                continue
            for chunk in translator.error(502, f"Zo error: {e}"):
                yield chunk.encode("utf-8")
            return

        except Exception as e:  # noqa: BLE001
            log.exception("[%s] unexpected", acc.label)
            for chunk in translator.error(500, f"proxy error: {e}"):
                yield chunk.encode("utf-8")
            return


# ---------------------------------------------------------------------------
# non-stream worker (с ротацией)
# ---------------------------------------------------------------------------


async def _do_nonstream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    anthropic_model_name: str,
    client_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Non-stream: тоже стримим, но собираем всё в один Anthropic-ответ.
    """
    attempts: list[str] = []
    while True:
        acc = _pick_account()
        if not acc:
            raise HTTPException(status_code=401, detail="No usable Zo account. Run setup.py.")
        if acc.label in attempts:
            raise HTTPException(status_code=502, detail=f"All accounts failed: {attempts}")
        attempts.append(acc.label)

        convo_key = _convo_key(acc.label, system, first_user)
        convo_id = _get_zo_convo_id(convo_key, acc.label)  # reuse zo conversation if available

        try:
            text_acc: list[str] = []
            new_conv: str | None = None
            # Ensure XML mode persona is active (idempotent + cached, skip for zo_sk_)
            if client_tool_names and not acc.is_api_key():
                try:
                    await ZO.ensure_xml_mode_active(acc)
                except Exception as _e:  # noqa: BLE001
                    log.warning("[%s] XML mode activation failed: %s", acc.label, _e)
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
                persona_id=_persona(acc),
            ):
                if conv_header and not new_conv:
                    new_conv = conv_header
                    _set_zo_convo_id(convo_key, acc.label, conv_header)
                if ev_name == "PartDeltaEvent":
                    delta = data.get("delta") or {}
                    dkind = delta.get("part_delta_kind")
                    if dkind in ("text", "thinking"):
                        text_acc.append(delta.get("content_delta") or "")
                elif ev_name == "PartStartEvent":
                    part = data.get("part") or {}
                    if part.get("part_kind") in ("text", "thinking"):
                        text_acc.append(part.get("content") or "")

            if new_conv:
                pass  # CONVO_CACHE disabled for multi-user isolation

            text = "".join(text_acc).strip()
            STORE.mark_ok(acc.label)

            # Парсим <zo:call> теги в настоящие tool_use блоки, иначе
            # Claude Code не исполнит тулы.
            from tool_parser import parse_full_text
            blocks = parse_full_text(text) if text else []
            content_blocks: list[dict[str, Any]] = []
            has_tool = False
            for b in blocks:
                if b.get("type") == "text":
                    txt = b.get("text", "").strip()
                    if txt:
                        content_blocks.append({"type": "text", "text": txt})
                elif b.get("type") == "tool_use":
                    has_tool = True
                    content_blocks.append({
                        "type": "tool_use",
                        "id": b.get("id") or ("toolu_" + uuid.uuid4().hex[:24]),
                        "name": b.get("name") or "unknown",
                        "input": b.get("input") or {},
                    })
            if not content_blocks:
                content_blocks = [{"type": "text", "text": text or "(empty response)"}]

            return {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message",
                "role": "assistant",
                "model": anthropic_model_name,
                "content": content_blocks,
                "stop_reason": "tool_use" if has_tool else "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": len(flat_input) // 4,
                    "output_tokens": max(1, len(text) // 4),
                },
            }

        except ZoAuthError as e:
            new = _force_rotate(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=401, detail=str(e))
        except ZoForbidden as e:
            new = _force_rotate(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=403, detail=str(e))
        except (ZoServerError, ZoBadRequest, httpx.HTTPError) as e:
            new = _rotate_on_error(acc, e)
            if new and new.label != acc.label:
                continue
            raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def _print_startup_banner() -> None:
    print()
    print("  ZoAPI proxy")
    print("  ==========")
    if not STORE.accounts:
        print()
        print("  No accounts yet.")
        print("  API will stay up and wait for an account.")
        print(f"  -> port: {PROXY_PORT}")
        print()
        return
    print()
    a = _pick_account()
    if a:
        print(f"  -> active: {a.label} ({a.email() or a.domain})")
    print(f"  -> port: {PROXY_PORT}")
    print(f"  -> default model: {ZO_DEFAULT_MODEL}")
    print(f"  -> max errors before rotate: {MAX_ERRORS_BEFORE_ROTATE}")
    print()


if __name__ == "__main__":
    import uvicorn

    _print_startup_banner()

    uvicorn.run(
        "proxy:app",
        host="127.0.0.1",
        port=PROXY_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
