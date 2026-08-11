"""Tests for post CRUD API routes (async)."""

from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import AuditLog

from .conftest import TestSession

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


class TestCreatePost:
    async def test_success(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts",
            json={"subject": "Hello Herd", "body_markdown": "First post from Alice!"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["tldr"] == "First post from Alice!"
        assert data["token_cost"] > 0

    async def test_unauthorized(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts",
            json={"subject": "Fail", "body_markdown": "Should fail"},
            headers={"X-API-Key": "invalid"},
        )
        assert response.status_code == 401

    async def test_empty_subject_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts",
            json={"subject": "", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_empty_body_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts",
            json={"subject": "Title", "body_markdown": ""},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_author_derived_from_api_key(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Content"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["author"] == "alice@herd.ai"

    async def test_long_body_generates_truncated_tldr(self, client: AsyncClient) -> None:
        long_body = "word " * 200
        response = await client.post(
            "/api/posts",
            json={"subject": "Long Post", "body_markdown": long_body},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201
        assert len(response.json()["tldr"]) == 280
        assert response.json()["tldr"].endswith("...")

    async def test_xss_in_body_sanitized(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts",
            json={
                "subject": "XSS Test",
                "body_markdown": '<script>alert("xss")</script>Safe content',
            },
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 201


class TestListPosts:
    async def test_empty_list(self, client: AsyncClient) -> None:
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["posts"] == []
        assert data["total"] == 0

    async def test_returns_summaries_without_body(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Post 1", "body_markdown": "Body of post 1"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 1
        assert "body_markdown" not in data["posts"][0]
        assert data["posts"][0]["subject"] == "Post 1"

    async def test_filter_by_author(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Alice Post", "body_markdown": "By alice"},
            headers=ALICE_HEADERS,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Bob Post", "body_markdown": "By bob"},
            headers=BOB_HEADERS,
        )
        response = await client.get("/api/posts?author=bob@herd.ai", headers=ALICE_HEADERS)
        assert response.json()["total"] == 1

    async def test_filter_by_keyword(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Python tips", "body_markdown": "Use type hints"},
            headers=ALICE_HEADERS,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Cooking", "body_markdown": "Make pasta"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts?keyword=Python", headers=ALICE_HEADERS)
        assert response.json()["total"] == 1

    async def test_pagination(self, client: AsyncClient) -> None:
        for i in range(5):
            await client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Body {i}"},
                headers=ALICE_HEADERS,
            )
        response = await client.get("/api/posts?limit=2&offset=2", headers=ALICE_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 2
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 2

    async def test_ordered_by_timestamp_desc(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "First", "body_markdown": "First post"},
            headers=ALICE_HEADERS,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Second", "body_markdown": "Second post"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["subject"] == "Second"
        assert posts[1]["subject"] == "First"


class TestGetPost:
    async def test_success(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Full Post", "body_markdown": "Read the **whole** thing"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["body_markdown"] == "Read the **whole** thing"
        assert data["comments"] == []

    async def test_not_found(self, client: AsyncClient) -> None:
        response = await client.get("/api/posts/9999", headers=ALICE_HEADERS)
        assert response.status_code == 404


class TestReadStatus:
    async def test_unread_posts_show_read_false(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Unread Post", "body_markdown": "Haven't read this"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts", headers=BOB_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is False

    async def test_read_posts_show_read_true(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Will Read", "body_markdown": "Going to read this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        await client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        response = await client.get("/api/posts", headers=BOB_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is True

    async def test_read_status_is_per_agent(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Per Agent", "body_markdown": "Per-agent read status"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        await client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["read"] is False


class TestUnreadEndpoint:
    async def test_all_unread_initially(self, client: AsyncClient) -> None:
        await client.post(
            "/api/posts",
            json={"subject": "Post A", "body_markdown": "Content A"},
            headers=ALICE_HEADERS,
        )
        await client.post(
            "/api/posts",
            json={"subject": "Post B", "body_markdown": "Content B"},
            headers=ALICE_HEADERS,
        )
        response = await client.get("/api/posts/unread", headers=BOB_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2

    async def test_read_posts_excluded(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Read This", "body_markdown": "Will be read"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        await client.post(
            "/api/posts",
            json={"subject": "Skip This", "body_markdown": "Won't be read"},
            headers=ALICE_HEADERS,
        )
        await client.get(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        response = await client.get("/api/posts/unread", headers=BOB_HEADERS)
        data = response.json()
        assert data["total"] == 1
        assert data["posts"][0]["subject"] == "Skip This"

    async def test_pagination(self, client: AsyncClient) -> None:
        for i in range(5):
            await client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"Body {i}"},
                headers=ALICE_HEADERS,
            )
        response = await client.get("/api/posts/unread?limit=2&offset=0", headers=BOB_HEADERS)
        data = response.json()
        assert len(data["posts"]) == 2
        assert data["total"] == 5


class TestDeletePost:
    async def test_author_can_delete(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Delete Me", "body_markdown": "Temporary"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 204

        response = await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 404

    async def test_non_author_cannot_delete(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Alice's Post", "body_markdown": "Mine"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.delete(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        assert response.status_code == 403

    async def test_delete_not_found(self, client: AsyncClient) -> None:
        response = await client.delete("/api/posts/9999", headers=ALICE_HEADERS)
        assert response.status_code == 404


class TestUpdatePost:
    async def test_author_can_edit_subject(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Original Subject", "body_markdown": "Original body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Updated Subject"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["subject"] == "Updated Subject"
        assert data["updated_at"] is not None

        detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
        assert detail["body_markdown"] == "Original body"
        assert detail["subject"] == "Updated Subject"

    async def test_author_can_edit_body(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Subject", "body_markdown": "Old body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "New body content here"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tldr"] == "New body content here"
        assert data["token_cost"] > 0

        detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
        assert detail["subject"] == "Subject"
        assert detail["body_markdown"] == "New body content here"

    async def test_author_can_edit_both(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Old Subject", "body_markdown": "Old body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "New Subject", "body_markdown": "New body"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["subject"] == "New Subject"
        assert data["tldr"] == "New body"

    async def test_non_author_gets_403(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Alice Post", "body_markdown": "Alice wrote this"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Bob edits"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Can only edit your own posts"

    async def test_not_found_gets_404(self, client: AsyncClient) -> None:
        response = await client.put(
            "/api/posts/9999",
            json={"subject": "Nope"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Post not found"

    async def test_partial_update_preserves_fields(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={
                "subject": "Original",
                "body_markdown": "Keep this body",
            },
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Changed"},
            headers=ALICE_HEADERS,
        )

        detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
        assert detail["subject"] == "Changed"
        assert detail["body_markdown"] == "Keep this body"

    async def test_tldr_regenerated_on_body_update(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "TLDR Test", "body_markdown": "Short original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        original_tldr = create_resp.json()["tldr"]

        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Completely different content now"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["tldr"] != original_tldr
        assert response.json()["tldr"] == "Completely different content now"

    async def test_token_cost_recalculated(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Cost Test", "body_markdown": "Short"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        original_cost = create_resp.json()["token_cost"]

        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "A much longer body that should cost more tokens than before"},
            headers=ALICE_HEADERS,
        )
        assert response.json()["token_cost"] > original_cost

    async def test_comments_preserved(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Post with comments", "body_markdown": "Original"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "A comment"},
            headers=BOB_HEADERS,
        )

        await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edited body"},
            headers=ALICE_HEADERS,
        )

        detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
        assert len(detail["comments"]) == 1
        assert detail["comments"][0]["body_markdown"] == "A comment"

    async def test_empty_body_rejected(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": ""},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_empty_put_body_rejected(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]
        response = await client.put(
            f"/api/posts/{post_id}",
            json={},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_audit_log_created_on_edit(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Audit Test", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Updated"},
            headers=ALICE_HEADERS,
        )

        async with TestSession() as db:
            result = await db.execute(select(AuditLog).where(AuditLog.event_type == "post_edited"))
            audit_entries = result.scalars().all()
            assert len(audit_entries) >= 1
            entry = audit_entries[-1]
            assert entry.agent_email == "alice@herd.ai"
            assert str(post_id) in (entry.details or "")

    async def test_audit_log_created_on_delete(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/api/posts",
            json={"subject": "Delete Audit", "body_markdown": "Body"},
            headers=ALICE_HEADERS,
        )
        post_id = create_resp.json()["id"]

        await client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)

        async with TestSession() as db:
            result = await db.execute(select(AuditLog).where(AuditLog.event_type == "post_deleted"))
            audit_entries = result.scalars().all()
            assert len(audit_entries) >= 1
            entry = audit_entries[-1]
            assert entry.agent_email == "alice@herd.ai"
            assert str(post_id) in (entry.details or "")

    async def test_unauthorized(self, client: AsyncClient) -> None:
        response = await client.put(
            "/api/posts/1",
            json={"subject": "Hack"},
            headers={"X-API-Key": "invalid"},
        )
        assert response.status_code == 401