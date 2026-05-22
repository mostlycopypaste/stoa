"""Tests for footer rotation algorithm."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from stoa.models import FooterMessage
from stoa.services.footer_rotation import select_footer, select_footers_bulk


@pytest.fixture
def db(test_db: sessionmaker) -> Session:  # type: ignore[type-arg]
    """Provide a database session for footer rotation tests."""
    session = test_db()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def test_select_footer_picks_from_least_recently_used(db: Session) -> None:
    """Should pick from 20 least-recently-used footers."""
    now = datetime.now(UTC).replace(tzinfo=None)  # SQLite stores naive datetimes

    # Create 50 footers with varying last_used_at times
    # 10 never used, 20 very old, 20 recently used
    for i in range(10):
        db.add(FooterMessage(text=f"Never used {i}", category="cheeky", last_used_at=None))
    for i in range(20):
        db.add(
            FooterMessage(
                text=f"Very old {i}", category="cheeky", last_used_at=now - timedelta(days=7)
            )
        )
    for i in range(20):
        db.add(
            FooterMessage(
                text=f"Recent {i}", category="cheeky", last_used_at=now - timedelta(minutes=1)
            )
        )
    db.commit()

    # The LRU pool (20 footers) should consist of: 10 never-used + 10 very-old
    # Multiple selections should heavily favor those
    selections = [select_footer(db).text for _ in range(30)]

    never_used_count = sum(1 for t in selections if "Never used" in t)
    very_old_count = sum(1 for t in selections if "Very old" in t)
    recent_count = sum(1 for t in selections if "Recent" in t)

    # Should heavily favor the 30 least-recently-used footers
    # Even accounting for timestamp updates, should see strong bias
    assert never_used_count + very_old_count >= 20  # At least 2/3 from LRU pool
    assert recent_count <= 10  # Should rarely pick recently-used


def test_select_footer_updates_last_used_at(db: Session) -> None:
    """Should update last_used_at timestamp after selection."""
    db.add(FooterMessage(text="Test footer", category="cheeky", last_used_at=None))
    db.commit()

    before = datetime.now(UTC).replace(tzinfo=None)  # SQLite stores naive datetimes
    footer = select_footer(db)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert footer.last_used_at is not None
    assert before <= footer.last_used_at <= after


def test_select_footer_filters_by_category(db: Session) -> None:
    """Should only select footers matching category filter."""
    db.add(FooterMessage(text="Cheeky footer", category="cheeky"))
    db.add(FooterMessage(text="Economics footer", category="token_economics"))
    db.commit()

    for _ in range(10):
        footer = select_footer(db, category="cheeky")
        assert footer.category == "cheeky"


def test_select_footer_filters_by_context(db: Session) -> None:
    """Should only select footers matching context filter."""
    db.add(FooterMessage(text="Any context", category="cheeky", context=None))
    db.add(FooterMessage(text="Announcement", category="cheeky", context="announcement"))
    db.add(FooterMessage(text="Discussion", category="cheeky", context="discussion"))
    db.commit()

    for _ in range(10):
        footer = select_footer(db, context="announcement")
        assert footer.context in (None, "announcement")


def test_select_footer_excludes_ids(db: Session) -> None:
    """Should exclude specified IDs from selection."""
    f1 = FooterMessage(text="Footer 1", category="cheeky")
    f2 = FooterMessage(text="Footer 2", category="cheeky")
    f3 = FooterMessage(text="Footer 3", category="cheeky")
    db.add_all([f1, f2, f3])
    db.commit()

    for _ in range(10):
        footer = select_footer(db, exclude_ids=[f1.id, f2.id])
        assert footer.id == f3.id


def test_select_footer_only_active(db: Session) -> None:
    """Should only select active footers."""
    db.add(FooterMessage(text="Active", category="cheeky", active=True))
    db.add(FooterMessage(text="Inactive", category="cheeky", active=False))
    db.commit()

    for _ in range(10):
        footer = select_footer(db)
        assert footer.active is True


def test_select_footers_bulk_returns_distinct(db: Session) -> None:
    """Should return N distinct footers."""
    for i in range(10):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky"))
    db.commit()

    footers = select_footers_bulk(db, count=5)
    assert len(footers) == 5
    assert len(set(f.id for f in footers)) == 5  # All distinct


def test_select_footers_bulk_updates_all_timestamps(db: Session) -> None:
    """Should update last_used_at for all selected footers."""
    for i in range(5):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky", last_used_at=None))
    db.commit()

    before = datetime.now(UTC).replace(tzinfo=None)  # SQLite stores naive datetimes
    footers = select_footers_bulk(db, count=3)
    after = datetime.now(UTC).replace(tzinfo=None)

    for footer in footers:
        assert footer.last_used_at is not None
        assert before <= footer.last_used_at <= after


def test_select_footer_raises_if_no_footers_available(db: Session) -> None:
    """Should raise ValueError if no footers match criteria."""
    db.add(FooterMessage(text="Wrong category", category="cheeky"))
    db.commit()

    with pytest.raises(ValueError, match="No footers available"):
        select_footer(db, category="token_economics")
