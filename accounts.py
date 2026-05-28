"""
Multi-account store для zo-claude-proxy.

Хранит несколько Zo-аккаунтов в accounts.json. Каждый аккаунт — это пара
(access_token, refresh_token) + домен пользователя. Прокси при работе
держит "активный" аккаунт, и при N ошибках подряд переключается на
следующий из пула (omnirouter-style).

Формат accounts.json:
{
  "active": "main",
  "accounts": [
    {
      "label": "main",
      "domain": "user",
      "access_token": "eyJ...",
      "refresh_token": "...",
      "added_at": "2026-05-28T13:00:00Z",
      "last_ok_at": "2026-05-28T13:20:00Z",
      "last_err": null,
      "error_streak": 0,
      "disabled": false
    }
  ]
}
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Локация файла accounts.json
# ---------------------------------------------------------------------------

ACCOUNTS_FILE = Path(__file__).resolve().parent / "accounts.json"


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@dataclass
class Account:
    label: str
    domain: str
    access_token: str
    refresh_token: str = ""
    added_at: str = ""
    last_ok_at: str | None = None
    last_err: str | None = None
    error_streak: int = 0
    disabled: bool = False
    balance_cents: int | None = None
    balance_checked_at: float | None = None
    # ID персоны zoapi-bridge на этом аккаунте. Создаётся автоматически
    # при первом запуске и хранится тут, чтобы не пересоздавать каждый раз.
    # Эта персона имеет scopes=[] — никаких серверных тулов Zo — и
    # хардкод-промпт, который убивает Zo-идентичность и заставляет модель
    # эмитить <zo:call> теги для тулов клиента.
    bridge_persona_id: str | None = None
    bridge_persona_checked_at: float | None = None
    api_key: str | None = None
    api_key_id: str | None = None

    # ------ JWT helpers ------

    def jwt_payload(self) -> dict[str, Any]:
        """Декодирует payload JWT access_token. Не проверяет подпись."""
        try:
            parts = self.access_token.split(".")
            if len(parts) < 2:
                return {}
            pad = "=" * (-len(parts[1]) % 4)
            raw = base64.urlsafe_b64decode(parts[1] + pad)
            return json.loads(raw)
        except Exception:
            return {}

    def expires_at(self) -> int | None:
        exp = self.jwt_payload().get("exp")
        return int(exp) if exp else None

    def seconds_until_expiry(self) -> int | None:
        exp = self.expires_at()
        return None if exp is None else exp - int(time.time())

    def is_expired(self, leeway: int = 60) -> bool:
        s = self.seconds_until_expiry()
        return s is not None and s < leeway

    def email(self) -> str:
        return self.jwt_payload().get("properties", {}).get("email", "")

    def workspace_origin(self) -> str:
        return f"https://{self.domain}.zo.computer"

    # ------ status flags ------

    def is_usable(self) -> bool:
        return bool(self.access_token) and not self.disabled

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(
            label=d["label"],
            domain=d["domain"],
            access_token=d["access_token"],
            refresh_token=d.get("refresh_token", ""),
            added_at=d.get("added_at", ""),
            last_ok_at=d.get("last_ok_at"),
            last_err=d.get("last_err"),
            error_streak=d.get("error_streak", 0),
            disabled=d.get("disabled", False),
            balance_cents=d.get("balance_cents"),
            balance_checked_at=d.get("balance_checked_at"),
            bridge_persona_id=d.get("bridge_persona_id"),
            bridge_persona_checked_at=d.get("bridge_persona_checked_at"),
            api_key=d.get("api_key"),
            api_key_id=d.get("api_key_id"),
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AccountStore:
    """
    Thread-safe storage для аккаунтов.
    Сериализуется в accounts.json при каждом изменении.
    """

    def __init__(self, path: Path = ACCOUNTS_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.accounts: list[Account] = []
        self.active_label: str | None = None
        self.mode: str = "fixed"  # "fixed" | "rotation"
        self._rr_idx: int = 0
        self.load()

    # ------ disk I/O ------

    def load(self) -> None:
        with self._lock:
            self.accounts = []
            self.active_label = None
            self.mode = "fixed"
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return
            self.accounts = [
                Account.from_dict(d) for d in data.get("accounts", [])
            ]
            self.active_label = data.get("active") or (
                self.accounts[0].label if self.accounts else None
            )
            self.mode = data.get("mode", "fixed")

    def save(self) -> None:
        with self._lock:
            data = {
                "active": self.active_label,
                "mode": self.mode,
                "accounts": [a.to_dict() for a in self.accounts],
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    # ------ CRUD ------

    def add(self, account: Account, make_active: bool = True) -> None:
        with self._lock:
            self.accounts = [a for a in self.accounts if a.label != account.label]
            self.accounts.append(account)
            if make_active or self.active_label is None:
                self.active_label = account.label
            self.save()

    def remove(self, label: str) -> bool:
        with self._lock:
            before = len(self.accounts)
            self.accounts = [a for a in self.accounts if a.label != label]
            if not self.accounts:
                self.active_label = None
            elif self.active_label == label:
                self.active_label = self.accounts[0].label
            removed = len(self.accounts) != before
            if removed:
                self.save()
            return removed

    def set_active(self, label: str) -> bool:
        with self._lock:
            if not any(a.label == label for a in self.accounts):
                return False
            self.active_label = label
            self.save()
            return True

    def get(self, label: str) -> Account | None:
        with self._lock:
            for a in self.accounts:
                if a.label == label:
                    return a
            return None

    def get_active(self) -> Account | None:
        with self._lock:
            if not self.active_label:
                return self.accounts[0] if self.accounts else None
            return self.get(self.active_label) or (
                self.accounts[0] if self.accounts else None
            )

    def list_usable(self) -> list[Account]:
        with self._lock:
            return [a for a in self.accounts if a.is_usable()]

    # ------ status updates ------

    def mark_ok(self, label: str) -> None:
        with self._lock:
            a = self.get(label)
            if not a:
                return
            a.last_ok_at = _iso_now()
            a.last_err = None
            a.error_streak = 0
            self.save()

    def mark_err(self, label: str, err: str, max_streak: int) -> bool:
        """
        Регистрирует ошибку у аккаунта. Возвращает True если accountу пора
        ротироваться (error_streak достиг max_streak).
        """
        with self._lock:
            a = self.get(label)
            if not a:
                return False
            a.last_err = err[:300]
            a.error_streak += 1
            should_rotate = a.error_streak >= max_streak
            self.save()
            return should_rotate

    def update_tokens(
        self,
        label: str,
        access_token: str,
        refresh_token: str | None = None,
    ) -> None:
        with self._lock:
            a = self.get(label)
            if not a:
                return
            a.access_token = access_token
            if refresh_token:
                a.refresh_token = refresh_token
            a.last_err = None
            self.save()

    def disable(self, label: str, reason: str) -> None:
        with self._lock:
            a = self.get(label)
            if not a:
                return
            a.disabled = True
            a.last_err = reason[:300]
            self.save()

    def enable(self, label: str) -> None:
        with self._lock:
            a = self.get(label)
            if not a:
                return
            a.disabled = False
            a.error_streak = 0
            a.last_err = None
            self.save()

    # ------ rotation ------

    def rotate_after_error(self, label: str) -> Account | None:
        """
        Переключается на следующий usable аккаунт по кругу.
        Возвращает нового active, или None если других нет.
        """
        with self._lock:
            usable = [
                a for a in self.accounts if a.is_usable() and a.label != label
            ]
            if not usable:
                return None
            next_acc = usable[0]
            self.active_label = next_acc.label
            self.save()
            return next_acc

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = "rotation" if mode == "rotation" else "fixed"
            self.save()

    def current(self) -> Account | None:
        """
        Возвращает аккаунт, который нужно использовать для следующего запроса.
        mode=fixed → get_active(); mode=rotation → round-robin по usable.
        """
        with self._lock:
            if self.mode != "rotation":
                return self.get_active()
            usable = [a for a in self.accounts if a.is_usable()]
            if not usable:
                return None
            self._rr_idx = (self._rr_idx + 1) % len(usable)
            return usable[self._rr_idx]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def parse_cookie_header(raw: str) -> dict[str, str]:
    """
    Парсит вставленную целиком cookie-строку из DevTools.
    Возвращает dict {name: value}.
    """
    out: dict[str, str] = {}
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()
    return out


def extract_tokens_from_cookie(raw: str) -> tuple[str, str]:
    """
    Из вставленного Cookie-хедера достаёт (access_token, refresh_token).
    refresh_token URL-decoded.
    """
    from urllib.parse import unquote

    cookies = parse_cookie_header(raw)
    access = cookies.get("access_token", "")
    refresh = unquote(cookies.get("refresh_token", ""))
    return access, refresh


def extract_domain_from_access_token(access_token: str) -> str | None:
    """Возвращает первый домен из payload.properties.domains."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        payload = json.loads(raw)
        domains = payload.get("properties", {}).get("domains") or []
        return domains[0] if domains else None
    except Exception:
        return None


def clean_domain(domain: str | None) -> str:
    """Нормализуем домен: убираем пробелы, обратные слэши, кавычки, лишние слэши."""
    if not domain:
        return ""
    s = str(domain).strip().strip('"\'')
    s = s.replace("\\", "").replace("//", "/")
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    return s.strip()
