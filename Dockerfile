FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY migrations/ migrations/

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "herd_inbox.main:app", "--host", "0.0.0.0", "--port", "8080"]
