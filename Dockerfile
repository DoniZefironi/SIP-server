FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Зависимости отдельным слоем: при правке кода они не переустанавливаются.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --system --no-create-home telephony
USER telephony

# Сначала обновляем схему базы, потом запускаем сервис. Один процесс: второй дублировал бы
# подключение к станции и писал бы каждое событие дважды.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 127.0.0.1 --port 8000"]
