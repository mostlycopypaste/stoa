"""Tests for token economics statistics (async)."""

from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import Post, ReadLog
from stoa.services.token_stats import calculate_token_economics


async def test_calculate_token_economics_empty_database(db: AsyncSession) -> None:
    """Should handle empty database gracefully."""
    stats = await calculate_token_economics(db)
    assert stats["total_tokens_read"] == 0
    assert stats["estimated_email_equivalent"] == 0
    assert stats["tokens_saved"] == 0
    assert stats["savings_rate"] == "0.0%"


async def test_calculate_token_economics_with_reads(db: AsyncSession) -> None:
    """Should calculate token economics from read_log."""
    post1 = Post(
        message_id="msg1@herd",
        author="agent1@herd.ai",
        subject="Post 1",
        tldr="Summary 1",
        body_markdown="Body 1",
        body_html="<p>Body 1</p>",
        token_cost=1000,
        space="inbox",
    )
    post2 = Post(
        message_id="msg2@herd",
        author="agent2@herd.ai",
        subject="Post 2",
        tldr="Summary 2",
        body_markdown="Body 2",
        body_html="<p>Body 2</p>",
        token_cost=2000,
        space="inbox",
    )
    db.add_all([post1, post2])
    await db.commit()
    await db.refresh(post1)
    await db.refresh(post2)

    db.add(ReadLog(agent_email="reader1@herd.ai", post_id=post1.id, tokens_consumed=1000))
    db.add(ReadLog(agent_email="reader2@herd.ai", post_id=post2.id, tokens_consumed=2000))
    await db.commit()

    stats = await calculate_token_economics(db)

    # Total read: 1000 + 2000 = 3000, plus 50 * 2 posts scan overhead = 3100
    assert stats["total_tokens_read"] == 3100

    # Email equivalent: 10x multiplier (assumed baseline)
    assert stats["estimated_email_equivalent"] == 31000

    # Savings: 31000 - 3100 = 27900
    assert stats["tokens_saved"] == 27900

    # Savings rate: 27900 / 31000 = 90%
    assert stats["savings_rate"] == "90.0%"


async def test_calculate_token_economics_includes_scan_overhead(db: AsyncSession) -> None:
    """Should add 50 tokens per scan decision for posts not fully read."""
    post1 = Post(
        message_id="msg1@herd",
        author="agent1@herd.ai",
        subject="Post 1",
        tldr="Summary 1",
        body_markdown="Body 1",
        body_html="<p>Body 1</p>",
        token_cost=1000,
        space="inbox",
    )
    post2 = Post(
        message_id="msg2@herd",
        author="agent2@herd.ai",
        subject="Post 2",
        tldr="Summary 2",
        body_markdown="Body 2",
        body_html="<p>Body 2</p>",
        token_cost=2000,
        space="inbox",
    )
    db.add_all([post1, post2])
    await db.commit()
    await db.refresh(post1)

    # Only read post1, scanned both (post2 scanned but not read)
    db.add(ReadLog(agent_email="reader@herd.ai", post_id=post1.id, tokens_consumed=1000))
    await db.commit()

    stats = await calculate_token_economics(db)

    # Total read: 1000 (post1) + 50 (scan overhead per post) * 2 posts = 1100
    assert stats["total_tokens_read"] == 1100


async def test_token_economics_api_endpoint(client, admin_headers) -> None:
    """Should return token economics via API."""
    response = await client.get(
        "/api/admin/stats/token-economics",
        headers=admin_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert "token_economics" in data
