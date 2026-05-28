"""
Конвертер Zo /ask SSE → OpenAI Chat Completions + Responses API SSE.

Для клиентов: OpenCode (через @ai-sdk/openai-compatible), Codex CLI,
aider, и любой другой OpenAI-compatible инструмент.

Поддерживает теги тулов `<zo:call name="..." id="...">{...}</zo:call>`
из текста модели и конвертирует их в НАСТОЯЩИЕ `tool_calls`
(Chat Completions) / `function_call` items (Responses API), чтобы клиент
исполнил тул локально, а не показал XML-сырьё пользователю.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Generator

from tool_parser import ToolCallTagParser


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sse_line(event: str | None, data: Any) -> str:
    raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {raw}\n\n"
    return f"data: {raw}\n\n"


# ---------------------------------------------------------------------------
# OpenAI Chat Completions translator
# ---------------------------------------------------------------------------

class ChatCompletionsTranslator:
    """
    Принимает Zo SSE-события (PartStartEvent/PartDeltaEvent/PartEndEvent)
    и выдаёт строки в формате OpenAI Chat Completions SSE.

    Если модель эмитит `<zo:call ...>{...}</zo:call>` — конвертируется в
    `tool_calls` дельты + finish_reason="tool_calls".
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self._created = int(time.time())
        self._started = False
        self._parser = ToolCallTagParser()
        # state для tool_calls
        self._cur_tool_index: int = -1   # порядковый номер tool_call в этом ответе
        self._in_tool: bool = False
        self._emitted_tool: bool = False

    # --- helpers, шлющие сырые chunks ---

    def _chunk_text(self, content: str) -> str:
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
                "logprobs": None,
            }],
        }
        return _sse_line(None, payload)

    def _chunk_tool_open(self, tool_index: int, call_id: str, name: str) -> str:
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": tool_index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }],
                },
                "finish_reason": None,
                "logprobs": None,
            }],
        }
        return _sse_line(None, payload)

    def _chunk_tool_args(self, tool_index: int, partial: str) -> str:
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": tool_index,
                        "function": {"arguments": partial},
                    }],
                },
                "finish_reason": None,
                "logprobs": None,
            }],
        }
        return _sse_line(None, payload)

    def _chunk_finish(self, reason: str) -> str:
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": reason,
                "logprobs": None,
            }],
        }
        return _sse_line(None, payload)

    # --- public ---

    def start(self) -> Generator[str, None, None]:
        """Начальный chunk с role."""
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }],
        }
        self._started = True
        yield _sse_line(None, payload)

    def _consume_parser(self, events) -> Generator[str, None, None]:
        for kind, payload in events:
            if kind == "text":
                if payload:
                    yield self._chunk_text(payload)
            elif kind == "tool_open":
                self._cur_tool_index += 1
                self._in_tool = True
                self._emitted_tool = True
                yield self._chunk_tool_open(self._cur_tool_index, payload["id"], payload["name"])
            elif kind == "tool_args":
                if payload:
                    yield self._chunk_tool_args(self._cur_tool_index, payload)
            elif kind == "tool_close":
                self._in_tool = False

    def feed(self, event_name: str, data: dict[str, Any]) -> Generator[str, None, None]:
        text = ""
        if event_name == "PartStartEvent":
            part = data.get("part") or {}
            kind = part.get("part_kind")
            if kind == "text":
                text = part.get("content") or ""
            elif kind == "thinking":
                # thinking рендерим как пустоту (OpenAI клиенты не ждут reasoning_content
                # в @ai-sdk/openai-compatible)
                return
        elif event_name == "PartDeltaEvent":
            delta = data.get("delta") or {}
            dkind = delta.get("part_delta_kind")
            if dkind == "text":
                text = delta.get("content_delta") or ""
            elif dkind == "thinking":
                return
        if not text:
            return
        yield from self._consume_parser(self._parser.feed(text))

    def finish(self) -> Generator[str, None, None]:
        # дофлашить парсер (вдруг хвост остался)
        yield from self._consume_parser(self._parser.finalize())
        reason = "tool_calls" if self._emitted_tool else "stop"
        yield self._chunk_finish(reason)
        yield "data: [DONE]\n\n"

    def error(self, status: int, message: str) -> Generator[str, None, None]:
        """Отдаёт ошибку в OpenAI формате."""
        payload = {
            "error": {
                "message": message,
                "type": "server_error",
                "code": status,
            }
        }
        yield _sse_line(None, payload)
        yield "data: [DONE]\n\n"


