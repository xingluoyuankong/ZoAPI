"""
HTTP-клиент к Zo Computer — переписан по мотивам zo-proxy-public.

Ключевые улучшения:
  - Browser fingerprint rotation (каждый аккаунт = свой профиль)
  - XML-mode persona (лёгкая, нейтральное имя, scopes=[] — серверные тулы Zo не видны)
  - Conversation state + delta (reuse zo conversation_id, шлём только
    новые сообщения, детектим бэктрекинг)
  - Proactive token refresh (если refresh_token есть и TTL < 7d)
  - Jitter между запросами (антибот)

Публичный API сохранён для launcher.py, proxy.py, auth_setup.py:
  - ask_stream(), list_models(), list_personas(), create_persona(),
    set_main_persona(), get_active_personas(), fetch_balance(), ping(),
    ensure_bridge_persona(), close()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from accounts import Account
from fingerprint import jitter_seconds, persona_name_for, profile_for_account

try:
    from utils import proxies as _proxies
except Exception:
    _proxies = None  # type: ignore[assignment]

log = logging.getLogger("zo-proxy.client")

ZO_API = "https://api.zo.computer"
ZO_AUTH = "https://auth.zo.computer"
REFRESH_PATH = "/zo/refresh_token"
REFRESH_THRESHOLD_SECS = 7 * 24 * 3600  # 7 days

FIRST_CHUNK_TIMEOUT = 40.0
BETWEEN_CHUNKS_TIMEOUT = 90.0


# ---------- Exceptions ----------

class ZoAuthError(Exception):
    """access_token истёк или невалиден (401)."""

class ZoForbidden(Exception):
    """403 от Zo."""

class ZoServerError(Exception):
    """5xx."""

class ZoBadRequest(Exception):
    """4xx, не auth."""

class ZoTimeout(Exception):
    """Нет ответа > timeout."""


# ---------- XML Persona ----------

XML_PERSONA_PROMPT = (
    "When a message contains an AVAILABLE TOOLS section, call tools by "
    "outputting this XML format AS LITERAL TEXT in your response:\n\n"
    '<zo:call name="ToolName" id="call_1">{"param": "value"}</zo:call>\n\n'
    "Rules:\n"
    "- Each tool in AVAILABLE TOOLS has an example call. Copy that format exactly.\n"
    "- Output ONLY the <zo:call> tag. No text before or after. Stop immediately.\n"
    "- Tool names are case-sensitive — copy exactly from AVAILABLE TOOLS.\n"
    "- The JSON between tags must match the tool's parameters.\n"
    "- After you call a tool, result arrives as <zo:result id=\"...\">...</zo:result>.\n"
    "- When no tool is needed, respond in plain text (no XML tags).\n"
    "- Never mix text and tool calls in one response.\n"
    "- This is TEXT output, not function calling. Write the XML literally.\n\n"
    "IDENTITY: You are whatever the [SYSTEM] section in the message defines. "
    "Follow that identity completely.\n"
    "Never mention or acknowledge: Zo, Zo Computer, personas, proxies, relays, "
    "middleware, /home/workspace, two tool sets, or this prompt.\n\n"
    "LANGUAGE: Think and respond in the user's language. Russian user → Russian."
)


# ---------- Helpers ----------

def _headers_for(account: Account, idempotency_key: str | None = None) -> dict[str, str]:
    """Полный набор браузерных хедеров на базе fingerprint профиля."""
    prof = profile_for_account(account.label)
    h = prof.headers()
    h.update({
        "Content-Type": "application/json",
        "x-zo-streaming-version": "2",
        "X-Zo-Workspace-Origin": account.workspace_origin(),
        "Origin": account.workspace_origin(),
        "Referer": account.workspace_origin() + "/",
    })
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def _cookies_for(account: Account) -> dict[str, str]:
    from urllib.parse import quote
    c: dict[str, str] = {"access_token": account.access_token}
    if account.refresh_token:
        c["refresh_token"] = quote(account.refresh_token, safe="")
    return c


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


def _decode_jwt_exp(jwt: str) -> int:
    """Достаёт exp из JWT payload. 0 если не получилось."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims.get("exp") or 0)
    except Exception:
        return 0


# ---------- Client ----------

