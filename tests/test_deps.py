"""Tests for database session dependency."""

from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Session

from stoa.deps import get_db


def test_get_db_yields_session(tmp_path: Path) -> None:
    """get_db yields a working SQLAlchemy session."""
    db_path = tmp_path / "test.db"
    with patch("stoa.deps.get_db_path", return_value=db_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        test_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        with patch("stoa.deps.SessionLocal", test_session_local):
            gen = get_db()
            db = next(gen)
            assert isinstance(db, Session)
            db.execute(text("SELECT 1"))
            try:
                gen.send(None)
            except StopIteration:
                pass


def test_get_db_rollback_on_error(tmp_path: Path) -> None:
    """get_db rolls back session on exception."""
    db_path = tmp_path / "test.db"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    test_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with patch("stoa.deps.SessionLocal", test_session_local):
        gen = get_db()
        db = next(gen)
        try:
            gen.throw(ValueError("test error"))
        except ValueError:
            pass
        # Session should be closed after rollback
        assert not db.in_transaction()
