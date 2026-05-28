# ZoAPI

Локальный API-хаб поверх Zo Computer.

Теперь так:
- **первый раз**: `setup.bat` / `setup.sh`
- **потом всегда**: `run.bat` / `run.sh`
- `run.*` больше ничего не ставит — только запускает приложение
- **в докере**: `docker_run.bat` / `docker_run.sh` (после первого логина на хосте)

---

## Что поднимается

После запуска открывается красивый терминальный интерфейс и сразу поднимается локальный API на `127.0.0.1:17878`.

Поддерживаются роуты:
- `POST /v1/messages`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `WS /v1/responses`
- `GET /v1/models`
- `GET /health`

---

## Установка

### Windows
```bat
setup.bat
```

### macOS / Linux
```bash
chmod +x setup.sh run.sh
./setup.sh
```

Что делает `setup`:
- ищет Python 3.10+
- создаёт `.venv`
- ставит зависимости из `requirements.txt`
- ставит Chromium для Playwright

---

## Запуск

### Windows
```bat
run.bat
```

### macOS / Linux
```bash
./run.sh
```

Если `run.*` ругается, что окружение не готово — просто снова запусти `setup.*`.

---

## Интерфейс

Внутри приложения:
- русский язык по умолчанию
- можно переключить язык на English
- зелёная тема
- большая шапка `ZoAPI`
- статус API и аккаунтов вынесен вниз
- плотный layout без лишнего мусора

Главное меню:
- `Обновить статус`
- `Аккаунты`
- `Перезапустить локальный API`
- `Показать лог локального API`
- `Показать ручную настройку`
- `Подключить к Codex / Claude Code`
- `Язык`
- `Открыть доки Zo API`
- `Выход`

---

## Автоматическая настройка клиентов

В меню `Подключить к Codex / Claude Code` лаунчер сам пропишет прокси:

- **env-переменные** (persistent):
  - Windows: `setx OPENAI_API_KEY zo-proxy`, `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` — user scope, переживает ребут.
  - macOS / Linux: блок `# >>> zoapi env >>> ... # <<< zoapi env <<<` в `~/.zshrc` и/или `~/.bashrc` (идемпотентно, можно откатить одной кнопкой).
- **Codex CLI**: `~/.codex/config.toml` с провайдером `zoapi` (`base_url = http://127.0.0.1:17878/v1`, `wire_api = "responses"`).
- **Claude Code**: `~/.claude/settings.json` с `env`-блоком (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`).

Опции в меню:
- `Оба сразу` — прописать и Codex, и Claude Code.
- `Codex (OpenAI-совместимый)` — только Codex.
- `Claude Code (Anthropic)` — только Claude Code.
- `Убрать всё (откат)` — снести env-переменные и наши блоки из конфигов.

Уже открытые терминалы новые env не подхватят — открой новое окно после установки.

---

## Добавление аккаунта

Через `Аккаунты` → `Добавить аккаунт через временный браузер`.

Что происходит:
1. Открывается **отдельный временный Chromium** через Playwright
2. Ты логинишься в Zo там
3. Как только появляются нужные cookies — окно закрывается само
4. Сразу идут проверки:
   - логин
   - баланс
   - модели
5. Аккаунт сохраняется

Важно:
- обычный Chrome / Edge / Firefox не читаются
- каждый раз используется отдельный чистый временный профиль
- можно добавлять сколько угодно аккаунтов

Если браузерный вариант не сработал, есть ручной запасной вариант через Cookie header.

---

## Ручная настройка приложений

### OpenAI-compatible приложения
Например: Codex app, OpenCode, другие клиенты.

Ставь:
- Base URL: `http://127.0.0.1:17878/v1`
- API key: `zo-proxy`

### Anthropic-compatible приложения
Ставь:
- Base URL: `http://127.0.0.1:17878`
- API key / token: `zo-proxy`
- endpoint: `/v1/messages`

---

## Файлы

```text
run.bat
run.sh
setup.bat
setup.sh
proxy.py
accounts.py
zo_client.py
config.py
openai_sse.py
anthropic_sse.py
requirements.txt
utils/launcher.py
```

---

## Безопасность

- `accounts.json` не коммитится
- временный браузерный профиль одноразовый
- токены живут только локально
- если cookie утёк — разлогинься и залогинься заново в Zo

---

## Лицензия

MIT

---

## Docker

Если хочется держать прокси в контейнере (например на сервере или просто чтобы не загрязнять основную систему):

```bat
REM Windows
docker_run.bat
```

```bash
# macOS / Linux
chmod +x docker_run.sh
./docker_run.sh
```

**Архитектура — split host/container:**

- Python и сам API крутятся внутри контейнера (`python:3.12-slim`, ~150 МБ).
- Браузерный логин (Playwright/Patchright) живёт **на хосте**, потому что внутри Docker открыть нормальное GUI-окно нельзя.
- `accounts.json` и `runtime.json` пробрасываются volume-маунтом из текущей папки в `/app/` контейнера. Правки из TUI на хосте применяются сразу — рестарт контейнера не нужен.

**Порядок установки в первый раз:**

1. `setup.bat` / `./setup.sh` — поставить venv и Playwright.
2. `run.bat` / `./run.sh` — открыть TUI, добавить аккаунт через «Добавить аккаунт через временный браузер», закрыть лаунчер.
3. `docker_run.bat` / `./docker_run.sh` — собрать образ и поднять контейнер.

Дальше для обычной работы достаточно держать контейнер запущенным. Если нужно добавить ещё аккаунт или перелогиниться — снова открой `run.*` на хосте, контейнер подхватит новый `accounts.json` через mount.

**Контейнер слушает `127.0.0.1:17878`** (не публикуется наружу). Полезные команды:

```bash
docker logs -f zoapi       # стрим логов
docker restart zoapi       # рестарт
docker stop zoapi          # стоп
docker rm -f zoapi         # снести
```

Healthcheck встроен в образ — `docker ps` покажет `healthy` после первого успешного запроса к `/health`.