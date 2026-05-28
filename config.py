"""
Глобальные настройки zo-claude-proxy.

Сами аккаунты Zo лежат в accounts.json (управляй через `python setup.py`),
здесь только дефолты прокси.
"""

# === Сетевое ===

# Где Zo держит чат-API (внутренний /ask). Менять не надо.
ZO_BASE_URL = "https://api.zo.computer"

# OAuth-провайдер Zo (для будущего refresh-флоу). Менять не надо.
ZO_AUTH_URL = "https://auth.zo.computer"

# На каком порту крутить локальный прокси.
PROXY_PORT = 17878


# === Модели ===

# Модель Zo по умолчанию (когда клиент не указал явно).
# Полный список: GET https://api.zo.computer/models/available
ZO_DEFAULT_MODEL = "zo:anthropic/claude-opus-4-7"

# Короткие алиасы. Ключ — ТОЧНОЕ имя (case-insensitive), не подстрока.
# Полные имена моделей (например claude-opus-4-8-20251210, gpt-5-codex)
# идут пассивом — proxy.py сам маршрутизирует их через провайдера по
# префиксу, поэтому добавлять сюда каждую новую версию НЕ надо.
MODEL_MAP = {
    "opus": "zo:anthropic/claude-opus-4-7",
    "sonnet": "zo:anthropic/claude-sonnet-4-6",
    "haiku": "zo:anthropic/claude-haiku-4-6",
    "codex": "zo:openai/gpt-5-codex",
    "mini": "zo:openai/gpt-5-mini",
    "gemini": "zo:google/gemini-3.0-pro",
    "grok": "zo:xai/grok-4",
    "deepseek": "zo:deepseek/deepseek-v3",
}


# === Поведение прокси ===

# Сколько ошибок подряд от аккаунта прежде чем ротировать на следующий.
MAX_ERRORS_BEFORE_ROTATE = 3

# Какие пути в твоей Zo-рабочей подсветить (необязательно).
# Эквивалент `expanded_paths` из чата. Помогает Zo лучше понять контекст.
EXPANDED_PATHS: list[str] = []

# Скрывать thinking-блоки в ответе (сейчас всё равно реэхим как text).
HIDE_THINKING = False

# Логирование: DEBUG / INFO / WARNING
LOG_LEVEL = "INFO"
