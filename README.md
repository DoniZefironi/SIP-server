# Телефония SAVT

Сервис на Python (FastAPI) рядом со станцией Asterisk. Слушает события станции, складывает их
в базу и отдаёт по HTTP. Меню, очереди и запись делает сам Asterisk; если сервис остановлен,
звонки идут как обычно.

Полное описание задачи — в [telephony-service-brief.md](telephony-service-brief.md).

## Что уже есть

- Подключение к станции по AMI (поток событий Asterisk, TCP-порт 5038) с переподключением при обрыве.
- Таблица `call_events`: события звонков (новый канал, вызов, ответ, очередь, отбой).
- `GET /health` — состояние сервиса, базы и связи со станцией (200 или 503).
- `GET /api/events?limit=50&linkedid=…` — последние события, для отладки.
- Приведение номеров к виду `+375291234567`.

Журнала звонков, определения звонящего, Bitrix и управления настройками станции пока нет.

## Устройство

```
app/
  main.py          запуск сервиса и фоновой задачи AMI
  config.py        настройки из переменных окружения (TELEPHONY_*)
  ami/client.py    клиент AMI
  ami/events.py    какие события сохраняем
  api/             HTTP-эндпоинты
  db/              модели и подключение к PostgreSQL
  services/        нормализация номеров
alembic/           миграции базы
tests/             тесты (станция в них поддельная, Asterisk не нужен)
```

## Запуск на рабочем компьютере (без Asterisk)

Нужны Python 3.13, uv и Docker.

```
copy .env.example .env
```

В `.env` задать `TELEPHONY_DB_PASSWORD` и поставить `TELEPHONY_AMI_ENABLED=false`. Затем:

```
uv sync
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Проверка: открыть http://127.0.0.1:8000/health и http://127.0.0.1:8000/docs.

Тесты и проверка кода:

```
uv run pytest
uv run ruff check .
```

## Запуск на сервере (ВМ siptrunk)

На реальной станции это ещё не проверялось.

**1. Пользователь AMI.** В файле `/etc/asterisk/manager.conf` должно быть:

```
[general]
enabled = yes
bindaddr = 127.0.0.1
port = 5038

[telephony]
secret = ПАРОЛЬ_AMI
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
read = call,agent
write =
```

`bindaddr = 127.0.0.1` — интерфейс доступен только с самой машины. `read = call,agent` — сервис
получает события звонков и очередей. `write` пустой — управлять станцией сервис пока не может.
Пароль придумать случайный, например командой `openssl rand -hex 16`. Применить:

```
sudo asterisk -rx "manager reload"
```

**2. Сервис.**

```
sudo git clone https://github.com/DoniZefironi/SIP-server.git /opt/savt-telephony
cd /opt/savt-telephony
sudo cp .env.example .env
sudo nano .env
```

В `.env` задать `TELEPHONY_DB_PASSWORD` (свой случайный пароль) и `TELEPHONY_AMI_SECRET`
(тот же пароль, что в `manager.conf`). Запуск:

```
sudo docker compose up -d --build
```

Команда собирает образ, поднимает базу, создаёт таблицы и запускает сервис. Проверка:

```
curl http://127.0.0.1:8000/health
```

Ожидаемый ответ: `{"status":"ok","db":"ok","ami":"connected"}`. После тестового звонка
`curl http://127.0.0.1:8000/api/events` покажет его события.

Сервис слушает только `127.0.0.1:8000`, база — только `127.0.0.1:5432`. Наружу ничего не открыто.
