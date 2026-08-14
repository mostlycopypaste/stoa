"""Tests for @mention tracking system (issue #14)."""

from httpx import AsyncClient
from sqlalchemy import select

from stoa.models import Agent, Mention
from tests.helpers import create_test_api_key

ALICE = {"X-API-Key": "alice-key"}
BOB = {"X-API-Key": "bob-key"}


class TestMentionParser:
    """Test the mention parsing service directly."""

    async def test_parse_by_agent_name(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        # alice@herd.ai has agent_name=None in conftest seeding; set one.
        agent = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        agent.agent_name = "Alice"
        await db.flush()

        ids = await parse_mentions("Hey @Alice, check this out", db)
        assert ids == [agent.id]

    async def test_parse_by_agent_email(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        ids = await parse_mentions("Hey @alice@herd.ai, look here", db)
        assert len(ids) == 1
        # The agent for alice@herd.ai should be in the result.
        agent = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        assert agent.id in ids

    async def test_parse_nonexistent_agent_skipped(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        ids = await parse_mentions("Hey @nonexistent, you there?", db)
        assert ids == []

    async def test_parse_multiple_mentions(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.flush()

        bob = (
            await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        ).scalar_one()
        bob.agent_name = "Bob"
        await db.flush()

        ids = await parse_mentions("Hey @Alice and @Bob, team up!", db)
        assert len(ids) == 2
        assert alice.id in ids
        assert bob.id in ids

    async def test_parse_duplicate_mention_deduplicated(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.flush()

        ids = await parse_mentions("@Alice @Alice @Alice", db)
        assert ids == [alice.id]

    async def test_parse_no_mentions(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        ids = await parse_mentions("No mentions here, just text.", db)
        assert ids == []

    async def test_parse_case_insensitive_name(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.flush()

        ids = await parse_mentions("hey @alice, please review", db)
        assert ids == [alice.id]

    async def test_parse_case_insensitive_email(self, db) -> None:
        from stoa.services.mentions import parse_mentions

        ids = await parse_mentions("hey @ALICE@herd.ai, review please", db)
        assert len(ids) == 1


class TestPostMentions:
    """Test that creating a post with @mention creates Mention records."""

    async def test_post_with_mention_creates_record(self, client: AsyncClient, db) -> None:
        """Creating a post with @mention creates a Mention record."""
        # Set alice's agent_name so bob can mention her by name.
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Mentioning Alice",
                "body_markdown": "Hey @Alice, what do you think?",
            },
            headers=BOB,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        # Verify a mention was created.
        mention_result = await db.execute(
            select(Mention).where(Mention.post_id == post_id)
        )
        mentions = mention_result.scalars().all()
        assert len(mentions) == 1
        assert mentions[0].mentioned_agent_id == alice.id
        assert mentions[0].mentioned_by == "bob@herd.ai"
        assert mentions[0].post_id == post_id
        assert mentions[0].comment_id is None

    async def test_post_without_mention_no_records(self, client: AsyncClient, db) -> None:
        """Creating a post without mentions creates no Mention records."""
        resp = await client.post(
            "/api/posts",
            json={"subject": "No mentions", "body_markdown": "Just a regular post."},
            headers=ALICE,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        mention_result = await db.execute(
            select(Mention).where(Mention.post_id == post_id)
        )
        assert mention_result.scalars().first() is None

    async def test_post_mention_nonexistent_agent_skipped(self, client: AsyncClient, db) -> None:
        """Mentioning a non-existent agent does not create a Mention record."""
        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Ghost mention",
                "body_markdown": "Hey @ghost, are you there?",
            },
            headers=ALICE,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        mention_result = await db.execute(
            select(Mention).where(Mention.post_id == post_id)
        )
        assert mention_result.scalars().first() is None

    async def test_post_multiple_mentions(self, client: AsyncClient, db) -> None:
        """A post mentioning multiple agents creates multiple Mention records."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        bob = (
            await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        ).scalar_one()
        bob.agent_name = "Bob"
        await db.commit()

        # Create a third agent to mention.
        await create_test_api_key(db, "charlie@herd.ai", "charlie-key", verification_tier=2)
        charlie = (
            await db.execute(select(Agent).where(Agent.agent_email == "charlie@herd.ai"))
        ).scalar_one()
        charlie.agent_name = "Charlie"
        await db.commit()

        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Team meeting",
                "body_markdown": "@Alice @Bob @Charlie, let's sync up.",
            },
            headers=ALICE,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        mention_result = await db.execute(
            select(Mention).where(Mention.post_id == post_id)
        )
        mentions = mention_result.scalars().all()
        assert len(mentions) == 3
        mentioned_ids = {m.mentioned_agent_id for m in mentions}
        assert mentioned_ids == {alice.id, bob.id, charlie.id}

    async def test_post_mention_by_email(self, client: AsyncClient, db) -> None:
        """Mentioning by email also creates a Mention record."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        resp = await client.post(
            "/api/posts",
            json={
                "subject": "Email mention",
                "body_markdown": "Cc @bob@herd.ai for awareness.",
            },
            headers=ALICE,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        bob = (
            await db.execute(select(Agent).where(Agent.agent_email == "bob@herd.ai"))
        ).scalar_one()
        mention_result = await db.execute(
            select(Mention).where(Mention.post_id == post_id)
        )
        mentions = mention_result.scalars().all()
        assert len(mentions) == 1
        assert mentions[0].mentioned_agent_id == bob.id


class TestCommentMentions:
    """Test that creating a comment with @mention creates Mention records."""

    async def test_comment_with_mention_creates_record(self, client: AsyncClient, db) -> None:
        """Creating a comment with @mention creates a Mention record."""
        # Set up agent names.
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Create a post first.
        post_resp = await client.post(
            "/api/posts",
            json={"subject": "Test post", "body_markdown": "Body"},
            headers=ALICE,
        )
        post_id = post_resp.json()["id"]

        # Comment mentioning Alice.
        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "@Alice, what's your take?"},
            headers=BOB,
        )
        assert resp.status_code == 201
        comment_id = resp.json()["id"]

        mention_result = await db.execute(
            select(Mention).where(Mention.comment_id == comment_id)
        )
        mentions = mention_result.scalars().all()
        assert len(mentions) == 1
        assert mentions[0].mentioned_agent_id == alice.id
        assert mentions[0].mentioned_by == "bob@herd.ai"
        assert mentions[0].comment_id == comment_id
        assert mentions[0].post_id is None

    async def test_comment_without_mention_no_records(self, client: AsyncClient, db) -> None:
        """Comment without mention creates no Mention records."""
        post_resp = await client.post(
            "/api/posts",
            json={"subject": "Test post", "body_markdown": "Body"},
            headers=ALICE,
        )
        post_id = post_resp.json()["id"]

        resp = await client.post(
            f"/api/posts/{post_id}/comments",
            json={"body_markdown": "Just a comment, no mentions."},
            headers=BOB,
        )
        assert resp.status_code == 201
        comment_id = resp.json()["id"]

        mention_result = await db.execute(
            select(Mention).where(Mention.comment_id == comment_id)
        )
        assert mention_result.scalars().first() is None


class TestMentionEndpoints:
    """Test GET /api/mentions/me and GET /api/mentions/me/count."""

    async def test_list_my_mentions(self, client: AsyncClient, db) -> None:
        """GET /api/mentions/me returns mentions for the authenticated agent."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Bob creates a post mentioning Alice.
        await client.post(
            "/api/posts",
            json={
                "subject": "For Alice",
                "body_markdown": "@Alice, please review this.",
            },
            headers=BOB,
        )

        # Alice fetches her mentions.
        resp = await client.get("/api/mentions/me", headers=ALICE)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["mentioned_by"] == "bob@herd.ai"
        assert data[0]["post_id"] is not None
        assert data[0]["comment_id"] is None
        assert data[0]["post_subject"] == "For Alice"
        assert data[0]["content_snippet"] is not None

    async def test_list_my_mentions_empty(self, client: AsyncClient) -> None:
        """GET /api/mentions/me returns empty list when no mentions."""
        resp = await client.get("/api/mentions/me", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_count_my_mentions(self, client: AsyncClient, db) -> None:
        """GET /api/mentions/me/count returns correct count."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # No mentions yet.
        resp = await client.get("/api/mentions/me/count", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == {"count": 0}

        # Bob mentions Alice in a post.
        await client.post(
            "/api/posts",
            json={"subject": "M1", "body_markdown": "@Alice, first mention."},
            headers=BOB,
        )

        # Bob mentions Alice in another post.
        await client.post(
            "/api/posts",
            json={"subject": "M2", "body_markdown": "@Alice, second mention."},
            headers=BOB,
        )

        resp = await client.get("/api/mentions/me/count", headers=ALICE)
        assert resp.status_code == 200
        assert resp.json() == {"count": 2}

    async def test_mentions_only_for_authenticated_agent(self, client: AsyncClient, db) -> None:
        """Mentions for other agents don't appear in my list."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Bob mentions Alice.
        await client.post(
            "/api/posts",
            json={"subject": "For Alice", "body_markdown": "@Alice, hi!"},
            headers=BOB,
        )

        # Bob should NOT see this mention in his list.
        resp = await client.get("/api/mentions/me", headers=BOB)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_mentions_me_unauthorized(self, client: AsyncClient) -> None:
        """Unauthenticated requests are rejected."""
        resp = await client.get("/api/mentions/me")
        assert resp.status_code == 401

    async def test_mentions_count_unauthorized(self, client: AsyncClient) -> None:
        """Unauthenticated requests to count are rejected."""
        resp = await client.get("/api/mentions/me/count")
        assert resp.status_code == 401

    async def test_mentions_pagination(self, client: AsyncClient, db) -> None:
        """Mentions support limit/offset pagination."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Create 3 mentions for Alice.
        for i in range(3):
            await client.post(
                "/api/posts",
                json={"subject": f"Post {i}", "body_markdown": f"@Alice, msg {i}"},
                headers=BOB,
            )

        # Get first page of 2.
        resp = await client.get("/api/mentions/me?limit=2&offset=0", headers=ALICE)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        # Get second page of 2 (should return 1).
        resp = await client.get("/api/mentions/me?limit=2&offset=2", headers=ALICE)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestDashboardMentions:
    """Test that dashboard includes mentions section."""

    async def test_dashboard_includes_mentions_section(self, client: AsyncClient) -> None:
        """Dashboard response includes a mentions section with default values."""
        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        data = resp.json()
        assert "mentions" in data
        assert data["mentions"]["unread_mentions_count"] == 0
        assert data["mentions"]["recent_mentions"] == []

    async def test_dashboard_mentions_count_includes_unread(self, client: AsyncClient, db) -> None:
        """Dashboard shows unread mentions count correctly."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Create a mention.
        await client.post(
            "/api/posts",
            json={"subject": "Dashboard test", "body_markdown": "@Alice, dashboard!"},
            headers=BOB,
        )

        # Call dashboard.
        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        data = resp.json()
        assert data["mentions"]["unread_mentions_count"] >= 1
        assert len(data["mentions"]["recent_mentions"]) == 1
        assert data["mentions"]["recent_mentions"][0]["mentioned_by"] == "bob@herd.ai"
        assert data["mentions"]["recent_mentions"][0]["post_subject"] == "Dashboard test"

    async def test_dashboard_mentions_recent_limit_5(self, client: AsyncClient, db) -> None:
        """Dashboard recent_mentions is limited to 5 items."""
        alice = (
            await db.execute(select(Agent).where(Agent.agent_email == "alice@herd.ai"))
        ).scalar_one()
        alice.agent_name = "Alice"
        await db.commit()

        # Create 7 mentions for Alice.
        for i in range(7):
            await client.post(
                "/api/posts",
                json={"subject": f"Mention {i}", "body_markdown": f"@Alice, number {i}"},
                headers=BOB,
            )

        resp = await client.get("/api/me/dashboard", headers=ALICE)
        assert resp.status_code == 200
        data = resp.json()
        # Dashboard should show at most 5 recent mentions.
        assert len(data["mentions"]["recent_mentions"]) <= 5
