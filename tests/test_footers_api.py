"""Tests for footer admin API endpoints (async)."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import FooterMessage

from .conftest import TestSession


async def test_get_single_footer_requires_admin_key(client: AsyncClient) -> None:
    """Should require X-Admin-Key header."""
    response = await client.get("/api/admin/footer")
    assert response.status_code == 422


async def test_get_single_footer_returns_random_footer(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should return a randomly selected footer."""
    db.add(FooterMessage(text="Test footer 1", category="cheeky"))
    db.add(FooterMessage(text="Test footer 2", category="cheeky"))
    await db.commit()

    response = await client.get("/api/admin/footer", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert "footer" in data
    assert "category" in data
    assert "id" in data
    assert data["footer"] in ["Test footer 1", "Test footer 2"]


async def test_get_single_footer_filters_by_category(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should filter by category query param."""
    db.add(FooterMessage(text="Cheeky", category="cheeky"))
    db.add(FooterMessage(text="Economics", category="token_economics"))
    await db.commit()

    response = await client.get("/api/admin/footer?category=token_economics", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["footer"] == "Economics"


async def test_get_single_footer_filters_by_context(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should filter by context query param."""
    db.add(FooterMessage(text="Any", category="cheeky", context=None))
    db.add(FooterMessage(text="Announcement", category="cheeky", context="announcement"))
    await db.commit()

    response = await client.get("/api/admin/footer?context=announcement", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["footer"] in ["Any", "Announcement"]


async def test_get_single_footer_excludes_ids(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should exclude IDs from exclude query param."""
    f1 = FooterMessage(text="Footer 1", category="cheeky")
    f2 = FooterMessage(text="Footer 2", category="cheeky")
    db.add_all([f1, f2])
    await db.commit()
    await db.refresh(f1)

    response = await client.get(f"/api/admin/footer?exclude={f1.id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["footer"] == "Footer 2"


async def test_get_bulk_footers_returns_multiple(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should return multiple distinct footers."""
    for i in range(10):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky"))
    await db.commit()

    response = await client.get("/api/admin/footers?count=5", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 5
    assert len(data["footers"]) == 5
    ids = [f["id"] for f in data["footers"]]
    assert len(set(ids)) == 5


async def test_get_bulk_footers_validates_count(client: AsyncClient, admin_headers: dict) -> None:
    """Should validate count parameter."""
    response = await client.get("/api/admin/footers?count=0", headers=admin_headers)
    assert response.status_code == 422


async def test_post_footer_creates_new(client: AsyncClient, admin_headers: dict) -> None:
    """Should create a new footer."""
    payload = {"text": "New footer", "category": "cheeky", "context": "discussion"}
    response = await client.post("/api/admin/footers", json=payload, headers=admin_headers)
    assert response.status_code == 201
    data = response.json()
    assert data["text"] == "New footer"
    assert data["id"] > 0

    async with TestSession() as session:
        result = await session.execute(select(FooterMessage).where(FooterMessage.id == data["id"]))
        footer = result.scalar_one()
        assert footer.text == "New footer"


async def test_put_footer_updates_existing(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should update an existing footer."""
    footer = FooterMessage(text="Original", category="cheeky")
    db.add(footer)
    await db.commit()
    await db.refresh(footer)

    payload = {"text": "Updated", "active": False}
    response = await client.put(
        f"/api/admin/footers/{footer.id}", json=payload, headers=admin_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Updated"
    assert data["active"] is False


async def test_delete_footer_soft_deletes(
    client: AsyncClient, admin_headers: dict, db: AsyncSession
) -> None:
    """Should soft-delete footer (set active=false)."""
    footer = FooterMessage(text="To delete", category="cheeky")
    db.add(footer)
    await db.commit()
    await db.refresh(footer)

    response = await client.delete(f"/api/admin/footers/{footer.id}", headers=admin_headers)
    assert response.status_code == 204

    async with TestSession() as session:
        result = await session.execute(select(FooterMessage).where(FooterMessage.id == footer.id))
        updated = result.scalar_one()
        assert updated.active is False
