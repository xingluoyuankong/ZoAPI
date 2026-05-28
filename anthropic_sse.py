"""
Конвертер событий Zo /ask SSE → Anthropic /v1/messages SSE.

Zo стримит события вроде:
  event: PartStartEvent
  data: {"index": 0, "part": {"part_kind": "thinking"|"text"|"tool_call",
                              "content": "...", ...}, ...}

  event: PartDeltaEvent
  data: {"index": 0, "delta": {"part_delta_kind": "text"|"thinking",
                               "content_delta": "..."}, ...}

  event: PartEndEvent
  data: {"index": 0, ...}

  event: FinalResultEvent
  data: {...}

  event: FrontendModelResponse
  data: {"parts": [...], "kind": "response"}

Anthropic /v1/messages (stream=true) шлёт:
  event: message_start
  event: content_block_start  (один на блок: text или thinking или tool_use)
  event: content_block_delta  (text_delta | thinking_delta | input_json_delta)
  event: content_block_stop
  event: message_delta        (stop_reason, usage)
  event: message_stop
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, AsyncIterator, Iterator

from tool_parser import ToolCallTagParser
from tool_bridge import is_zo_server_tool, remap_tool_name, remap_args, stringify_args_for_streaming


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


class AnthropicStreamTranslator:
    """
    Translator одного запроса.

    Использование:
        t = AnthropicStreamTranslator(
            model="claude-opus-4-7",
            client_tool_names={"Bash", "Read", "Edit", ...},
            enable_thinking=True,  # если клиент запросил extended thinking
        )
        yield t.start()
        async for ev_name, data in zo_stream:
            for chunk in t.feed(ev_name, data):
                yield chunk
        yield from t.finish()
    """

    def __init__(
        self,
        model: str = "claude-opus-4-7",
        client_tool_names: set[str] | list[str] | None = None,
        enable_thinking: bool = False,
    ) -> None:
        self.model = model
        self.client_tool_names: set[str] = set(client_tool_names or [])
        self.message_id = "msg_" + uuid.uuid4().hex[:24]
        self.started = False
        self.closed = False
        self.current_block_index = -1
        self.current_block_kind: str | None = None
        self.output_tokens_est = 0
        self.stop_reason = "end_turn"
        self._tool_buf = ""
        self.tool_parser = ToolCallTagParser()
        self._tool_block_open = False
        self._emitted_tool_use = False
        self.enable_thinking = enable_thinking

        # Состояние для перехвата серверных Zo tool_call:
        # когда модель напрямую вызывает 'bash'/'read_file'/etc., мы
        # маппим имя на тул клиента и переименовываем аргументы. Аргументы
        # приходят дельтами JSON, поэтому буферизуем до tool_close, потом
        # одной дельтой отдаём клиенту в правильном формате.
        self._zo_tool_active = False
        self._zo_tool_name: str | None = None
        self._zo_tool_rename: dict[str, str] = {}
        self._zo_tool_arg_buf: list[str] = []

        # state for in-text <zo:call> tool calls (parsed from model text)
        self._zo_text_tool_rename: dict[str, str] = {}
        self._zo_text_tool_arg_buf: list[str] = []

    def _handle_streamed_text(self, text: str) -> Iterator[str]:
        for kind, payload in self.tool_parser.feed(text):
            if kind == 'text':
                if self.current_block_kind != "text":
                    yield from self._open_block("text")
                yield from self._delta_text(payload)
            elif kind == 'tool_open':
                # модель может выдумать имя ("PowerShell" вместо "Bash") —
                # маппим на ближайший тул клиента
                name = payload['name']
                mapped = remap_tool_name(name, self.client_tool_names) if self.client_tool_names else None
                if mapped:
                    name = mapped[0]
                    self._zo_text_tool_rename = mapped[1] or {}
                else:
                    self._zo_text_tool_rename = {}
                yield from self._open_block("tool_use", tool_name=name, tool_id=payload['id'])
                self._tool_block_open = True
            elif kind == 'tool_args':
                if self._zo_text_tool_rename:
                    self._zo_tool_arg_buf.append(payload)
                else:
                    yield from self._delta_tool_input(payload)
            elif kind == 'tool_close':
                if self._zo_text_tool_rename:
                    self._zo_tool_arg_buf.append(payload)
                else:
                    pass

    def _flush_parser(self) -> Iterator[str]:
        for kind, payload in self.tool_parser.finalize():
            if kind == 'text':
                if self.current_block_kind != "text":
                    yield from self._open_block("text")
                yield from self._delta_text(payload)
            elif kind == 'tool_open':
                # модель может выдумать имя ("PowerShell" вместо "Bash") —
                # маппим на ближайший тул клиента
                name = payload['name']
                mapped = remap_tool_name(name, self.client_tool_names) if self.client_tool_names else None
                if mapped:
                    name = mapped[0]
                    self._zo_text_tool_rename = mapped[1] or {}
                else:
                    self._zo_text_tool_rename = {}
                yield from self._open_block("tool_use", tool_name=name, tool_id=payload['id'])
                self._tool_block_open = True
            elif kind == 'tool_args':
                if self._zo_text_tool_rename:
                    self._zo_tool_arg_buf.append(payload)
                else:
                    yield from self._delta_tool_input(payload)
            elif kind == 'tool_close':
                if self._zo_text_tool_rename:
                    self._zo_tool_arg_buf.append(payload)
                else:
                    pass

    def _handle_streamed_thinking(self, text: str) -> Iterator[str]:
        """Emit a thinking_delta event, opening a thinking block lazily."""
        if self.current_block_kind != "thinking":
            yield from self._open_block("thinking")
        if text:
            yield from self._delta_thinking(text)

    # ---------------- lifecycle ----------------

    def start(self) -> Iterator[str]:
        if self.started:
            return
        self.started = True
        yield sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        )

    def finish(self) -> Iterator[str]:
        if self.closed:
            return
        yield from self._flush_parser()
        # закрыть открытый блок если есть
        if self.current_block_kind is not None:
            yield sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.current_block_index},
            )
            self.current_block_kind = None

        # КРИТИЧНО: если в этом ответе был хотя бы один tool_use блок —
        # stop_reason обязан быть "tool_use", иначе Claude Code НЕ исполнит тул.
        if self._emitted_tool_use:
            self.stop_reason = "tool_use"

        yield sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": self.stop_reason,
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": self.output_tokens_est},
            },
        )
        yield sse("message_stop", {"type": "message_stop"})
        self.closed = True

    def error(self, status: int, message: str) -> Iterator[str]:
        """Шлёт Anthropic-совместимое error-событие."""
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
        yield sse(
            "error",
            {
                "type": "error",
                "error": {"type": err_type, "message": message},
            },
        )

    # ---------------- block helpers ----------------

    def _open_block(self, kind: str, tool_name: str = "", tool_id: str = "") -> Iterator[str]:
        # закрыть предыдущий если был
        if self.current_block_kind is not None:
            yield sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.current_block_index},
            )

        self.current_block_index += 1
        self.current_block_kind = kind

        if kind == "text":
            block = {"type": "text", "text": ""}
        elif kind == "thinking":
            block = {"type": "thinking", "thinking": "", "signature": ""}
        elif kind == "tool_use":
            block = {
                "type": "tool_use",
                "id": tool_id or "toolu_" + uuid.uuid4().hex[:24],
                "name": tool_name or "unknown",
                "input": {},
            }
            self._tool_buf = ""
            self._emitted_tool_use = True
        else:
            block = {"type": "text", "text": ""}

        yield sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": self.current_block_index,
                "content_block": block,
            },
        )

    def _delta_text(self, text: str) -> Iterator[str]:
        self.output_tokens_est += max(1, len(text) // 4)
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.current_block_index,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _delta_thinking(self, text: str) -> Iterator[str]:
        self.output_tokens_est += max(1, len(text) // 4)
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.current_block_index,
                "delta": {"type": "thinking_delta", "thinking": text},
            },
        )

    def _delta_tool_input(self, partial_json: str) -> Iterator[str]:
        self._tool_buf += partial_json
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.current_block_index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": partial_json,
                },
            },
        )

    # ---------------- main dispatch ----------------

    def feed(self, ev_name: str, data: dict[str, Any]) -> Iterator[str]:
        """
        Обрабатывает одно событие из Zo /ask и шлёт нужные Anthropic события.
        """
        if not self.started:
            yield from self.start()

        if ev_name == "PartStartEvent":
            part = data.get("part") or {}
            kind = part.get("part_kind")  # "thinking" | "text" | "tool_call" | "tool_return"
            if kind in ("thinking",):
                # Claude Code обычно не запрашивает thinking — отдаём как обычный текст,
                # чтобы Claude Code не падал. Можно вырубить через config.HIDE_THINKING.
                if self.enable_thinking:
                    yield from self._handle_streamed_thinking(part.get("content") or "")
                else:
                    yield from self._handle_streamed_text(part.get("content") or "")
            elif kind == "text":
                yield from self._handle_streamed_text(part.get("content") or "")
            elif kind in ("tool_call", "tool_use"):
                tool_name = part.get("tool_name") or part.get("name") or "tool"
                tool_id = part.get("tool_call_id") or part.get("id") or ""
                yield from self._start_server_tool_call(tool_name, tool_id, part)
            else:
                # неизвестный part_kind — игнорируем
                pass

        elif ev_name == "PartDeltaEvent":
            delta = data.get("delta") or {}
            dkind = delta.get("part_delta_kind")
            if dkind == "text":
                text = delta.get("content_delta") or ""
                yield from self._handle_streamed_text(text)
            elif dkind == "thinking":
                text = delta.get("content_delta") or ""
                # см. выше — рендерим thinking как текст
                if self.enable_thinking:
                    yield from self._handle_streamed_thinking(text)
                else:
                    yield from self._handle_streamed_text(text)
            elif dkind in ("tool_call", "tool_use", "args"):
                partial = delta.get("args_delta") or delta.get("content_delta") or ""
                if self._zo_tool_active:
                    # буферизуем — отдадим в _finish_server_tool_call с переименованием
                    if partial:
                        if not isinstance(partial, str):
                            partial = json.dumps(partial, ensure_ascii=False)
                        self._zo_tool_arg_buf.append(partial)
                else:
                    if self.current_block_kind != "tool_use":
                        yield from self._open_block("tool_use")
                    self._emitted_tool_use = True
                    if partial:
                        if not isinstance(partial, str):
                            partial = json.dumps(partial, ensure_ascii=False)
                        yield from self._delta_tool_input(partial)
            else:
                pass

        elif ev_name == "PartEndEvent":
            # Если был серверный Zo tool_call — финализируем его (буфер
            # args собран, теперь переименуем и отправим клиенту).
            if self._zo_tool_active:
                yield from self._finish_server_tool_call()

        elif ev_name == "FinalResultEvent":
            # маркер финала pydantic_ai. Пропускаем — реальный конец будет
            # по закрытию http-стрима.
            pass

        elif ev_name == "FrontendModelResponse":
            # echo полного ответа в конце. Игнорируем — мы уже стримили дельтами.
            pass

        elif ev_name == "FrontendModelRequest":
            # echo нашего запроса. Игнорируем.
            pass

        elif ev_name == "End":
            self.stop_reason = "end_turn"

        elif ev_name == "Error":
            msg = data.get("message") or data.get("error") or json.dumps(data)
            yield from self.error(500, f"Zo stream error: {msg}")
            self.stop_reason = "end_turn"

        else:
            # неизвестное событие — лог не нужен, просто игнор
            pass

    # ---------------- server-tool interception ----------------

    def _start_server_tool_call(
        self,
        tool_name: str,
        tool_id: str,
        part: dict[str, Any],
    ) -> Iterator[str]:
        """
        Zo прислал PartStartEvent с part_kind='tool_call'.

        Если это серверный Zo-тул и у клиента есть эквивалент — маппим,
        буферизуем args, и финализируем в _finish_server_tool_call.
        Иначе работаем по старому: открываем tool_use с именем как есть.
        """
        if is_zo_server_tool(tool_name) and self.client_tool_names:
            mapped = remap_tool_name(tool_name, self.client_tool_names)
            if mapped is not None:
                client_name, rename_map = mapped
                self._zo_tool_active = True
                self._zo_tool_name = tool_name
                self._zo_tool_rename = rename_map
                self._zo_tool_arg_buf = []
                # initial args, если они уже в PartStartEvent
                args = part.get("args") or part.get("arguments")
                if args is not None:
                    partial = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                    self._zo_tool_arg_buf.append(partial)
                # открываем tool_use блок С ИМЕНЕМ КЛИЕНТА
                yield from self._open_block("tool_use", tool_name=client_name, tool_id=tool_id)
                self._emitted_tool_use = True
                return

        # default path: открываем как есть
        yield from self._open_block("tool_use", tool_name=tool_name, tool_id=tool_id)
        self._emitted_tool_use = True
        args = part.get("args") or part.get("arguments")
        if args is not None:
            partial = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            yield from self._delta_tool_input(partial)

    def _finish_server_tool_call(self) -> Iterator[str]:
        """Финализирует перехваченный серверный tool_call: парсит буфер
        args, переименовывает ключи и отдаёт клиенту одной input_json дельтой."""
        if not self._zo_tool_active:
            return
        raw = "".join(self._zo_tool_arg_buf)
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                args = {"_value": args}
        except Exception:
            args = {}
        mapped_args = remap_args(self._zo_tool_name or "", args, self._zo_tool_rename)
        partial = stringify_args_for_streaming(mapped_args)
        yield from self._delta_tool_input(partial)

        # сброс состояния
        self._zo_tool_active = False
        self._zo_tool_name = None
        self._zo_tool_rename = {}
        self._zo_tool_arg_buf = []
