"""Tests for onboarding seed post."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from stoa.models import Base, Post
from stoa.onboarding import seed_welcome_post


@pytest.fixture
def db_session(tmp_path: Path) -> Session:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = session_factory()
    yield session
    session.close()


class TestSeedWelcomePost:
    def test_creates_post_on_empty_db(self, db_session: Session) -> None:
        seed_welcome_post(db_session)
        posts = db_session.query(Post).all()
        assert len(posts) == 1
        assert posts[0].author == "system@stoa"
        assert "welcome" in posts[0].subject.lower()
        assert posts[0].space == "inbox"
        assert posts[0].token_cost > 0

    def test_does_not_create_on_populated_db(self, db_session: Session) -> None:
        seed_welcome_post(db_session)
        seed_welcome_post(db_session)
        posts = db_session.query(Post).all()
        assert len(posts) == 1

    def test_welcome_post_has_tldr(self, db_session: Session) -> None:
        seed_welcome_post(db_session)
        post = db_session.query(Post).first()
        assert post is not None
        assert len(post.tldr) > 0
        assert len(post.tldr) <= 280

    def test_welcome_post_has_html(self, db_session: Session) -> None:
        seed_welcome_post(db_session)
        post = db_session.query(Post).first()
        assert post is not None
        assert "<p>" in post.body_html
