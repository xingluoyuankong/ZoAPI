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
- `WS  /v1/responses`
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

Скрипт сам:
- создаст venv
- тихо поставит зависимости
- поднимет локальный прокси
- покажет меню запуска клиента
- запомнит последний выбор в `launcher_state.json`

---

## Использование

Теперь отдельные `use-*.bat` не нужны.

Запускаешь просто:

**Windows**:
```cmd
run.bat
```

**macOS / Linux**:
```bash
./run.sh
```

Дальше меню:
- `1` — Claude Code
- `2` — Codex
- `3` — OpenCode
- `4` — Hermes
- `5` — аккаунты / ротация
- `Enter` — повторить последний выбранный клиент

Последний выбор запоминается, но меню всё равно показывается каждый раз.

Что делает запускатель:
- для **Claude Code** выставляет `ANTHROPIC_BASE_URL=http://127.0.0.1:17878`, `ANTHROPIC_AUTH_TOKEN=zo-proxy`, очищает локальный `ANTHROPIC_API_KEY`
- для **Codex** пишет локальный `CODEX_HOME/config.toml` с `openai_base_url = "http://127.0.0.1:17878/v1"`
- для **OpenCode** прокидывает `OPENCODE_CONFIG_CONTENT` с custom OpenAI-compatible provider
- для **Hermes** выставляет `OPENAI_BASE_URL` и `OPENAI_API_KEY`

---

## Управление аккаунтами

Из главного меню нажми `5`, либо отдельно:

```bash
./setup-cli.sh
```
или:
```cmd
setup-cli.bat
```

Команды:
- `a` — добавить аккаунт
- `s N` — сделать аккаунт активным
- `m` — переключить режим `fixed / rotation`
- `t` — проверить аккаунты
- `d N` / `e N` — выключить / включить
- `r N` — удалить
- `q` — выход

Режимы:
- `fixed` — используется выбранный активный аккаунт
- `rotation` — usable аккаунты идут по кругу

После добавления аккаунта меню не закрывается — просто возвращает назад в список.

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
launcher.py           # общий запускатель и меню клиентов
config.py             # порт, MODEL_MAP и дефолты
run.bat / run.sh
setup-cli.bat / setup-cli.sh
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
