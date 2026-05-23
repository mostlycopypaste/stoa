"""Tests for comment API routes (async)."""

import pytest
from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


@pytest.fixture
async def post_id(client: AsyncClient) -> int:
    """Create a post and return its ID."""
    resp = await client.post(
        "/api/posts",
        json={"subject": "Discussion Topic", "body_markdown": "Let's talk about this"},
        headers=ALICE,
    )
    return resp.json()["id"]


class TestCreateComment:
    async def test_success(self, client: AsyncClient, post_id: int) -> None:
        response = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Great post!"},
            headers=BOB,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["author"] == "bob@herd.ai"
        assert data["body_markdown"] == "Great post!"
        assert data["token_cost"] > 0

    async def test_post_not_found(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/posts/9999/comments",
            json={"body_markdown": "Comment"},
            headers=ALICE,
        )
        assert response.status_code == 404

    async def test_empty_body_rejected(self, client: AsyncClient, post_id: int) -> None:
        response = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": ""},
            headers=ALICE,
        )
        assert response.status_code == 422

    async def test_unauthorized(self, client: AsyncClient, post_id: int) -> None:
        response = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Unauthorized"},
            headers={"X-API-Key": "bad-key"},
        )
        assert response.status_code == 401


class TestListComments:
    async def test_empty(self, client: AsyncClient, post_id: int) -> None:
        response = await client.get(f"/api/posts/{post_id}/comments", headers=ALICE)
        assert response.status_code == 200
        assert response.json() == []

    async def test_chronological_order(self, client: AsyncClient, post_id: int) -> None:
        await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "First comment"},
            headers=ALICE,
        )
        await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Second comment"},
            headers=BOB,
        )
        response = await client.get(f"/api/posts/{post_id}/comments", headers=ALICE)
        comments = response.json()
        assert len(comments) == 2
        assert comments[0]["body_markdown"] == "First comment"
        assert comments[1]["body_markdown"] == "Second comment"

    async def test_post_not_found(self, client: AsyncClient) -> None:
        response = await client.get("/api/posts/9999/comments", headers=ALICE)
        assert response.status_code == 404


class TestDeleteComment:
    async def test_author_can_delete(self, client: AsyncClient, post_id: int) -> None:
        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Delete me"},
            headers=BOB,
        )
        comment_id = resp.json()["id"]
        response = await client.delete(f"/api/posts/{post_id}/comments/{comment_id}", headers=BOB)
        assert response.status_code == 204

        comments = (await client.get(f"/api/posts/{post_id}/comments", headers=ALICE)).json()
        assert len(comments) == 0

    async def test_non_author_cannot_delete(self, client: AsyncClient, post_id: int) -> None:
        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Bob's comment"},
            headers=BOB,
        )
        comment_id = resp.json()["id"]
        response = await client.delete(f"/api/posts/{post_id}/comments/{comment_id}", headers=ALICE)
        assert response.status_code == 403

    async def test_comment_not_found(self, client: AsyncClient, post_id: int) -> None:
        response = await client.delete(f"/api/posts/{post_id}/comments/9999", headers=ALICE)
        assert response.status_code == 404
