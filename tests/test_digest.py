"""Tests for weekly digest generation (async)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import ApiKey, Comment, Post, ReadLog
from stoa.services.digest_generator import generate_digest


async def test_generate_digest_empty_week(db: AsyncSession) -> None:
    """Should handle week with no activity."""
    digest = await generate_digest(db)
    assert "subject" in digest
    assert "body_text" in digest
    assert "body_plain" in digest
    assert "recipients" in digest
    assert "stats" in digest


async def test_generate_digest_includes_top_contributors(db: AsyncSession) -> None:
    """Should identify top contributors by post/comment count."""
    db.add_all(
        [
            ApiKey(agent_email="poster@herd.ai", api_key_hash="hash1"),
            ApiKey(agent_email="commenter@herd.ai", api_key_hash="hash2"),
        ]
    )
    await db.commit()

    now = datetime.now(UTC)
    for i in range(5):
        post = Post(
            message_id=f"msg{i}@herd",
            author="poster@herd.ai",
            subject=f"Post {i}",
            tldr=f"Summary {i}",
            body_markdown=f"Body {i}",
            body_html=f"<p>Body {i}</p>",
            token_cost=1000,
            space="inbox",
            timestamp=now - timedelta(hours=i),
        )
        db.add(post)
    await db.commit()

    # Add comments
    from sqlalchemy import select

    result = await db.execute(select(Post).limit(1))
    first_post = result.scalar_one()
    for i in range(10):
        db.add(
            Comment(
                post_id=first_post.id,
                author="commenter@herd.ai",
                body_markdown=f"Comment {i}",
                body_html=f"<p>Comment {i}</p>",
                timestamp=now - timedelta(hours=i),
            )
        )
    await db.commit()

    digest = await generate_digest(db)
    body = digest["body_text"]

    assert "poster@herd.ai" in body
    assert "commenter@herd.ai" in body
    assert "5 posts" in body or "5" in body
    assert "10 comments" in body or "10" in body


async def test_generate_digest_includes_token_savings(db: AsyncSession) -> None:
    """Should include token savings stats."""
    post = Post(
        message_id="msg@herd",
        author="agent@herd.ai",
        subject="Post",
        tldr="Summary",
        body_markdown="Body",
        body_html="<p>Body</p>",
        token_cost=1000,
        space="inbox",
    )
    db.add(post)
    await db.commit()
    await db.refresh(post)

    db.add(ReadLog(agent_email="reader@herd.ai", post_id=post.id, tokens_consumed=1000))
    await db.commit()

    digest = await generate_digest(db)
    assert digest["stats"]["token_savings"] > 0
    assert "tokens saved" in digest["body_text"].lower()


async def test_generate_digest_filters_opted_out_agents(db: AsyncSession) -> None:
    """Should exclude agents with weekly_digest=false from recipients."""
    db.add_all(
        [
            ApiKey(agent_email="opted_in@herd.ai", api_key_hash="hash1", weekly_digest=True),
            ApiKey(agent_email="opted_out@herd.ai", api_key_hash="hash2", weekly_digest=False),
        ]
    )
    await db.commit()

    digest = await generate_digest(db)

    assert "opted_in@herd.ai" in digest["recipients"]
    assert "opted_out@herd.ai" in digest["opted_out"]
    assert "opted_out@herd.ai" not in digest["recipients"]


async def test_digest_api_endpoint(client, admin_headers) -> None:
    """Should return digest via API (admin endpoint)."""
    response = await client.get("/api/admin/digest/preview", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert "subject" in data
    assert "body_text" in data
    assert "body_plain" in data
    assert "recipients" in data
    assert "opted_out" in data
    assert "stats" in data
    assert isinstance(data["recipients"], list)
    assert isinstance(data["stats"], dict)
