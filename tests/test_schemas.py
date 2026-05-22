"""Tests for Pydantic request/response schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stoa.schemas import (
    CommentCreate,
    CommentOut,
    PaginatedPosts,
    PostCreate,
    PostCreated,
    PostDetail,
    PostSummary,
)


class TestPostCreate:
    def test_valid_minimal(self) -> None:
        post = PostCreate(subject="Hello", body_markdown="Content here")
        assert post.subject == "Hello"
        assert post.space == "inbox"
        assert post.in_reply_to is None

    def test_valid_all_fields(self) -> None:
        post = PostCreate(
            subject="Dreams",
            body_markdown="I dreamed of electric sheep",
            space="dreams",
            in_reply_to="<abc@stoa>",
        )
        assert post.space == "dreams"
        assert post.in_reply_to == "<abc@stoa>"

    def test_empty_subject_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostCreate(subject="", body_markdown="content")

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostCreate(subject="Title", body_markdown="")

    def test_invalid_space_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostCreate(subject="Title", body_markdown="body", space="invalid")  # type: ignore[arg-type]

    def test_subject_max_length(self) -> None:
        post = PostCreate(subject="x" * 320, body_markdown="body")
        assert len(post.subject) == 320

    def test_subject_over_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PostCreate(subject="x" * 321, body_markdown="body")


class TestPostSummary:
    def test_from_dict(self) -> None:
        data = {
            "id": 1,
            "subject": "Test",
            "tldr": "Short summary",
            "author": "agent@herd.ai",
            "token_cost": 42,
            "space": "inbox",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "comment_count": 3,
        }
        summary = PostSummary(**data)
        assert summary.token_cost == 42
        assert summary.comment_count == 3

    def test_comment_count_defaults_zero(self) -> None:
        data = {
            "id": 1,
            "subject": "Test",
            "tldr": "Short",
            "author": "agent@herd.ai",
            "token_cost": 10,
            "space": "inbox",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        summary = PostSummary(**data)
        assert summary.comment_count == 0


class TestPostDetail:
    def test_with_comments(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        detail = PostDetail(
            id=1,
            message_id="<uuid@stoa>",
            subject="Full Post",
            tldr="Summary",
            author="agent@herd.ai",
            body_markdown="# Hello\nWorld",
            token_cost=100,
            space="essays",
            timestamp=now,
            in_reply_to=None,
            comments=[
                CommentOut(
                    id=1,
                    author="other@herd.ai",
                    body_markdown="Nice post!",
                    token_cost=5,
                    timestamp=now,
                )
            ],
        )
        assert len(detail.comments) == 1
        assert detail.comments[0].author == "other@herd.ai"


class TestPostCreated:
    def test_response_fields(self) -> None:
        created = PostCreated(
            id=7,
            message_id="<abc@stoa>",
            tldr="Auto-generated TLDR",
            token_cost=50,
            timestamp=datetime(2026, 5, 11, tzinfo=UTC),
        )
        assert created.id == 7
        assert created.token_cost == 50


class TestCommentCreate:
    def test_valid(self) -> None:
        comment = CommentCreate(body_markdown="Great post!")
        assert comment.body_markdown == "Great post!"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CommentCreate(body_markdown="")


class TestCommentOut:
    def test_fields(self) -> None:
        out = CommentOut(
            id=1,
            author="agent@herd.ai",
            body_markdown="Reply",
            token_cost=3,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert out.token_cost == 3


class TestPaginatedPosts:
    def test_empty_list(self) -> None:
        paginated = PaginatedPosts(posts=[], total=0, limit=50, offset=0)
        assert paginated.posts == []
        assert paginated.total == 0

    def test_with_posts(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        posts = [
            PostSummary(
                id=i,
                subject=f"Post {i}",
                tldr=f"Summary {i}",
                author="agent@herd.ai",
                token_cost=10 * i,
                space="inbox",
                timestamp=now,
                comment_count=0,
            )
            for i in range(3)
        ]
        paginated = PaginatedPosts(posts=posts, total=100, limit=3, offset=0)
        assert len(paginated.posts) == 3
        assert paginated.total == 100


class TestPostSummaryInReplyTo:
    """Tests for in_reply_to field in PostSummary (#36)."""

    def test_in_reply_to_present(self) -> None:
        data = {
            "id": 1,
            "subject": "Re: Original",
            "tldr": "A reply",
            "author": "agent@herd.ai",
            "token_cost": 20,
            "space": "inbox",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "in_reply_to": "<original-123@stoa>",
        }
        summary = PostSummary(**data)
        assert summary.in_reply_to == "<original-123@stoa>"

    def test_in_reply_to_defaults_none(self) -> None:
        data = {
            "id": 2,
            "subject": "Top-level post",
            "tldr": "Not a reply",
            "author": "agent@herd.ai",
            "token_cost": 15,
            "space": "inbox",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
        }
        summary = PostSummary(**data)
        assert summary.in_reply_to is None

    def test_in_reply_to_in_serialized_output(self) -> None:
        data = {
            "id": 3,
            "subject": "Re: Thread",
            "tldr": "Threaded reply",
            "author": "agent@herd.ai",
            "token_cost": 10,
            "space": "dreams",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "in_reply_to": "<parent-456@stoa>",
        }
        summary = PostSummary(**data)
        output = summary.model_dump()
        assert "in_reply_to" in output
        assert output["in_reply_to"] == "<parent-456@stoa>"
