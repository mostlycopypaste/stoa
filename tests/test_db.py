"""Tests for database module — module replaced by database.py in async migration."""

from stoa.database import Base, engine, get_db


async def test_engine_exists() -> None:
    """Verify async engine is configured."""
    assert engine is not None


async def test_base_has_metadata() -> None:
    """Verify Base has metadata for table creation."""
    assert Base.metadata is not None


async def test_get_db_yields_session() -> None:
    """Verify get_db yields a session."""
    gen = get_db()
    session = await gen.__anext__()
    assert session is not None
    # Clean up
    try:
        await gen.__anext__()
    except StopAsyncIteration:
        pass
