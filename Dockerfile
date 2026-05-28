# ZoAPI proxy container.
#
# Содержит ТОЛЬКО HTTP-прокси (proxy.py) — Anthropic/OpenAI/Responses
# роуты + ротация аккаунтов. Никаких браузеров внутри: Playwright/Patchright
# здесь не ставятся, потому что логин в Zo делается ВРУЧНУЮ на хосте через
# `run.bat` / `run.sh` (там нативное GUI-окно браузера). После логина
# accounts.json пробрасывается в контейнер volume-маунтом, и прокся берёт
# токены оттуда.
#
# Сборка/запуск делается скриптами docker_run.bat / docker_run.sh.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Минимальные системные зависимости для httpx/uvicorn и стандартного TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Ставим Python-зависимости БЕЗ playwright/patchright — браузер живёт на хосте.
COPY requirements.txt /tmp/requirements.txt
RUN grep -ivE '^(playwright|patchright|questionary|rich)\b' /tmp/requirements.txt \
        > /tmp/requirements-docker.txt \
    && pip install -r /tmp/requirements-docker.txt \
    && rm -f /tmp/requirements.txt /tmp/requirements-docker.txt

# Копируем только то, что нужно прокси (без TUI/installers/setup/run).
COPY proxy.py            ./proxy.py
COPY anthropic_sse.py    ./anthropic_sse.py
COPY openai_sse.py       ./openai_sse.py
COPY tool_parser.py      ./tool_parser.py
COPY zo_client.py        ./zo_client.py
COPY accounts.py         ./accounts.py
COPY runtime.py          ./runtime.py
COPY config.py           ./config.py

# accounts.json / runtime.json подтягиваем volume-маунтом из хоста — не COPY.
# Если контейнер запустили без маунта — proxy.py поднимется и будет ждать
# аккаунт (печатает «No accounts yet» в баннере).

EXPOSE 17878

# Healthcheck через /health роут (не требует аккаунта).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:17878/health >/dev/null || exit 1

# Запускаем прокси напрямую (без launcher.py — он TUI и требует TTY/браузер).
CMD ["python", "proxy.py"]
