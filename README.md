# zo-claude-proxy

Локальный прокси: превращает [Zo Computer](https://zo.computer) в Anthropic-совместимый бэкенд для **Claude Code CLI**.

- Claude Code думает что говорит с `api.anthropic.com`, а на самом деле — с твоим Zo.
- Модели Zo (Opus 4.7, Sonnet 4.6, GPT-5.5, Gemini 3.1, DeepSeek V4, GLM 5 и др.) доступны прямо в Claude Code.
- Кредиты тратятся на Zo. Подписка Anthropic **не нужна**.
- **Multi-account**: добавляй сколько угодно Zo-аккаунтов, прокси автоматически ротирует при ошибках (omnirouter-style).
- Стрим, conversation memory, понятные ошибки в формате Anthropic.

---

## Как это работает

У Zo есть два API:

| Эндпоинт | Auth | Кому открыт |
|---|---|---|
| `POST /zo/ask` | `Authorization: Bearer zo_sk_…` | Обычно закрыт у большинства аккаунтов (403) |
| `POST /ask` | Сессионные cookies `access_token` + `refresh_token` | Работает у всех, кто залогинен в чат |

Прокси использует **внутренний `/ask`** с твоими cookies — тот же эндпоинт, что и веб-чат `<твой-домен>.zo.computer`.

```
┌─────────────┐    Anthropic API    ┌─────────────┐    /ask + cookies    ┌──────────┐
│ Claude Code │ ──────────────────▶ │ zo-claude-  │ ───────────────────▶ │   Zo     │
│    CLI      │                     │   proxy     │                      │ Computer │
└─────────────┘                     └─────────────┘                      └──────────┘
       ↑                                  │
       │     Anthropic SSE                │  Multi-account rotation
       └──────────────────────────────────┘  при ошибках
```

---

## Установка

Нужен Python 3.10+ и (опционально) Node для Claude Code CLI.

```bash
git clone https://github.com/UvenaliyS/ZoAPI
cd ZoAPI
```

**Windows**:
```cmd
run.bat
```
Скрипт сам создаст venv, поставит зависимости и при первом запуске откроет мастер добавления аккаунта.

**macOS / Linux**:
```bash
./run.sh
```

Когда увидишь приглашение — следуй инструкциям по добавлению аккаунта (см. ниже).

---

## Добавление аккаунта (cookies)

1. Открой свой Zo workspace (`https://<твой-домен>.zo.computer`)
2. F12 → **Network**
3. Напиши любое сообщение в чат
4. Найди запрос **POST `/ask`** (Type: `event-stream`)
5. **Headers** → раздел **Request Headers** → найди строку **`cookie: …`**
6. ПКМ → **Copy value** (или просто выдели и скопируй)
7. В мастере setup'а вставь эту строку, нажми Enter дважды
8. Подтверди label и домен — готово

Парсер сам выдернет из cookie-строки `access_token` (JWT, ~30 дней TTL) и `refresh_token`.

**Безопасность**: эти токены дают полный доступ к твоему Zo. Не публикуй их и не коммить `accounts.json` (он в `.gitignore`).

---

## Использование с Claude Code

Установи Claude Code если не установлен:
```bash
npm install -g @anthropic-ai/claude-code
```

Запусти прокси (в одном окне):
```bash
./run.sh        # или run.bat
```

В другом окне запусти Claude Code, направленный на прокси:
```bash
./use-claude.sh   # или use-claude.bat
```

Эти скрипты выставляют:
- `ANTHROPIC_BASE_URL=http://127.0.0.1:17878`
- `ANTHROPIC_AUTH_TOKEN=zo-proxy` (любая непустая строка)
- `ANTHROPIC_API_KEY=` (пустая — иначе CLI пойдёт мимо прокси к Anthropic)

Локально, без скриптов:
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:17878 \
ANTHROPIC_AUTH_TOKEN=zo-proxy \
ANTHROPIC_API_KEY="" \
claude
```

---

## Управление аккаунтами

Открыть интерактивный CLI:
```bash
./setup-cli.sh        # mac/linux
setup-cli.bat         # windows
```

Команды:
- `a` — добавить аккаунт (вставка cookies)
- `s N` — сделать активным аккаунт #N
- `r N` — удалить
- `t` — пингануть все аккаунты (`/models/available`)
- `d N` / `e N` — отключить / включить
- `q` — выход

Состояние каждого аккаунта: e-mail, домен, TTL access_token, error streak, on/off.

---

## Ротация

Прокси ведёт счётчик ошибок (`error_streak`) на каждом аккаунте:

- **Auth-ошибки (401) / 403** → ротация **сразу** на следующий usable аккаунт (нет смысла повторять с тем же токеном).
- **Сетевые / 5xx** → ротация после `MAX_ERRORS_BEFORE_ROTATE` (по умолчанию 3) подряд.
- Успешный запрос → счётчик сбрасывается.

Если ни один аккаунт не отвечает — Claude Code получит чистую Anthropic-форматированную ошибку (`authentication_error` / `api_error`).

Сменить активный аккаунт без рестарта прокси:
```bash
curl -X POST http://127.0.0.1:17878/v1/admin/active \
  -H 'Content-Type: application/json' \
  -d '{"label":"acc2"}'
```

Посмотреть статус:
```bash
curl http://127.0.0.1:17878/v1/admin/accounts | jq
```

---

## Выбор модели

Claude Code обычно просит `claude-sonnet-4-5`, `claude-opus-4-…` и т.п. — прокси находит подходящую модель Zo через `MODEL_MAP` в `config.py`:

```python
MODEL_MAP = {
    "opus":     "zo:anthropic/claude-opus-4-7",
    "sonnet":   "zo:anthropic/claude-sonnet-4-6",
    "haiku":    "zo:anthropic/claude-opus-4-7",
    "gpt-5":    "zo:openai/gpt-5.5",
    "codex":    "zo:openai/gpt-5.3-codex",
    "mini":     "zo:openai/gpt-5.4-mini",
    "gemini":   "zo:google/gemini-3.1-pro-preview",
    "deepseek": "zo:deepseek/deepseek-v4-pro",
    "glm":      "zo:zai/glm-5",
}
```

Совпадение по подстроке в имени. Если ничего не подошло — используется `ZO_DEFAULT_MODEL`.

Список реально доступных тебе моделей:
```bash
curl http://127.0.0.1:17878/v1/models | jq '.data[] | {id, display_name, tier}'
```

---

## Что про тулы Claude Code

Claude Code присылает свои локальные тулы (`Read`, `Edit`, `Bash`, `Glob`, `Grep`, …) в каждом запросе. Прокси сейчас передаёт их **как описание в системном промпте** Zo. Zo отвечает текстом, и Claude Code выполняет файловые операции локально на твоей машине.

Полная нативная конвертация Zo `tool_call` ↔ Anthropic `tool_use` через стрим **частично** реализована (см. `anthropic_sse.py`). На крупных тул-цепочках Zo иногда пытается использовать собственные серверные тулы (потому что инструкция «не используй свои» — мягкая). Если столкнёшься с этим — отключи персону или используй отдельную минимальную персону для Claude Code.

---

## Файлы проекта

```
proxy.py            # HTTP сервер (FastAPI + Uvicorn)
accounts.py         # multi-account хранилище и ротация
zo_client.py        # клиент к /ask со стримом
anthropic_sse.py    # конвертер Zo SSE → Anthropic SSE
setup.py            # интерактивный CLI
config.py           # глобальные дефолты (порт, MODEL_MAP)
accounts.json       # хранилище сессий (gitignored, создаётся setup'ом)
run.bat / run.sh
use-claude.bat / use-claude.sh
setup-cli.bat / setup-cli.sh
```

---

## Известные ограничения

- **Auto-refresh JWT** ещё не реализован — когда `access_token` истечёт (~30 дней), пересоздай аккаунт через setup. Видно в `setup.py` колонке TTL.
- Картинки/файлы во входе пока не пробрасываются.
- Тулы Claude Code (см. выше) ходят с оговорками.

---

## Безопасность

- `accounts.json` лежит локально, в `.gitignore`.
- Токены кладёшь только в свой собственный setup — никуда не шлёшь.
- Если ты делился своим cookie-хедером для отладки (например с другим разработчиком) — пересоздай сессию: logout + login в браузере. Старые токены сразу инвалидируются.

---

## Лицензия

MIT
