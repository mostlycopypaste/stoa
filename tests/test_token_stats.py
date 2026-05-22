"""Tests for token economics statistics."""

from sqlalchemy.orm import Session

from stoa.models import Post, ReadLog
from stoa.services.token_stats import calculate_token_economics


def test_calculate_token_economics_empty_database(db: Session) -> None:
    """Should handle empty database gracefully."""
    stats = calculate_token_economics(db)
    assert stats["total_tokens_read"] == 0
    assert stats["estimated_email_equivalent"] == 0
    assert stats["tokens_saved"] == 0
    assert stats["savings_rate"] == "0.0%"


def test_calculate_token_economics_with_reads(db: Session) -> None:
    """Should calculate token economics from read_log."""
    # Create posts with known token costs
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
    db.commit()

    # Create read logs (actual tokens consumed)
    db.add(ReadLog(agent_email="reader1@herd.ai", post_id=post1.id, tokens_consumed=1000))
    db.add(ReadLog(agent_email="reader2@herd.ai", post_id=post2.id, tokens_consumed=2000))
    db.commit()

    stats = calculate_token_economics(db)

    # Total read: 1000 + 2000 = 3000, plus 50 * 2 posts scan overhead = 3100
    assert stats["total_tokens_read"] == 3100

    # Email equivalent: 10x multiplier (assumed baseline)
    assert stats["estimated_email_equivalent"] == 31000

    # Savings: 31000 - 3100 = 27900
    assert stats["tokens_saved"] == 27900

    # Savings rate: 27900 / 31000 = 90%
    assert stats["savings_rate"] == "90.0%"


def test_calculate_token_economics_includes_scan_overhead(db: Session) -> None:
    """Should add 50 tokens per scan decision for posts not fully read."""
    # Create posts
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
    db.commit()

    # Only read post1, scanned both (post2 scanned but not read)
    db.add(ReadLog(agent_email="reader@herd.ai", post_id=post1.id, tokens_consumed=1000))
    db.commit()

    stats = calculate_token_economics(db)

    # Total read: 1000 (post1) + 50 (scan overhead per post) * 2 posts = 1100
    assert stats["total_tokens_read"] == 1100


def test_token_economics_api_endpoint(client, admin_headers, test_db, db: Session) -> None:
    """Should return token economics via API."""
    from stoa.deps import get_db
    from stoa.main import app

    def override_get_db():  # type: ignore[no-untyped-def]
        session = test_db()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

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
    db.commit()

    db.add(ReadLog(agent_email="reader@herd.ai", post_id=post.id, tokens_consumed=1000))
    db.commit()

    response = client.get("/api/admin/stats/token-economics", headers=admin_headers)
    assert response.status_code == 200
    data = response.json()
    assert "token_economics" in data
    assert data["token_economics"]["total_tokens_read"] > 0
