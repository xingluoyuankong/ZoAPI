"""
zo-claude-proxy
===============

Локальный прокси, который превращает Zo Computer (`/zo/ask`) в Anthropic-
совместимый эндпоинт `/v1/messages`. Позволяет натравить Claude Code CLI
на свой Zo (с моделями Zo, твоими кредитами Zo) вместо подписки Anthropic.

Запуск:
    python proxy.py
    # затем в другом терминале:
    ANTHROPIC_BASE_URL=http://127.0.0.1:17878 \
    ANTHROPIC_AUTH_TOKEN=любая-строка \
    ANTHROPIC_API_KEY="" \
    claude
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

load_dotenv()

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

ZO_API_KEY = os.environ.get("ZO_API_KEY", "").strip()
ZO_BASE_URL = os.environ.get("ZO_BASE_URL", "https://api.zo.computer").rstrip("/")
ZO_DEFAULT_MODEL = os.environ.get("ZO_DEFAULT_MODEL", "anthropic:claude-opus-4-7")
ZO_PERSONA_ID = os.environ.get("ZO_PERSONA_ID", "").strip() or None
PROXY_PORT = int(os.environ.get("PROXY_PORT", "17878"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

try:
    MODEL_MAP: dict[str, str] = json.loads(os.environ.get("MODEL_MAP", "{}"))
except json.JSONDecodeError:
    MODEL_MAP = {}

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("zo-proxy")

if not ZO_API_KEY:
    log.warning("ZO_API_KEY не задан. Скопируй .env.example → .env и пропиши ключ.")


# ---------------------------------------------------------------------------
# маркеры для tool_use (что мы просим Zo эмитить в тексте)
# ---------------------------------------------------------------------------

TOOL_OPEN = "<<<TOOL_USE>>>"
TOOL_CLOSE = "<<<END_TOOL_USE>>>"
TOOL_REGEX = re.compile(
    re.escape(TOOL_OPEN) + r"(.*?)" + re.escape(TOOL_CLOSE),
    flags=re.DOTALL,
)


def build_proxy_system_prompt(tools: list[dict[str, Any]]) -> str:
    """
    Системный промпт, который мы вшиваем в начало каждого запроса к Zo.
    Он превращает Zo (агента со своими тулами) в "чистого" Claude-style LLM,
    который умеет звать тулы клиента через специальные маркеры.
    """
    base = (
        "Ты выступаешь в роли LLM-движка для Claude Code CLI у пользователя на локальной "
        "машине. К тебе подключён прокси, который маршрутит твои ответы в Claude Code.\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "1. НЕ используй НИКАКИЕ свои серверные тулы Zo (Read/Edit/Run command/Web search и т.д.). "
        "   Файлы и команды пользователя НЕ у тебя — они у Claude Code локально.\n"
        "2. Если тебе нужно прочитать/изменить файл, запустить команду, поискать в коде — "
        "   вызывай ТУЛЫ КЛИЕНТА (список ниже) через маркеры в своём ответе:\n"
        f"     {TOOL_OPEN}{{\"name\": \"ToolName\", \"input\": {{...аргументы...}}}}{TOOL_CLOSE}\n"
        "3. После маркера ОСТАНОВИСЬ. Клиент исполнит тул и пришлёт результат "
        "   следующим сообщением (как tool_result).\n"
        "4. Можно эмитить несколько маркеров подряд — они исполнятся параллельно.\n"
        "5. Обычный текст вне маркеров пользователь видит как твой ответ.\n"
        "6. Никогда не оборачивай маркеры в ``` или другой код-блок.\n"
        "7. JSON внутри маркера — валидный, без комментариев, без trailing-запятых.\n"
    )
    if tools:
        tools_brief = json.dumps(tools, ensure_ascii=False, indent=2)
        base += "\nДОСТУПНЫЕ ТУЛЫ КЛИЕНТА:\n" + tools_brief + "\n"
    else:
        base += "\nКЛИЕНТ НЕ ПРЕДОСТАВИЛ ТУЛОВ. Отвечай обычным текстом.\n"
    return base


# ---------------------------------------------------------------------------
# конвертация Anthropic messages -> один Zo input
# ---------------------------------------------------------------------------

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
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            parts.append(
                f"{TOOL_OPEN}"
                + json.dumps(
                    {
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    },
                    ensure_ascii=False,
                )
                + f"{TOOL_CLOSE}"
            )
        elif btype == "tool_result":
            tool_id = block.get("tool_use_id", "")
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in inner
                )
            is_err = block.get("is_error", False)
            tag = "TOOL_ERROR" if is_err else "TOOL_RESULT"
            parts.append(f"<<<{tag} id={tool_id}>>>\n{inner}\n<<<END_{tag}>>>")
        elif btype == "image":
            parts.append("[image attachment elided]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def anthropic_messages_to_zo_input(
    system: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[str, str]:
    """
    Превращает Anthropic-формат (system + messages[]) в одну текстовую input для /zo/ask.
    Возвращает (full_input_for_zo, conversation_key_seed).

    conversation_key_seed используется как ключ кэша conversation_id — чтобы
    в рамках одной "сессии" Claude Code продолжать тот же тред Zo.
    """
    proxy_sys = build_proxy_system_prompt(tools or [])

    # Anthropic system может быть строкой или списком text-блоков
    if isinstance(system, list):
        user_sys = "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in system
        )
    else:
        user_sys = system or ""

    transcript_lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = _stringify_content(m.get("content", ""))
        if not content:
            continue
        if role == "user":
            transcript_lines.append(f"### USER\n{content}")
        elif role == "assistant":
            transcript_lines.append(f"### ASSISTANT\n{content}")
        else:
            transcript_lines.append(f"### {role.upper()}\n{content}")

    full_input = (
        "[SYSTEM — PROXY DIRECTIVE]\n"
        + proxy_sys
        + "\n\n[SYSTEM — USER]\n"
        + user_sys
        + "\n\n[CONVERSATION SO FAR]\n"
        + "\n\n".join(transcript_lines)
        + "\n\n### ASSISTANT\n"
    )

    # ключ для conversation_id: только system + первое user-сообщение (стабильный)
    seed_src = user_sys + "||" + (
        _stringify_content(messages[0]["content"]) if messages else ""
    )
    seed = hashlib.sha256(seed_src.encode("utf-8")).hexdigest()[:32]
    return full_input, seed


# ---------------------------------------------------------------------------
# выбор модели
# ---------------------------------------------------------------------------

def resolve_model(requested: str | None) -> str:
    if not requested:
        return ZO_DEFAULT_MODEL
    # точное совпадение по словарю MODEL_MAP
    if requested in MODEL_MAP:
        return MODEL_MAP[requested]
    # подстрочный поиск (claude-sonnet-4-5 → ищем 'sonnet')
    low = requested.lower()
    for needle, target in MODEL_MAP.items():
        if needle.lower() in low:
            return target
    # если уже выглядит как Zo-модель — пропускаем как есть
    if requested.startswith(("anthropic:", "openai:", "google:", "byok:", "groq:", "cerebras:")):
        return requested
    return ZO_DEFAULT_MODEL


# ---------------------------------------------------------------------------
# кэш conversation_id (живёт пока процесс жив)
# ---------------------------------------------------------------------------

CONVO_CACHE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Zo client
# ---------------------------------------------------------------------------

class ZoClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=ZO_BASE_URL,
            headers={
                "Authorization": f"Bearer {ZO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> dict[str, Any]:
        r = await self._client.get("/models/available")
        r.raise_for_status()
        return r.json()

    async def ask(
        self,
        input_text: str,
        *,
        conversation_id: str | None,
        model_name: str | None,
        persona_id: str | None,
        stream: bool,
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "input": input_text,
            "stream": stream,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        if model_name:
            body["model_name"] = model_name
        if persona_id:
            body["persona_id"] = persona_id
        return await self._client.post(
            "/zo/ask",
            json=body,
            headers={"Accept": "text/event-stream" if stream else "application/json"},
        )

    async def stream_ask(
        self,
        input_text: str,
        *,
        conversation_id: str | None,
        model_name: str | None,
        persona_id: str | None,
    ) -> AsyncIterator[tuple[str, dict[str, Any], str | None]]:
        """
        Async generator: yields (event_type, data_dict, conversation_id_header).
        conversation_id_header возвращается только на первом yield.
        """
        body: dict[str, Any] = {"input": input_text, "stream": True}
        if conversation_id:
            body["conversation_id"] = conversation_id
        if model_name:
            body["model_name"] = model_name
        if persona_id:
            body["persona_id"] = persona_id

        async with self._client.stream(
            "POST",
            "/zo/ask",
            json=body,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Zo error: {err_body}",
                )
            conv_header = resp.headers.get("x-conversation-id")
            first = True

            event_type: str | None = None
            data_buf: list[str] = []

            async for line in resp.aiter_lines():
                if not line:
                    # пустая строка — конец SSE-сообщения
                    if event_type and data_buf:
                        raw = "\n".join(data_buf)
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {"raw": raw}
                        yield event_type, data, conv_header if first else None
                        first = False
                    event_type = None
                    data_buf = []
                    continue
                if line.startswith("event: "):
                    event_type = line[7:].strip()
                elif line.startswith("data: "):
                    data_buf.append(line[6:])
                # игнорим id:/retry:/комментарии


ZO: ZoClient | None = None


# ---------------------------------------------------------------------------
# tool-marker parser (стримовый)
# ---------------------------------------------------------------------------

class StreamParser:
    """
    Принимает поток текста от Zo, разбивает на (text_chunk | tool_call).
    Буферизует, чтобы маркер не разорвался между чанками.
    """

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        """
        Возвращает список событий:
          {"kind": "text", "text": "..."}
          {"kind": "tool", "tool": {...}}
        """
        self.buf += chunk
        out: list[dict[str, Any]] = []
        while True:
            open_idx = self.buf.find(TOOL_OPEN)
            if open_idx == -1:
                # нет открытия — почти всё можно отдать как text,
                # но оставим хвост на случай если открытие "в процессе"
                safe_cut = max(0, len(self.buf) - (len(TOOL_OPEN) - 1))
                if safe_cut > 0:
                    out.append({"kind": "text", "text": self.buf[:safe_cut]})
                    self.buf = self.buf[safe_cut:]
                return out

            # есть открытие — текст до него можно слить
            if open_idx > 0:
                out.append({"kind": "text", "text": self.buf[:open_idx]})
                self.buf = self.buf[open_idx:]

            # теперь buf начинается с TOOL_OPEN — ищем закрытие
            close_idx = self.buf.find(TOOL_CLOSE)
            if close_idx == -1:
                # ждём ещё данных
                return out

            payload = self.buf[len(TOOL_OPEN):close_idx]
            self.buf = self.buf[close_idx + len(TOOL_CLOSE):]
            try:
                tool = json.loads(payload)
                out.append({"kind": "tool", "tool": tool})
            except json.JSONDecodeError as e:
                log.warning("Сломанный tool JSON: %s | payload=%r", e, payload)
                # отдаём как обычный текст — пусть Claude Code увидит и ругнётся
                out.append(
                    {"kind": "text", "text": TOOL_OPEN + payload + TOOL_CLOSE}
                )

    def flush(self) -> list[dict[str, Any]]:
        if self.buf:
            tail = self.buf
            self.buf = ""
            return [{"kind": "text", "text": tail}]
        return []


# ---------------------------------------------------------------------------
# Anthropic SSE writer
# ---------------------------------------------------------------------------

def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="zo-claude-proxy", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    global ZO
    ZO = ZoClient()
    log.info("zo-claude-proxy слушает http://127.0.0.1:%d", PROXY_PORT)
    log.info("Дефолтная модель: %s", ZO_DEFAULT_MODEL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if ZO:
        await ZO.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "zo_base": ZO_BASE_URL, "default_model": ZO_DEFAULT_MODEL}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    assert ZO
    try:
        zo_models = await ZO.list_models()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Zo /models/available недоступен: {e}")
    data = []
    for m in zo_models.get("models", []):
        data.append(
            {
                "id": m.get("model_name"),
                "type": "model",
                "display_name": m.get("label"),
                "created_at": "2025-01-01T00:00:00Z",
            }
        )
    return {"data": data, "has_more": False, "first_id": None, "last_id": None}


@app.post("/v1/messages")
async def messages(req: Request) -> Any:
    assert ZO
    try:
        body = await req.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad json: {e}")

    model = body.get("model")
    msgs = body.get("messages", [])
    system = body.get("system", "")
    tools = body.get("tools", [])
    stream_flag = bool(body.get("stream", False))
    max_tokens = int(body.get("max_tokens", 4096))

    zo_model = resolve_model(model)
    zo_input, convo_seed = anthropic_messages_to_zo_input(system, msgs, tools)
    convo_id = CONVO_CACHE.get(convo_seed)

    log.info(
        "POST /v1/messages: model=%s -> %s, msgs=%d, tools=%d, stream=%s, convo=%s",
        model, zo_model, len(msgs), len(tools or []), stream_flag,
        convo_id[:8] + "…" if convo_id else "new",
    )

    if not stream_flag:
        return await _handle_nonstream(zo_input, zo_model, convo_seed, convo_id, max_tokens)
    return StreamingResponse(
        _handle_stream(zo_input, zo_model, convo_seed, convo_id, max_tokens),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# non-stream
# ---------------------------------------------------------------------------

async def _handle_nonstream(
    zo_input: str,
    zo_model: str,
    convo_seed: str,
    convo_id: str | None,
    max_tokens: int,
) -> JSONResponse:
    assert ZO
    resp = await ZO.ask(
        zo_input,
        conversation_id=convo_id,
        model_name=zo_model,
        persona_id=ZO_PERSONA_ID,
        stream=False,
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Zo: {resp.text}")
    data = resp.json()
    new_conv = data.get("conversation_id")
    if new_conv:
        CONVO_CACHE[convo_seed] = new_conv

    raw_text = data.get("output", "")
    if isinstance(raw_text, dict):
        raw_text = json.dumps(raw_text, ensure_ascii=False)

    # парсим маркеры → content blocks
    parser = StreamParser()
    events = parser.feed(raw_text) + parser.flush()

    content_blocks: list[dict[str, Any]] = []
    cur_text = ""
    stop_reason = "end_turn"
    for ev in events:
        if ev["kind"] == "text":
            cur_text += ev["text"]
        else:
            if cur_text.strip():
                content_blocks.append({"type": "text", "text": cur_text})
                cur_text = ""
            t = ev["tool"]
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": t.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": t.get("name", "unknown"),
                    "input": t.get("input", {}),
                }
            )
            stop_reason = "tool_use"
    if cur_text.strip() and stop_reason != "tool_use":
        content_blocks.append({"type": "text", "text": cur_text})
    elif cur_text.strip() and stop_reason == "tool_use":
        # текст после последнего tool_use редко осмыслен в Anthropic protocol;
        # вкладываем его перед tool_use если можно
        content_blocks.insert(0, {"type": "text", "text": cur_text})

    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    return JSONResponse(
        {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": zo_model,
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": len(raw_text) // 4,  # грубая оценка
            },
        }
    )


# ---------------------------------------------------------------------------
# stream (главная сложная часть)
# ---------------------------------------------------------------------------

async def _handle_stream(
    zo_input: str,
    zo_model: str,
    convo_seed: str,
    convo_id: str | None,
    max_tokens: int,
) -> AsyncIterator[bytes]:
    assert ZO
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    # 1. message_start
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": zo_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    parser = StreamParser()

    # состояние блоков
    cur_block_idx = -1                          # текущий открытый блок
    cur_block_kind: str | None = None           # "text" | "tool_use"
    text_buf_for_keepalive = ""                 # для ping'ов (не обязательно)
    output_tokens_est = 0
    stop_reason = "end_turn"
    saw_anything = False
    last_ping = time.time()

    async def open_text_block() -> bytes:
        nonlocal cur_block_idx, cur_block_kind
        cur_block_idx += 1
        cur_block_kind = "text"
        return sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": cur_block_idx,
                "content_block": {"type": "text", "text": ""},
            },
        )

    async def close_block() -> bytes | None:
        nonlocal cur_block_kind
        if cur_block_kind is None:
            return None
        out = sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": cur_block_idx},
        )
        cur_block_kind = None
        return out

    try:
        async for ev_type, ev_data, conv_header in ZO.stream_ask(
            zo_input,
            conversation_id=convo_id,
            model_name=zo_model,
            persona_id=ZO_PERSONA_ID,
        ):
            if conv_header:
                CONVO_CACHE[convo_seed] = conv_header

            if ev_type == "Error":
                # пишем как текст и закрываем
                err_msg = ev_data.get("message", "unknown Zo error")
                if cur_block_kind != "text":
                    closed = await close_block()
                    if closed:
                        yield closed
                    yield await open_text_block()
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": cur_block_idx,
                        "delta": {
                            "type": "text_delta",
                            "text": f"\n\n[zo error] {err_msg}",
                        },
                    },
                )
                stop_reason = "end_turn"
                break

            if ev_type == "End":
                if "output" in ev_data:
                    # structured output — отдадим как текст
                    obj = ev_data["output"]
                    text = obj if isinstance(obj, str) else json.dumps(
                        obj, ensure_ascii=False
                    )
                    if cur_block_kind != "text":
                        closed = await close_block()
                        if closed:
                            yield closed
                        yield await open_text_block()
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": cur_block_idx,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                break

            if ev_type != "FrontendModelResponse":
                continue

            chunk = ev_data.get("content", "")
            if not chunk:
                continue
            saw_anything = True
            output_tokens_est += max(1, len(chunk) // 4)

            for piece in parser.feed(chunk):
                if piece["kind"] == "text":
                    text = piece["text"]
                    if not text:
                        continue
                    if cur_block_kind != "text":
                        closed = await close_block()
                        if closed:
                            yield closed
                        yield await open_text_block()
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": cur_block_idx,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                else:
                    tool = piece["tool"]
                    tool_id = tool.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                    tool_name = tool.get("name", "unknown")
                    tool_input = tool.get("input", {})

                    closed = await close_block()
                    if closed:
                        yield closed
                    cur_block_idx += 1
                    cur_block_kind = "tool_use"
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": cur_block_idx,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": {},
                            },
                        },
                    )
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": cur_block_idx,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps(
                                    tool_input, ensure_ascii=False
                                ),
                            },
                        },
                    )
                    yield sse(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": cur_block_idx},
                    )
                    cur_block_kind = None
                    stop_reason = "tool_use"

            # пинг каждые 15с чтобы Claude Code не закрыл соединение
            if time.time() - last_ping > 15:
                yield b": ping\n\n"
                last_ping = time.time()

        # flush остатков парсера
        for piece in parser.flush():
            if piece["kind"] == "text" and piece["text"]:
                if cur_block_kind != "text":
                    closed = await close_block()
                    if closed:
                        yield closed
                    yield await open_text_block()
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": cur_block_idx,
                        "delta": {"type": "text_delta", "text": piece["text"]},
                    },
                )

        # закрыть последний блок если открыт
        closed = await close_block()
        if closed:
            yield closed

        if not saw_anything:
            # пустой ответ — пусть Claude Code получит хотя бы пустой текст-блок
            yield await open_text_block()
            yield sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": cur_block_idx},
            )

        # message_delta + message_stop
        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens_est},
            },
        )
        yield sse("message_stop", {"type": "message_stop"})

    except HTTPException as e:
        yield sse(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": str(e.detail)}},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("stream failed")
        yield sse(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": repr(e)}},
        )


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "proxy:app",
        host="127.0.0.1",
        port=PROXY_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
