"""
runtime.py — живые настройки, которые проксе и лаунчеру нужно видеть друг
у друга без перезапуска.

Хранение: ./runtime.json рядом с proxy.py.
Чтение: с проверкой mtime, так что прокси подхватывает изменения, сделанные
из TUI, на следующем же запросе.

Сейчас тут только `force_model` — принудительная модель, которая
перекрывает то, что прислал клиент. Пустая строка / отсутствие ключа =
никакого перекрытия, прокси работает в обычном режиме.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


RUNTIME_FILE = Path(__file__).resolve().parent / "runtime.json"

_cache: dict[str, Any] = {"mtime": -1.0, "data": {}}


def _load() -> dict[str, Any]:
    try:
        st = RUNTIME_FILE.stat()
    except FileNotFoundError:
        _cache["mtime"] = -1.0
        _cache["data"] = {}
        return _cache["data"]
    mt = st.st_mtime
    if mt != _cache["mtime"]:
        try:
            raw = RUNTIME_FILE.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _cache["mtime"] = mt
        _cache["data"] = data
    return _cache["data"]


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001
    try:
        raw = RUNTIME_FILE.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception:
        data = {}
    if value in (None, ""):
        data.pop(key, None)
    else:
        data[key] = value
    RUNTIME_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        _cache["mtime"] = RUNTIME_FILE.stat().st_mtime
    except FileNotFoundError:
        _cache["mtime"] = -1.0
    _cache["data"] = data


def clear(key: str) -> None:
    set(key, None)


# --- удобные шорткаты для force_model ---


def get_force_model() -> str:
    val = get("force_model", "")
    return val if isinstance(val, str) else ""


def set_force_model(value: str) -> None:
    set("force_model", value or None)
