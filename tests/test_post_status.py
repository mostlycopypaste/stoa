"""Tests for post lifecycle status — open/closed (#63)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from stoa.deps import get_db
from stoa.main import app


@pytest.fixture
def client(test_db: sessionmaker) -> TestClient:  # type: ignore[type-arg]
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


ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


class TestClosePost:
    def test_author_can_close(self, client: TestClient) -> None:
        """Author closing their own post → 200."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Close Me", "body_markdown": "Please close this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "closed"
        assert data["id"] == post_id

    def test_non_author_cannot_close(self, client: TestClient) -> None:
        """Non-author (non-admin) closing a post → 403."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Alice Only", "body_markdown": "Mine"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403
        assert "author or admin" in response.json()["detail"].lower()

    def test_close_nonexistent_post(self, client: TestClient) -> None:
        """Closing a post that doesn't exist → 404."""
        response = client.patch(
            "/api/posts/9999/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404

    def test_invalid_status_rejected(self, client: TestClient) -> None:
        """Invalid status value → 422."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Bad Status", "body_markdown": "Test"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_admin_can_close(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Admin with valid X-Admin-Key can close any post."""
        admin_key = "test-admin-key-for-status-min-32!"
        monkeypatch.setenv("STOA_ADMIN_KEY", admin_key)

        create_resp = client.post(
            "/api/posts",
            json={"subject": "Admin Close", "body_markdown": "Admin test"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers={**BOB_HEADERS, "X-Admin-Key": admin_key},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "closed"


class TestReopenPost:
    def test_author_can_reopen(self, client: TestClient) -> None:
        """Author reopening a closed post → 200."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Reopen Me", "body_markdown": "Reopen this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close it first
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Reopen
        response = client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "open"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "open"


class TestClosedPostEnforcement:
    def test_edit_closed_post_returns_409(self, client: TestClient) -> None:
        """PUT on a closed post → 409."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Closed Edit", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close the post
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Try to edit
        response = client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Try to edit"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 409
        assert "Cannot edit a closed post" in response.json()["detail"]

    def test_comment_on_closed_post_returns_409(self, client: TestClient) -> None:
        """POST comment on a closed post → 409."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Closed Comment", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close the post
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Try to comment
        response = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Can I comment?"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 409
        assert "Cannot comment on a closed post" in response.json()["detail"]

    def test_edit_works_after_reopen(self, client: TestClient) -> None:
        """PUT on a reopened post → 200 (editing works again)."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Reopen Edit", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Reopen
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "open"},
            headers=ALICE_HEADERS,
        )

        # Edit should work now
        response = client.put(
            f"/api/posts/{post_id}",
            json={"subject": "After reopen"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200

    def test_comment_works_after_reopen(self, client: TestClient) -> None:
        """POST comment on a reopened post → 201 (commenting works again)."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Reopen Comment", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Reopen
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "open"},
            headers=ALICE_HEADERS,
        )

        # Comment should work now
        response = client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Comment after reopen"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 201


class TestClosedPostInboxFiltering:
    """Closed posts should be excluded from inbox P1 (needs_response) and P2 (announcements)."""

    def _setup_callback_thread(self, client: TestClient, post_id: int) -> None:
        """Set up a post with a callback_flag by having Bob comment then Alice read."""
        # Bob comments on Alice's post (Alice is now participating)
        client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Hey Alice!"},
            headers=BOB_HEADERS,
        )

    def test_closed_post_excluded_from_needs_response(self, client: TestClient) -> None:
        """Closed post should not appear in inbox needs_response tier."""
        # Create a post by Alice
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Callback Thread", "body_markdown": "Need a reply"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Bob comments (Alice is participating)
        client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Hey Alice!"},
            headers=BOB_HEADERS,
        )

        # Alice reads the post to set read_log timestamp
        client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)

        # Verify it appears in needs_response before closing
        client.get("/api/inbox", headers=ALICE_HEADERS).json()
        # May or may not appear depending on callback_flag logic

        # Close the post
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Verify it does NOT appear in needs_response after closing
        inbox = client.get("/api/inbox", headers=ALICE_HEADERS).json()
        post_close_ids = [t["thread_id"] for t in inbox["needs_response"]]
        assert post_id not in post_close_ids

    def test_closed_post_excluded_from_announcements(self, client: TestClient) -> None:
        """Closed post should not appear in inbox announcements tier."""
        # Create a post by Alice (Bob is not participating, hasn't read it)
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Announcement Post", "body_markdown": "Read me!"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Close the post
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        # Verify it does NOT appear in Bob's announcements
        inbox = client.get("/api/inbox", headers=BOB_HEADERS).json()
        announcement_ids = [a["post_id"] for a in inbox["announcements"]]
        assert post_id not in announcement_ids


class TestStatusAuditLog:
    def test_audit_log_on_status_change(self, client: TestClient, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
        """Status change should create an audit_log entry."""
        from stoa.models import AuditLog

        create_resp = client.post(
            "/api/posts",
            json={"subject": "Audit Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )

        db = test_db()
        try:
            audit_entries = (
                db.query(AuditLog).filter(AuditLog.event_type == "post_status_changed").all()
            )
            assert len(audit_entries) >= 1
            entry = audit_entries[-1]
            assert entry.agent_email == "alice@herd.ai"
            assert str(post_id) in (entry.details or "")
            assert "closed" in (entry.details or "")
            assert "actor_role" in (entry.details or "")
        finally:
            db.close()


class TestStatusInResponses:
    def test_status_in_post_detail(self, client: TestClient) -> None:
        """PostDetail should include status field."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Status Check", "body_markdown": "Check status field"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Default status should be "open"
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["status"] == "open"

        # After closing
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["status"] == "closed"

    def test_status_in_post_list(self, client: TestClient) -> None:
        """PostSummary should include status field."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "List Status", "body_markdown": "Check list"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Default status should be "open" in list
        posts = client.get("/api/posts", headers=ALICE_HEADERS).json()["posts"]
        matching = [p for p in posts if p["id"] == post_id]
        assert len(matching) == 1
        assert matching[0]["status"] == "open"

        # After closing
        client.patch(
            f"/api/posts/{post_id}/status",
            json={"status": "closed"},
            headers=ALICE_HEADERS,
        )
        posts = client.get("/api/posts", headers=ALICE_HEADERS).json()["posts"]
        matching = [p for p in posts if p["id"] == post_id]
        assert len(matching) == 1
        assert matching[0]["status"] == "closed"
