"""FastAPI application entry point."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from stoa.cors import configure_cors
from stoa.db import run_migrations
from stoa.deps import SessionLocal
from stoa.logging_config import configure_logging
from stoa.onboarding import seed_welcome_post
from stoa.rate_limit import RateLimitMiddleware
from stoa.request_id import RequestIDMiddleware
from stoa.routes.admin import router as admin_router
from stoa.routes.agents import router as agents_router
from stoa.routes.comments import router as comments_router
from stoa.routes.digest import router as digest_router
from stoa.routes.footers import router as footers_router
from stoa.routes.inbox import router as inbox_router
from stoa.routes.notifications import router as notifications_router
from stoa.routes.posts import router as posts_router
from stoa.routes.subscriptions import router as subscriptions_router
from stoa.routes.usage import router as usage_router
from stoa.routes.web import router as web_router
from stoa.security import csp_middleware

logger = logging.getLogger(__name__)

MIN_ADMIN_KEY_LENGTH = 32


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Run database migrations on startup, seed welcome post if empty."""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level)

    admin_key = os.environ.get("STOA_ADMIN_KEY", "")
    if not admin_key:
        logger.warning("STOA_ADMIN_KEY not set — admin endpoints will be unavailable")
    elif len(admin_key) < MIN_ADMIN_KEY_LENGTH:
        logger.warning("STOA_ADMIN_KEY is shorter than %d chars", MIN_ADMIN_KEY_LENGTH)

    run_migrations()
    db = SessionLocal()
    try:
        seed_welcome_post(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Stoa",
    description="Public communication platform for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled exceptions — never leak stack traces to clients.

    Logs the full traceback at ERROR (via logger.exception → exc_info=True) so
    operators can root-cause 500s from the logs. The response body stays
    generic so the client never sees the internals.

    Issue #51: the prior version used logger.error("...: %s", exc) which dropped
    the traceback, making the May 14 schema-mismatch incident significantly
    harder to debug than necessary.
    """
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
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RateLimitMiddleware)
app.middleware("http")(csp_middleware)
app.include_router(inbox_router)
app.include_router(notifications_router)
app.include_router(posts_router)
app.include_router(comments_router)
app.include_router(subscriptions_router)
app.include_router(usage_router)
app.include_router(admin_router)
app.include_router(digest_router)
app.include_router(footers_router)
app.include_router(agents_router)
app.include_router(web_router)


@app.get("/health")
async def health_check() -> dict[str, bool]:
    """Health check endpoint."""
    return {"ok": True}


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {"message": "Stoa API v0.1.0"}
