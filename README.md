# zo-claude-proxy

Локальный прокси: превращает [Zo Computer](https://zo.computer) в совместимый бэкенд для **Claude Code**, **Codex** (CLI + desktop), **OpenCode** и **Hermes**.

- Claude Code ходит в Anthropic-совместимый `POST /v1/messages`.
- Codex / OpenCode / Hermes ходят в OpenAI-совместимые `POST /v1/chat/completions`, `POST /v1/responses` и `WS /v1/responses`.
- Всё это уходит в твой Zo через **внутренний cookie-based `POST /ask`** (тот же эндпоинт, что использует веб-чат Zo), не через `zo_sk_…` API ключ.
- Кредиты тратятся на Zo. Отдельная подписка Anthropic / OpenAI не нужна.
- **Multi-account**: можно держать несколько Zo-аккаунтов, прокси ротирует при ошибках.
- Аккаунт регистрируется **через браузер**: жмёшь "Войти", лончер открывает Zo, ты логинишься, возвращаешься в терминал, cookies подсасываются автоматически (Chrome / Edge / Firefox / Brave / Opera / Vivaldi / Chromium / Safari).

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

В главном меню `Аккаунты` → откроется подменю:

```
=== zo-claude-proxy: аккаунты ===
  режим: fixed
  active  #  label   email                       domain     TTL       state
  ----------------------------------------------------------------------------
  ★       0  main    you@example.com             you        12 дн     ok

Что делаем?
 · ➕ Добавить аккаунт через браузер
   ⇄ Переключить режим (сейчас fixed → rotation)
   ★ Сделать аккаунт активным
   ⊘ Отключить / включить аккаунт
   🗑 Удалить аккаунт
   📡 Пингануть все
   ─────────────────────
   ← Назад
```

### Добавление через браузер

1. Выбираешь `➕ Добавить аккаунт через браузер`.
2. Лончер открывает [zo.computer](https://zo.computer) в браузере по умолчанию.
3. Логинишься в Zo (или просто убеждаешься, что уже залогинен).
4. Возвращаешься в терминал, жмёшь `Enter`.
5. Лончер находит cookies `access_token` + `refresh_token` через `browser-cookie3` и сохраняет аккаунт.

Если автоматика не сработала (закрытый keychain на macOS / нестандартный профиль), есть fallback — ручная вставка Cookie-хедера.

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
proxy.py              # FastAPI сервер
launcher.py           # TUI-лончер (questionary)
accounts.py           # multi-account store + ротация
zo_client.py          # клиент к Zo /ask
anthropic_sse.py      # Zo SSE → Anthropic SSE
openai_sse.py         # Zo SSE → OpenAI Chat / Responses SSE / WS
setup.py              # тонкая обёртка для совместимости (--check)
config.py             # порт, MODEL_MAP и дефолты
run.bat / run.sh      # единая точка входа
```

---

## Известные ограничения

- **Auto-refresh JWT** пока нет — когда cookie-сессия протухнет (~30 дней), пере-добавь аккаунт.
- Вложения картинок/файлов пока режутся до текстового суррогата.
- Tool-call мост пока не идеален, особенно вне Claude Code.

---

## Безопасность

- `accounts.json` и `.codex-home/` не коммитятся.
- Токены живут только локально.
- Если cookie утёк — разлогинься и залогинься заново в Zo.

---

## Лицензия

MIT
