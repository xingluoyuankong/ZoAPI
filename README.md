# zo-claude-proxy

Локальный прокси: превращает [Zo Computer](https://zo.computer) в совместимый бэкенд для **Claude Code**, **Codex**, **OpenCode** и **Hermes**.

- Claude Code ходит в Anthropic-совместимый `POST /v1/messages`.
- Codex / OpenCode / Hermes ходят в OpenAI-совместимые `POST /v1/chat/completions` и `POST /v1/responses`.
- Всё это на самом деле уходит в твой Zo через **внутренний cookie-based `POST /ask`**, а не через публичный Zo API key.
- Кредиты тратятся на Zo. Отдельная подписка Anthropic/OpenAI для этих CLI не нужна.
- **Multi-account**: можно держать несколько Zo-аккаунтов, прокси ротирует их при ошибках.
- Есть стрим, conversation memory и нормальные ошибки в формате клиента.

---

## Как это работает

У Zo есть два API:

| Эндпоинт | Auth | Кому открыт |
|---|---|---|
| `POST /zo/ask` | `Authorization: Bearer zo_sk_…` | Обычно закрыт у большинства аккаунтов (403) |
| `POST /ask` | Сессионные cookies `access_token` + `refresh_token` | Работает у всех, кто залогинен в чат |

Прокси использует **внутренний `/ask`** с твоими cookies — тот же эндпоинт, что использует веб-чат Zo.

```
┌─────────────┐    Anthropic / OpenAI API    ┌─────────────┐    /ask + cookies    ┌──────────┐
│ Claude Code │ ───────────────────────────▶ │ zo-claude-  │ ───────────────────▶ │   Zo     │
│ Codex       │                              │   proxy     │                      │ Computer │
│ OpenCode    │                              └─────────────┘                      └──────────┘
│ Hermes      │                                     │
└─────────────┘                                     │  Multi-account rotation
       ↑                                            │  + conversation cache
       └────────────── client-compatible SSE ───────┘
```

---

## Что уже умеет

### Anthropic-совместимое
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

### OpenAI-совместимое
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `GET /v1/models`

---

## Установка

Нужен Python 3.10+.

```bash
git clone https://github.com/UvenaliyS/ZoAPI
cd ZoAPI
```

**Windows**:
```cmd
run.bat
```

**macOS / Linux**:
```bash
./run.sh
```

Скрипт сам создаст venv, тихо поставит зависимости и при первом запуске откроет мастер добавления аккаунта.

---

## Добавление аккаунта (cookies)

1. Открой свой Zo workspace (`https://<твой-домен>.zo.computer`)
2. F12 → **Network**
3. Напиши любое сообщение в чат
4. Найди запрос **POST `/ask`**
5. В **Request Headers** скопируй строку **`cookie: …`**
6. Вставь её в `setup.py`

Парсер сам вытащит `access_token` и `refresh_token`.

**Важно:** это полный доступ к твоему Zo. Не публикуй и не коммить `accounts.json`.

---

## Использование

### Claude Code
В одном окне держи прокси:
```bash
./run.sh
```

Во втором:
```bash
./use-claude.sh
```
или на Windows:
```cmd
use-claude.bat
```

Лончер:
- ставит `ANTHROPIC_BASE_URL=http://127.0.0.1:17878`
- ставит `ANTHROPIC_AUTH_TOKEN=zo-proxy`
- очищает локальный `ANTHROPIC_API_KEY`
- на Windows ещё предупреждает, если `ANTHROPIC_*` уже прописаны в shell или в user environment

### Codex
```bash
./use-codex.sh
```
или:
```cmd
use-codex.bat
```

Лончер:
- поднимает локальный `CODEX_HOME`
- пишет туда `config.toml` с `openai_base_url = "http://127.0.0.1:17878/v1"`
- выставляет `OPENAI_API_KEY=zo-proxy`
- по умолчанию ставит модель `gpt-5.3-codex`

### OpenCode
```bash
./use-opencode.sh
```
или:
```cmd
use-opencode.bat
```

Лончер:
- выставляет `OPENAI_API_KEY=zo-proxy`
- выставляет `OPENAI_BASE_URL=http://127.0.0.1:17878/v1`
- прокидывает `OPENCODE_CONFIG_CONTENT` с custom provider на базе `@ai-sdk/openai-compatible`
- по умолчанию использует `zo/gpt-5.3-codex`

### Hermes
```bash
./use-hermes.sh
```
или:
```cmd
use-hermes.bat
```

Лончер:
- выставляет `OPENAI_API_KEY=zo-proxy`
- выставляет `OPENAI_BASE_URL=http://127.0.0.1:17878/v1`
- по умолчанию ставит `HERMES_MODEL=gpt-5.5`

---

## Управление аккаунтами

```bash
./setup-cli.sh
```
или:
```cmd
setup-cli.bat
```

Команды:
- `a` — добавить аккаунт
- `s N` — сделать активным
- `r N` — удалить
- `t` — проверить аккаунты
- `d N` / `e N` — выключить / включить
- `q` — выход

---

## Ротация

- **401 / 403** → мгновенная ротация на другой usable аккаунт
- **5xx / сеть** → ротация после `MAX_ERRORS_BEFORE_ROTATE`
- успешный запрос → streak сбрасывается

Сменить активный аккаунт без рестарта:
```bash
curl -X POST http://127.0.0.1:17878/v1/admin/active \
  -H 'Content-Type: application/json' \
  -d '{"label":"acc2"}'
```

Проверить статус:
```bash
curl http://127.0.0.1:17878/v1/admin/accounts | jq
```

---

## Выбор модели

`config.py` содержит подстрочные алиасы для типовых запросов от Claude/Codex/OpenCode/Hermes:

```python
MODEL_MAP = {
    "claude-opus": "zo:anthropic/claude-opus-4-7",
    "claude-sonnet": "zo:anthropic/claude-sonnet-4-6",
    "gpt-5.5": "zo:openai/gpt-5.5",
    "codex": "zo:openai/gpt-5.3-codex",
    "gpt-5.4-mini": "zo:openai/gpt-5.4-mini",
    "gemini": "zo:google/gemini-3.1-pro-preview",
    "deepseek": "zo:deepseek/deepseek-v4-pro",
    "glm": "zo:zai/glm-5",
}
```

Список реально доступных моделей:
```bash
curl http://127.0.0.1:17878/v1/models | jq '.data[] | {id, display_name, vendor}'
```

---

## Что с тулами

- Claude Code tools сейчас пробрасываются как текстовое описание в промпт.
- OpenAI-совместимые клиенты тоже пока сводятся к текстовому контексту, а не к полноценному серверному tool-calling мосту.
- Для обычного coding/chat потока этого хватает, но сложные tool-call цепочки могут работать неидеально.

---

## Файлы проекта

```
proxy.py              # FastAPI сервер
accounts.py           # multi-account store и ротация
zo_client.py          # клиент к Zo /ask
anthropic_sse.py      # Zo SSE -> Anthropic SSE
openai_sse.py         # Zo SSE -> OpenAI Chat/Responses SSE
setup.py              # интерактивный мастер аккаунтов
config.py             # порт, MODEL_MAP и дефолты
run.bat / run.sh
setup-cli.bat / setup-cli.sh
use-claude.bat / use-claude.sh
use-codex.bat / use-codex.sh
use-opencode.bat / use-opencode.sh
use-hermes.bat / use-hermes.sh
```

---

## Известные ограничения

- **Auto-refresh JWT** пока нет — когда cookie-сессия протухнет, просто пере-добавь аккаунт через setup.
- Вложения картинок/файлов пока режутся до текстового суррогата.
- Tool use мост пока не идеален, особенно вне Claude Code.
- Реальная совместимость лучше всего на текстовых и обычных coding-сценариях; экзотические фичи конкретных CLI могут ожидать ещё больше полей.

---

## Безопасность

- `accounts.json` и `.codex-home/` не коммитятся.
- Токены живут только локально.
- Если cookie утёк — разлогинься и залогинься заново в Zo.

---

## Лицензия

MIT
