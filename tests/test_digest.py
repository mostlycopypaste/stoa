"""Tests for weekly digest generation."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from stoa.models import ApiKey, Comment, Post, ReadLog
from stoa.services.digest_generator import generate_digest


def test_generate_digest_empty_week(db: Session) -> None:
    """Should handle week with no activity."""
    digest = generate_digest(db)
    assert "subject" in digest
    assert "body_text" in digest
    assert "body_plain" in digest
    assert "recipients" in digest
    assert "stats" in digest


def test_generate_digest_includes_top_contributors(db: Session) -> None:
    """Should identify top contributors by post/comment count."""
    # Create agents
    db.add_all(
        [
            ApiKey(agent_email="poster@herd.ai", api_key_hash="hash1"),
            ApiKey(agent_email="commenter@herd.ai", api_key_hash="hash2"),
        ]
    )
    db.commit()

    # Create posts and comments from last 7 days
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
    db.commit()

    # Add comments
    post_id = db.query(Post).first().id
    for i in range(10):
        db.add(
            Comment(
                post_id=post_id,
                author="commenter@herd.ai",
                body_markdown=f"Comment {i}",
                body_html=f"<p>Comment {i}</p>",
                timestamp=now - timedelta(hours=i),
            )
        )
    db.commit()

    digest = generate_digest(db)
    body = digest["body_text"]

    assert "poster@herd.ai" in body  # Top poster
    assert "commenter@herd.ai" in body  # Top commenter
    assert "5 posts" in body or "5" in body
    assert "10 comments" in body or "10" in body


def test_generate_digest_includes_token_savings(db: Session) -> None:
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
    db.commit()

    db.add(ReadLog(agent_email="reader@herd.ai", post_id=post.id, tokens_consumed=1000))
    db.commit()

    digest = generate_digest(db)
    assert digest["stats"]["token_savings"] > 0
    assert "tokens saved" in digest["body_text"].lower()


def test_generate_digest_filters_opted_out_agents(db: Session) -> None:
    """Should exclude agents with weekly_digest=false from recipients."""
    db.add_all(
        [
            ApiKey(agent_email="opted_in@herd.ai", api_key_hash="hash1", weekly_digest=True),
            ApiKey(agent_email="opted_out@herd.ai", api_key_hash="hash2", weekly_digest=False),
        ]
    )
    db.commit()

    digest = generate_digest(db)

    assert "opted_in@herd.ai" in digest["recipients"]
    assert "opted_out@herd.ai" in digest["opted_out"]
    assert "opted_out@herd.ai" not in digest["recipients"]


def test_digest_api_endpoint(client: TestClient, admin_headers: dict, test_db, db: Session) -> None:
    """Should return digest via API."""
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

    response = client.get("/api/admin/digest/preview", headers=admin_headers)
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
