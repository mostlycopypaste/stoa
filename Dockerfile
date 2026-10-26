FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Apply Debian security updates even between official Python image rebuilds.
# asyncpg does not require libpq; pip is not needed at runtime either.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes pip

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
RUN UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev --no-editable --no-cache

FROM base AS runtime
RUN groupadd --gid 10001 stoa \
    && useradd --uid 10001 --gid stoa --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin stoa

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations/ migrations/

ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8080

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn stoa.main:app --host 0.0.0.0 --port 8080"]
