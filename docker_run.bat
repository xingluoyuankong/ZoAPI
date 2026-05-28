@echo off
REM ZoAPI в Docker (Windows).
REM
REM Что делает:
REM   1) проверяет что docker установлен и accounts.json существует
REM   2) собирает образ `zoapi:latest`
REM   3) сносит старый контейнер `zoapi` если он был
REM   4) запускает новый: порт 17878, accounts.json/runtime.json смонтированы
REM      из текущей папки (живые правки через TUI на хосте применяются сразу)
REM
REM ВАЖНО: браузерный логин (добавление аккаунта Zo) внутри Docker НЕ работает.
REM Сначала запусти setup.bat и потом run.bat на хосте, добавь аккаунт через
REM "Добавить аккаунт через временный браузер", закрой лаунчер — после этого
REM запускай этот скрипт.

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

set "IMAGE_NAME=zoapi"
set "CONTAINER_NAME=zoapi"
set "PORT=17878"
set "ACCOUNTS_FILE=%cd%\accounts.json"
set "RUNTIME_FILE=%cd%\runtime.json"

echo.
echo                ZoAPI ^(Docker^)
echo       =============================
echo.

REM --- 1. docker ---
where docker >nul 2>&1
if errorlevel 1 (
    echo [!] Docker не найден в PATH. Поставь Docker Desktop:
    echo     https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo [!] Docker daemon не отвечает. Открой Docker Desktop и подожди
    echo     пока он полностью запустится.
    pause
    exit /b 1
)

REM --- 2. accounts.json ---
if not exist "%ACCOUNTS_FILE%" (
    echo [!] accounts.json не найден в %cd%
    echo.
    echo     Сначала сделай на хосте:
    echo       1^) setup.bat          ^(первый раз — поставит venv^)
    echo       2^) run.bat            ^(откроет TUI^)
    echo       3^) в меню: "Добавить аккаунт через временный браузер"
    echo       4^) закрой лаунчер
    echo     После этого снова запусти docker_run.bat
    pause
    exit /b 1
)

REM --- 3. runtime.json (необязательный, но если нет — создадим пустой) ---
if not exist "%RUNTIME_FILE%" (
    echo {} > "%RUNTIME_FILE%"
)

REM --- 4. build ---
echo [*] Собираю образ %IMAGE_NAME% ...
docker build -t %IMAGE_NAME% .
if errorlevel 1 (
    echo [!] docker build провалился, см. вывод выше.
    pause
    exit /b 1
)

REM --- 5. снести старый контейнер ---
docker rm -f %CONTAINER_NAME% >nul 2>&1

REM --- 6. run ---
echo [*] Запускаю контейнер %CONTAINER_NAME% ...
docker run -d ^
    --name %CONTAINER_NAME% ^
    --restart unless-stopped ^
    -p 127.0.0.1:%PORT%:%PORT% ^
    -v "%ACCOUNTS_FILE%":/app/accounts.json ^
    -v "%RUNTIME_FILE%":/app/runtime.json ^
    %IMAGE_NAME%

if errorlevel 1 (
    echo [!] docker run провалился, см. вывод выше.
    pause
    exit /b 1
)

REM --- 7. ждём health ---
echo [*] Жду health ...
set /a TRIES=0
:wait_health
set /a TRIES+=1
if !TRIES! GTR 30 (
    echo [!] /health не отвечает за 30 секунд. Смотри: docker logs %CONTAINER_NAME%
    goto :done
)
curl -fsS "http://127.0.0.1:%PORT%/health" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto :wait_health
)
echo [*] OK.

:done
echo.
echo  ZoAPI запущен в Docker
echo  ======================
echo    URL:           http://127.0.0.1:%PORT%
echo    accounts.json: %ACCOUNTS_FILE%   ^(volume^)
echo    runtime.json:  %RUNTIME_FILE%    ^(volume^)
echo.
echo  Полезное:
echo    docker logs -f %CONTAINER_NAME%       — стрим логов
echo    docker restart %CONTAINER_NAME%       — рестарт
echo    docker stop    %CONTAINER_NAME%       — стоп
echo    docker rm -f   %CONTAINER_NAME%       — снести
echo.
echo  Браузерный логин / редактирование аккаунтов — только через run.bat
echo  на хосте. Контейнер увидит изменения сразу (mount общий с хостом).
echo.
pause
endlocal
