"""SSL policy must survive URL normalization and both engine entrypoints."""

import asyncio
import inspect
import runpy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from stoa.config import Settings, settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SSL_MODES = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


@pytest.fixture
def setup_db() -> None:
    """These tests intercept connections and do not need the shared test schema."""


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
@pytest.mark.parametrize("mode", SSL_MODES)
def test_sslmode_preserves_operator_policy(scheme: str, mode: str) -> None:
    configured = Settings(database_url=f"{scheme}://user:password@localhost/stoa?sslmode={mode}")
    url = make_url(configured.database_url)

    assert url.drivername == "postgresql+asyncpg"
    assert url.query == {"ssl": mode}


def test_normalization_preserves_credentials_and_unrelated_query_parameters() -> None:
    configured = Settings(
        database_url="postgres://user:p%40ss%25word@localhost/stoa?sslmode=require"
        "&target_session_attrs=read-write&prepared_statement_cache_size=0"
        "&host=primary:5432&host=secondary:5432&unused="
    )

    assert make_url(configured.database_url).password == "p@ss%word"
    assert parse_qs(urlsplit(configured.database_url).query, keep_blank_values=True) == {
        "ssl": ["require"],
        "prepared_statement_cache_size": ["0"],
        "host": ["primary:5432", "secondary:5432"],
        "unused": [""],
    }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", {}),
        ("?ssl=verify-full", {"ssl": "verify-full"}),
        ("?sslmode=require&ssl=require", {"ssl": "require"}),
    ],
)
def test_existing_ssl_option_and_driver_default_are_preserved(
    query: str, expected: dict[str, str]
) -> None:
    configured = Settings(database_url=f"postgresql://user:password@localhost/stoa{query}")
    assert make_url(configured.database_url).query == expected


def test_conflicting_ssl_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting ssl and sslmode"):
        Settings(database_url="postgresql://localhost/stoa?sslmode=verify-full&ssl=disable")


@pytest.mark.parametrize(
    "query", ["sslmode=", "sslmode=invalid", "sslmode=require&sslmode=disable"]
)
def test_invalid_sslmode_is_rejected(query: str) -> None:
    with pytest.raises(ValueError, match="invalid sslmode"):
        Settings(database_url=f"postgresql://localhost/stoa?{query}")


def test_sqlite_url_is_unchanged() -> None:
    url = "sqlite+aiosqlite:///file:stoa.db?mode=ro&uri=true"
    assert Settings(database_url=url).database_url == url


class ConnectionInterceptedError(Exception):
    """Stop before network I/O after checking the real driver's arguments."""


@pytest.mark.parametrize("entrypoint", ["application", "migration"])
@pytest.mark.parametrize("mode", [None, *SSL_MODES])
def test_engines_deliver_ssl_policy_to_asyncpg(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, mode: str | None
) -> None:
    query = f"?sslmode={mode}" if mode is not None else ""
    configured = Settings(database_url=f"postgresql://user:password@localhost/stoa{query}")
    monkeypatch.setattr(settings, "database_url", configured.database_url)
    connect_signature = inspect.signature(asyncpg.connect)
    captured = {}

    async def intercept_connect(*args, **kwargs):
        # Binding the actual signature also catches unsupported keyword arguments.
        connect_signature.bind(*args, **kwargs)
        captured.update(kwargs)
        raise ConnectionInterceptedError

    monkeypatch.setattr(asyncpg, "connect", intercept_connect)

    with pytest.raises(ConnectionInterceptedError):
        if entrypoint == "application":
            # Execute the production engine setup without reloading shared ORM models.
            namespace = runpy.run_path(str(REPO_ROOT / "src/stoa/database.py"))
            engine = namespace["engine"]

            async def connect() -> None:
                try:
                    async with engine.connect():
                        pass
                finally:
                    await engine.dispose()

            asyncio.run(connect())
        else:
            config = Config()
            config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
            command.upgrade(config, "head")

    assert "sslmode" not in captured
    if mode is None:
        assert "ssl" not in captured
    else:
        assert captured["ssl"] == mode
