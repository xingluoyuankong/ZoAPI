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
            args = block.get("input", {})
            parts.append(f"\n<tool_use name=\"{name}\">{json.dumps(args, ensure_ascii=False)}</tool_use>\n")
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
            parts.append(f"\n<tool_result id=\"{tid}\">{text}</tool_result>\n")
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
            args = block.get("arguments") or block.get("input") or {}
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            parts.append(f"\n<tool_use name=\"{name}\">{args}</tool_use>\n")
        elif t in ("tool_result", "function_call_output"):
            call_id = block.get("call_id") or block.get("tool_call_id") or "?"
            output = block.get("output") or block.get("content") or ""
            if isinstance(output, list):
                output = "\n".join(_stringify_openai_content([item]) for item in output)
            parts.append(f"\n<tool_result id=\"{call_id}\">{output}</tool_result>\n")
        else:
            parts.append(str(block))
    return "".join(parts)


def _flatten_messages(
    system: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> str:
    """
    Собирает Anthropic messages в один текст для отправки в Zo /ask.
    Включает system + историю + список доступных тулов.
    """
    chunks: list[str] = []

    if system:
        chunks.append("=== SYSTEM ===")
        chunks.append(system.strip())
        chunks.append("")

    if tools:
        chunks.append("=== AVAILABLE CLIENT TOOLS ===")
        chunks.append(
            "У клиента (Claude Code) есть локальные инструменты. "
            "Когда тебе нужно их вызвать, отвечай ТЕКСТОМ с инструкцией — "
            "клиент сам решит, какой тул использовать. НЕ пытайся выполнять "
            "файловые/системные операции у себя на сервере — это машина клиента."
        )
        for t in tools[:50]:
            name = t.get("name", "?")
            desc = (t.get("description") or "").strip().split("\n")[0][:160]
            chunks.append(f"- {name}: {desc}")
        chunks.append("")

    # сообщения
    for m in messages:
        role = m.get("role", "user")
        text = _stringify_content(m.get("content"))
        if not text.strip():
            continue
        chunks.append(f"=== {role.upper()} ===")
        chunks.append(text.strip())
        chunks.append("")

    return "\n".join(chunks).strip()


def _flatten_openai_messages(
    instructions: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> str:
    chunks: list[str] = []
    if instructions:
        chunks.append("=== SYSTEM ===")
        chunks.append(instructions.strip())
        chunks.append("")
    if tools:
        chunks.append("=== AVAILABLE CLIENT TOOLS ===")
        chunks.append(
            "У клиента есть локальные инструменты. Если нужен вызов инструмента, "
            "опиши его текстом и не пытайся выполнять локальные действия на сервере прокси."
        )
        for t in tools[:50]:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or t.get("function", {}).get("name") or "?"
            desc = (t.get("description") or t.get("function", {}).get("description") or "").strip().split("\n")[0][:160]
            chunks.append(f"- {name}: {desc}")
        chunks.append("")
    for m in messages:
        role = (m.get("role") or "user").lower()
        if role == "developer":
            role = "system"
        text = _stringify_openai_content(m.get("content"))
        if not text.strip():
            continue
        chunks.append(f"=== {role.upper()} ===")
        chunks.append(text.strip())
        chunks.append("")
    return "\n".join(chunks).strip()


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


def _resolve_model(requested: str | None) -> str:
    forced = runtime.get_force_model().strip() if hasattr(runtime, "get_force_model") else ""
    name = (forced or (requested or "")).strip()
    if not name:
        return ZO_DEFAULT_MODEL
    if name.startswith("zo:"):
        return name

    # 1) Точные алиасы (короткие имена) — case-insensitive.
    low = name.lower()
    for needle, target in MODEL_MAP.items():
        if needle.lower() == low:
            return target

    # 2) Умная маршрутизация по префиксу.
    if name.startswith("claude"):
        return f"zo:anthropic/{name}"
    if name.startswith("gpt-") or name.startswith("o1") or name.startswith("o3") or name.startswith("o4") or name.startswith("codex"):
        return f"zo:openai/{name}"
    if name.startswith("gemini"):
        return f"zo:google/{name}"
    if name.startswith("grok"):
        return f"zo:xai/{name}"
    if name.startswith("deepseek"):
        return f"zo:deepseek/{name}"
    if name.startswith("llama"):
        return f"zo:meta/{name}"
    if name.startswith("qwen"):
        return f"zo:alibaba/{name}"
    if name.startswith("kimi"):
        return f"zo:moonshot/{name}"

    return ZO_DEFAULT_MODEL


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
            }
            for a in STORE.accounts
        ],
    }


