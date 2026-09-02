"""Async database engine, session factory, and dependency."""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from stoa.config import settings

connect_args: dict[str, bool] = {}
if "asyncpg" in settings.database_url:
    connect_args["ssl"] = False


def build_engine_kwargs(connect_args: dict[str, bool]) -> dict[str, Any]:
    """Engine options that harden the pool against stale connections (#77).

    pool_pre_ping validates every connection at checkout, so a connection the
    DB side has silently closed (idle timeout, network blip) is detected,
    invalidated, and replaced before a query touches it — instead of surfacing
    as a transient 500 on the auth path, or worse, letting #72's witness layer
    record a stale read that looks successful.

    pool_recycle caps connection age regardless of health. The server-side
    idle timeout is not yet measured; set DB_POOL_RECYCLE_SECONDS below it
    once known (0 disables) — pool_pre_ping covers correctness either way.
    """
    kwargs: dict[str, Any] = {
        "echo": False,
        "connect_args": connect_args,
        "pool_pre_ping": True,
    }
    if settings.db_pool_recycle_seconds > 0:
        kwargs["pool_recycle"] = settings.db_pool_recycle_seconds
    return kwargs


engine = create_async_engine(settings.database_url, **build_engine_kwargs(connect_args))
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session; the caller is responsible for commit/rollback."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
