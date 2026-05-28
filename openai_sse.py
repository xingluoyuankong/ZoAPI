"""
Конвертер Zo /ask SSE → OpenAI Chat Completions + Responses API SSE.

Для клиентов: Hermes, OpenCode/OpenClaw, aider, old Codex (chat completions),
new OpenAI Codex CLI (responses), и любой другой OpenAI-compatible инструмент.

Форматы вывода:

[Chat Completions stream]
  data: {"id":"chatcmpl-...","object":"chat.completion.chunk","created":...,"model":"...",
         "choices":[{"index":0,"delta":{"content":"..."},"finish_reason":null}]}
  ...
  data: {"id":"...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},
         "finish_reason":"stop"}]}
  data: [DONE]

[Responses API stream — новый Codex CLI >=0.59]
  event: response.created
  data: {"type":"response.created","response":{"id":"resp-...","status":"in_progress",...}}

  event: response.output_item.added
  data: {"type":"response.output_item.added","item":{"id":"item-...","type":"message",...}}

  event: response.content_part.added
  data: {"type":"response.content_part.added","part":{"type":"output_text","text":""}}

  event: response.output_text.delta
  data: {"type":"response.output_text.delta","delta":"chunk text..."}

  event: response.output_text.done
  data: {"type":"response.output_text.done","text":"full text"}

  event: response.output_item.done
  data: {"type":"response.output_item.done","item":{...}}

  event: response.completed
  data: {"type":"response.completed","response":{...}}
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Generator


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
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self._created = int(time.time())
        self._started = False
        self._buf = ""

    def _chunk(self, content: str = "", finish: str | None = None) -> str:
        choice: dict[str, Any] = {"index": 0, "logprobs": None}
        if finish:
            choice["delta"] = {}
            choice["finish_reason"] = finish
        else:
            choice["delta"] = {"content": content}
            choice["finish_reason"] = None
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [choice],
        }
        return _sse_line(None, payload)

    def start(self) -> Generator[str, None, None]:
        """Начальный chunk с role."""
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "system_fingerprint": "zo-proxy",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        }
        self._started = True
        yield _sse_line(None, payload)

    def feed(self, event_name: str, data: dict[str, Any]) -> Generator[str, None, None]:
        if event_name == "PartStartEvent":
            part = data.get("part") or {}
            kind = part.get("part_kind")
            if kind in ("text",):
                text = part.get("content") or ""
                if text:
                    yield self._chunk(text)
        elif event_name == "PartDeltaEvent":
            delta = data.get("delta") or {}
            if delta.get("part_delta_kind") == "text":
                text = delta.get("content_delta") or ""
                if text:
                    yield self._chunk(text)
        # thinking — не показываем в OpenAI mode (клиент не ждёт)
        # FrontendModelResponse / FinalResultEvent — игнорируем, текст уже пришёл

    def finish(self) -> Generator[str, None, None]:
        yield self._chunk(finish="stop")
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
    """Собирает полный OpenAI Chat Completions (non-stream) ответ."""
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": "zo-proxy",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(text.split()),
            "total_tokens": len(text.split()),
        },
    }


# ---------------------------------------------------------------------------
# OpenAI Responses API translator (новый Codex CLI >=0.59)
# ---------------------------------------------------------------------------

class ResponsesApiTranslator:
    """
    Принимает Zo SSE и выдаёт события OpenAI Responses API.
    Нужен для нового Codex CLI который звонит на /v1/responses.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._resp_id = "resp-" + uuid.uuid4().hex[:24]
        self._item_id = "item-" + uuid.uuid4().hex[:24]
        self._created = int(time.time())
        self._text_acc: list[str] = []

    def _ev(self, name: str, payload: dict[str, Any]) -> str:
        return _sse_line(name, payload)

    def start(self) -> Generator[str, None, None]:
        yield self._ev("response.created", {
            "type": "response.created",
            "response": {
                "id": self._resp_id,
                "object": "realtime.response",
                "status": "in_progress",
                "output": [],
                "model": self.model,
                "created_at": self._created,
            },
        })
        yield self._ev("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": self._item_id,
                "object": "realtime.item",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        })
        yield self._ev("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": self._item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        })

    def feed(self, event_name: str, data: dict[str, Any]) -> Generator[str, None, None]:
        text = ""
        if event_name == "PartStartEvent":
            part = data.get("part") or {}
            if part.get("part_kind") == "text":
                text = part.get("content") or ""
        elif event_name == "PartDeltaEvent":
            delta = data.get("delta") or {}
            if delta.get("part_delta_kind") == "text":
                text = delta.get("content_delta") or ""
        if text:
            self._text_acc.append(text)
            yield self._ev("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": self._item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            })

    def finish(self) -> Generator[str, None, None]:
        full_text = "".join(self._text_acc)
        yield self._ev("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": self._item_id,
            "output_index": 0,
            "content_index": 0,
            "text": full_text,
        })
        yield self._ev("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": self._item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text}],
            },
        })
        yield self._ev("response.completed", {
            "type": "response.completed",
            "response": {
                "id": self._resp_id,
                "status": "completed",
                "output": [
                    {
                        "id": self._item_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": full_text}],
                    }
                ],
                "model": self.model,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": len(full_text.split()),
                    "total_tokens": len(full_text.split()),
                },
            },
        })

    def error(self, status: int, message: str) -> Generator[str, None, None]:
        yield self._ev("error", {
            "type": "error",
            "code": str(status),
            "message": message,
            "param": None,
            "event_id": uuid.uuid4().hex,
        })


def build_responses_nonstream(model: str, text: str, resp_id: str | None = None) -> dict[str, Any]:
    rid = resp_id or ("resp-" + uuid.uuid4().hex[:24])
    iid = "item-" + uuid.uuid4().hex[:24]
    return {
        "id": rid,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": iid,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": len(text.split()),
            "total_tokens": len(text.split()),
        },
    }
