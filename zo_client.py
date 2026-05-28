"""
HTTP-клиент к Zo Computer.

Эндпоинты:
 - POST /ask            — основной чат (streaming SSE)
 - GET  /conversations  — список разговоров
 - GET  /models/available — модели
 - GET  /personas/available — персоны

Авторизация — сессионные cookies (access_token, refresh_token).
Шлёт всё с теми же хедерами, что и веб-чат Zo (Origin, Referer,
X-Zo-Workspace-Origin, x-zo-streaming-version и т.д.), потому что
бэкенд их проверяет и без них даёт 401.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

import httpx

from accounts import Account

log = logging.getLogger("zo-proxy.client")

FIRST_CHUNK_TIMEOUT = 40.0  # сек до первого события от Zo → ZoTimeout
BETWEEN_CHUNKS_TIMEOUT = 90.0  # сек между чанками внутри стрима


class ZoAuthError(Exception):
    """access_token истёк или невалиден."""


class ZoForbidden(Exception):
    """403 от Zo."""


class ZoServerError(Exception):
    """5xx."""


class ZoBadRequest(Exception):
    """4xx, не auth."""


class ZoTimeout(Exception):
    """Нет ответа >FIRST_CHUNK_TIMEOUT секунд."""


def _headers_for(account: Account, idempotency_key: str | None = None) -> dict[str, str]:
    """Полный набор браузерных хедеров для запроса к /ask."""
    h = {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": account.workspace_origin(),
        "referer": account.workspace_origin() + "/",
        "x-zo-workspace-origin": account.workspace_origin(),
        "x-zo-streaming-version": "2",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
    }
    if idempotency_key:
        h["idempotency-key"] = idempotency_key
    return h


def _cookies_for(account: Account) -> dict[str, str]:
    from urllib.parse import quote
    return {
        "access_token": account.access_token,
        "refresh_token": quote(account.refresh_token, safe=""),
    }


class ZoClient:
    """
    Тонкая обёртка над httpx.AsyncClient.
    Все методы принимают Account явно — клиент сам по себе stateless.
    """

    def __init__(
        self,
        base_url: str = "https://api.zo.computer",
        timeout_connect: float = 30.0,
        timeout_read: float = 900.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=timeout_connect,
                read=timeout_read,
                write=60.0,
                pool=30.0,
            ),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------- non-stream helpers -------------------------

    async def list_models(self, account: Account) -> list[dict[str, Any]]:
        r = await self._client.get(
            "/models/available",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json().get("models", [])

    async def list_personas(self, account: Account) -> list[dict[str, Any]]:
        r = await self._client.get(
            "/personas/available",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json().get("personas", [])

    async def list_conversations(self, account: Account) -> list[dict[str, Any]]:
        r = await self._client.get(
            "/conversations",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("conversations", [])

    async def ping(self, account: Account) -> bool:
        """Быстрый health-check: пытаемся получить список моделей."""
        try:
            await self.list_models(account)
            return True
        except Exception:
            return False

    async def fetch_balance(self, account: Account) -> int | None:
        """Возвращает суммарный available баланс в центах или None при ошибке."""
        try:
            r = await self._client.get(
                "/billing/credit-grants?testmode=false",
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
            if r.status_code != 200:
                return None
            grants = r.json()
            if isinstance(grants, list):
                return sum(g.get("available_cents", 0) for g in grants)
        except Exception:
            pass
        return None

    # ------------------------- streaming chat -------------------------

    async def ask_stream(
        self,
        account: Account,
        *,
        q: str,
        model_name: str | None,
        conversation_id: str | None = None,
        context_paths: list[str] | None = None,
        command_paths: list[str] | None = None,
        expanded_paths: list[str] | None = None,
        persona_id: str | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any], str | None]]:
        """
        Async generator: yields (event_type, data_dict, conversation_id_header).
        conversation_id_header возвращается только на первом yield.
        """
        body: dict[str, Any] = {
            "q": q,
            "context_paths": context_paths or [],
            "command_paths": command_paths or [],
            "expanded_paths": expanded_paths or [],
        }
        if model_name:
            body["model_name"] = model_name
        if conversation_id:
            body["conversation_id"] = conversation_id
        if persona_id:
            body["persona_id"] = persona_id

        idempotency = str(uuid.uuid4())

        async with self._client.stream(
            "POST",
            "/ask",
            json=body,
            headers=_headers_for(account, idempotency),
            cookies=_cookies_for(account),
        ) as resp:
            if resp.status_code >= 400:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                _raise_status(resp.status_code, err)

            conv_header = resp.headers.get("x-conversation-id")
            first_event = True
            got_first = False

            event_type: str | None = None
            data_lines: list[str] = []

            aiter = resp.aiter_lines().__aiter__()
            while True:
                timeout = FIRST_CHUNK_TIMEOUT if not got_first else BETWEEN_CHUNKS_TIMEOUT
                try:
                    line = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise ZoTimeout(
                        f"Zo не ответил за {timeout:.0f}с — "
                        f"{'первый чанк' if not got_first else 'продолжение'}"
                    )

                got_first = True

                if not line:
                    if event_type and data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {"_raw": raw}
                        yield event_type, data, conv_header if first_event else None
                        first_event = False
                    event_type = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _raise_for_status(r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    _raise_status(r.status_code, r.text)


def _raise_status(status: int, body: str) -> None:
    if status in (401,):
        raise ZoAuthError(_short(body) or "Invalid or expired token")
    if status in (403,):
        raise ZoForbidden(_short(body) or "Access denied")
    if 400 <= status < 500:
        raise ZoBadRequest(f"{status}: {_short(body)}")
    raise ZoServerError(f"{status}: {_short(body)}")


def _short(body: str, n: int = 400) -> str:
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            for k in ("detail", "error", "message"):
                if k in j and isinstance(j[k], str):
                    return j[k][:n]
            return json.dumps(j)[:n]
    except Exception:
        pass
    return body[:n].strip()
