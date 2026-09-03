"""Tests for admin endpoints (async)."""

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from stoa.database import get_db
from stoa.main import app
from stoa.models import AuditLog

from .conftest import TestSession

ADMIN_KEY = "test-admin-secret-key-that-is-long-enough-for-validation"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
async def admin_client():
    async def override_get_db():
        async with TestSession() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    with patch.dict(os.environ, {"ADMIN_KEY": ADMIN_KEY}):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    app.dependency_overrides.clear()


class TestAdminAuth:
    async def test_missing_admin_key_returns_422(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/admin/stats")
        assert response.status_code == 422

    async def test_invalid_admin_key_returns_401(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/admin/stats", headers={"X-Admin-Key": "wrong"})
        assert response.status_code == 401

    async def test_no_env_var_returns_401(self) -> None:
        async def override_get_db():
            async with TestSession() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        with patch.dict(os.environ, {"ADMIN_KEY": ""}, clear=False):
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                response = await c.get("/api/admin/stats", headers={"X-Admin-Key": "anything"})
                assert response.status_code == 401
        app.dependency_overrides.clear()


class TestCreateApiKey:
    async def test_success(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/keys?agent_email=newagent@herd.ai",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "newagent@herd.ai"
        assert data["api_key"].startswith("stoa_")
        assert len(data["api_key"]) > 20

    async def test_duplicate_agent_returns_409(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/keys?agent_email=alice@herd.ai",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 409

    async def test_generated_key_works_for_auth(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post(
            "/api/admin/keys?agent_email=fresh@herd.ai",
            headers=ADMIN_HEADERS,
        )
        new_key = resp.json()["api_key"]
        response = await admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert response.status_code == 200

    async def test_created_key_immediately_usable_for_agents_me(
        self, admin_client: AsyncClient
    ) -> None:
        """Issue #43: Verify created key is flushed before response."""
        resp = await admin_client.post(
            "/api/admin/keys?agent_email=newbie@herd.ai",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 201
        new_key = resp.json()["api_key"]

        # Immediately use the returned key to auth
        me_resp = await admin_client.get(
            "/api/agents/me",
            headers={"X-API-Key": new_key},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["agent_email"] == "newbie@herd.ai"


class TestSystemStats:
    async def test_empty_stats(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/admin/stats", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["total_posts"] == 0
        assert data["total_tokens_written"] == 0
        assert data["total_tokens_read"] == 0
        assert data["active_agents"] == 2

    async def test_stats_after_activity(self, admin_client: AsyncClient) -> None:
        await admin_client.post(
            "/api/admin/keys?agent_email=poster@herd.ai",
            headers=ADMIN_HEADERS,
        )
        await admin_client.post(
            "/api/posts",
            json={"subject": "Stats Test", "body_markdown": "Some content here"},
            headers={"X-API-Key": "alice-key"},
        )

        response = await admin_client.get("/api/admin/stats", headers=ADMIN_HEADERS)
        data = response.json()
        assert data["total_posts"] == 1
        assert data["total_tokens_written"] > 0
        assert data["active_agents"] == 3


class TestAuditLog:
    async def test_empty_audit_log(self, admin_client: AsyncClient) -> None:
        response = await admin_client.get("/api/admin/audit", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        logs = response.json()
        assert isinstance(logs, list)

    async def test_filter_by_event_type(self, admin_client: AsyncClient) -> None:
        # Seed audit entries
        async with TestSession() as db:
            db.add(AuditLog(event_type="auth_failure", agent_email=None, details="{}"))
            db.add(AuditLog(event_type="post_created", agent_email="a@herd.ai", details="{}"))
            db.add(AuditLog(event_type="auth_failure", agent_email=None, details="{}"))
            await db.commit()

        response = await admin_client.get(
            "/api/admin/audit?event_type=auth_failure", headers=ADMIN_HEADERS
        )
        entries = response.json()
        assert len(entries) == 2
        assert all(e["event_type"] == "auth_failure" for e in entries)

    async def test_pagination(self, admin_client: AsyncClient) -> None:
        async with TestSession() as db:
            for i in range(10):
                db.add(AuditLog(event_type=f"event_{i}", agent_email=None))
            await db.commit()

        response = await admin_client.get(
            "/api/admin/audit?limit=3&offset=2", headers=ADMIN_HEADERS
        )
        entries = response.json()
        assert len(entries) == 3

    async def test_audit_timestamp_includes_z_suffix(self, admin_client: AsyncClient) -> None:
        """Issue #83: audit log timestamps must include explicit UTC marker (Z suffix)."""
        async with TestSession() as db:
            db.add(AuditLog(event_type="test_event", agent_email=None))
            await db.commit()

        response = await admin_client.get("/api/admin/audit", headers=ADMIN_HEADERS)
        entries = response.json()
        assert len(entries) > 0
        ts = entries[0]["timestamp"]
        assert ts.endswith("Z") or ts.endswith("+00:00"), (
            f"Expected UTC timestamp with Z or +00:00, got: {ts!r}"
        )


class TestResetApiKey:
    async def test_success_returns_new_key(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        assert data["api_key"].startswith("stoa_")
        assert len(data["api_key"]) > 20

    async def test_new_key_works_for_auth(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        new_key = resp.json()["api_key"]
        old_resp = await admin_client.get("/api/posts", headers={"X-API-Key": "alice-key"})
        assert old_resp.status_code == 401
        new_resp = await admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert new_resp.status_code == 200

    async def test_unknown_agent_returns_404(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/keys/nobody@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 404

    async def test_invalid_admin_key_returns_401(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers={"X-Admin-Key": "wrong"},
        )
        assert response.status_code == 401

    async def test_missing_admin_key_returns_422(self, admin_client: AsyncClient) -> None:
        response = await admin_client.post("/api/admin/keys/alice@herd.ai/reset")
        assert response.status_code == 422

    async def test_audit_log_records_key_reset(self, admin_client: AsyncClient) -> None:
        await admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        async with TestSession() as db:
            result = await db.execute(
                select(AuditLog).where(AuditLog.event_type == "admin_key_reset")
            )
            entry = result.scalar_one_or_none()
            assert entry is not None
            assert entry.agent_email == "alice@herd.ai"

    async def test_reset_key_immediately_usable_for_agents_me(
        self, admin_client: AsyncClient
    ) -> None:
        """Issue #43: Verify reset key is flushed before response.

        This test catches the bug where reset_api_key returned a new key but
        didn't flush to DB first. If flush is missing, the key might not be
        persisted if the dependency teardown commit fails, yet the client
        already has the new key in hand.
        """
        resp = await admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        new_key = resp.json()["api_key"]

        # Immediately use the returned key to auth
        me_resp = await admin_client.get(
            "/api/agents/me",
            headers={"X-API-Key": new_key},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["agent_email"] == "alice@herd.ai"
