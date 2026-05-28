"""
utils/proxies.py — пул прокси для исходящих запросов к Zo Computer.

Файлы (на диске рядом с проектом):
- proxies.txt        — один прокси на строку, человекочитаемый
- proxies_state.json — { enabled, last_check, alive: [{url, latency_ms}] }

Поддерживаемые форматы строк в proxies.txt:
    host:port
    user:pass@host:port
    host:port:user:pass
    http://host:port
    http://user:pass@host:port
    socks5://host:port

Использование из основного кода:
    if proxies.is_enabled():
        url = proxies.pick_for_account(account.label)
        # url может быть None если включено но пул пуст / не проверен
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import httpx

ROOT = Path(__file__).resolve().parent.parent
PROXIES_FILE = ROOT / "proxies.txt"
PROXIES_STATE = ROOT / "proxies_state.json"

# Список «бесплатных» источников прокси. Берём http/https — для socks5 поднимем
# отдельный пункт позже, если попросят.
FREE_LIST_URLS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
]

CHECK_URL = "https://api.zo.computer/"
CHECK_TIMEOUT_S = 2.0
CHECK_CONCURRENCY = 80


# ---------------------------------------------------------------------------
# normalize / IO
# ---------------------------------------------------------------------------


def normalize(raw: str) -> str | None:
    """Нормализуем строку к виду scheme://[user:pass@]host:port."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        if host and port.isdigit():
            return f"http://{host}:{port}"
        return None
    if len(parts) == 4:
        host, port, user, pwd = parts
        if host and port.isdigit():
            return f"http://{user}:{pwd}@{host}:{port}"
        return None
    return None


def load_proxies() -> list[str]:
    if not PROXIES_FILE.exists():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in PROXIES_FILE.read_text(encoding="utf-8").splitlines():
        p = normalize(line)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def save_proxies(items: Iterable[str]) -> int:
    items = [p for p in (normalize(x) for x in items) if p]
    items = list(dict.fromkeys(items))  # сохранить порядок, убрать дубли
    PROXIES_FILE.write_text("\n".join(items) + ("\n" if items else ""), encoding="utf-8")
    return len(items)


def load_state() -> dict[str, Any]:
    if not PROXIES_STATE.exists():
        return {"enabled": False, "last_check": 0, "alive": []}
    try:
        data = json.loads(PROXIES_STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"enabled": False, "last_check": 0, "alive": []}
        data.setdefault("enabled", False)
        data.setdefault("last_check", 0)
        data.setdefault("alive", [])
        return data
    except Exception:
        return {"enabled": False, "last_check": 0, "alive": []}


def save_state(state: dict[str, Any]) -> None:
    PROXIES_STATE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def is_enabled() -> bool:
    return bool(load_state().get("enabled"))


def set_enabled(enabled: bool) -> dict[str, Any]:
    state = load_state()
    state["enabled"] = bool(enabled)
    save_state(state)
    return state


def toggle_enabled() -> dict[str, Any]:
    return set_enabled(not is_enabled())


def alive_proxies() -> list[str]:
    return [item["url"] for item in load_state().get("alive", []) if isinstance(item, dict) and item.get("url")]


# ---------------------------------------------------------------------------
# проверка живости
# ---------------------------------------------------------------------------


async def _check_one(proxy: str, timeout: float) -> tuple[str, bool, float]:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=timeout),
            follow_redirects=False,
        ) as c:
            r = await c.get(CHECK_URL)
        latency = (time.monotonic() - start) * 1000.0
        # Любой ответ от Zo (включая 401/404) — значит прокси доносит трафик.
        if r.status_code < 500 and latency <= timeout * 1000.0:
            return proxy, True, latency
    except Exception:
        pass
    return proxy, False, 0.0


async def _check_all(
    proxies: list[str],
    timeout: float,
    on_progress: Callable[[int, int], Awaitable[None] | None] | None,
) -> list[tuple[str, float]]:
    sem = asyncio.Semaphore(CHECK_CONCURRENCY)
    done = 0
    total = len(proxies)
    results: list[tuple[str, float]] = []

    async def worker(p: str) -> None:
        nonlocal done
        async with sem:
            proxy, ok, latency = await _check_one(p, timeout)
        done += 1
        if ok:
            results.append((proxy, latency))
        if on_progress is not None:
            try:
                res = on_progress(done, total)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    await asyncio.gather(*(worker(p) for p in proxies))
    return sorted(results, key=lambda x: x[1])


def check_and_store(
    timeout: float = CHECK_TIMEOUT_S,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Проверить весь список из proxies.txt, оставить только живые ≤ timeout."""
    proxies = load_proxies()
    if not proxies:
        state = load_state()
        state["alive"] = []
        state["last_check"] = int(time.time())
        save_state(state)
        return {"total": 0, "alive": 0, "ms": 0, "items": []}

    started = time.monotonic()
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)

        async def progress(done: int, total: int) -> None:
            if on_progress is not None:
                on_progress(done, total)

        alive = loop.run_until_complete(_check_all(proxies, timeout, progress))
    finally:
        loop.close()

    state = load_state()
    state["alive"] = [{"url": p, "latency_ms": int(lat)} for p, lat in alive]
    state["last_check"] = int(time.time())
    save_state(state)
    return {
        "total": len(proxies),
        "alive": len(alive),
        "ms": int((time.monotonic() - started) * 1000),
        "items": state["alive"],
    }


# ---------------------------------------------------------------------------
# загрузка свободных листов
# ---------------------------------------------------------------------------


def download_free(append: bool = True) -> dict[str, Any]:
    """Скачиваем бесплатные списки прокси, нормализуем, кладём в proxies.txt."""
    existing = set(load_proxies()) if append else set()
    collected: list[str] = list(existing) if append else []
    seen: set[str] = set(existing)
    sources_ok = 0
    sources_err: list[str] = []

    with httpx.Client(timeout=20.0, follow_redirects=True) as c:
        for url in FREE_LIST_URLS:
            try:
                r = c.get(url)
                if r.status_code != 200 or not r.text:
                    sources_err.append(url)
                    continue
                added = 0
                for line in r.text.splitlines():
                    p = normalize(line)
                    if p and p not in seen:
                        seen.add(p)
                        collected.append(p)
                        added += 1
                if added > 0:
                    sources_ok += 1
                else:
                    sources_err.append(url)
            except Exception:
                sources_err.append(url)

    save_proxies(collected)
    return {
        "total": len(collected),
        "added": len(collected) - len(existing),
        "sources_ok": sources_ok,
        "sources_err": sources_err,
    }


# ---------------------------------------------------------------------------
# выбор прокси под аккаунт (sticky hash)
# ---------------------------------------------------------------------------


def _stable_hash(s: str) -> int:
    h = 1469598103934665603
    for ch in s:
        h ^= ord(ch)
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def pick_for_account(label: str | None) -> str | None:
    """Стабильный выбор прокси под label. None если прокси выключены / пул пуст."""
    if not is_enabled():
        return None
    pool = alive_proxies()
    if not pool:
        return None
    if not label:
        return pool[0]
    return pool[_stable_hash(label) % len(pool)]
