"""Tests for footer rotation algorithm (async)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from stoa.models import FooterMessage
from stoa.services.footer_rotation import select_footer, select_footers_bulk


async def test_select_footer_picks_from_least_recently_used(db: AsyncSession) -> None:
    """Should pick from 20 least-recently-used footers."""
    now = datetime.now(UTC).replace(tzinfo=None)

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
    await db.commit()

    selections = []
    for _ in range(30):
        footer = await select_footer(db)
        selections.append(footer.text)

    never_used_count = sum(1 for t in selections if "Never used" in t)
    very_old_count = sum(1 for t in selections if "Very old" in t)
    recent_count = sum(1 for t in selections if "Recent" in t)

    assert never_used_count + very_old_count >= 20
    assert recent_count <= 10


async def test_select_footer_updates_last_used_at(db: AsyncSession) -> None:
    """Should update last_used_at timestamp after selection."""
    db.add(FooterMessage(text="Test footer", category="cheeky", last_used_at=None))
    await db.commit()

    before = datetime.now(UTC).replace(tzinfo=None)
    footer = await select_footer(db)
    after = datetime.now(UTC).replace(tzinfo=None)

    assert footer.last_used_at is not None
    assert before <= footer.last_used_at <= after


async def test_select_footer_filters_by_category(db: AsyncSession) -> None:
    """Should only select footers matching category filter."""
    db.add(FooterMessage(text="Cheeky footer", category="cheeky"))
    db.add(FooterMessage(text="Economics footer", category="token_economics"))
    await db.commit()

    for _ in range(10):
        footer = await select_footer(db, category="cheeky")
        assert footer.category == "cheeky"


async def test_select_footer_filters_by_context(db: AsyncSession) -> None:
    """Should only select footers matching context filter."""
    db.add(FooterMessage(text="Any context", category="cheeky", context=None))
    db.add(FooterMessage(text="Announcement", category="cheeky", context="announcement"))
    db.add(FooterMessage(text="Discussion", category="cheeky", context="discussion"))
    await db.commit()

    for _ in range(10):
        footer = await select_footer(db, context="announcement")
        assert footer.context in (None, "announcement")


async def test_select_footer_excludes_ids(db: AsyncSession) -> None:
    """Should exclude specified IDs from selection."""
    f1 = FooterMessage(text="Footer 1", category="cheeky")
    f2 = FooterMessage(text="Footer 2", category="cheeky")
    f3 = FooterMessage(text="Footer 3", category="cheeky")
    db.add_all([f1, f2, f3])
    await db.commit()
    await db.refresh(f1)
    await db.refresh(f2)
    await db.refresh(f3)

    for _ in range(10):
        footer = await select_footer(db, exclude_ids=[f1.id, f2.id])
        assert footer.id == f3.id


async def test_select_footer_only_active(db: AsyncSession) -> None:
    """Should only select active footers."""
    db.add(FooterMessage(text="Active", category="cheeky", active=True))
    db.add(FooterMessage(text="Inactive", category="cheeky", active=False))
    await db.commit()

    for _ in range(10):
        footer = await select_footer(db)
        assert footer.active is True


async def test_select_footers_bulk_returns_distinct(db: AsyncSession) -> None:
    """Should return N distinct footers."""
    for i in range(10):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky"))
    await db.commit()

    footers = await select_footers_bulk(db, count=5)
    assert len(footers) == 5
    assert len(set(f.id for f in footers)) == 5


async def test_select_footers_bulk_updates_all_timestamps(db: AsyncSession) -> None:
    """Should update last_used_at for all selected footers."""
    for i in range(5):
        db.add(FooterMessage(text=f"Footer {i}", category="cheeky", last_used_at=None))
    await db.commit()

    before = datetime.now(UTC).replace(tzinfo=None)
    footers = await select_footers_bulk(db, count=3)
    after = datetime.now(UTC).replace(tzinfo=None)

    for footer in footers:
        assert footer.last_used_at is not None
        assert before <= footer.last_used_at <= after


async def test_select_footer_raises_if_no_footers_available(db: AsyncSession) -> None:
    """Should raise ValueError if no footers match criteria."""
    db.add(FooterMessage(text="Wrong category", category="cheeky"))
    await db.commit()

    with pytest.raises(ValueError, match="No footers available"):
        await select_footer(db, category="token_economics")
