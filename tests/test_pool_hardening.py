"""Pool hardening tests — stale pooled connections must never reach a query (#77).

Acceptance criterion from issue #77 (Jules, pinned by Marey): a read over a
dead pooled connection must surface as an error or transparently retry —
never succeed with stale data. pool_pre_ping provides the transparent retry.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from stoa.config import settings
from stoa.database import build_engine_kwargs


def test_build_engine_kwargs_enables_pool_pre_ping() -> None:
    """The production engine must validate connections at checkout."""
    kwargs = build_engine_kwargs({})
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["echo"] is False
    assert kwargs["connect_args"] == {}


def test_build_engine_kwargs_pool_recycle_follows_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pool_recycle is configurable (0 disables) for when the DB idle timeout is known."""
    monkeypatch.setattr(settings, "db_pool_recycle_seconds", 0)
    assert "pool_recycle" not in build_engine_kwargs({})
    monkeypatch.setattr(settings, "db_pool_recycle_seconds", 300)
    assert build_engine_kwargs({})["pool_recycle"] == 300


async def _seed(engine: AsyncEngine) -> None:
    """Create the probe table and update it once — current state is 'v2'."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await conn.execute(text("INSERT INTO t VALUES (1, 'v1')"))
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE t SET v = 'v2' WHERE id = 1"))


async def _kill_pooled_connection(engine: AsyncEngine) -> None:
    """Park a dead connection in the pool — the production failure mode.

    Simulates the DB side silently closing an idle pooled connection (asyncpg
    "connection is closed" after an idle gap): check out the most recently
    used connection, return it to the pool, then close its DBAPI handle
    behind the pool's back while it sits idle.
    """
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        stale = raw.driver_connection
    await stale.close()


async def test_pre_ping_recycles_dead_pooled_connection(tmp_path) -> None:
    """A read over a dead pooled connection transparently retries with fresh data."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pool.db", pool_pre_ping=True)
    try:
        await _seed(engine)
        await _kill_pooled_connection(engine)

        # The dead connection is the most recently returned one, so this read
        # checks it out first. pool_pre_ping must detect the dead handle,
        # replace it, and serve current data — not a 500, not stale data.
        async with engine.connect() as conn:
            value = (await conn.execute(text("SELECT v FROM t WHERE id = 1"))).scalar_one()
        assert value == "v2"
    finally:
        await engine.dispose()


async def test_dead_pooled_connection_without_pre_ping_raises(tmp_path) -> None:
    """Without pre_ping the same handout raises — the pre-#77 failure mode.

    Control for the test above: without the fix, the dead connection reaches
    the query and the error surfaces at execution time instead of checkout
    (observed wrapped as OperationalError: "no active connection").
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pool.db", pool_pre_ping=False)
    try:
        await _seed(engine)
        await _kill_pooled_connection(engine)

        with pytest.raises(OperationalError):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT v FROM t WHERE id = 1"))
    finally:
        await engine.dispose()
