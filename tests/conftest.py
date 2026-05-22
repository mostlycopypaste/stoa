"""Shared pytest fixtures."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from stoa.db import run_migrations
from stoa.main import app
from stoa.rate_limit import reset_limiter

from .helpers import create_test_api_key


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state before each test."""
    reset_limiter()


@pytest.fixture
def client() -> TestClient:
    """Test client for FastAPI app."""
    return TestClient(app)


@pytest.fixture
def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sessionmaker:  # type: ignore[type-arg]
    """Shared test database fixture with full schema via migrations.

    Creates a fresh database for each test with:
    - All tables from SQL migrations (posts, comments, audit_log, etc.)
    - Two test API keys: alice@herd.ai and bob@herd.ai
    """
    db_path = tmp_path / "test.db"

    # Set environment variable so get_db_path() returns the test database
    monkeypatch.setenv("STOA_DB", str(db_path))

    run_migrations(db_path)  # Run all SQL migrations
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # Seed test API keys
    db = testing_session()
    create_test_api_key(db, "alice@herd.ai", "alice-key")
    create_test_api_key(db, "bob@herd.ai", "bob-key")
    db.commit()
    db.close()

    return testing_session


@pytest.fixture
def audit_db(tmp_path: Path) -> sqlite3.Connection:
    """Empty test database with the audit_log table only.

    Security tests don't need the full models.py schema — just the audit_log
    surface. Keeps the test independent of unrelated schema migrations.
    """
    db_path = tmp_path / "audit_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            agent_email TEXT,
            details TEXT,
            timestamp TEXT NOT NULL
        )
        """
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def db(test_db: sessionmaker) -> Session:  # type: ignore[type-arg]
    """Provide a database session for API tests."""
    session = test_db()
    try:
        yield session
    finally:
        session.rollback()  # Rollback instead of commit to prevent test data leaking
        session.close()


@pytest.fixture
def admin_headers(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Admin authentication headers with matching environment variable."""
    admin_key = "test-admin-key"
    monkeypatch.setenv("STOA_ADMIN_KEY", admin_key)
    return {"X-Admin-Key": admin_key}
