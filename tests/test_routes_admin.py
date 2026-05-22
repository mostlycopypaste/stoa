"""Tests for admin endpoints."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app
from stoa.models import AuditLog

ADMIN_KEY = "test-admin-secret-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def admin_client(test_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
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
    with patch.dict(os.environ, {"STOA_ADMIN_KEY": ADMIN_KEY}):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestAdminAuth:
    def test_missing_admin_key_returns_422(self, admin_client: TestClient) -> None:
        response = admin_client.get("/api/admin/stats")
        assert response.status_code == 422

    def test_invalid_admin_key_returns_401(self, admin_client: TestClient) -> None:
        response = admin_client.get("/api/admin/stats", headers={"X-Admin-Key": "wrong"})
        assert response.status_code == 401

    def test_no_env_var_returns_401(self, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
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
        with patch.dict(os.environ, {"STOA_ADMIN_KEY": ""}, clear=False):
            c = TestClient(app, raise_server_exceptions=False)
            response = c.get("/api/admin/stats", headers={"X-Admin-Key": "anything"})
            assert response.status_code == 401
        app.dependency_overrides.clear()


class TestCreateApiKey:
    def test_success(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/api/admin/keys?agent_email=newagent@herd.ai",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["agent_email"] == "newagent@herd.ai"
        assert data["api_key"].startswith("herd_")
        assert len(data["api_key"]) > 20

    def test_duplicate_agent_returns_409(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/api/admin/keys?agent_email=alice@herd.ai",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 409

    def test_generated_key_works_for_auth(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/admin/keys?agent_email=fresh@herd.ai",
            headers=ADMIN_HEADERS,
        )
        new_key = resp.json()["api_key"]

        # Use the new key to list posts
        response = admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert response.status_code == 200


class TestSystemStats:
    def test_empty_stats(self, admin_client: TestClient) -> None:
        response = admin_client.get("/api/admin/stats", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["total_posts"] == 0
        assert data["total_tokens_written"] == 0
        assert data["total_tokens_read"] == 0
        assert data["active_agents"] == 2  # alice + bob seeded

    def test_stats_after_activity(self, admin_client: TestClient) -> None:
        # Create a key and post something
        admin_client.post(
            "/api/admin/keys?agent_email=poster@herd.ai",
            headers=ADMIN_HEADERS,
        )
        # Just use alice's key
        admin_client.post(
            "/api/posts",
            json={"subject": "Stats Test", "body_markdown": "Some content here"},
            headers={"X-API-Key": "alice-key"},
        )

        response = admin_client.get("/api/admin/stats", headers=ADMIN_HEADERS)
        data = response.json()
        assert data["total_posts"] == 1
        assert data["total_tokens_written"] > 0
        assert data["active_agents"] == 3  # alice + bob + poster


class TestAuditLog:
    def test_empty_audit_log(self, admin_client: TestClient) -> None:
        response = admin_client.get("/api/admin/audit", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        # The query itself creates an admin_audit_query log entry
        logs = response.json()
        assert len(logs) == 1
        assert logs[0]["event_type"] == "admin_audit_query"

    def test_filter_by_event_type(self, admin_client: TestClient, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
        # Seed some audit entries
        db = test_db()
        db.add(AuditLog(event_type="auth_failure", agent_email=None, details="{}"))
        db.add(AuditLog(event_type="post_created", agent_email="a@herd.ai", details="{}"))
        db.add(AuditLog(event_type="auth_failure", agent_email=None, details="{}"))
        db.commit()
        db.close()

        response = admin_client.get(
            "/api/admin/audit?event_type=auth_failure", headers=ADMIN_HEADERS
        )
        entries = response.json()
        assert len(entries) == 2
        assert all(e["event_type"] == "auth_failure" for e in entries)

    def test_pagination(self, admin_client: TestClient, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
        db = test_db()
        for i in range(10):
            db.add(AuditLog(event_type=f"event_{i}", agent_email=None))
        db.commit()
        db.close()

        response = admin_client.get("/api/admin/audit?limit=3&offset=2", headers=ADMIN_HEADERS)
        entries = response.json()
        assert len(entries) == 3


class TestResetApiKey:
    def test_success_returns_new_key(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent_email"] == "alice@herd.ai"
        assert data["api_key"].startswith("herd_")
        assert len(data["api_key"]) > 20

    def test_new_key_works_for_auth(self, admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        new_key = resp.json()["api_key"]
        # Old key should no longer work
        old_resp = admin_client.get("/api/posts", headers={"X-API-Key": "alice-key"})
        assert old_resp.status_code == 401
        # New key should work
        new_resp = admin_client.get("/api/posts", headers={"X-API-Key": new_key})
        assert new_resp.status_code == 200

    def test_unknown_agent_returns_404(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/api/admin/keys/nobody@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 404

    def test_invalid_admin_key_returns_401(self, admin_client: TestClient) -> None:
        response = admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers={"X-Admin-Key": "wrong"},
        )
        assert response.status_code == 401

    def test_missing_admin_key_returns_422(self, admin_client: TestClient) -> None:
        response = admin_client.post("/api/admin/keys/alice@herd.ai/reset")
        assert response.status_code == 422

    def test_audit_log_records_key_reset(
        self,
        admin_client: TestClient,
        test_db: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        admin_client.post(
            "/api/admin/keys/alice@herd.ai/reset",
            headers=ADMIN_HEADERS,
        )
        db = test_db()
        entry = db.query(AuditLog).filter(AuditLog.event_type == "admin_key_reset").first()
        db.close()
        assert entry is not None
        assert entry.agent_email == "alice@herd.ai"
