#!/usr/bin/env bash
# ZoAPI в Docker (macOS / Linux).
#
# Браузерный логин делается на ХОСТЕ (./run.sh → "Добавить аккаунт через
# временный браузер"), потому что Playwright/Patchright не может открыть
# GUI-окно внутри контейнера. Контейнер только держит HTTP-прокси и
# читает accounts.json по volume-маунту, общему с хостом.

set -euo pipefail
cd "$(dirname "$0")"

IMAGE_NAME="zoapi"
CONTAINER_NAME="zoapi"
PORT="17878"
ACCOUNTS_FILE="$(pwd)/accounts.json"
RUNTIME_FILE="$(pwd)/runtime.json"

echo
echo "                ZoAPI (Docker)"
echo "       ============================="
echo

# --- 1. docker ---
if ! command -v docker >/dev/null 2>&1; then
    echo "[!] Docker не найден в PATH."
    echo "    macOS:   https://www.docker.com/products/docker-desktop/"
    echo "    Linux:   https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "[!] Docker daemon не отвечает. Запусти Docker Desktop / 'systemctl start docker'."
    exit 1
fi

# --- 2. accounts.json ---
if [[ ! -f "$ACCOUNTS_FILE" ]]; then
    echo "[*] accounts.json не найден — создаю пустой."
    echo "    После запуска контейнера добавь аккаунт через браузер по адресу"
    echo "    http://localhost:${PORT}/auth (вкладка 'add account')."
    echo
    echo '{"active": null, "accounts": []}' > "$ACCOUNTS_FILE"
    NEED_BROWSER_AUTH=1
fi

# --- 3. runtime.json (если нет — создаём пустой, чтобы mount не упал) ---
if [[ ! -f "$RUNTIME_FILE" ]]; then
    echo "{}" > "$RUNTIME_FILE"
fi

# --- 4. build ---
echo "[*] Собираю образ $IMAGE_NAME ..."
docker build -t "$IMAGE_NAME" .

# --- 5. снести старый ---
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# --- 6. run ---
echo "[*] Запускаю контейнер $CONTAINER_NAME ..."
docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -p "127.0.0.1:${PORT}:${PORT}" \
    -v "$ACCOUNTS_FILE":/app/accounts.json \
    -v "$RUNTIME_FILE":/app/runtime.json \
    "$IMAGE_NAME" >/dev/null

# --- 7. ждём health ---
echo -n "[*] Жду health "
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo " OK."
        break
    fi
    echo -n "."
    sleep 1
done

if ! curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo
    echo "[!] /health не отвечает. Логи:"
    echo "    docker logs $CONTAINER_NAME"
    exit 1
fi

cat <<EOF

 ZoAPI запущен в Docker
 ======================
   URL:           http://127.0.0.1:${PORT}
   accounts.json: ${ACCOUNTS_FILE}   (volume)
   runtime.json:  ${RUNTIME_FILE}    (volume)

 Полезное:
   docker logs -f $CONTAINER_NAME       — стрим логов
   docker restart $CONTAINER_NAME       — рестарт
   docker stop    $CONTAINER_NAME       — стоп
   docker rm -f   $CONTAINER_NAME       — снести

 Добавление аккаунта в контейнер:
   →  Открой в браузере  http://127.0.0.1:${PORT}/auth
      Зайди в свой zo.computer в соседней вкладке, открой DevTools →
      Application → Cookies, скопируй access_token и refresh_token,
      вставь в форму. Контейнер сразу провалидирует и сохранит.

 Альтернатива (если у тебя есть хост с GUI):
   →  ./run.sh на хосте — Playwright откроет временный браузер,
      ты залогинишься, и accounts.json подхватится через mount.

EOF
