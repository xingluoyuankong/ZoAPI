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
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

import config
from accounts import Account, AccountStore
from anthropic_sse import AnthropicStreamTranslator, sse
from zo_client import (
    ZoAuthError,
    ZoBadRequest,
    ZoClient,
    ZoForbidden,
    ZoServerError,
)

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
            # обычно не реэхим thinking назад
            pass
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
    if not requested:
        return ZO_DEFAULT_MODEL
    if requested.startswith("zo:"):
        return requested
    low = requested.lower()
    for needle, target in MODEL_MAP.items():
        if needle.lower() in low:
            return target
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

app = FastAPI(title="zo-claude-proxy")


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
    return _anthropic_err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _val_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _anthropic_err(400, f"Bad request body: {exc.errors()[:3]}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await ZO.close()


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


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    a = _pick_account()
    if not a:
        raise HTTPException(status_code=401, detail="No usable Zo account. Run setup.py.")
    try:
        models = await _get_models_for(a)
    except ZoAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Zo /models/available failed: {e}")

    data = []
    for m in models:
        zo_name = m.get("model_name")
        if not zo_name:
            continue
        # отрисуем имя в Anthropic-стиле, чтобы Claude Code не ругался
        anth_id = zo_name.split("/")[-1]
        data.append(
            {
                "id": anth_id,
                "type": "model",
                "display_name": m.get("label") or anth_id,
                "created_at": "2024-01-01T00:00:00Z",
                "zo_model_name": zo_name,
                "context_window": m.get("context_window"),
                "vendor": m.get("vendor"),
                "tier": m.get("type"),
            }
        )
    return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None}


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
    print("  zo-claude-proxy")
    print("  ===============")
    if not STORE.accounts:
        print()
        print("  Аккаунтов нет. Запусти setup.py и добавь первый аккаунт:")
        print("    python setup.py")
        print()
        return
    from setup import render_table
    print()
    print(render_table(STORE))
    a = _pick_account()
    if a:
        print(f"\n  → активный: {a.label} ({a.email() or a.domain})")
    print(f"  → порт: {PROXY_PORT}")
    print(f"  → дефолтная модель: {ZO_DEFAULT_MODEL}")
    print(f"  → max ошибок до ротации: {MAX_ERRORS_BEFORE_ROTATE}")
    print()


if __name__ == "__main__":
    import uvicorn

    _print_startup_banner()
    if not STORE.accounts:
        import sys
        # подсказка и автозапуск setup.py если запустили вручную
        try:
            if sys.stdin.isatty():
                print("Открыть мастер добавления аккаунта прямо сейчас? [Y/n]: ", end="", flush=True)
                ans = sys.stdin.readline().strip().lower()
                if ans in ("", "y", "yes", "д", "да"):
                    import setup as _setup
                    _setup.menu(STORE)
                    if not STORE.accounts:
                        print("Аккаунтов всё ещё нет — выхожу.")
                        sys.exit(1)
        except Exception:
            pass
        if not STORE.accounts:
            sys.exit(1)

    uvicorn.run(
        "proxy:app",
        host="127.0.0.1",
        port=PROXY_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