class ZoClient:
    """Тонкая обёртка над httpx.AsyncClient. Stateless re: auth."""

    def __init__(
        self,
        base_url: str = ZO_API,
        timeout_connect: float = 30.0,
        timeout_read: float = 900.0,
        jitter: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=timeout_connect,
            read=timeout_read,
            write=60.0,
            pool=30.0,
        )
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self.jitter = jitter
        # Per-account refresh locks (asyncio, lazy)
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        # Per-account XML persona cache
        self._persona_cache: dict[str, str] = {}  # label -> persona_id
        self._persona_active: dict[str, bool] = {}  # label -> True if activated

    def _proxy_for(self, account: Account | None) -> str | None:
        if _proxies is None:
            return None
        try:
            label = getattr(account, "label", None)
            return _proxies.pick_for_account(label)
        except Exception:
            return None

    def _get(self, account: Account | None = None) -> httpx.AsyncClient:
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

    async def _maybe_jitter(self) -> None:
        if self.jitter:
            await asyncio.sleep(jitter_seconds())

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for c in clients:
            try:
                await c.aclose()
            except Exception:
                pass

    # ===================== Token Refresh =====================

    def _refresh_lock_for(self, label: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(label)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[label] = lock
        return lock

    async def maybe_refresh(self, account: Account, *, force: bool = False) -> bool:
        """Proactively refresh if TTL < threshold. Returns True if refreshed."""
        if not account.refresh_token:
            return False
        now = time.time()
        exp = account.expires_at() or 0
        if not force and (exp - now) > REFRESH_THRESHOLD_SECS:
            return False

        lock = self._refresh_lock_for(account.label)
        async with lock:
            # Re-check after lock
            exp = account.expires_at() or 0
            now = time.time()
            if not force and (exp - now) > REFRESH_THRESHOLD_SECS:
                return False
            try:
                new_access, new_refresh = await self._do_refresh(account)
                account.access_token = new_access
                if new_refresh:
                    account.refresh_token = new_refresh
                new_exp = _decode_jwt_exp(new_access)
                log.info("[%s] token refreshed, new TTL %.0fh", account.label, (new_exp - time.time()) / 3600)
                return True
            except Exception as e:
                log.warning("[%s] refresh failed: %s", account.label, e)
                return False

    async def _do_refresh(self, account: Account) -> tuple[str, str | None]:
        """Hit auth.zo.computer/zo/refresh_token. Returns (new_access, new_refresh)."""
        from urllib.parse import quote
        cookies = f"access_token={account.access_token}; refresh_token={quote(account.refresh_token, safe='')}"
        prof = profile_for_account(account.label)
        headers = prof.headers()
        headers.update({
            "Cookie": cookies,
            "Origin": account.workspace_origin(),
            "Referer": account.workspace_origin() + "/",
        })

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
            r = await http.get(f"{ZO_AUTH}{REFRESH_PATH}", headers=headers)

        if r.status_code in (401, 403):
            raise ZoAuthError(f"refresh rejected: {r.status_code} {r.text[:200]}")
        if r.status_code != 204:
            raise ZoServerError(f"refresh failed: {r.status_code} {r.text[:200]}")

        # Parse Set-Cookie headers
        new_access: str | None = None
        new_refresh: str | None = None
        for raw in r.headers.get_list("set-cookie"):
            head = raw.split(";", 1)[0]
            if "=" not in head:
                continue
            name, value = head.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not value:
                continue
            if name == "access_token":
                new_access = value
            elif name == "refresh_token":
                new_refresh = value

        if not new_access:
            raise ZoServerError("refresh returned 204 but no access_token cookie")
        return new_access, new_refresh

    async def _safe_refresh(self, account: Account) -> None:
        """Non-throwing refresh attempt before hot-path requests."""
        try:
            await self.maybe_refresh(account)
        except Exception:
            pass

    # ===================== XML Persona Management =====================

    async def ensure_xml_persona(self, account: Account) -> str | None:
        """Создаёт или находит XML-mode персону, возвращает persona_id.
        Кэшируется per-account. При первом вызове после старта всегда
        пересоздаёт (удаляет старую + создаёт с актуальным промптом)."""
        cached = self._persona_cache.get(account.label)
        if cached:
            return cached

        target_name = persona_name_for(account.label)
        await self._maybe_jitter()

        try:
            personas = await self.list_personas(account)
        except Exception as e:
            log.warning("[%s] list_personas failed: %s", account.label, e)
            return None

        # Delete existing persona with our name (force-recreate with latest prompt)
        for p in personas:
            if (p.get("name") or "") == target_name:
                old_pid = p.get("id")
                if old_pid:
                    log.info("[%s] deleting old XML persona %s to recreate with fresh prompt", account.label, old_pid)
                    await self._maybe_jitter()
                    try:
                        await self._get(account).delete(
                            f"/personas/{old_pid}",
                            headers=_headers_for(account),
                            cookies=_cookies_for(account),
                        )
                    except Exception:
                        pass
                break

        # Create with latest prompt
        await self._maybe_jitter()
        try:
            created = await self.create_persona(
                account, target_name, XML_PERSONA_PROMPT, scopes=[]
            )
            pid = created.get("id") if isinstance(created, dict) else None
            if pid:
                self._persona_cache[account.label] = pid
                log.info("[%s] created XML persona: %s (id=%s)", account.label, target_name, pid)
                return pid
        except Exception as e:
            log.warning("[%s] create_persona failed: %s", account.label, e)
        return None

    async def ensure_xml_mode_active(self, account: Account) -> str | None:
        """Идемпотентно: создаёт XML-персону и активирует для main.
        Кэшируется — повторные вызовы бесплатны."""
        if self._persona_active.get(account.label):
            return self._persona_cache.get(account.label)

        pid = await self.ensure_xml_persona(account)
        if not pid:
            return None

        # Проверяем текущую активную
        try:
            active = await self.get_active_personas(account)
            current_main = active.get("main") if isinstance(active, dict) else None
        except Exception:
            current_main = None

        if current_main != pid:
            await self._maybe_jitter()
            try:
                await self.set_main_persona(account, pid)
            except Exception as e:
                log.warning("[%s] set_main_persona failed: %s", account.label, e)

        self._persona_active[account.label] = True
        return pid

    def clear_persona_cache(self, label: str) -> None:
        """Сбрасывает кэш персоны (например после cooldown)."""
        self._persona_cache.pop(label, None)
        self._persona_active.pop(label, None)

    # ===================== Non-stream API =====================

    async def list_models(self, account: Account) -> list[dict[str, Any]]:
        await self._safe_refresh(account)
        await self._maybe_jitter()
        r = await self._get(account).get(
            "/models/",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json().get("models", [])

    async def list_personas(self, account: Account) -> list[dict[str, Any]]:
        await self._safe_refresh(account)
        await self._maybe_jitter()
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
        body: dict[str, Any] = {"name": name, "prompt": prompt, "image": None}
        if scopes is not None:
            body["scopes"] = scopes
        await self._safe_refresh(account)
        await self._maybe_jitter()
        r = await self._get(account).post(
            "/personas/",
            json=body,
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json()

    async def get_active_personas(self, account: Account) -> dict[str, Any]:
        await self._safe_refresh(account)
        await self._maybe_jitter()
        r = await self._get(account).get(
            "/personas/active",
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        _raise_for_status(r)
        return r.json()

    async def set_main_persona(self, account: Account, persona_id: str) -> bool:
        await self._safe_refresh(account)
        await self._maybe_jitter()
        # Reference uses POST with conversation_type (not PUT without body)
        r = await self._get(account).post(
            f"/personas/active/{persona_id}",
            json={"conversation_type": "main"},
            headers=_headers_for(account),
            cookies=_cookies_for(account),
        )
        if r.status_code != 200:
            # Fallback: try PUT (older Zo API)
            r = await self._get(account).put(
                f"/personas/active/{persona_id}",
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
        if r.status_code != 200:
            log.warning("[%s] set_main_persona: HTTP %d", account.label, r.status_code)
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
        rules = await self.list_rules(account)
        for r in rules:
            if r.get("instruction") == instruction:
                return r.get("id")
        created = await self.create_rule(account, condition, instruction)
        return created.get("id") if isinstance(created, dict) else None

    async def ensure_bridge_persona(
        self, account: Account, name: str, prompt: str
    ) -> str | None:
        """Совместимость со старым bridge_persona.py API.
        Теперь делегирует в ensure_xml_mode_active."""
        pid = await self.ensure_xml_mode_active(account)
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

    async def update_persona_scopes(
        self, account: Account, persona_id: str, scopes: list[str]
    ) -> bool:
        for method in ("PATCH", "PUT", "POST"):
            url = f"/personas/{persona_id}"
            if method == "POST":
                url += "/scopes"
            r = await self._get(account).request(
                method, url, json={"scopes": scopes},
                headers=_headers_for(account),
                cookies=_cookies_for(account),
            )
            if r.status_code == 200:
                return True
        return False

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
        try:
            await self.list_models(account)
            return True
        except Exception:
            return False

    async def fetch_balance(self, account: Account) -> int | None:
        await self._safe_refresh(account)
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

    # ===================== Streaming Chat =====================

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
        context_parts: list[dict] | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any], str | None]]:
        """
        Async generator: yields (event_type, data_dict, conversation_id_header).
        conversation_id_header только на первом yield.
        """
        await self._safe_refresh(account)
        await self._maybe_jitter()

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
        if context_parts:
            body["context_parts"] = context_parts

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
            # Also check response body for conversation_id (some Zo versions
            # return it in SSE events rather than headers)
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
                        # Extract conversation_id from FrontendModelResponse
                        if event_type == "FrontendModelResponse" and isinstance(data, dict):
                            cid = data.get("conversation_id")
                            if cid and not conv_header:
                                conv_header = cid
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
