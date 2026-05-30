"""
Парсер `<zo:call name="..." id="...">{...}</zo:call>` тегов из стрима модели.

Используется и Anthropic-, и OpenAI-конвертерами, чтобы локальный клиент
(Claude Code / OpenCode / Codex) получал НАСТОЯЩИЕ tool_use / tool_calls
вместо сырого XML-текста.

Парсер потоковый: модель может пушить теги по символу — мы корректно
буферизуем `<zo:call` и `</zo:call>` префиксы, чтобы не «протекали»
куски тега в текст.

КЛЮЧЕВОЙ ИНВАРИАНТ:
  Если в буфере есть `<zo:call` без закрывающей `>`, мы ОБЯЗАНЫ
  удерживать всё начиная с `<` — даже если после `<zo:call` идут
  сотни символов атрибутов. Иначе тег протечёт как текст.
"""

from __future__ import annotations

import re
import uuid


class ToolCallTagParser:
    # Полный открывающий тег: <zo:call name="..." id="...">
    OPEN_RE = re.compile(r'<zo:call\b([^>]*)>', re.DOTALL)
    # Закрывающий тег
    CLOSE_RE = re.compile(r'</zo:call>')
    # Атрибуты внутри открывающего тега
    ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

    # Для определения неполного открывающего тега (есть <zo:call но нет >)
    OPEN_START_RE = re.compile(r'<zo:call\b')

    # Минимальные маркеры для удержания хвоста буфера
    OPEN_PARTIAL = '<zo:call'    # 8 chars
    CLOSE_PARTIAL = '</zo:call>'  # 10 chars

    def __init__(self) -> None:
        self.buf = ''
        self.in_call = False
        self._cur_name = ''
        self._cur_id = ''

    @staticmethod
    def _suffix_overlap(buf: str, marker: str) -> int:
        """Возвращает длину наибольшего суффикса buf, который является
        префиксом marker. Если совпадения нет — 0.

        Пример: buf="abc<zo", marker="<zo:call" → суффикс "<zo" (3 символа),
        значит последние 3 символа buf нужно удержать."""
        n = len(buf)
        m = len(marker)
        max_check = min(n, m - 1)  # marker[:m-1] — макс. неполный префикс
        for k in range(max_check, 0, -1):
            if buf.endswith(marker[:k]):
                return k
        return 0

    def feed(self, text: str):
        """Принимает очередной кусок текста. Генерирует события:
          ('text', str)       — обычный текст
          ('tool_open', dict) — начало тега {name, id}
          ('tool_args', str)  — кусок аргументов (JSON)
          ('tool_close', None) — конец тега
        """
        if not text:
            return
        self.buf += text

        while True:
            if not self.in_call:
                # --- Ищем полный открывающий тег ---
                m = self.OPEN_RE.search(self.buf)
                if m:
                    # Текст ДО тега
                    if m.start() > 0:
                        yield ('text', self.buf[:m.start()])
                    # Парсим атрибуты
                    attrs = dict(self.ATTR_RE.findall(m.group(1)))
                    self._cur_name = attrs.get('name', 'unknown')
                    self._cur_id = attrs.get('id') or ('toolu_' + uuid.uuid4().hex[:24])
                    self.buf = self.buf[m.end():]
                    self.in_call = True
                    yield ('tool_open', {'name': self._cur_name, 'id': self._cur_id})
                    continue  # Может быть ещё один тег после

                # --- OPEN_RE не нашёл. Две причины: ---
                # 1) В буфере есть `<zo:call` но нет `>` (неполный тег)
                # 2) Буфер заканчивается на неполный PREFIX `<zo:call`
                #    (например `<zo:c` — ждём ещё символов)

                # Проверяем причину 1: ищем `<zo:call\b` без `>`
                m_start = self.OPEN_START_RE.search(self.buf)
                if m_start:
                    # Есть начало тега — удерживаем всё от него
                    safe = m_start.start()
                    if safe > 0:
                        yield ('text', self.buf[:safe])
                    self.buf = self.buf[safe:]
                    return  # Ждём больше данных

                # Причина 2: хвост может быть началом `<zo:call`
                overlap = self._suffix_overlap(self.buf, self.OPEN_PARTIAL)
                if overlap > 0:
                    safe = len(self.buf) - overlap
                    if safe > 0:
                        yield ('text', self.buf[:safe])
                    self.buf = self.buf[safe:]
                else:
                    # Ничего подозрительного — отдаём всё
                    if self.buf:
                        yield ('text', self.buf)
                    self.buf = ''
                return  # Ждём больше данных

            else:
                # --- Внутри <zo:call>...тут мы...</zo:call> ---
                m = self.CLOSE_RE.search(self.buf)
                if m:
                    # Аргументы до закрывающего тега
                    inner = self.buf[:m.start()]
                    if inner:
                        yield ('tool_args', inner)
                    yield ('tool_close', None)
                    self.buf = self.buf[m.end():]
                    self.in_call = False
                    continue  # Может быть ещё контент после

                # Нет закрывающего — удерживаем хвост на случай partial </zo:call>
                overlap = self._suffix_overlap(self.buf, self.CLOSE_PARTIAL)
                if overlap > 0:
                    safe = len(self.buf) - overlap
                    if safe > 0:
                        yield ('tool_args', self.buf[:safe])
                    self.buf = self.buf[safe:]
                else:
                    # Можно отдать всё как args
                    if self.buf:
                        yield ('tool_args', self.buf)
                    self.buf = ''
                return  # Ждём больше данных

    def finalize(self):
        """Вызвать когда стрим закончился. Флашит остатки буфера."""
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
