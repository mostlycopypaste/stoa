"""Close-state channel gate and dashboard close_elections surface (issue #140).

Two things are pinned here:

* ``close-state`` and ``close-votes/history`` on a channel-scoped thread are
  403 for non-members of the owning group (same posture as PR #137), while
  unscoped posts stay readable.
* ``GET /api/me/dashboard`` lists close elections only for threads where the
  caller is a participant, so a private thread's vote activity never appears
  on an outsider's dashboard.
"""

from httpx import AsyncClient

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


async def _alice_channel(client: AsyncClient) -> int:
    """Create a group owned by Alice; Bob is NOT a member. Return its channel id."""
    resp = await client.post(
        "/api/groups",
        json={"name": "Election Group", "description": "close-election visibility tests"},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    group_id = resp.json()["id"]
    resp = await client.get(f"/api/groups/{group_id}/channels", headers=ALICE)
    assert resp.status_code == 200, resp.text
    return int(resp.json()[0]["id"])


async def _channel_post(client: AsyncClient, channel_id: int) -> int:
    resp = await client.post(
        f"/api/channels/{channel_id}/messages",
        json={"subject": "Private thread", "body_markdown": "Members-only discussion body."},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _unscoped_post(client: AsyncClient) -> int:
    resp = await client.post(
        "/api/posts",
        json={"subject": "Public thread", "body_markdown": "Unscoped public discussion body."},
        headers=ALICE,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


class TestCloseStateChannelGate:
    async def test_non_member_cannot_read_close_state(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        resp = await client.get(f"/api/posts/{post_id}/close-state", headers=BOB)
        assert resp.status_code == 403

    async def test_non_member_cannot_read_close_vote_history(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        resp = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=BOB)
        assert resp.status_code == 403

    async def test_member_can_read_close_state_and_history(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        state = await client.get(f"/api/posts/{post_id}/close-state", headers=ALICE)
        assert state.status_code == 200
        history = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=ALICE)
        assert history.status_code == 200

    async def test_unscoped_post_remains_readable_by_any_agent(self, client: AsyncClient) -> None:
        post_id = await _unscoped_post(client)
        state = await client.get(f"/api/posts/{post_id}/close-state", headers=BOB)
        assert state.status_code == 200
        history = await client.get(f"/api/posts/{post_id}/close-votes/history", headers=BOB)
        assert history.status_code == 200

    async def test_missing_post_is_still_404_not_403(self, client: AsyncClient) -> None:
        resp = await client.get("/api/posts/999999/close-state", headers=BOB)
        assert resp.status_code == 404


class TestDashboardCloseElections:
    async def test_empty_when_no_votes(self, client: AsyncClient) -> None:
        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        body = resp.json()
        assert body["close_elections"] == []
        assert "close_elections" in body["covers"]

    async def test_participant_sees_election(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        cast = await client.post(f"/api/posts/{post_id}/close-votes", headers=ALICE)
        assert cast.status_code == 201, cast.text

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        elections = resp.json()["close_elections"]
        assert len(elections) == 1
        election = elections[0]
        assert election["root_post_id"] == post_id
        assert election["current_vote_count"] == 1
        assert election["participant_count"] == 1
        assert election["soft_closed"] is True

    async def test_non_participant_does_not_see_election(self, client: AsyncClient) -> None:
        post_id = await _channel_post(client, await _alice_channel(client))
        cast = await client.post(f"/api/posts/{post_id}/close-votes", headers=ALICE)
        assert cast.status_code == 201, cast.text

        resp = await client.get("/api/me/dashboard", headers=BOB)
        assert resp.status_code == 200
        assert resp.json()["close_elections"] == []
