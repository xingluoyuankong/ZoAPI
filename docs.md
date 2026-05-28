# ZoAPI — документация

ZoAPI поднимает локальный API-хаб поверх Zo Computer.

- Адрес: `http://127.0.0.1:17878`
- Anthropic-роуты: `POST /v1/messages`, `GET /v1/models`
- OpenAI-роуты: `POST /v1/chat/completions`, `POST /v1/responses`, `WS /v1/responses`, `GET /v1/models`
- Любые ключи — фейковые, локальные. **Не клади сюда настоящий ключ Anthropic/OpenAI**, прокси использует твой Zo, а не их API.

## Установка

1. Первый раз: `setup.bat` (Windows) или `./setup.sh` (mac/linux)
2. Каждый следующий раз: `run.bat` / `./run.sh`
3. В лончере добавь аккаунт Zo через "Добавить аккаунт через временный браузер"

> Лончер сам поднимает локальный API и держит его, пока открыто окно лончера.
> Не закрывай лончер — закроется и API.

---

## Claude Code

Claude Code читает переменные `ANTHROPIC_BASE_URL` и `ANTHROPIC_AUTH_TOKEN`.

### Windows (постоянно через `setx`)

В обычном `cmd`:

```bat
setx ANTHROPIC_BASE_URL "http://127.0.0.1:17878"
setx ANTHROPIC_AUTH_TOKEN "zo-proxy"
```

Открой **новое** окно `cmd` (старое не подхватит).
Проверка:

```bat
echo %ANTHROPIC_BASE_URL%
echo %ANTHROPIC_AUTH_TOKEN%
```

Запусти Claude Code:

```bat
claude
```

### Windows (только в текущей сессии)

```bat
set ANTHROPIC_BASE_URL=http://127.0.0.1:17878
set ANTHROPIC_AUTH_TOKEN=zo-proxy
claude
```

### macOS / Linux

В `~/.zshrc` или `~/.bashrc`:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:17878"
export ANTHROPIC_AUTH_TOKEN="zo-proxy"
```

```bash
source ~/.zshrc
claude
```

### Снять обратно

Windows:

```bat
setx ANTHROPIC_BASE_URL ""
setx ANTHROPIC_AUTH_TOKEN ""
```

mac/linux: убери `export` строки из rc-файла.

---

## Codex CLI

Codex CLI (`codex-cli`) читает `~/.codex/config.toml` и `OPENAI_API_KEY`.

### Конфиг

Создай (или допиши) файл `~/.codex/config.toml` (на Windows: `%USERPROFILE%\.codex\config.toml`):

```toml
openai_base_url = "http://127.0.0.1:17878/v1"
model = "gpt-5.3-codex"
```

### Ключ

Windows:

```bat
setx OPENAI_API_KEY "zo-proxy"
```

mac/linux:

```bash
export OPENAI_API_KEY="zo-proxy"
```

### Запуск

```bash
codex
```

---

## Codex (десктоп-приложение)

В Codex desktop env-переменные не работают — настройки внутри приложения.

1. Открой Codex → Settings → Providers (или Model providers).
2. Добавь нового провайдера:
   - **Name**: `ZoAPI`
   - **Base URL**: `http://127.0.0.1:17878/v1`
   - **API key**: `zo-proxy` (любая непустая строка)
   - **Type**: OpenAI-compatible / Responses API
3. Сохрани и выбери его как активного провайдера.
4. В списке моделей укажи, например: `gpt-5.3-codex`, `gpt-5.5`, `claude-sonnet-4-6`, `claude-opus-4-7`.

> Если приложение не поддерживает прямое добавление провайдера через UI — используй Codex CLI вариант выше, он работает гарантированно.

---

## OpenCode (SST)

OpenCode читает `opencode.json` в корне проекта **или** глобально из `~/.config/opencode/opencode.json`.

### Конфиг

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "zo": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ZoAPI",
      "options": {
        "baseURL": "http://127.0.0.1:17878/v1",
        "apiKey": "{env:OPENAI_API_KEY}"
      },
      "models": {
        "gpt-5.3-codex": { "name": "Codex (Zo)" },
        "gpt-5.5":       { "name": "GPT-5.5 (Zo)" },
        "claude-sonnet-4-6": { "name": "Sonnet 4.6 (Zo)" },
        "claude-opus-4-7":   { "name": "Opus 4.7 (Zo)" }
      }
    }
  }
}
```

### Ключ

Windows:

```bat
setx OPENAI_API_KEY "zo-proxy"
```

mac/linux:

```bash
export OPENAI_API_KEY="zo-proxy"
```

### Выбор модели

```bash
opencode --model zo/gpt-5.3-codex
```

---

## Hermes

Hermes читает `OPENAI_API_KEY` и `OPENAI_BASE_URL`.

Windows:

```bat
setx OPENAI_API_KEY "zo-proxy"
setx OPENAI_BASE_URL "http://127.0.0.1:17878/v1"
```

mac/linux:

```bash
export OPENAI_API_KEY="zo-proxy"
export OPENAI_BASE_URL="http://127.0.0.1:17878/v1"
```

Запуск:

```bash
hermes
```

---

## Любой OpenAI-совместимый клиент

Если в клиенте есть поля Base URL и API key:

| Поле     | Значение                          |
| -------- | --------------------------------- |
| Base URL | `http://127.0.0.1:17878/v1`       |
| API key  | `zo-proxy` (любая непустая строка)|
| Модель   | например `gpt-5.5` или `gpt-5.3-codex` |

