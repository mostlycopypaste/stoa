FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY alembic.ini ./
COPY migrations/ migrations/

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn stoa.main:app --host 0.0.0.0 --port 8080"]
