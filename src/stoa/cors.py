"""CORS configuration for future web UI integration."""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def configure_cors(app: FastAPI) -> None:
    """Add CORS middleware with explicit origin allowlist.

    Origins are configured via STOA_CORS_ORIGINS env var (comma-separated).
    Defaults to production domain if not set.
    """
    origins_str = os.environ.get("STOA_CORS_ORIGINS", "https://herd.mostlycopyandpaste.com")
    origins = [o.strip() for o in origins_str.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Admin-Key", "X-Request-ID"],
    )