---

## Любой Anthropic-совместимый клиент

| Поле     | Значение                        |
| -------- | ------------------------------- |
| Base URL | `http://127.0.0.1:17878`        |
| API key  | `zo-proxy`                      |
| Модель   | `claude-opus-4-7`, `claude-sonnet-4-6` |

---

## Проверка, что всё живо

```bash
curl http://127.0.0.1:17878/health
curl http://127.0.0.1:17878/v1/models
```

Должно ответить JSON со списком моделей и статусом.

---

## Частые косяки

- **`API офлайн` в лончере.** Лончер сам должен поднимать API; если нет — нажми "Перезапустить локальный API" → "Показать лог локального API".
- **Claude Code всё равно ходит в Anthropic.** Открой **новое** окно после `setx`. Проверь `echo %ANTHROPIC_BASE_URL%`.
- **Codex desktop не видит провайдера.** Только через UI приложения, env его не настраивает.
- **Cloudflare ругается при логине в Zo.** Лончер использует patchright и встроенный Edge, попробуй снова, желательно не из VPN, и не закрывай окно браузера до того, как лончер сам его закроет.

## Принудительная модель

В главном меню есть пункт **«Принудительная модель»**. Когда там задано
значение, прокси использует именно эту модель — что бы ни прислал клиент
(claude, codex, opencode). Когда поле пустое (passthrough) — клиент сам
решает, какая модель уходит в Zo.

Состояние живёт в `runtime.json` рядом с `proxy.py`. Файл читается по
mtime, так что переключения из TUI подхватываются на следующем же
запросе — перезапуск прокси не нужен.

Пункт «Сбросить (passthrough)» либо ввод пустой строки возвращают
поведение по умолчанию.

## Вызовы тулов: как это теперь работает

Эндпоинт `https://api.zo.computer/ask` — это полноценный *агент*, не
сырой completion. Поэтому модель за ним по умолчанию имеет СВОИ
серверные тулы (`bash` на `/home/workspace`, `read_file`, `list_directory`
и т.д.), и при наивной проксировке часто либо лезла в серверный bash,
либо писала прозой «выполни эту команду у себя» вместо того, чтобы
эмитить тул клиента.

В прокси встроены три слоя защиты:

1. **Жёсткий bridge-промпт**. Поверх запроса клиента приклеивается явный
   override: модель должна забыть, что она «Zo Computer», список
   FORBIDDEN-тулов перечислен явно, а в самом конце промпта (recency)
   подсовывается секция `=== AVAILABLE CLIENT TOOLS ===` со схемами и
   конкретным примером вызова.

2. **Перехват серверных tool_call**. Если модель всё-таки эмитит
   серверный `bash`/`read_file`/etc. через Zo, прокси на лету маппит:
   - имя тула → имя у клиента (`bash` → `Bash` для Claude Code,
     `bash` для OpenCode и т.д.);
   - имена аргументов (`cmd` → `command`, `target_file` → `file_path`
     или `filePath`).
   Клиент получает корректный tool_use и исполняет вызов ЛОКАЛЬНО.

3. **Опциональная персона без скоупов** (см. ниже).

## Персона без тулов (опционально)

Самый чистый способ полностью отрубить серверные тулы Zo — создать на
своём Zo-аккаунте персону без скоупов (chat-only, no tools), и
сконфигурировать прокси использовать её.

Шаги:

1. В Zo-чате (на своём `bralwekjr.zo.computer`) попроси Zo создать
   персону, например:
   > Создай персону с именем "zoapi-passthrough" и поставь ей пустой
   > список scopes (`set_persona_scopes ["chat-only"]` или `[]`).

2. Получи `persona_id` (Zo вернёт его при создании, или через
   `list_personas`).

3. Положи его в `runtime.json` рядом с `proxy.py`:
   ```json
   {
     "persona_id": "psn_abc123..."
   }
   ```

4. Перезапуск прокси НЕ нужен — `runtime.json` подхватывается по mtime
   на следующем же запросе.

После этого серверный агент Zo не получит ни одного тула, и модель
будет вынуждена использовать только теги `<zo:call>` для тулов клиента.

## Доступные модели

Полный список можно получить запросом `GET https://api.zo.computer/models/available`
(нужен валидный аккаунт). Ниже — то, что обычно есть, разбито по
провайдерам. Прокси работает в **passthrough**-режиме: любое имя
вида `claude-*` уходит в `zo:anthropic/{name}`, `gpt-*`/`o*`/`codex-*` —
в `zo:openai/{name}`, и так для всех провайдеров. Так что когда выходит
новая версия (`claude-opus-4-9`, `gpt-6` и т.п.) — её не надо нигде
добавлять, она поедет автоматически.

### Anthropic
- `claude-opus-4-8`, `claude-opus-4-8-thinking`