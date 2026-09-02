"""Startup configuration validation tests."""

import logging
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

import stoa.main as main


async def _run_lifespan() -> None:
    """Run the app lifespan context once."""
    async with main.lifespan(FastAPI()):
        pass


@pytest.fixture
def isolate_lifespan_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real database/bootstrap side effects while testing startup validation."""

    async def _noop_ensure_commons_exists(_session: object) -> None:
        return None

    @asynccontextmanager
    async def _fake_async_session_factory():
        yield object()

    monkeypatch.setattr(main, "ensure_commons_exists", _noop_ensure_commons_exists)
    monkeypatch.setattr(main, "async_session_factory", _fake_async_session_factory)
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        main.settings,
        "database_url",
        "postgresql+asyncpg://example:example@localhost:5432/stoa",
    )
    monkeypatch.setenv("ADMIN_KEY", "a" * 32)


def _has_secret_warning(records: list[logging.LogRecord]) -> bool:
    return any("SECRET_KEY is not securely configured" in record.message for record in records)


async def test_dev_mode_default_secret_warns_and_startup_continues(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "secret_key", "change-me-in-production")

    with caplog.at_level(logging.WARNING, logger="stoa.main"):
        await _run_lifespan()

    assert _has_secret_warning(caplog.records)


async def test_production_mode_default_secret_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "secret_key", "change-me-in-production")

    with pytest.raises(RuntimeError, match="SECRET_KEY is not securely configured"):
        await _run_lifespan()


async def test_production_mode_short_secret_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "secret_key", "too-short")

    with pytest.raises(RuntimeError, match="SECRET_KEY is not securely configured"):
        await _run_lifespan()


async def test_production_mode_strong_secret_starts_without_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "secret_key", "s" * 48)

    with caplog.at_level(logging.WARNING, logger="stoa.main"):
        await _run_lifespan()

    assert not _has_secret_warning(caplog.records)


async def test_dev_mode_empty_secret_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "development")
    monkeypatch.setattr(main.settings, "secret_key", "")

    with caplog.at_level(logging.WARNING, logger="stoa.main"):
        await _run_lifespan()

    assert _has_secret_warning(caplog.records)


async def test_admin_key_validation_remains_warning_only_in_production(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "secret_key", "s" * 48)
    monkeypatch.delenv("ADMIN_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="stoa.main"):
        await _run_lifespan()

    assert any("ADMIN_KEY not set" in record.message for record in caplog.records)
    assert not _has_secret_warning(caplog.records)


async def test_unknown_app_env_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "prodcution")
    monkeypatch.setattr(main.settings, "secret_key", "s" * 48)

    with pytest.raises(RuntimeError, match="Unknown APP_ENV"):
        await _run_lifespan()


async def test_prod_alias_treated_as_production(
    monkeypatch: pytest.MonkeyPatch,
    isolate_lifespan_dependencies: None,
) -> None:
    monkeypatch.setattr(main.settings, "app_env", "prod")
    monkeypatch.setattr(main.settings, "secret_key", "change-me-in-production")

    with pytest.raises(RuntimeError, match="SECRET_KEY is not securely configured"):
        await _run_lifespan()
