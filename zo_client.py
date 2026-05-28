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

try:
    from utils import proxies as _proxies  # type: ignore
except Exception:  # noqa: BLE001
    _proxies = None  # type: ignore[assignment]

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
        self._timeout = httpx.Timeout(
            connect=timeout_connect,
            read=timeout_read,
            write=60.0,
            pool=30.0,
        )
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def _proxy_for(self, account: "Account | None") -> str | None:
        if _proxies is None:
            return None
        try:
            label = getattr(account, "label", None)
            return _proxies.pick_for_account(label)
        except Exception:
            return None

    def _get(self, account: "Account | None") -> httpx.AsyncClient:
        proxy = self._proxy_for(account)
        client = self._clients.get(proxy)
        if client is not None:
            return client
        kwargs: dict[str, Any] = dict(
            base_url=self.base_url,
            timeout=self._timeout,
            follow_redirects=False,
        )
        if proxy:
            kwargs["proxy"] = proxy
        client = httpx.AsyncClient(**kwargs)
        self._clients[proxy] = client
        return client

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for c in clients:
            try:
                await c.aclose()
            except Exception:
                pass

    # ------------------------- non-stream helpers -------------------------

    async def list_models(self, account: Account) -> list[dict[str, Any]]:
        r = await self._get(account).get(
            "/models/",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json().get("models", [])

    async def list_personas(self, account: Account) -> list[dict[str, Any]]:
        """GET /personas/ — куки, без API-ключа (так делает веб-UI)."""
        r = await self._get(account).get(
            "/personas/",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("personas", []) if isinstance(data, dict) else []

    async def create_persona(
        self,
        account: Account,
        name: str,
        prompt: str,
        scopes: list[str] | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        """POST /personas/ — create a new persona. Returns the created persona dict.

        Body shape (captured fr
        """
        body = {"name": name, "prompt": prompt, "image": None, "scopes": scopes or []}
        r = await self._get(account).post(
            "/personas/",
            json=body,
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json()

    async def update_persona_scopes(
        self,
        account: Account,
        persona_id: str,
        scopes: list[str],
    ) -> bool:
        """Пытается выставить persona.scopes через несколько вариантов
        эндпоинтов (точный путь Zo не задокументирован).
        """
        for method in ("PATCH", "PUT", "POST"):
            url = f"/personas/{persona_id}"
            if method == "POST":
                url += "/scopes"
            r = await self._get(account).request(
                method, url, json={"scopes": scopes}, headers=_headers_for(account), cookies=_cookies_for(account)
            )
            if r.status_code == 200:
                return True
        return False

    async def get_active_personas(self, account: Account) -> dict[str, Any]:
        """GET /personas/active, returns the {main, greeting, sms, email, telegram, discord, slack, schedule} dict."""
        r = await self._get(account).get(
            "/personas/active",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json()

    async def set_main_persona(self, account: Account, persona_id: str) -> bool:
        """Делает персону активной для канала main.

        PUT /personas/active/{persona_id} с пустым body — как делает веб-UI.
        Возвращает True если success=true в ответе.
        """
        r = await self._get(account).put(
            f"/personas/active/{persona_id}",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        if r.status_code != 200:
            log.warning(
                "[%s] set_main_persona: HTTP %d body=%s",
                account.label, r.status_code, r.text[:200]
            )
            return False
        try:
            return bool(r.json().get("success", False))
        except Exception:
            return r.status_code == 200

    async def list_rules(self, account: Account) -> list[dict[str, Any]]:
        try:
            r = await self._get(account).get(
                "/rules/",
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("rules", []) if isinstance(data, dict) else []
        except Exception:
            return []

    async def create_rule(
        self, account: Account, condition: str, instruction: str
    ) -> dict[str, Any] | None:
        try:
            r = await self._get(account).post(
                "/rules/",
                json={"condition": condition, "instruction": instruction},
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
            if r.status_code in (200, 201):
                return r.json()
        except Exception:
            pass
        return None

    async def ensure_rule(
        self, account: Account, instruction: str, condition: str = ""
    ) -> str | None:
        """Find a rule whose instruction matches; create if missing. Returns rule id or None."""
        rules = await self.list_rules(account)
        for r in rules:
            if r.get("instruction") == instruction:
                return r.get("id")
        created = await self.create_rule(account, condition, instruction)
        return created.get("id") if isinstance(created, dict) else None

    async def ensure_bridge_persona(
        self, account: Account, name: str, prompt: str
    ) -> str | None:
        """
        Полностью настраивает bridge-персону. Логика как у веб-UI:
          1. GET /personas/                 — ищем по name
          2. POST /personas/                — если нет, создаём со scopes=[]
          3. PUT  /personas/active/{id}     — делаем активной
          4. GET  /personas/active          — проверяем что main == id
        Между шагами sleep(1) чтобы бэкенд успел.
        """
        import asyncio
        import runtime
        # --- 1) list ---
        try:
            personas = await self.list_personas(account)
        except Exception as e:
            log.warning("[%s] list_personas failed: %s", account.label, e)
            return None
        log.info(
            "[%s] ensure_bridge_persona: %d existing, looking for %r",
            account.label, len(personas), name,
        )
        pid = None
        for pp in personas:
            if pp.get("name") == name:
                pid = pp.get("id")
                break

        # --- 2) create if missing ---
        if not pid:
            try:
                created = await self.create_persona(
                    account, name, prompt, scopes=["web:browse"]
                )
                pid = created.get("id") if isinstance(created, dict) else None
                log.info(
                    "[%s] ensure_bridge_persona: created persona id=%s",
                    account.label, pid,
                )
            except Exception as e:
                log.exception(
                    "[%s] ensure_bridge_persona: create_persona FAILED: %s",
                    account.label, e,
                )
                return None
            await asyncio.sleep(1.0)
        else:
            log.info("[%s] ensure_bridge_persona: persona already exists id=%s", account.label, pid)

        if not pid:
            log.warning("[%s] ensure_bridge_persona: no pid after create", account.label)
            return None

        # --- 3) set main (всегда: бридж должна быть активной)
        try:
            ok = await self.set_main_persona(account, pid)
            log.info("[%s] ensure_bridge_persona: set_main_persona ok=%s pid=%s", account.label, ok, pid)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] ensure_bridge_persona: set_main_persona FAILED: %s", account.label, e)

        await asyncio.sleep(1.0)

        # --- 4) verify ---
        try:
            active = await self.get_active_personas(account)
            current_main = active.get("main")
            if current_main == pid:
                log.info("[%s] ensure_bridge_persona: VERIFIED main=%s", account.label, pid)
            else:
                log.warning("[%s] ensure_bridge_persona: main mismatch — expected %s got %s, retry", account.label, pid, current_main)
                await asyncio.sleep(1.0)
                try:
                    await self.set_main_persona(account, pid)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] ensure_bridge_persona: verify failed: %s", account.label, e)

        return pid

    # ----------------------- API keys -----------------------

    async def list_api_keys(self, account: Account) -> list[dict]:
        r = await self._get(account).get(
            "/api-keys/",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("api_keys", []) if isinstance(data, dict) else []

    async def create_api_key(self, account: Account, name: str) -> dict[str, Any]:
        """POST /api-keys/ with {name}. Returns {id, name, key, key_prefix, created_at}."""
        r = await self._get(account).post(
            "/api-keys/",
            json={"name": name},
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json() if r.content else {}

    async def ensure_api_key(self, account: Account, name: str = "zoapi-bridge") -> dict[str, Any] | None:
        """Гарантирует API-ключ с заданным name на аккаунте.
        
        Алгоритм:
          1. GET /api-keys/ — ищем по name
          2. Если есть и есть .key — возвращаем
          3. Если есть но .key скрыт (Zo показывает полный key только при создании) —
             всё равно возвращаем (мы используем существующий)
          4. Если нет — POST /api-keys/ с {name}, возвращаем
        
        Возвращает {id, key, key_prefix, name, ...} или None при ошибке.
        """
        try:
            keys = await self.list_api_keys(account)
        except Exception as e:
            log.warning("[%s] list_api_keys failed: %s", account.label, e)
            return None
        match = next((k for k in keys if k.get("name") == name), None)
        if match:
            log.info("[%s] ensure_api_key: existing match id=%s prefix=%s",
                     account.label, match.get("id"), match.get("key_prefix"))
            return match
        try:
            created = await self.create_api_key(account, name)
            log.info("[%s] ensure_api_key: CREATED id=%s prefix=%s",
                     account.label, created.get("id"), created.get("key_prefix"))
            return created
        except Exception as e:
            log.exception("[%s] create_api_key failed: %s", account.label, e)
            return None

    async def list_conversations(self, account: Account) -> list[dict[str, Any]]:
        r = await self._get(account).get(
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
            r = await self._get(account).get(
                "/billing/credit-grants?testmode=false",
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
            if r.status_code != 200:
                return None
            grants = r.json()
            if isinstance(grants, list):
                return sum(g.get("available_cents", 0) for g in grants)
            if isinstance(grants, dict):
                for key in ("grants", "credit_grants", "items"):
                    if key in grants and isinstance(grants[key], list):
                        return sum(g.get("available_cents", 0) for g in grants[key])
                if "available_cents" in grants:
                    return grants["available_cents"]
                if "remaining_cents" in grants:
                    return grants["remaining_cents"]
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

        async with self._get(account).stream(
            "POST",
            "/ask",
            json=body,
            headers=_headers_for(account, idempotency),
            cookies=_cookies_for(account),
        ) as resp:
            if resp.status_code >= 400:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                log.warning("[%s] Zo /ask %d body: %s", account.label, resp.status_code, err[:1500])
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