def build_openai_nonstream(model: str, text: str) -> dict[str, Any]:
    """Собирает полный OpenAI Chat Completions (non-stream) ответ.

    Если в `text` есть `<zo:call>` теги — конвертируются в
    `message.tool_calls` + finish_reason="tool_calls".
    """
    from tool_parser import parse_full_text

    blocks = parse_full_text(text)
    msg_text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
    tool_calls = []
    for b in blocks:
        if b.get("type") != "tool_use":
            continue
        try:
            arg_str = json.dumps(b.get("input") or {}, ensure_ascii=False)
        except Exception:
            arg_str = "{}"
        tool_calls.append({
            "id": b.get("id") or ("call_" + uuid.uuid4().hex[:24]),
            "type": "function",
            "function": {
                "name": b.get("name") or "unknown",
                "arguments": arg_str,
            },
        })
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(msg_text_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"

    full = "".join(msg_text_parts)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": "zo-proxy",
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": max(1, len(full.split())),
            "total_tokens": max(1, len(full.split())),
        },
    }


# ---------------------------------------------------------------------------
# OpenAI Responses API translator (новый Codex CLI >=0.59)
# ---------------------------------------------------------------------------

class ResponsesApiTranslator:
    """
    Принимает Zo SSE и выдаёт события OpenAI Responses API.
    Нужен для Codex CLI который звонит на /v1/responses.

    Поддерживает `<zo:call>` теги: конвертируются в `function_call` items
    с правильными `response.output_item.added` / `function_call_arguments.delta`
    / `function_call_arguments.done` / `response.output_item.done` событиями.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._resp_id = "resp-" + uuid.uuid4().hex[:24]
        self._text_item_id = "msg-" + uuid.uuid4().hex[:24]
        self._created = int(time.time())
        self._text_acc: list[str] = []
        self._parser = ToolCallTagParser()

        # state машина для output items
        self._next_output_index = 0
        self._text_item_opened = False
        self._text_item_done = False
        self._text_output_index: int | None = None
        self._text_content_index = 0

        self._cur_tool_item_id: str | None = None
        self._cur_tool_call_id: str | None = None
        self._cur_tool_name: str | None = None
        self._cur_tool_output_index: int | None = None
        self._cur_tool_args: list[str] = []

        # Все эмитнутые items (для финального response.completed)
        self._finished_items: list[dict[str, Any]] = []
        self._emitted_tool_call: bool = False

    def _ev(self, name: str, payload: dict[str, Any]) -> str:
        return _sse_line(name, payload)

    # ---- lifecycle ----

    def start_events(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "response.created",
                "response": {
                    "id": self._resp_id,
                    "object": "realtime.response",
                    "status": "in_progress",
                    "output": [],
                    "model": self.model,
                    "created_at": self._created,
                },
            },
        ]

    def start(self) -> Generator[str, None, None]:
        for payload in self.start_events():
            yield self._ev(payload["type"], payload)

    # ---- text item helpers ----

    def _open_text_item(self) -> list[dict[str, Any]]:
        if self._text_item_opened:
            return []
        self._text_output_index = self._next_output_index
        self._next_output_index += 1
        self._text_item_opened = True
        return [
            {
                "type": "response.output_item.added",
                "output_index": self._text_output_index,
                "item": {
                    "id": self._text_item_id,
                    "object": "realtime.item",
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
            {
                "type": "response.content_part.added",
                "item_id": self._text_item_id,
                "output_index": self._text_output_index,
                "content_index": self._text_content_index,
                "part": {"type": "output_text", "text": ""},
            },
        ]

    def _close_text_item(self) -> list[dict[str, Any]]:
        if not self._text_item_opened or self._text_item_done:
            return []
        full_text = "".join(self._text_acc)
        events = [
            {
                "type": "response.output_text.done",
                "item_id": self._text_item_id,
                "output_index": self._text_output_index,
                "content_index": self._text_content_index,
                "text": full_text,
            },
            {
                "type": "response.content_part.done",
                "item_id": self._text_item_id,
                "output_index": self._text_output_index,
                "content_index": self._text_content_index,
                "part": {"type": "output_text", "text": full_text},
            },
            {
                "type": "response.output_item.done",
                "output_index": self._text_output_index,
                "item": {
                    "id": self._text_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": full_text}],
                },
            },
        ]
        self._finished_items.append({
            "id": self._text_item_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text}],
        })
        self._text_item_done = True
        return events

    # ---- tool item helpers ----

    def _open_tool_item(self, call_id: str, name: str) -> list[dict[str, Any]]:
        out_idx = self._next_output_index
        self._next_output_index += 1
        item_id = "fc-" + uuid.uuid4().hex[:24]
        self._cur_tool_item_id = item_id
        self._cur_tool_call_id = call_id
        self._cur_tool_name = name
        self._cur_tool_output_index = out_idx
        self._cur_tool_args = []
        self._emitted_tool_call = True
        return [
            {
                "type": "response.output_item.added",
                "output_index": out_idx,
                "item": {
                    "id": item_id,
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "",
                },
            },
        ]

    def _tool_args_delta(self, partial: str) -> list[dict[str, Any]]:
        if self._cur_tool_item_id is None or not partial:
            return []
        self._cur_tool_args.append(partial)
        return [
            {
                "type": "response.function_call_arguments.delta",
                "item_id": self._cur_tool_item_id,
                "output_index": self._cur_tool_output_index,
                "delta": partial,
            },
        ]

    def _close_tool_item(self) -> list[dict[str, Any]]:
        if self._cur_tool_item_id is None:
            return []
        full_args = "".join(self._cur_tool_args)
        events = [
            {
                "type": "response.function_call_arguments.done",
                "item_id": self._cur_tool_item_id,
                "output_index": self._cur_tool_output_index,
                "arguments": full_args,
            },
            {
                "type": "response.output_item.done",
                "output_index": self._cur_tool_output_index,
                "item": {
                    "id": self._cur_tool_item_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": self._cur_tool_call_id,
                    "name": self._cur_tool_name,
                    "arguments": full_args,
                },
            },
        ]
        self._finished_items.append({
            "id": self._cur_tool_item_id,
            "type": "function_call",
            "call_id": self._cur_tool_call_id,
            "name": self._cur_tool_name,
            "arguments": full_args,
        })
        self._cur_tool_item_id = None
        self._cur_tool_call_id = None
        self._cur_tool_name = None
        self._cur_tool_output_index = None
        self._cur_tool_args = []
        return events

    # ---- parser dispatch ----

    def _consume_parser(self, events) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind, payload in events:
            if kind == "text":
                if not payload:
                    continue
                # если открыт tool — нельзя текст лить, закрываем тул и открываем text
                if self._cur_tool_item_id is not None:
                    out.extend(self._close_tool_item())
                out.extend(self._open_text_item())
                self._text_acc.append(payload)
                out.append({
                    "type": "response.output_text.delta",
                    "item_id": self._text_item_id,
                    "output_index": self._text_output_index,
                    "content_index": self._text_content_index,
                    "delta": payload,
                })
            elif kind == "tool_open":
                # закрыть текущий text item (если был), затем открыть tool
                if self._text_item_opened and not self._text_item_done:
                    out.extend(self._close_text_item())
                if self._cur_tool_item_id is not None:
                    out.extend(self._close_tool_item())
                out.extend(self._open_tool_item(payload["id"], payload["name"]))
            elif kind == "tool_args":
                out.extend(self._tool_args_delta(payload))
            elif kind == "tool_close":
                out.extend(self._close_tool_item())
        return out

    def feed_events(self, event_name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        text = ""
        if event_name == "PartStartEvent":
            part = data.get("part") or {}
            kind = part.get("part_kind")
            if kind == "text":
                text = part.get("content") or ""
        elif event_name == "PartDeltaEvent":
            delta = data.get("delta") or {}
            if delta.get("part_delta_kind") == "text":
                text = delta.get("content_delta") or ""
        if not text:
            return []
        return self._consume_parser(self._parser.feed(text))

    def feed(self, event_name: str, data: dict[str, Any]) -> Generator[str, None, None]:
        for payload in self.feed_events(event_name, data):
            yield self._ev(payload["type"], payload)

    def finish_events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # дофлашить парсер
        out.extend(self._consume_parser(self._parser.finalize()))
        # закрыть открытые items
        if self._cur_tool_item_id is not None:
            out.extend(self._close_tool_item())
        if self._text_item_opened and not self._text_item_done:
            out.extend(self._close_text_item())

        # сгенерировать output для response.completed
        full_text = "".join(self._text_acc)
        out.append({
            "type": "response.completed",
            "response": {
                "id": self._resp_id,
                "status": "completed",
                "output": self._finished_items,
                "model": self.model,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": max(1, len(full_text.split())),
                    "total_tokens": max(1, len(full_text.split())),
                },
            },
        })
        return out

    def finish(self) -> Generator[str, None, None]:
        for payload in self.finish_events():
            yield self._ev(payload["type"], payload)

    def error_events(self, status: int, message: str) -> list[dict[str, Any]]:
        return [
            {
                "type": "error",
                "code": str(status),
                "message": message,
                "param": None,
                "event_id": uuid.uuid4().hex,
            }
        ]

    def error(self, status: int, message: str) -> Generator[str, None, None]:
        for payload in self.error_events(status, message):
            yield self._ev(payload["type"], payload)


def build_responses_nonstream(model: str, text: str, resp_id: str | None = None) -> dict[str, Any]:
    """Собирает /v1/responses non-stream ответ.

    Если в `text` есть `<zo:call>` — они конвертируются в `function_call`
    items в `output[]`.
    """
    from tool_parser import parse_full_text

    rid = resp_id or ("resp-" + uuid.uuid4().hex[:24])
    output: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    msg_id = "msg-" + uuid.uuid4().hex[:24]
    msg_text_parts: list[str] = []

    for b in parse_full_text(text):
        if b.get("type") == "text":
            msg_text_parts.append(b["text"])
            full_text_parts.append(b["text"])
        elif b.get("type") == "tool_use":
            # сначала сбросим накопленный текст в message item если есть
            if msg_text_parts:
                output.append({
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "".join(msg_text_parts)}],
                })
                msg_text_parts = []
                msg_id = "msg-" + uuid.uuid4().hex[:24]
            try:
                arg_str = json.dumps(b.get("input") or {}, ensure_ascii=False)
            except Exception:
                arg_str = "{}"
            output.append({
                "id": "fc-" + uuid.uuid4().hex[:24],
                "type": "function_call",
                "status": "completed",
                "call_id": b.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                "name": b.get("name") or "unknown",
                "arguments": arg_str,
            })

    if msg_text_parts:
        output.append({
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "".join(msg_text_parts)}],
        })

    if not output:
        # пустой ответ — отдадим хоть пустое message
        output.append({
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": ""}],
        })

    full = "".join(full_text_parts)
    return {
        "id": rid,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": 0,
            "output_tokens": max(1, len(full.split())),
            "total_tokens": max(1, len(full.split())),
        },
    }
