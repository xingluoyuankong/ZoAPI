"""
mapper.py — конвертация OpenAI/Anthropic messages в строку `q` для Zo /ask.

Подход из референса: описываем тулы клиента в XML-формате, модель выдаёт
ответ как один XML-тег — парсим обратно tool_calls/tool_use.

Отличия от оригинала:
  - Используем `<zo:call>` вместо голых `<tool_name>` тегов, как принято
    в нашем проекте (tool_parser.py и tool_bridge.py уже это парсят).
  - НЕ используем жирный BRIDGE_PROLOGUE — вместо этого XML-персона на
    стороне Zo убирает серверные тулы. Fallback-промпт есть, но лёгкий.
  - Conversation delta: передаём только новые сообщения, если Zo-конво
    уже существует.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


# ---------- helpers ----------


def _gather_text(content: Any) -> str:
    """Достаёт plain text из OpenAI/Anthropic content (строка или массив)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t in ("text", "input_text", "output_text"):
            chunks.append(part.get("text", ""))
        elif t == "tool_use":
            name = part.get("name", "?")
            tid = part.get("id") or part.get("tool_use_id") or "?"
            args = part.get("input", {})
            chunks.append(
                f'<zo:call name="{name}" id="{tid}">{json.dumps(args, ensure_ascii=False)}</zo:call>'
            )
        elif t == "tool_result":
            tid = part.get("tool_use_id", "?")
            inner = part.get("content")
            if isinstance(inner, list):
                text = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in inner
                )
            else:
                text = str(inner or "")
            chunks.append(f'<zo:result id="{tid}">{text}</zo:result>')
        elif t in ("function_call_output", "tool_result"):
            call_id = part.get("call_id") or part.get("tool_call_id") or "?"
            output = part.get("output") or part.get("content") or ""
            chunks.append(f'<zo:result id="{call_id}">{output}</zo:result>')
    return "\n".join(c for c in chunks if c)


# ---------- XML tool formatting ----------


def _format_tool_example(name: str, params: dict, required: list[str]) -> str:
    """Пример <zo:call> вызова для одного тула."""
    arg_hints: dict[str, str] = {}
    for p_name, p_schema in params.items():
        if not isinstance(p_schema, dict):
            continue
        ptype = p_schema.get("type", "string")
        if ptype == "boolean":
            arg_hints[p_name] = "true"
        elif ptype in ("integer", "number"):
            arg_hints[p_name] = "0"
        elif ptype == "array":
            arg_hints[p_name] = "[]"
        elif ptype == "object":
            arg_hints[p_name] = "{}"
        else:
            arg_hints[p_name] = f"<{p_name}>"
    sample = json.dumps(arg_hints, ensure_ascii=False) if arg_hints else "{}"
    return f'<zo:call name="{name}" id="call_example">{sample}</zo:call>'


def _format_tools_prompt(tools: list[dict] | None) -> str:
    """Рендерит tools[] клиента как описание с примерами вызова."""
    if not tools:
        return ""
    lines = [
        "# AVAILABLE TOOLS",
        "",
        "Call a tool by outputting this EXACT XML format as text:",
        '  <zo:call name="ToolName" id="call_N">{"param": "value"}</zo:call>',
        "",
        "IMPORTANT: Output the XML literally as text. One call per response, then stop.",
        "",
    ]
    for i, t in enumerate(tools[:40]):
        if not isinstance(t, dict):
            continue
        # OpenAI format
        if t.get("type") == "function":
            fn = t.get("function") or {}
            name = fn.get("name", "")
            desc = (fn.get("description") or "").strip()
            params_obj = fn.get("parameters") or {}
        else:
            # Anthropic format or flat
            name = t.get("name", "")
            desc = (t.get("description") or "").strip()
            params_obj = t.get("input_schema") or t.get("inputSchema") or t.get("parameters") or {}

        if not name:
            continue

        props = params_obj.get("properties") or {}
        required = params_obj.get("required") or []

        lines.append(f"## {name}")
        if desc:
            lines.append(desc.split("\n\n")[0][:300])

        # Build example call with required params
        example_args: dict[str, str] = {}
        for p_name in list(required)[:4]:
            p_schema = props.get(p_name, {})
            ptype = p_schema.get("type", "string")
            if ptype == "boolean":
                example_args[p_name] = "true"
            elif ptype in ("integer", "number"):
                example_args[p_name] = "0"
            else:
                example_args[p_name] = "..."

        if not example_args and props:
            first_prop = list(props.keys())[0]
            example_args[first_prop] = "..."

        import json as _json
        example_json = _json.dumps(example_args, ensure_ascii=False)
        lines.append(f'  <zo:call name="{name}" id="call_{i+1}">{example_json}</zo:call>')
        lines.append("")

    return "\n".join(lines)


