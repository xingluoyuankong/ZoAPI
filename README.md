# zo-claude-proxy

Локальный прокси: поднимает **локальный API-хаб** поверх Zo Computer и делает его совместимым с клиентами формата **Anthropic** и **OpenAI**.

Теперь логика такая:
- `run.bat` / `run.sh` **сразу поднимает локальный API** на `127.0.0.1:17878`
- дальше открывается красивый terminal UI для аккаунтов, статуса и подсказок по настройке
- **Claude Code / Codex / OpenCode / Hermes сам лончер больше не запускает**
- ты сам указываешь в нужном приложении локальный Base URL и API key

---

## Что есть

### Anthropic-совместимое
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

### OpenAI-совместимое
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `WS   /v1/responses`
- `GET  /v1/models`

---

## Запуск

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
- найдёт Python 3.10+
- создаст `.venv`, если её ещё нет
- на каждом запуске быстро проверит зависимости
- если чего-то не хватает — доставит
- поднимет локальный прокси на `http://127.0.0.1:17878`
- откроет TUI с аккаунтами, статусом и настройками

Пока это окно открыто — локальный API работает.

---

## Добавление аккаунта

Внутри UI:
- `Accounts`
- `Add account via temporary browser`

Что происходит:
1. При необходимости ставится Playwright Chromium
2. Открывается **временный отдельный Chromium**
3. Ты логинишься в Zo
4. Как только появляются `access_token` + `refresh_token`, окно автоматически закрывается
5. Лончер сразу проверяет логин, баланс и модели
6. Аккаунт сохраняется локально в `accounts.json`

Важно:
- **не читаются куки из твоего обычного браузера**
- для каждого добавления используется свежий временный браузер
- так можно спокойно добавлять разные аккаунты

Есть и fallback: `manual cookie fallback`, если вдруг авторизация через встроенный браузер не сработала.

---

## Настройка клиентов вручную

## OpenAI-совместимые приложения
Подходит для Codex desktop, OpenCode и любого другого OpenAI-compatible клиента.

- Base URL: `http://127.0.0.1:17878/v1`
- API key: `zo-proxy`
- Model examples:
  - `gpt-5.3-codex`
  - `gpt-5.5`
  - `claude-sonnet-4-6`
  - `claude-opus-4-7`

## Anthropic-совместимые приложения
Подходит для Claude Code и клиентов, которые ждут Anthropic Messages API.

- Base URL: `http://127.0.0.1:17878`
- Auth token / API key: `zo-proxy`
- Endpoint: `/v1/messages`

---

## Multi-account

В UI можно:
- добавить аккаунт
- выбрать активный аккаунт
- включать / выключать аккаунты
- удалять аккаунты
- обновлять проверку логина / баланса / моделей
- переключать режим `fixed / rotation`

### Режимы
- `fixed` — всегда использовать активный аккаунт
- `rotation` — usable-аккаунты идут по кругу

### API для управления
Список аккаунтов:
```bash
curl http://127.0.0.1:17878/v1/admin/accounts
```

Сменить активный:
```bash
curl -X POST http://127.0.0.1:17878/v1/admin/active \
  -H 'Content-Type: application/json' \
  -d '{"label":"acc2"}'
```

---

## Интерфейс

Сейчас UI умеет:
- более плотный layout
- верхний статус-бар
- нижний статус-бар
- таблицу аккаунтов
- живое обновление логина / баланса / моделей по команде `Refresh`
- цвета и ASCII-safe иконки, чтобы не ломалось на Windows

---

## Файлы

```text
run.bat / run.sh      # единая точка входа
utils/launcher.py     # красивый terminal UI
proxy.py              # FastAPI сервер
accounts.py           # multi-account store + ротация
zo_client.py          # клиент к Zo /ask
anthropic_sse.py      # Zo SSE → Anthropic SSE
openai_sse.py         # Zo SSE → OpenAI Chat / Responses SSE / WS
config.py             # порт, MODEL_MAP и дефолты
requirements.txt      # зависимости
```

---

## Ограничения

- auto-refresh JWT пока нет
- когда cookie-сессия протухнет, аккаунт надо пере-добавить
- вложения картинок/файлов пока режутся до текстового суррогата
- tool-call мост пока не идеален

---

## Безопасность

- `accounts.json` хранится локально
- временный браузерный профиль удаляется сразу после захвата куков
- обычный браузер не трогается
- если сессия утекла — просто перелогинься в Zo

---

## Лицензия

MIT
