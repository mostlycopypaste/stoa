"""Tests for post editing with version history (#54) and post management (#58).

Covers:
- PostRevision snapshots on edit
- Frozen subjects (subject edits rejected/ignored)
- GET /api/posts/{id}/revisions
- Archive (status='archived')
- Move (change channel_id)
- Pin / unpin (admin only)
- Soft delete (status='deleted')
- Listing behavior (archived/deleted hidden, pinned first)
"""

from httpx import AsyncClient

ALICE_HEADERS = {"X-API-Key": "alice-key"}
BOB_HEADERS = {"X-API-Key": "bob-key"}


async def _create_post(
    client: AsyncClient, subject: str = "Test Post", body: str = "Test body"
) -> int:
    """Create a post as Alice and return its id."""
    resp = await client.post(
        "/api/posts",
        json={"subject": subject, "body_markdown": body},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_group_and_channel(
    client: AsyncClient, name: str = "Test Group"
) -> tuple[int, int]:
    """Create a group+channel as Alice and return (group_id, channel_id)."""
    resp = await client.post(
        "/api/groups",
        json={"name": name, "description": "For management tests"},
        headers=ALICE_HEADERS,
    )
    assert resp.status_code == 201
    group_id = resp.json()["id"]

    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE_HEADERS)
    assert resp.status_code == 200
    return group_id, resp.json()[0]["id"]


# ─── Editing + Revisions (#54) ──────────────────────────────────────────────


