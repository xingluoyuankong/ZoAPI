# zo-claude-proxy

Локальный прокси: превращает [Zo Computer](https://zo.computer) в совместимый бэкенд для **Claude Code**, **Codex** (CLI + desktop), **OpenCode** и **Hermes**.

- Красивый TUI-лончер в терминале: панели, таблицы, цвета, стрелки ↑↓, Enter.
- Авторизация аккаунта теперь идёт через **временный Playwright Chromium** с отдельным чистым профилем.
- Claude Code ходит в Anthropic-совместимый `POST /v1/messages`.
- Codex / OpenCode / Hermes ходят в OpenAI-совместимые `POST /v1/chat/completions`, `POST /v1/responses` и `WS /v1/responses`.
- Всё это уходит в твой Zo через **внутренний cookie-based `POST /ask`** (тот же эндпоинт, что использует веб-чат Zo), не через `zo_sk_…` API ключ.
- Кредиты тратятся на Zo. Отдельная подписка Anthropic / OpenAI не нужна.
- **Multi-account**: можно держать несколько Zo-аккаунтов, прокси ротирует при ошибках.

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
- найдёт Python 3.10+ (`py -3` / `python` / `python3`)
- создаст `.venv`, если её ещё нет
- каждый запуск быстро проверит нужные пакеты
- если чего-то не хватает — тихо доставит из `requirements.txt`
- откроет TUI-меню

---

## Использование

Один лончер — никаких отдельных `use-*` батников.

```
zo-claude-proxy — что запускаем?
 · ▶ Claude Code
   ▶ Codex
   ▶ OpenCode
   ▶ Hermes
   ─────────────────────
   ⚙ Аккаунты (1 шт, режим=fixed, активный=main)
   ✕ Выход
```

- стрелки ↑↓ — выбор
- `Enter` — подтвердить
- последний клиент запоминается (`launcher_state.json`) и подсвечивается дефолтом

Что лончер делает за тебя:
- **Claude Code** → `ANTHROPIC_BASE_URL=http://127.0.0.1:17878`, `ANTHROPIC_AUTH_TOKEN=zo-proxy`, локально чистит `ANTHROPIC_API_KEY`
- **Codex CLI** → пишет локальный `CODEX_HOME/config.toml` с `openai_base_url = "http://127.0.0.1:17878/v1"`
- **OpenCode** → передаёт `OPENCODE_CONFIG_CONTENT` с custom OpenAI-compatible провайдером
- **Hermes** → `OPENAI_BASE_URL` + `OPENAI_API_KEY`

### Codex desktop

Codex как приложение тоже работает — он говорит на тех же `/v1/responses` (HTTP + WebSocket). Один раз в настройках Codex desktop:

- Base URL: `http://127.0.0.1:17878/v1`
- API key: `zo-proxy` (любая непустая строка)
- Model: `gpt-5.3-codex` (или другая, см. `config.py → MODEL_MAP`)

Запусти проксю через `run.bat` / `./run.sh` (выбери любого клиента, либо пункт `Аккаунты` — главное, чтобы прокся была поднята) и держи окно открытым.

---

## Аккаунты

В главном меню `Accounts`:

- `Add account via temporary browser`
- `Switch mode: fixed / rotation`
- `Set active account`
- `Enable / disable account`
- `Delete account`
- `Ping and check balances`

Флоу добавления аккаунта:
1. Лончер при необходимости ставит Playwright Chromium
2. Открывает **отдельный временный Chromium**
3. Ты логинишься в Zo
4. Как только появляются `access_token` + `refresh_token`, окно закрывается автоматически
5. Лончер сразу проверяет аккаунт, подтягивает баланс/модели и сохраняет его

Твой обычный Chrome/Edge/Firefox не трогаются.

### Режимы

- `fixed` — используется выбранный активный аккаунт
- `rotation` — usable аккаунты идут по кругу при ошибках

Сменить активный аккаунт без рестарта прокси:

```bash
curl -X POST http://127.0.0.1:17878/v1/admin/active \
  -H 'Content-Type: application/json' \
  -d '{"label":"acc2"}'
```

Статус всех:

```bash
curl http://127.0.0.1:17878/v1/admin/accounts | jq
```

---

## Эндпоинты

### Anthropic-совместимое
- `POST /v1/messages`
- `GET  /v1/models`
- `GET  /health`

### OpenAI-совместимое
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `WS   /v1/responses` (Codex `responses_websocket` транспорт)
- `GET  /v1/models`

---

## Файлы

```
run.bat / run.sh      # единая точка входа
utils/launcher.py     # TUI-лончер (questionary)
proxy.py              # FastAPI сервер
accounts.py           # multi-account store + ротация
zo_client.py          # клиент к Zo /ask
anthropic_sse.py      # Zo SSE → Anthropic SSE
openai_sse.py         # Zo SSE → OpenAI Chat / Responses SSE / WS
config.py             # порт, MODEL_MAP и дефолты
requirements.txt      # зависимости
```

---

## Известные ограничения

- **Auto-refresh JWT** пока нет — когда cookie-сессия протухнет (~30 дней), пере-добавь аккаунт.
- Вложения картинок/файлов пока режутся до текстового суррогата.
- Tool-call мост пока не идеален, особенно вне Claude Code.

---

## Безопасность

- `accounts.json`, `.codex-home/` и временный browser profile не коммитятся.
- Токены живут только локально.
- Если cookie утёк — разлогинься и залогинься заново в Zo.

---

## Лицензия

MIT
