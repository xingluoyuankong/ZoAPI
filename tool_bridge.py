"""
Мост между серверными тулами Zo и тулами клиента.

Корень проблемы: эндпоинт /ask — это полноценный *агент*. Когда мы
проксируем туда запрос Claude Code / OpenCode / Codex, серверный агент
получает ДВА набора тулов:
  1) свои собственные (bash, read_file, list_directory, и т.д.), которые
     исполняются на КОНТЕЙНЕРЕ Zo (/home/workspace);
  2) наши <zo:call> XML-теги, которые описаны в промпте.

Несмотря на жёсткий bridge-промпт, иногда модель всё равно дёргает свои
серверные тулы. Это видно как PartStartEvent с part_kind='tool_call' и
именем 'bash' / 'read_file' / etc.

Этот модуль ловит такие вызовы и маппит их на ближайший эквивалент из
списка тулов клиента (Bash / Read / LS / Grep / WebFetch / ...). Имена
аргументов тоже переименовываем (cmd → command, target_file → file_path,
и т.п.). Так клиент получит ОСМЫСЛЕННЫЙ tool_use блок и выполнит вызов
на МАШИНЕ ПОЛЬЗОВАТЕЛЯ, а не на сервере Zo.

(Тул-вызов всё равно дублируется на стороне Zo — отменить мы не можем,
но это безвредно: результат серверного исполнения мы клиенту не отдаём.)
"""

from __future__ import annotations

from typing import Any, Iterable


# Имена серверных тулов Zo, которые мы знаем и можем маппить.
ZO_SERVER_TOOL_NAMES: set[str] = {
    "bash",
    "run_sequential_cmds",
    "run_parallel_cmds",
    "read_file",
    "write_file",
    "edit_file",
    "edit_file_llm",
    "list_directory",
    "grep_search",
    "read_webpage",
    "open_webpage",
    "view_webpage",
    "use_webpage",
    "save_webpage",
    "web_search",
    "web_research",
    "find_similar_links",
    "image_search",
    "x_search",
    "maps_search",
    "transcribe_audio",
    "transcribe_video",
    "send_email_to_user",
    "generate_image",
    "edit_image",
    "generate_video",
    "generate_d2_diagram",
    "tool_docs",
}

# zo_tool_name -> [(client_candidate_name, arg_renames), ...]
# Перебираем кандидатов в порядке предпочтения; первый, что есть у
# клиента, выигрывает. arg_renames: {zo_arg: client_arg}.
#
# В каждом списке держим и PascalCase (Claude Code), и lowercase
# (OpenCode/Codex) варианты — чтобы аргументы переименовывались правильно
# даже если имя тула совпадает по case.
_REMAP_TABLE: dict[str, list[tuple[str, dict[str, str]]]] = {
    "bash": [
        ("Bash",       {"cmd": "command", "description": "description", "timeout": "timeout"}),
        ("bash",       {"cmd": "command"}),  # OpenCode
        ("Shell",      {"cmd": "command"}),
        ("Terminal",   {"cmd": "command"}),
        ("execute",    {"cmd": "command"}),
        ("run",        {"cmd": "command"}),
    ],
    "run_sequential_cmds": [
        ("Bash",       {}),
        ("bash",       {}),
    ],
    "run_parallel_cmds": [
        ("Bash",       {}),
        ("bash",       {}),
    ],
    "read_file": [
        ("Read",       {"target_file": "file_path", "start_line": "offset", "end_line": "limit"}),
        ("read",       {"target_file": "filePath"}),  # OpenCode
        ("View",       {"target_file": "path"}),
        ("ReadFile",   {"target_file": "path"}),
    ],
    "write_file": [
        ("Write",      {"target_file": "file_path", "content": "content"}),
        ("write",      {"target_file": "filePath", "content": "content"}),  # OpenCode
        ("WriteFile",  {"target_file": "path"}),
        ("CreateFile", {"target_file": "path"}),
    ],
    "edit_file_llm": [
        ("Edit",       {"target_file": "file_path", "code_edit": "new_string"}),
        ("edit",       {"target_file": "filePath", "code_edit": "newString"}),  # OpenCode
        ("EditFile",   {"target_file": "path", "code_edit": "edit"}),
    ],
    "edit_file": [
        ("Edit",       {"target_file": "file_path"}),
        ("edit",       {"target_file": "filePath"}),
        ("EditFile",   {"target_file": "path"}),
    ],
    "list_directory": [
        ("LS",         {"path": "path"}),
        ("ls",         {"path": "path"}),
        ("list",       {"path": "path"}),
        ("ListDir",    {"path": "path"}),
        ("Glob",       {"path": "path"}),
    ],
    "grep_search": [
        ("Grep",       {"query": "pattern", "include_pattern": "glob", "exclude_pattern": "exclude"}),
        ("grep",       {"query": "pattern", "include_pattern": "include"}),  # OpenCode
        ("Glob",       {"query": "pattern"}),
        ("Search",     {"query": "query"}),
    ],
    "read_webpage": [
        ("WebFetch",   {"url": "url"}),
        ("webfetch",   {"url": "url"}),  # OpenCode
        ("FetchURL",   {"url": "url"}),
        ("fetch",      {"url": "url"}),
    ],
    "open_webpage": [
        ("WebFetch",   {"url": "url"}),
        ("webfetch",   {"url": "url"}),
    ],
    "save_webpage": [
        ("WebFetch",   {"url": "url"}),
        ("webfetch",   {"url": "url"}),
    ],
    "web_search": [
        ("WebSearch",  {"query": "query"}),
        ("websearch",  {"query": "query"}),
        ("Search",     {"query": "query"}),
        ("web_search", {"query": "query"}),
    ],
    "web_research": [
        ("WebSearch",  {"query": "query"}),
        ("websearch",  {"query": "query"}),
    ],
}


