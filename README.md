# Телефония SAVT

Сервис на Python (FastAPI) рядом со станцией Asterisk. Слушает события станции, складывает их
в базу и отдаёт по HTTP. Меню, очереди и запись делает сам Asterisk; если сервис остановлен,
звонки идут как обычно.

Полное описание задачи — в [telephony-service-brief.md](telephony-service-brief.md).

## Что уже есть

- Подключение к станции по AMI (поток событий Asterisk, TCP-порт 5038) с переподключением при обрыве.
- Настройки станции в [asterisk/](asterisk/): сценарий, очереди, SIP, AMI и гайд по применению.
- Таблица `call_events`: события звонков (новый канал, вызов, ответ, очередь, отбой)
  и этапы сценария станции (`SavtCall`, `SavtRecording`, `SavtMessage`).
- `GET /health` — состояние сервиса, базы и связи со станцией (200 или 503).
- `GET /api/events?limit=50&linkedid=…` — последние события, для отладки.
- Журнал звонков: `GET /api/calls` (фильтры `sector`, `outcome`, `caller`, `date_from`, `date_to`),
  `GET /api/calls/{id}`, записи `GET /api/calls/{id}/audio/{mix|client|staff|message}`.
- Очередь `recording_jobs`: готовый WAV разговора или голосового сообщения завершённого звонка
  ставится в очередь один раз, в том числе если файл появился после отбоя. Проверка проходит
  при запуске и затем каждые 15 секунд, пока включена `TELEPHONY_RECORDING_QUEUE_ENABLED`.
- Асинхронное распознавание через Yandex SpeechKit. `GET /api/calls/{id}` отдаёт статус и текст.
  Для запуска задать `TELEPHONY_SPEECHKIT_ENABLED=true` и `TELEPHONY_SPEECHKIT_API_KEY` в `.env`.
  После включения сервис отправляет WAV до 60 МБ из очереди, включая накопленные задачи.
  Если SpeechKit мог принять файл, но идентификатор операции не сохранился, задача переходит
  в `failed`, чтобы избежать автоматической повторной загрузки.
- Обслуживание раз в час: удаление пустых записей, записей старше года
  (`TELEPHONY_RECORDINGS_RETENTION_DAYS`) и сырых событий старше 90 дней.
- Приведение номеров к виду `+375291234567`.

Определения звонящего, Bitrix, выжимки, управления настройками станции и защиты API
пока нет: API доступен только с самой машины.

## Устройство

```
app/
  main.py          запуск сервиса и фоновой задачи AMI
  config.py        настройки из переменных окружения (TELEPHONY_*)
  ami/client.py    клиент AMI
  ami/events.py    какие события сохраняем
  api/             HTTP-эндпоинты
  db/              модели и подключение к PostgreSQL
  services/        журнал звонков, номера, обслуживание
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

**1. Станция.** Сначала применить настройки Asterisk по [asterisk/GUIDE.md](asterisk/GUIDE.md):
там же скачивается репозиторий в `/opt/savt-telephony` и создаётся пользователь AMI `telephony`
(пароль — в `/root/ami-secret.txt`).

**2. Сервис.**

```
cd /opt/savt-telephony
sudo cp .env.example .env
sudo cat /root/ami-secret.txt
sudo nano .env
```

В `.env` задать:
- `TELEPHONY_DB_PASSWORD` — свой случайный пароль, например из `openssl rand -hex 16`;
- `TELEPHONY_AMI_SECRET` — пароль, который показала команда `sudo cat /root/ami-secret.txt`;
- `TELEPHONY_ASTERISK_GID` — число из команды `getent group asterisk | cut -d: -f3`
  (сервис входит в группу `asterisk`, чтобы читать и чистить записи в `/srv/recordings`).

Запуск:

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