class TestPostEditingRevisions:
    async def test_edit_saves_revision(self, client: AsyncClient) -> None:
        """Editing a post saves a PostRevision snapshot and updates body."""
        post_id = await _create_post(client, "Rev Test", "Original body")

        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edited body content"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revision_number"] == 1
        assert data["tldr"] == "Edited body content"
        assert data["updated_at"] is not None

        # Verify the post was updated
        detail = (await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)).json()
        assert detail["body_markdown"] == "Edited body content"

    async def test_multiple_edits_create_multiple_revisions(self, client: AsyncClient) -> None:
        """Each edit creates a new revision with incrementing number."""
        post_id = await _create_post(client, "Multi Rev", "Body v1")

        r1 = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Body v2"},
            headers=ALICE_HEADERS,
        )
        assert r1.json()["revision_number"] == 1

        r2 = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Body v3"},
            headers=ALICE_HEADERS,
        )
        assert r2.json()["revision_number"] == 2

    async def test_subject_frozen_on_edit(self, client: AsyncClient) -> None:
        """Subject field is not accepted in PostUpdate (frozen, issue #54)."""
        post_id = await _create_post(client, "Frozen Subject", "Body")

        # Sending subject only should fail (422) since it's not a valid field
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"subject": "Changed Subject"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_edit_non_author_403(self, client: AsyncClient) -> None:
        """Non-author cannot edit another agent's post."""
        post_id = await _create_post(client, "Alice's Post", "Alice's body")

        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Bob's edit"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403

    async def test_edit_nonexistent_404(self, client: AsyncClient) -> None:
        """Editing a non-existent post → 404."""
        response = await client.put(
            "/api/posts/9999",
            json={"body_markdown": "Nope"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404

    async def test_edit_archived_post_409(self, client: AsyncClient) -> None:
        """Cannot edit an archived post → 409."""
        post_id = await _create_post(client, "Archive Edit", "Body")

        # Archive it
        await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )

        # Try to edit
        response = await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edit after archive"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 409
        assert "archived" in response.json()["detail"]


class TestGetRevisions:
    async def test_get_revisions_as_author(self, client: AsyncClient) -> None:
        """Author can list revisions for their post."""
        post_id = await _create_post(client, "Rev List", "Original")

        await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "First edit"},
            headers=ALICE_HEADERS,
        )
        await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Second edit"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            f"/api/posts/{post_id}/revisions",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        revisions = response.json()
        assert len(revisions) == 2
        assert revisions[0]["revision_number"] == 1
        assert revisions[1]["revision_number"] == 2
        assert revisions[0]["body_markdown"] == "Original"
        assert revisions[1]["body_markdown"] == "First edit"

    async def test_get_revisions_non_author_403(self, client: AsyncClient) -> None:
        """Non-author (non-admin) cannot view revisions → 403."""
        post_id = await _create_post(client, "Private Revs", "Body")

        await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edited"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            f"/api/posts/{post_id}/revisions",
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403

    async def test_get_revisions_nonexistent_post_404(self, client: AsyncClient) -> None:
        """Revisions for non-existent post → 404."""
        response = await client.get(
            "/api/posts/9999/revisions",
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404

    async def test_get_revisions_admin_allowed(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """Admin can view revisions for any post."""
        post_id = await _create_post(client, "Admin Revs", "Body")

        await client.put(
            f"/api/posts/{post_id}",
            json={"body_markdown": "Edited"},
            headers=ALICE_HEADERS,
        )

        response = await client.get(
            f"/api/posts/{post_id}/revisions",
            headers={**BOB_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        assert len(response.json()) == 1


# ─── Archive (#58) ───────────────────────────────────────────────────────────


class TestArchive:
    async def test_author_archives_own_post(self, client: AsyncClient) -> None:
        """Author can archive their own post → 200, status='archived'."""
        post_id = await _create_post(client, "Archive Me", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "archived"

    async def test_archived_post_hidden_from_default_listing(self, client: AsyncClient) -> None:
        """Archived posts are excluded from default listing."""
        post_id = await _create_post(client, "Hidden Archive", "Body")

        await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )

        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert all(p["id"] != post_id for p in posts)

    async def test_archived_post_visible_with_status_param(self, client: AsyncClient) -> None:
        """Archived posts visible with ?status=archived."""
        post_id = await _create_post(client, "Visible Archive", "Body")

        await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )

        response = await client.get("/api/posts?status=archived", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        matching = [p for p in posts if p["id"] == post_id]
        assert len(matching) == 1
        assert matching[0]["status"] == "archived"

    async def test_non_author_archive_403(self, client: AsyncClient) -> None:
        """Non-author (non-admin) cannot archive → 403."""
        post_id = await _create_post(client, "Alice Only", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403

    async def test_admin_archives_any_post(self, client: AsyncClient, admin_headers: dict) -> None:
        """Admin can archive any post."""
        post_id = await _create_post(client, "Admin Archive", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived"},
            headers={**BOB_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "archived"


# ─── Move (#58) ──────────────────────────────────────────────────────────────


class TestMove:
    async def test_author_moves_post_to_channel(self, client: AsyncClient) -> None:
        """Author can move post to another channel in a group they're a member of."""
        _, channel_id = await _create_group_and_channel(client, "Move Group")
        post_id = await _create_post(client, "Move Me", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"channel_id": channel_id},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "open"  # status unchanged

    async def test_move_to_nonexistent_channel_404(self, client: AsyncClient) -> None:
        """Moving to a non-existent channel → 404."""
        post_id = await _create_post(client, "Bad Move", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"channel_id": 9999},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404

    async def test_move_to_channel_not_member_403(self, client: AsyncClient) -> None:
        """Moving to a channel in a group the author isn't a member of → 403."""
        # Alice creates a group+channel
        _, channel_id = await _create_group_and_channel(client, "Alice's Group")

        # Bob creates a post (no channel)
        resp = await client.post(
            "/api/posts",
            json={"subject": "Bob Move", "body_markdown": "Body"},
            headers=BOB_HEADERS,
        )
        post_id = resp.json()["id"]

        # Bob tries to move to Alice's channel (not a member)
        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"channel_id": channel_id},
            headers=BOB_HEADERS,
        )
        assert response.status_code == 403


# ─── Pin (#58) ───────────────────────────────────────────────────────────────


class TestPin:
    async def test_admin_pins_post(self, client: AsyncClient, admin_headers: dict) -> None:
        """Admin can pin a post → 200, pinned=True, pinned_at set."""
        post_id = await _create_post(client, "Pin Me", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"pinned": True},
            headers={**ALICE_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pinned"] is True
        assert data["pinned_at"] is not None

    async def test_non_admin_pin_403(self, client: AsyncClient) -> None:
        """Non-admin cannot pin → 403."""
        post_id = await _create_post(client, "No Pin", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"pinned": True},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 403

    async def test_pinned_post_appears_first_in_listing(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """Pinned posts appear before unpinned ones in listings."""
        # Create two posts
        post1_id = await _create_post(client, "First Post", "Body 1")
        post2_id = await _create_post(client, "Second Post", "Body 2")

        # Pin the second one (older by creation order, but pinned should be first)
        await client.patch(
            f"/api/posts/{post2_id}/manage",
            json={"pinned": True},
            headers={**ALICE_HEADERS, **admin_headers},
        )

        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        posts = response.json()["posts"]
        assert posts[0]["id"] == post2_id
        assert posts[0]["pinned"] is True
        assert posts[1]["id"] == post1_id
        assert posts[1]["pinned"] is False

    async def test_unpin_post(self, client: AsyncClient, admin_headers: dict) -> None:
        """Admin can unpin a post → pinned=False, pinned_at=None."""
        post_id = await _create_post(client, "Unpin Me", "Body")

        # Pin first
        await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"pinned": True},
            headers={**ALICE_HEADERS, **admin_headers},
        )

        # Unpin
        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"pinned": False},
            headers={**ALICE_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        assert response.json()["pinned"] is False
        assert response.json()["pinned_at"] is None


# ─── Soft Delete (#58) ──────────────────────────────────────────────────────


class TestSoftDelete:
    async def test_author_soft_deletes_own_post(self, client: AsyncClient) -> None:
        """Author soft-deletes own post → 204, post hidden from listings."""
        post_id = await _create_post(client, "Delete Me", "Body")

        response = await client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 204

        # Post should be 404 on direct fetch
        response = await client.get(f"/api/posts/{post_id}", headers=ALICE_HEADERS)
        assert response.status_code == 404

    async def test_soft_deleted_hidden_from_all_listings(self, client: AsyncClient) -> None:
        """Soft-deleted posts hidden from all listings, even ?status=archived."""
        post_id = await _create_post(client, "Hidden Delete", "Body")

        await client.delete(f"/api/posts/{post_id}", headers=ALICE_HEADERS)

        # Default listing
        response = await client.get("/api/posts", headers=ALICE_HEADERS)
        assert all(p["id"] != post_id for p in response.json()["posts"])

        # Archived listing
        response = await client.get("/api/posts?status=archived", headers=ALICE_HEADERS)
        assert all(p["id"] != post_id for p in response.json()["posts"])

        # All listing
        response = await client.get("/api/posts?status=all", headers=ALICE_HEADERS)
        # Soft-deleted should still be hidden from standard listings
        assert all(p["id"] != post_id for p in response.json()["posts"])

    async def test_admin_soft_deletes_any_post(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """Admin can soft-delete any post."""
        post_id = await _create_post(client, "Admin Delete", "Body")

        response = await client.delete(
            f"/api/posts/{post_id}",
            headers={**BOB_HEADERS, **admin_headers},
        )
        assert response.status_code == 204

    async def test_non_author_non_admin_delete_403(self, client: AsyncClient) -> None:
        """Non-author non-admin cannot delete → 403."""
        post_id = await _create_post(client, "Alice's Post", "Body")

        response = await client.delete(f"/api/posts/{post_id}", headers=BOB_HEADERS)
        assert response.status_code == 403


# ─── Combined / Edge Cases ────────────────────────────────────────────────────


class TestManageEdgeCases:
    async def test_empty_manage_body_422(self, client: AsyncClient) -> None:
        """Empty manage body → 422."""
        post_id = await _create_post(client, "Edge", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 422

    async def test_manage_nonexistent_post_404(self, client: AsyncClient) -> None:
        """Managing a non-existent post → 404."""
        response = await client.patch(
            "/api/posts/9999/manage",
            json={"status": "archived"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 404

    async def test_admin_can_soft_delete_via_manage(
        self, client: AsyncClient, admin_headers: dict
    ) -> None:
        """Admin can set status to 'deleted' via manage endpoint."""
        post_id = await _create_post(client, "Manage Delete", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "deleted"},
            headers={**BOB_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"

    async def test_non_admin_cannot_delete_via_manage(self, client: AsyncClient) -> None:
        """Non-admin cannot set status to 'deleted' via manage → 403."""
        post_id = await _create_post(client, "No Delete", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "deleted"},
            headers=ALICE_HEADERS,
        )
        assert response.status_code == 403

    async def test_combined_archive_and_pin(self, client: AsyncClient, admin_headers: dict) -> None:
        """Admin can archive and pin in one request."""
        post_id = await _create_post(client, "Combo", "Body")

        response = await client.patch(
            f"/api/posts/{post_id}/manage",
            json={"status": "archived", "pinned": True},
            headers={**ALICE_HEADERS, **admin_headers},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "archived"
        assert data["pinned"] is True
