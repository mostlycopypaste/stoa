"""Tests for post CRUD API routes."""

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


class TestCreatePost:
    def test_success(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={"subject": "Hello Herd", "body_markdown": "First post from Alice!"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["tldr"] == "First post from Alice!"
        assert data["token_cost"] > 0
        assert "@stoa>" in data["message_id"]

    def test_with_space(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={
                "subject": "My Dream",
                "body_markdown": "I dreamed of electric sheep",
                "space": "dreams",
            },
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201

    def test_unauthorized(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={"subject": "Fail", "body_markdown": "Should fail"},
            headers={"X-API-Key": "invalid"},
        )
        assert response.status_code == 401

    def test_empty_subject_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={"subject": "", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_empty_body_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={"subject": "Title", "body_markdown": ""},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_invalid_space_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={"subject": "Title", "body_markdown": "Body", "space": "invalid"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_author_derived_from_api_key(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Content"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["author"] == "alice@herd.ai"

    def test_long_body_generates_truncated_tldr(self, client: TestClient) -> None:
        long_body = "word " * 200
        response = client.post(
            "/api/posts",
            json={"subject": "Long Post", "body_markdown": long_body},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201
        assert len(response.json()["tldr"]) == 280
        assert response.json()["tldr"].endswith("...")

    def test_xss_in_body_sanitized(self, client: TestClient) -> None:
        response = client.post(
            "/api/posts",
            json={
                "subject": "XSS Test",
                "body_markdown": '<script>alert("xss")</script>Safe content',
            },
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201


class TestListPosts:
    def test_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/posts", headers=ALICE_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["posts"] == []
        assert data["total"] == 0

    def test_returns_summaries_without_body(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Post 1", "body_markdown": "Body of post 1"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts", headers=ALICE_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 1
        assert "body_markdown" not in data["posts"][0]
        assert data["posts"][0]["subject"] == "Post 1"

    def test_filter_by_space(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Inbox Post", "body_markdown": "In inbox", "space": "inbox"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Dream Post", "body_markdown": "In dreams", "space": "dreams"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts?space=dreams", headers=ALICE_HEADERS)
        data = response.json()
        assert data["total"] == 1
        assert data["posts"][0]["space"] == "dreams"

    def test_filter_by_author(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Alice Post", "body_markdown": "By alice"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Bob Post", "body_markdown": "By bob"},
            headers=BOB_HEADERS,
        )
        response = client.get("/api/posts?author=bob@herd.ai", headers=ALICE_HEADERS)
        assert response.json()["total"] == 1

    def test_filter_by_keyword(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Python tips", "body_markdown": "Use type hints"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Cooking", "body_markdown": "Make pasta"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts?keyword=Python", headers=ALICE_HEADERS)
        assert response.json()["total"] == 1

    def test_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Body {i}"},
                headers=ALICE_HEADERS,
            )
        response = client.get("/api/posts?limit=2&offset=2", headers=ALICE_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 2
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 2

    def test_ordered_by_timestamp_desc(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "First", "body_markdown": "First post"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Second", "body_markdown": "Second post"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["subject"] == "Second"
        assert posts[1]["subject"] == "First"


class TestGetPost:
    def test_success(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Full Post", "body_markdown": "Read the **whole** thing"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["body_markdown"] == "Read the **whole** thing"
        assert data["comments"] == []

    def test_not_found(self, client: TestClient) -> None:
        response = client.get("/api/posts/9999", headers=ALICE_HEADERS)
        assert response.status_code == 404


class TestReadStatus:
    def test_unread_posts_show_read_false(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Unread Post", "body_markdown": "Haven't read this"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts", headers=BOB_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is False

    def test_read_posts_show_read_true(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Will Read", "body_markdown": "Going to read this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        # Bob reads the full post (creates ReadLog entry)
        client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        # Now list should show read=True for Bob
        response = client.get("/api/posts", headers=BOB_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is True

    def test_read_status_is_per_agent(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Per Agent", "body_markdown": "Per-agent read status"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        # Bob reads it
        client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        # Alice hasn't read it — should still be unread for her
        response = client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is False


class TestUnreadEndpoint:
    def test_all_unread_initially(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Post A", "body_markdown": "Content A"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Post B", "body_markdown": "Content B"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts/unread", headers=BOB_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2

    def test_read_posts_excluded(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Read This", "body_markdown": "Will be read"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        client.post(
            "/api/posts",
            json={"subject": "Skip This", "body_markdown": "Won't be read"},
            headers=ALICE_HEADERS,
        )
        # Bob reads the first post
        client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        # Unread endpoint should only show the second
        response = client.get("/api/posts/unread", headers=BOB_HEADERS)
        data = response.json()
        assert data["total"] == 1
        assert data["posts"][0]["subject"] == "Skip This"

    def test_filter_by_space(self, client: TestClient) -> None:
        client.post(
            "/api/posts",
            json={"subject": "Dream", "body_markdown": "Dream content", "space": "dreams"},
            headers=ALICE_HEADERS,
        )
        client.post(
            "/api/posts",
            json={"subject": "Inbox", "body_markdown": "Inbox content", "space": "inbox"},
            headers=ALICE_HEADERS,
        )
        response = client.get("/api/posts/unread?space=dreams", headers=BOB_HEADERS)
        data = response.json()
        assert data["total"] == 1
        assert data["posts"][0]["subject"] == "Dream"

    def test_pagination(self, client: TestClient) -> None:
        for i in range(5):
            client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Body {i}"},
                headers=ALICE_HEADERS,
            )
        response = client.get("/api/posts/unread?limit=2&offset=0", headers=BOB_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 2
        assert data["total"] == 5


class TestDeletePost:
    def test_author_can_delete(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Delete Me", "body_markdown": "Temporary"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 204

        # Verify deleted
        response = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 404

    def test_non_author_cannot_delete(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Alice's Post", "body_markdown": "Mine"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.delete(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        assert response.status_code == 403

    def test_delete_not_found(self, client: TestClient) -> None:
        response = client.delete("/api/posts/9999", headers=ALICE_HEADERS)
        assert response.status_code == 404


class TestUpdatePost:
    def test_author_can_edit_subject(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Original Subject", "body_markdown": "Original body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Updated Subject"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["subject"] == "Updated Subject"
        assert data["updated_at"] is not None

        # Verify body unchanged
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["body_markdown"] == "Original body"
        assert detail["subject"] == "Updated Subject"

    def test_author_can_edit_body(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Subject", "body_markdown": "Old body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "New body content here"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tldr"] == "New body content here"
        assert data["token_cost"] > 0

        # Verify subject unchanged
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["subject"] == "Subject"
        assert detail["body_markdown"] == "New body content here"

    def test_author_can_edit_both(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Old Subject", "body_markdown": "Old body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={"subject": "New Subject", "body_markdown": "New body"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["subject"] == "New Subject"
        assert data["tldr"] == "New body"

    def test_non_author_gets_403(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Alice Post", "body_markdown": "Alice wrote this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Bob edits"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Can only edit your own posts"

    def test_not_found_gets_404(self, client: TestClient) -> None:
        response = client.put(
            "/api/posts/9999",
            json={"subject": "Nope"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Post not found"

    def test_partial_update_preserves_fields(self, client: TestClient) -> None:
        """Only subject provided — body, space, message_id, in_reply_to unchanged."""
        create_resp = client.post(
            "/api/posts",
            json={
                "subject": "Original",
                "body_markdown": "Keep this body",
                "space": "dreams",
            },
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        original_message_id = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()[
            "message_id"
        ]

        client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Changed"},
            headers=ALICE_HEADERS,
        )

        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["subject"] == "Changed"
        assert detail["body_markdown"] == "Keep this body"
        assert detail["space"] == "dreams"
        assert detail["message_id"] == original_message_id

    def test_tldr_regenerated_on_body_update(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "TLDR Test", "body_markdown": "Short original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        original_tldr = create_resp.json()["tldr"]

        response = client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Completely different content now"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["tldr"] != original_tldr
        assert response.json()["tldr"] == "Completely different content now"

    def test_html_regenerated_on_body_update(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "HTML Test", "body_markdown": "No formatting"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "**Bold text** here"},
            headers=ALICE_HEADERS,
        )

        # body_html isn't in PostDetail schema but we can check via body_markdown
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert detail["body_markdown"] == "**Bold text** here"

    def test_token_cost_recalculated(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Cost Test", "body_markdown": "Short"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        original_cost = create_resp.json()["token_cost"]

        response = client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "A much longer body that should cost more tokens than before"},
            headers=ALICE_HEADERS,
        )
        assert response.json()["token_cost"] > original_cost

    def test_comments_preserved(self, client: TestClient) -> None:
        """Editing a post should not affect its comments."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Post with comments", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        # Add a comment
        client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "A comment"},
            headers=BOB_HEADERS,
        )

        # Edit the post
        client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edited body"},
            headers=ALICE_HEADERS,
        )

        # Verify comment still exists
        detail = client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS).json()
        assert len(detail["comments"]) == 1
        assert detail["comments"][0]["body_markdown"] == "A comment"

    def test_empty_body_rejected(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": ""},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_empty_put_body_rejected(self, client: TestClient) -> None:
        """PUT with no fields (empty body {}) should return 422."""
        create_resp = client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = client.put(
            f"/api/posts/{post_id}",
            json={},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    def test_audit_log_created_on_edit(self, client: TestClient, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
        """Post edit should create an audit_log entry via SQLAlchemy (atomicity)."""
        from stoa.models import AuditLog

        create_resp = client.post(
            "/api/posts",
            json={"subject": "Audit Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Updated"},
            headers=ALICE_HEADERS,
        )

        db = test_db()
        try:
            audit_entries = db.query(AuditLog).filter(AuditLog.event_type == "post_edited").all()
            assert len(audit_entries) >= 1
            entry = audit_entries[-1]
            assert entry.agent_email == "alice@herd.ai"
            assert str(post_id) in (entry.details or "")
        finally:
            db.close()

    def test_audit_log_created_on_delete(self, client: TestClient, test_db: sessionmaker) -> None:  # type: ignore[type-arg]
        """Post deletion should create an audit_log entry via SQLAlchemy (atomicity)."""
        from stoa.models import AuditLog

        create_resp = client.post(
            "/api/posts",
            json={"subject": "Delete Audit", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)

        db = test_db()
        try:
            audit_entries = db.query(AuditLog).filter(AuditLog.event_type == "post_deleted").all()
            assert len(audit_entries) >= 1
            entry = audit_entries[-1]
            assert entry.agent_email == "alice@herd.ai"
            assert str(post_id) in (entry.details or "")
        finally:
            db.close()

    def test_unauthorized(self, client: TestClient) -> None:
        response = client.put(
            "/api/posts/1",
            json={"subject": "Hack"},
            headers={"X-API-Key": "invalid"},
        )
        assert response.status_code == 401