def is_zo_server_tool(name: str) -> bool:
    return name in ZO_SERVER_TOOL_NAMES


def remap_tool_name(
    zo_name: str,
    client_tool_names: Iterable[str] | None,
) -> tuple[str, dict[str, str]] | None:
    """
    Маппит имя серверного Zo-тула на имя тула клиента.

    Возвращает (client_name, arg_rename_map) или None если эквивалента нет.

    Алгоритм:
      1) Если zo_name — известный серверный тул из ZO_SERVER_TOOL_NAMES,
         ВСЕГДА идём через _REMAP_TABLE (даже если у клиента есть тул с
         тем же именем — у него могут быть другие имена аргументов).
      2) Иначе — пробуем прямой и case-insensitive матч имени.
    """
    if not client_tool_names:
        return None
    names_set = set(client_tool_names)
    if not names_set:
        return None
    lower_map = {n.lower(): n for n in names_set}

    if is_zo_server_tool(zo_name):
        candidates = _REMAP_TABLE.get(zo_name, [])
        # Pass 1: exact-case match — каждый кандидат сравниваем строго.
        for candidate, renames in candidates:
            if candidate in names_set:
                return candidate, renames
        # Pass 2: case-insensitive — нужен только если ни один exact не зашёл.
        for candidate, renames in candidates:
            if candidate.lower() in lower_map:
                return lower_map[candidate.lower()], renames
        # На крайний случай — само имя совпадает (но не в таблице).
        if zo_name in names_set:
            return zo_name, {}
        if zo_name.lower() in lower_map:
            return lower_map[zo_name.lower()], {}
        return None

    # Незнакомый тул — просто пробуем найти у клиента по имени.
    if zo_name in names_set:
        return zo_name, {}
    if zo_name.lower() in lower_map:
        return lower_map[zo_name.lower()], {}
    return None


def remap_args(zo_name: str, args: dict[str, Any], rename_map: dict[str, str]) -> dict[str, Any]:
    """
    Применяет переименование ключей + спец-обработку под конкретные тулы.
    """
    if not isinstance(args, dict):
        return args

    # Спец-кейсы, где нужна не просто переименовка, а склейка значений.
    if zo_name == "run_sequential_cmds":
        cmds = args.get("cmd_list") or args.get("command")
        if isinstance(cmds, list):
            return {"command": " && ".join(str(c) for c in cmds)}
    if zo_name == "run_parallel_cmds":
        cmds = args.get("cmd_list") or args.get("command")
        if isinstance(cmds, list):
            return {"command": " & ".join(str(c) for c in cmds)}

    if not rename_map:
        return args

    out: dict[str, Any] = {}
    for k, v in args.items():
        new_k = rename_map.get(k, k)
        # Не перетираем уже существующий ключ.
        if new_k in out:
            continue
        out[new_k] = v
    return out


def stringify_args_for_streaming(args: dict[str, Any]) -> str:
    """JSON-сериализация для отправки в качестве дельты аргументов."""
    import json
    try:
        return json.dumps(args, ensure_ascii=False)
    except Exception:
        return "{}"
