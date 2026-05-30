"""
Browser fingerprint profiles — пул реалистичных Chrome/Firefox/Safari/Edge
header-сетов. Каждый аккаунт стабильно получает один профиль (md5(handle) → index).

Так запросы от разных аккаунтов выглядят как разные браузеры.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass


@dataclass
class BrowserProfile:
    name: str
    user_agent: str
    accept_language: str
    sec_ch_ua: str | None = None
    sec_ch_ua_mobile: str | None = None
    sec_ch_ua_platform: str | None = None

    def headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
            "Accept": "text/event-stream, application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if self.sec_ch_ua:
            h["Sec-CH-UA"] = self.sec_ch_ua
            h["Sec-CH-UA-Mobile"] = self.sec_ch_ua_mobile or "?0"
            h["Sec-CH-UA-Platform"] = self.sec_ch_ua_platform or '"Windows"'
        return h


PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        name="chrome-win11",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        sec_ch_ua='"Not?A_Brand";v="99", "Google Chrome";v="142", "Chromium";v="142"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        name="chrome-mac",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        sec_ch_ua='"Not?A_Brand";v="99", "Google Chrome";v="142", "Chromium";v="142"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"macOS"',
    ),
    BrowserProfile(
        name="chrome-linux",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        accept_language="en-US,en;q=0.9",
        sec_ch_ua='"Not?A_Brand";v="99", "Google Chrome";v="142", "Chromium";v="142"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Linux"',
    ),
    BrowserProfile(
        name="brave-win11",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
        accept_language="en-GB,en;q=0.9",
        sec_ch_ua='"Brave";v="141", "Not?A_Brand";v="99", "Chromium";v="141"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        name="edge-win11",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
        ),
        accept_language="en-US,en;q=0.9",
        sec_ch_ua='"Microsoft Edge";v="142", "Not?A_Brand";v="99", "Chromium";v="142"',
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='"Windows"',
    ),
    BrowserProfile(
        name="firefox-win11",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
            "Gecko/20100101 Firefox/140.0"
        ),
        accept_language="en-US,en;q=0.5",
    ),
    BrowserProfile(
        name="firefox-mac",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:140.0) "
            "Gecko/20100101 Firefox/140.0"
        ),
        accept_language="en-US,en;q=0.5",
    ),
    BrowserProfile(
        name="safari-mac",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.3 Safari/605.1.15"
        ),
        accept_language="en-US,en;q=0.9",
    ),
]


def profile_for_account(label: str) -> BrowserProfile:
    """Стабильный профиль для аккаунта. Один и тот же label всегда
    получает один и тот же fingerprint."""
    digest = hashlib.md5(label.encode("utf-8")).digest()
    idx = digest[0] % len(PROFILES)
    return PROFILES[idx]


def persona_suffix(label: str) -> str:
    """8-символьный hex-суффикс для имени персоны (стабильный per-account)."""
    return hashlib.md5(label.encode("utf-8")).hexdigest()[:8]


PERSONA_PREFIXES = ("coder", "helper", "dev", "assistant", "buddy", "scribe", "agent", "tinker")


def persona_name_for(label: str) -> str:
    """Нейтральное стабильное имя персоны: `coder_a1b2c3d4`.
    Рассредоточено по нескольким префиксам, чтобы не палиться."""
    digest = hashlib.md5(label.encode("utf-8")).digest()
    prefix = PERSONA_PREFIXES[digest[1] % len(PERSONA_PREFIXES)]
    return f"{prefix}_{persona_suffix(label)}"


def jitter_seconds(min_s: float = 0.15, max_s: float = 1.0) -> float:
    """Небольшая случайная задержка между запросами от одного аккаунта."""
    return random.uniform(min_s, max_s)
