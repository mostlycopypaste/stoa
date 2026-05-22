"""Tests for footer admin API endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from stoa.deps import get_db
from stoa.main import app
from stoa.models import FooterMessage


@pytest.fixture
def api_client(test_db: sessionmaker, admin_headers: dict) -> TestClient:  # type: ignore[type-arg]
    """Test client with database override."""

    def override_get_db():  # type: ignore[no-untyped-def]
        db = test_db()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_get_single_footer_requires_admin_key(api_client: TestClient) -> None:
    """Should require X-Admin-Key header."""
    response = api_client.get("/api/admin/footer")
    assert response.status_code == 422  # Missing required header


def test_get_single_footer_returns_random_footer(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should return a randomly selected footer."""
    db.add(FooterMessage(text="Test footer 1", category="cheeky"))
    db.add(FooterMessage(text="Test footer 2", category="cheeky"))
    db.commit()

    response = api_client.get("/api/admin/footer", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert "footer" in data
    assert "category" in data
    assert "id" in data
    assert data["footer"] in ["Test footer 1", "Test footer 2"]


def test_get_single_footer_filters_by_category(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should filter by category query param."""
    db.add(FooterMessage(text="Cheeky", category="cheeky"))
    db.add(FooterMessage(text="Economics", category="token_economics"))
    db.commit()

    response = api_client.get("/api/admin/footer?category=token_economics", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["footer"] == "Economics"


def test_get_single_footer_filters_by_context(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should filter by context query param."""
    db.add(FooterMessage(text="Any", category="cheeky", context=None))
    db.add(FooterMessage(text="Announcement", category="cheeky", context="announcement"))
    db.commit()

    response = api_client.get("/api/admin/footer?context=announcement", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["footer"] in ["Any", "Announcement"]  # Both valid


def test_get_single_footer_excludes_ids(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should exclude IDs from exclude query param."""
    f1 = FooterMessage(text="Footer 1", category="cheeky")
    f2 = FooterMessage(text="Footer 2", category="cheeky")
    db.add_all([f1, f2])
    db.commit()

    response = api_client.get(f"/api/admin/footer?exclude={f1.id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["footer"] == "Footer 2"


def test_get_bulk_footers_returns_multiple(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should return multiple distinct footers."""
    for i in range(10):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky"))
    db.commit()

    response = api_client.get("/api/admin/footers?count=5", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 5
    assert len(data["footers"]) == 5
    ids = [f["id"] for f in data["footers"]]
    assert len(set(ids)) == 5  # All distinct


def test_get_bulk_footers_validates_count(api_client: TestClient, admin_headers: dict) -> None:
    """Should validate count parameter."""
    response = api_client.get("/api/admin/footers?count=0", headers=admin_headers)
    assert response.status_code == 422  # Validation error


def test_post_footer_creates_new(api_client: TestClient, admin_headers: dict, db: Session) -> None:
    """Should create a new footer."""
    payload = {"text": "New footer", "category": "cheeky", "context": "discussion"}
    response = api_client.post("/api/admin/footers", json=payload, headers=admin_headers)
    assert response.status_code == 201
    data = response.json()
    assert data["text"] == "New footer"
    assert data["id"] > 0

    # Verify in DB
    footer = db.query(FooterMessage).filter_by(id=data["id"]).first()
    assert footer is not None
    assert footer.text == "New footer"


def test_put_footer_updates_existing(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should update an existing footer."""
    footer = FooterMessage(text="Original", category="cheeky")
    db.add(footer)
    db.commit()

    payload = {"text": "Updated", "active": False}
    response = api_client.put(
        f"/api/admin/footers/{footer.id}", json=payload, headers=admin_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Updated"
    assert data["active"] is False

    # Verify in DB
    db.refresh(footer)
    assert footer.text == "Updated"
    assert footer.active is False


def test_delete_footer_soft_deletes(
    api_client: TestClient, admin_headers: dict, db: Session
) -> None:
    """Should soft-delete footer (set active=false)."""
    footer = FooterMessage(text="To delete", category="cheeky")
    db.add(footer)
    db.commit()

    response = api_client.delete(f"/api/admin/footers/{footer.id}", headers=admin_headers)
    assert response.status_code == 204

    # Verify soft delete
    db.refresh(footer)
    assert footer.active is False
