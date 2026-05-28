# ZoAPI docs

ZoAPI поднимает локальный прокси для Zo Computer на `127.0.0.1:17878`.

## Что запускать

- Первый запуск: `setup.bat` или `setup.sh`
- Дальше: `run.bat` или `run.sh`

## Роуты

### Anthropic-compatible
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

База:
- `http://127.0.0.1:17878`

Токен:
- `zo-proxy`

### OpenAI-compatible
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `WS /v1/responses`
- `GET /v1/m