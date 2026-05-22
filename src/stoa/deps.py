"""FastAPI dependencies for database session management."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from stoa.db import get_db_path


def _build_engine_url() -> str:
    path = get_db_path()
    return f"sqlite:///{path}"


engine = create_engine(_build_engine_url(), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
