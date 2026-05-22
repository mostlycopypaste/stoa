"""Tests for API key authentication."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from stoa.auth import get_current_agent
from stoa.deps import get_db
from stoa.models import Base

from .helpers import create_test_api_key


@pytest.fixture
def auth_db(tmp_path: Path) -> sessionmaker:  # type: ignore[type-arg]
    """Create a test database with the full schema and a test API key."""
    db_path = tmp_path / "auth_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    db = testing_session()
    create_test_api_key(db, "test-agent@herd.ai", "valid-test-key-123")
    db.commit()
    db.close()

    return testing_session


@pytest.fixture
def auth_client(auth_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
    """FastAPI test client with auth dependency and test database."""
    app = FastAPI()

    def override_get_db():  # type: ignore[no-untyped-def]
        db = auth_db()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    @app.get("/test-auth")
    def protected_route(
        agent_email: str = pytest.importorskip("fastapi").Depends(get_current_agent),
    ) -> dict[str, str]:  # type: ignore[type-arg]
        return {"agent": agent_email}

    return TestClient(app)


def test_valid_api_key_returns_agent_email(auth_client: TestClient) -> None:
    response = auth_client.get("/test-auth", headers={"X-API-Key": "valid-test-key-123"})
    assert response.status_code == 200
    assert response.json() == {"agent": "test-agent@herd.ai"}


def test_missing_api_key_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/test-auth")
    assert response.status_code == 422  # FastAPI returns 422 for missing required header


def test_invalid_api_key_returns_401(auth_client: TestClient) -> None:
    response = auth_client.get("/test-auth", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]
