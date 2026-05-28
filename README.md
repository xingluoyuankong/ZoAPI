# zo-claude-proxy

Локальный прокси, который превращает [Zo Computer](https://zo.computer) в
**Anthropic-совместимый бэкенд** для Claude Code CLI.

Клуло-код ждёт обычный `/v1/messages`. Zo даёт свой `/zo/ask`. Этот скрипт
живёт между ними, переводит запросы туда-обратно, тащит стриминг (SSE),
помнит `conversation_id`, и эмулирует tool-use клиента маркерами в ответе
модели — чтобы локальные тулы Claude Code (Read / Edit / Bash / Glob / Grep
и т.д.) реально срабатывали.

---

## Что получится

- В Claude Code модель — твоя из Zo (Opus 4.7, Sonnet 4.5, любая BYOK).
- Кредиты тратятся **на Zo**, не на подписку Anthropic.
- Все тулы Claude Code (Read, Edit, Bash, …) работают локально на твоей машине.
- Стриминг.
- Память диалога между запросами (один Claude Code тред = один Zo conversation).

## Чего может не быть

- Картинки/файлы во входе пока не пробрасываются (пишет `[image attachment elided]`).
- Очень точная цена/usage — у Zo нет токенайзера в ответе, число output_tokens
  оценивается как `len(text)//4`.
- Иногда модель Zo всё-таки заюзает свой серверный тул вместо клиентского —
  тогда в чате ты просто увидишь её ответ "я уже сделал". Системный промпт
  это запрещает, но Sonnet/Opus может ошибиться (~5% случаев на сложных задачах).

---

## Установка

```bash
cd /home/workspace/Projects/zo-claude-proxy
cp .env.example .env
# открой .env и впиши ZO_API_KEY (Settings > Advanced > Access Tokens)
nano .env
```

## Запуск

```bash
./run.sh
```

При первом запуске сам создаст `.venv`, поставит зависимости, поднимет
прокси на `http://127.0.0.1:17878`. Дальше — оставляешь работать в одном
терминале.

Проверка живости:
```bash
curl http://127.0.0.1:17878/health
curl http://127.0.0.1:17878/v1/models | jq
```

## Подключаем Claude Code

В другом терминале:

```bash
./use-claude.sh
```

Этот скрипт выставляет:
```
ANTHROPIC_BASE_URL=http://127.0.0.1:17878
ANTHROPIC_AUTH_TOKEN=zo-proxy   # любая непустая строка
ANTHROPIC_API_KEY=              # обязательно ПУСТАЯ (иначе CLI пойдёт к Anthropic)
```

и запускает обычный `claude`. Внутри говоришь `/model sonnet` или `/model opus` —
mapping в `.env` разрулит это в нужный `model_name` у Zo.

Альтернативно, если хочешь чтобы Claude Code всегда юзал прокси:

```jsonc
// ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:17878",
    "ANTHROPIC_AUTH_TOKEN": "zo-proxy",
    "ANTHROPIC_API_KEY": "",
    "DISABLE_TELEMETRY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "model": "sonnet"
}
```

---

## Как это работает (под капотом)

```
Claude Code CLI                  zo-claude-proxy                Zo Computer
─────────────────                ───────────────                ───────────

POST /v1/messages                                                
  system, messages[],     ──▶    Склеивает в один text         
  tools[], stream=true           input для /zo/ask:            
                                 ─ proxy directive             
                                 ─ user system                 
                                 ─ описание тулов клиента       
                                 ─ transcript USER/ASSISTANT   
                                 ─ tool_result как блоки       
                                                               
                                 POST /zo/ask stream=true ─▶   /zo/ask  
                                                                 │
                                                                 │ SSE
                                                                 ▼
                                 event: FrontendModelResponse  
                                 data.content = "...text..."    
                                                               
                                 StreamParser ищет маркеры:    
                                   <<<TOOL_USE>>>{...}<<<END>>>
                                                               
                                 → text_delta для текста       
                                 → content_block tool_use      
                                   для tool-call               
                                                               
Anthropic SSE        ◀──         message_start, deltas,        
                                 content_block_stop, …,        
                                 message_stop                   

Claude Code исполняет
tool локально, шлёт
tool_result следующим
запросом → цикл повторяется
```

Главный трюк — **системный промпт к Zo**, который запрещает агенту Zo
использовать свои внутренние тулы и заставляет эмитить вызовы тулов клиента
через текстовые маркеры:

```
<<<TOOL_USE>>>{"name": "Read", "input": {"file_path": "/x/y.ts"}}<<<END_TOOL_USE>>>
```

Прокси на лету парсит маркеры из стрима и конвертит в Anthropic-овский
`content_block_start` / `input_json_delta` / `content_block_stop` с
`type=tool_use`. Claude Code думает, что говорит с Claude, и исполняет тул
у себя. Результат прилетает обратно как `tool_result` блок, прокси
сериализует его в текст с тегами `<<<TOOL_RESULT id=…>>>…<<<END_TOOL_RESULT>>>`
и отправляет в следующий `/zo/ask` — Zo видит результат и продолжает.

---

## Конфиг (`.env`)

| Переменная | Что делает |
|---|---|
| `ZO_API_KEY` | твой ключ Zo (`zo_sk_...`) |
| `ZO_BASE_URL` | по умолчанию `https://api.zo.computer` |
| `ZO_DEFAULT_MODEL` | модель если Claude Code не задал явно. По умолчанию `anthropic:claude-opus-4-7` |
| `MODEL_MAP` | JSON-словарь, ключ ищется как подстрока в имени модели из запроса |
| `ZO_PERSONA_ID` | подключить персону Zo (опционально) |
| `PROXY_PORT` | порт прокси (`17878` по умолчанию) |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |

Список доступных моделей Zo:
```bash
curl -H "Authorization: Bearer $ZO_API_KEY" \
  https://api.zo.computer/models/available | jq .models
```

---

## Отладка

- `LOG_LEVEL=DEBUG ./run.sh` — видишь все события из стрима Zo.
- `curl -N -H 'Authorization: Bearer x' -H 'Content-Type: application/json' \
    -d '{"model":"sonnet","stream":true,"max_tokens":1024,"messages":[{"role":"user","content":"Привет"}]}' \
    http://127.0.0.1:17878/v1/messages` — посмотреть сырой SSE.
- Если Claude Code ругается на `401`/`403` — у тебя выставлен `ANTHROPIC_API_KEY`,
  CLI ходит мимо прокси. Сделай `unset ANTHROPIC_API_KEY` или поставь пустую строку.
- Если стрим обрывается — проверь, что Zo отвечает (`curl https://api.zo.computer/models/available` с ключом).

---

## Известные ограничения

1. **Не Anthropic Beta endpoints** — `/v1/messages/count_tokens`, batches, files
   не реализованы. Claude Code их обычно не требует, но если упрётся — добавлю.
2. **Никаких vision** во входе. Картинки в `tool_result` уходят как `[image]`.
3. **Параллельные tool_use** в одном ответе поддержаны, но Claude Code иногда
   ожидает чёткой стриминговой нарезки `input_json_delta` по символам — мы
   шлём всё JSON одним куском. Работает в подавляющем большинстве случаев,
   но если CLI заглючит на сложном tool-call — это первый кандидат на фикс.
4. **prompt caching** Anthropic пробрасывается через системный промпт без
   эффективного reuse (Zo своё кеширование делает сам).

---

## Файлы

| | |
|---|---|
| `proxy.py` | весь сервер (FastAPI + парсер маркеров + SSE-конвертер) |
| `run.sh` | подъём в venv |
| `use-claude.sh` | запуск Claude Code с правильным окружением |
| `.env.example` | конфиг с комментариями |
| `requirements.txt` | зависимости (fastapi, uvicorn, httpx, pydantic, dotenv) |

---

## Лицензия

MIT. Это твой код, делай что хочешь.
