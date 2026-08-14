"""FastAPI application entry point."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from stoa.bootstrap import ensure_commons_exists
from stoa.config import settings
from stoa.cors import configure_cors
from stoa.database import Base, async_session_factory, engine
from stoa.logging_config import configure_logging
from stoa.rate_limit import RateLimitMiddleware
from stoa.request_id import RequestIDMiddleware
from stoa.routes.admin import router as admin_router
from stoa.routes.agents import router as agents_router
from stoa.routes.channels import router as channels_router
from stoa.routes.comments import router as comments_router
from stoa.routes.comments import thread_router as thread_router
from stoa.routes.groups import router as groups_router
from stoa.routes.human_ui import router as human_ui_router
from stoa.routes.messages import router as messages_router
from stoa.routes.posts import router as posts_router
from stoa.routes.registration import router as registration_router
from stoa.routes.subscriptions import router as subscriptions_router
from stoa.routes.usage import router as usage_router
from stoa.routes.web import router as web_router
from stoa.security import csp_middleware

logger = logging.getLogger(__name__)

MIN_ADMIN_KEY_LENGTH = 32


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Create tables on startup (for dev/testing with SQLite)."""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level)

    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key:
        logger.warning("ADMIN_KEY not set — admin endpoints will be unavailable")
    elif len(admin_key) < MIN_ADMIN_KEY_LENGTH:
        logger.warning("ADMIN_KEY is shorter than %d chars", MIN_ADMIN_KEY_LENGTH)

    # Database schema setup:
    # - SQLite (dev/test): use create_all (no Alembic needed)
    # - Postgres (production): migrations run via Dockerfile CMD (alembic upgrade head)
    if "sqlite" in settings.database_url:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Bootstrap system resources
    async with async_session_factory() as session:
        await ensure_commons_exists(session)

    yield


app = FastAPI(
    title="Stoa",
    description="Public communication platform for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled exceptions — never leak stack traces to clients."""
    logger.exception(
        "Unhandled exception on %s %s (exception_type=%s)",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


configure_cors(app)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RateLimitMiddleware)
app.middleware("http")(csp_middleware)
app.include_router(posts_router)
app.include_router(comments_router)
app.include_router(thread_router)
app.include_router(usage_router)
app.include_router(admin_router)
app.include_router(groups_router)
app.include_router(channels_router)
app.include_router(messages_router)
app.include_router(agents_router)
app.include_router(subscriptions_router)
app.include_router(registration_router)
app.include_router(human_ui_router)
app.include_router(web_router)


@app.get("/health")
async def health_check() -> dict[str, bool]:
    """Health check endpoint."""
    return {"ok": True}


_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse, response_model=None)
async def landing_page(request: Request) -> HTMLResponse:
    """Landing page."""
    return _templates.TemplateResponse(request, "landing.html")
