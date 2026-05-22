"""Database connection and initialization for Stoa."""

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "stoa.db"


def get_db_path() -> Path:
    """Return the database path from env or default."""
    import os

    return Path(os.environ.get("STOA_DB", str(DEFAULT_DB_PATH)))


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled.

    Args:
        db_path: Path to the database file. Uses default if not provided.

    Returns:
        sqlite3.Connection with row factory for dict-like access.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations(db_path: Path | None = None) -> None:
    """Run all pending migrations in order, skipping already-applied ones.

    Args:
        db_path: Path to the database file. Uses default if not provided.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at DATETIME DEFAULT (datetime('now')))"
        )
        conn.commit()

        applied = {
            row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }

        migration_files = sorted(MIGRATIONS_DIR.glob("[0-9]*_*.sql"))
        migration_files = [f for f in migration_files if "rollback" not in f.stem]

        for migration_file in migration_files:
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)", (migration_file.name,)
            )
            conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Initialize the database: run migrations and return a connection.

    Args:
        db_path: Path to the database file. Uses default if not provided.

    Returns:
        sqlite3.Connection ready for use.
    """
    path = db_path or get_db_path()
    run_migrations(path)
    return get_connection(path)


def drop_tables(db_path: Path | None = None) -> None:
    """Drop all tables. For testing only.

    Args:
        db_path: Path to the database file. Uses default if not provided.
    """
    conn = get_connection(db_path)
    try:
        rollback_file = MIGRATIONS_DIR / "001_rollback.sql"
        if rollback_file.exists():
            conn.executescript(rollback_file.read_text())
    finally:
        conn.close()