@app.post("/v1/admin/active")
async def admin_set_active(req: Request) -> dict[str, Any]:
    body = await req.json()
    label = (body or {}).get("label")
    if not label or not STORE.set_active(label):
        raise HTTPException(status_code=400, detail=f"unknown label: {label}")
    return {"ok": True, "active": label}


# ------------------------- models -------------------------


ANTHROPIC_CATALOG: list[dict[str, Any]] = [
    {"id": "claude-opus-4-7", "display_name": "Claude Opus 4.7", "summary": "Most capable for complex work"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "summary": "Best for everyday tasks"},
    {"id": "claude-haiku-4-6", "display_name": "Claude Haiku 4.6", "summary": "Fastest for quick answers"},
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
            _do_stream(flat, zo_model, system, first_user, model_req or "claude"),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )

    return await _do_nonstream(flat, zo_model, system, first_user, model_req or "claude")


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
            _do_openai_chat_stream(flat, zo_model, instructions, first_user, model_req or "gpt-5"),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    return await _do_openai_chat_nonstream(flat, zo_model, instructions, first_user, model_req or "gpt-5")


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
            _do_responses_stream(flat, zo_model, instructions, first_user, model_req or "gpt-5"),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )
    return await _do_responses_nonstream(flat, zo_model, instructions, first_user, model_req or "gpt-5")


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

    translator = ResponsesApiTranslator(model=model_req or "gpt-5")
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
        convo_id = CONVO_CACHE.get(convo_key)
        try:
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if not started:
                    for payload in translator.start_events():
                        await ws.send_json(payload)
                    started = True
                if conv_header:
                    CONVO_CACHE[convo_key] = conv_header
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
) -> AsyncIterator[bytes]:
    from openai_sse import ChatCompletionsTranslator

    translator = ChatCompletionsTranslator(model=openai_model_name)
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
        convo_id = CONVO_CACHE.get(convo_key)
        try:
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    CONVO_CACHE[convo_key] = conv_header
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
) -> dict[str, Any]:
    from openai_sse import build_openai_nonstream

    text = await _collect_text_response(flat_input, zo_model, system, first_user)
    return build_openai_nonstream(openai_model_name, text)


async def _do_responses_stream(
    flat_input: str,
    zo_model: str,
    system: str | None,
    first_user: str,
    openai_model_name: str,
) -> AsyncIterator[bytes]:
    from openai_sse import ResponsesApiTranslator

    translator = ResponsesApiTranslator(model=openai_model_name)
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
        convo_id = CONVO_CACHE.get(convo_key)
        try:
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    CONVO_CACHE[convo_key] = conv_header
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
        convo_id = CONVO_CACHE.get(convo_key)
        try:
            text_acc: list[str] = []
            new_conv: str | None = None
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if conv_header and not new_conv:
                    new_conv = conv_header
                if ev_name == "PartDeltaEvent":
                    delta = data.get("delta") or {}
                    if delta.get("part_delta_kind") in ("text", "thinking"):
                        text_acc.append(delta.get("content_delta") or "")
                elif ev_name == "PartStartEvent":
                    part = data.get("part") or {}
                    if part.get("part_kind") in ("text", "thinking"):
                        text_acc.append(part.get("content") or "")
            if new_conv:
                CONVO_CACHE[convo_key] = new_conv
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
) -> dict[str, Any]:
    from openai_sse import build_responses_nonstream

    text = await _collect_text_response(flat_input, zo_model, system, first_user)
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
) -> AsyncIterator[bytes]:
    translator = AnthropicStreamTranslator(model=anthropic_model_name)
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
        convo_id = CONVO_CACHE.get(convo_key)

        try:
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if not started:
                    for chunk in translator.start():
                        yield chunk.encode("utf-8")
                    started = True
                if conv_header:
                    CONVO_CACHE[convo_key] = conv_header
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
        convo_id = CONVO_CACHE.get(convo_key)

        try:
            text_acc: list[str] = []
            new_conv: str | None = None
            async for ev_name, data, conv_header in ZO.ask_stream(
                acc,
                q=flat_input,
                model_name=zo_model,
                conversation_id=convo_id,
                expanded_paths=EXPANDED_PATHS,
            ):
                if conv_header and not new_conv:
                    new_conv = conv_header
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
                CONVO_CACHE[convo_key] = new_conv

            text = "".join(text_acc).strip() or "(empty response)"
            STORE.mark_ok(acc.label)

            return {
                "id": "msg_" + uuid.uuid4().hex[:24],
                "type": "message",
                "role": "assistant",
                "model": anthropic_model_name,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": len(flat_input) // 4,
                    "output_tokens": len(text) // 4,
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
