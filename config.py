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

# Мэппинг: что клиент просит → что отправляем в Zo.
# Ключ — подстрока в названии модели из запроса.
MODEL_MAP = {
    "claude-opus": "zo:anthropic/claude-opus-4-7",
    "claude-sonnet": "zo:anthropic/claude-sonnet-4-6",
    "claude-haiku": "zo:anthropic/claude-opus-4-7",
    "opus": "zo:anthropic/claude-opus-4-7",
    "sonnet": "zo:anthropic/claude-sonnet-4-6",
    "haiku": "zo:anthropic/claude-opus-4-7",
    "gpt-5.5": "zo:openai/gpt-5.5",
    "gpt-5.4-mini": "zo:openai/gpt-5.4-mini",
    "gpt-5.3-codex": "zo:openai/gpt-5.3-codex",
    "gpt-5.3": "zo:openai/gpt-5.3-codex",
    "gpt-5-mini": "zo:openai/gpt-5.4-mini",
    "gpt-5": "zo:openai/gpt-5.5",
    "codex": "zo:openai/gpt-5.3-codex",
    "o4-mini": "zo:openai/gpt-5.4-mini",
    "o3": "zo:openai/gpt-5.5",
    "mini": "zo:openai/gpt-5.4-mini",
    "gemini": "zo:google/gemini-3.1-pro-preview",
    "deepseek": "zo:deepseek/deepseek-v4-pro",
    "glm": "zo:zai/glm-5",
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
