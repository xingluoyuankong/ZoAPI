"""
Парсер `<zo:call name="..." id="...">{...}</zo:call>` тегов из стрима модели.

Используется и Anthropic-, и OpenAI-конвертерами, чтобы локальный клиент
(Claude Code / OpenCode / Codex) получал НАСТОЯЩИЕ tool_use / tool_calls
вместо сырого XML-текста.

Парсер потоковый: модель может пушить теги по символу — мы корректно
буферизуем `<zo:call` и `</zo:call>` префиксы, чтобы не «протекали»
куски тега в текст.
"""

from __future__ import annotations

import re
import uuid


class ToolCallTagParser:
    OPEN_RE = re.compile(r'<zo:call\b([^>]*)>', re.DOTALL)
    CLOSE_RE = re.compile(r'</zo:call>')
    ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
    OPEN_PARTIAL = '<zo:call'
    CLOSE_PARTIAL = '</zo:call>'

    def __init__(self) -> None:
        self.buf = ''
        self.in_call = False
        self._cur_name = ''
        self._cur_id = ''

    @staticmethod
    def _safe_text_len(buf: str, marker: str) -> int:
        n = len(buf)
        m = len(marker)
        max_suffix = min(n, m - 1)
        for k in range(max_suffix, 0, -1):
            if buf.endswith(marker[:k]):
                return n - k
        return n

    def feed(self, text: str):
        if not text:
            return
        self.buf += text
        while True:
            if not self.in_call:
                m = self.OPEN_RE.search(self.buf)
                if m:
                    if m.start() > 0:
                        yield ('text', self.buf[:m.start()])
                    attrs = dict(self.ATTR_RE.findall(m.group(1)))
                    self._cur_name = attrs.get('name', 'unknown')
                    self._cur_id = attrs.get('id') or ('toolu_' + uuid.uuid4().hex[:24])
                    self.buf = self.buf[m.end():]
                    self.in_call = True
                    yield ('tool_open', {'name': self._cur_name, 'id': self._cur_id})
                else:
                    safe = self._safe_text_len(self.buf, self.OPEN_PARTIAL)
                    if safe > 0:
                        yield ('text', self.buf[:safe])
                        self.buf = self.buf[safe:]
                    return
            else:
                m = self.CLOSE_RE.search(self.buf)
                if m:
                    inner = self.buf[:m.start()]
                    if inner:
                        yield ('tool_args', inner)
                    yield ('tool_close', None)
                    self.buf = self.buf[m.end():]
                    self.in_call = False
                else:
                    safe = self._safe_text_len(self.buf, self.CLOSE_PARTIAL)
                    if safe > 0:
                        yield ('tool_args', self.buf[:safe])
                        self.buf = self.buf[safe:]
                    return

    def finalize(self):
        if self.in_call:
            if self.buf:
                yield ('tool_args', self.buf)
            yield ('tool_close', None)
            self.in_call = False
            self.buf = ''
        elif self.buf:
            yield ('text', self.buf)
            self.buf = ''


def parse_full_text(text: str) -> list[dict]:
    """
    Разбирает уже собранный полный текст (non-stream путь) на список
    блоков: [{"type":"text","text":"..."}, {"type":"tool_use","id":"...",
    "name":"...","input":{...}}, ...]
    Если JSON внутри тега кривой — input будет пустой {}.
    """
    import json

    blocks: list[dict] = []
    parser = ToolCallTagParser()
    cur_text = ''
    cur_tool: dict | None = None
    cur_args_buf = ''

    def flush_text():
        nonlocal cur_text
        if cur_text:
            blocks.append({"type": "text", "text": cur_text})
            cur_text = ''

    for kind, payload in parser.feed(text):
        if kind == 'text':
            cur_text += payload
        elif kind == 'tool_open':
            flush_text()
            cur_tool = {
                "type": "tool_use",
                "id": payload['id'],
                "name": payload['name'],
                "input": {},
            }
            cur_args_buf = ''
        elif kind == 'tool_args':
            cur_args_buf += payload
        elif kind == 'tool_close':
            if cur_tool is not None:
                try:
                    cur_tool["input"] = json.loads(cur_args_buf or '{}')
                except Exception:
                    cur_tool["input"] = {}
                blocks.append(cur_tool)
            cur_tool = None
            cur_args_buf = ''

    for kind, payload in parser.finalize():
        if kind == 'text':
            cur_text += payload
        elif kind == 'tool_args':
            cur_args_buf += payload
        elif kind == 'tool_close':
            if cur_tool is not None:
                try:
                    cur_tool["input"] = json.loads(cur_args_buf or '{}')
                except Exception:
                    cur_tool["input"] = {}
                blocks.append(cur_tool)
            cur_tool = None
            cur_args_buf = ''

    flush_text()
    return blocks