# ---------- Messages → q string ----------


def build_q_from_messages(
    messages: list[dict],
    tools: list[dict] | None,
    messages_subset: list[dict] | None = None,
) -> str:
    """Собирает строку `q` для Zo /ask из OpenAI/Anthropic messages.

    Если messages_subset задан — форматируем только эти сообщения (дельта),
    без повтора system-промпта и tools (они уже есть в Zo-конво).
    """
    target = messages_subset if messages_subset is not None else messages

    sys_blocks: list[str] = []
    convo: list[str] = []

    for m in target:
        role = (m.get("role") or "user").lower()
        if role == "developer":
            role = "system"
        text = _gather_text(m.get("content"))

        if role == "system":
            if text:
                sys_blocks.append(text)
        elif role == "user":
            if text:
                convo.append(f"[user]\n{text}")
        elif role == "assistant":
            if text:
                convo.append(f"[assistant]\n{text}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                convo.append(
                    f'[assistant tool_call]\n'
                    f'<zo:call name="{fn.get("name", "")}" id="{tc.get("id", "")}">'
                    f'{fn.get("arguments", "{}")}</zo:call>'
                )
        elif role == "tool":
            tcid = m.get("tool_call_id", "")
            convo.append(f"[tool_result id={tcid}]\n{text}")

    # Дельта: только conversation turns
    if messages_subset is not None:
        return "\n\n".join(p for p in convo if p)

    # Полный запрос: context frame + system + conversation + tools
    tools_prompt = _format_tools_prompt(tools)
    parts: list[str] = []

    # Inline context — minimal framing for tool mode
    parts.append(
        "[TOOL SESSION] Call tools by outputting <zo:call> XML as text. "
        "Do NOT use native function calling. Respond in user's language."
    )

    if sys_blocks:
        parts.append("[SYSTEM]\n" + "\n\n".join(sys_blocks))
    parts.extend(convo)
    if tools_prompt:
        parts.append(tools_prompt)
    return "\n\n".join(p for p in parts if p)


# ---------- Conversation delta helpers ----------


def get_conversation_id(messages: list[dict]) -> str:
    """Стабильный client-side conversation ID по хешу первых system+user сообщений."""
    first_user = ""
    first_system = ""
    for msg in messages:
        role = msg.get("role")
        if role == "user" and not first_user:
            first_user = _gather_text(msg.get("content"))
        elif role == "system" and not first_system:
            first_system = _gather_text(msg.get("content"))

    sig = f"sys:{first_system}|user:{first_user}"
    if not first_user and not first_system:
        import uuid
        sig = str(uuid.uuid4())
    return hashlib.md5(sig.encode("utf-8")).hexdigest()


def get_messages_delta(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Находит дельту (новые сообщения) после последнего ответа assistant.

    Returns (delta_messages, history_messages).
    """
    last_assistant_idx = -1
    is_prefill = len(messages) > 0 and messages[-1].get("role") == "assistant"

    if is_prefill:
        assistant_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                assistant_count += 1
                if assistant_count == 2:
                    last_assistant_idx = i
                    break
    else:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break

    if last_assistant_idx == -1:
        return messages, []

    history_msgs = messages[: last_assistant_idx + 1]
    delta_msgs = messages[last_assistant_idx + 1:]
    return delta_msgs, history_msgs
