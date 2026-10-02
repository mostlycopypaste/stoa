"""Alembic up/down coverage for the issue #134 Phase A auth objects.

Runs the real migration entrypoint (the same command the Dockerfile CMD
runs at deploy: ``uv run alembic upgrade head``) against a throwaway SQLite
database, then exercises the one-step downgrade of ``d1b3f5a7c9e2``:

- upgrade head          -> auth_challenges + auth_sessions exist,
                           agents.auth_epoch exists, unique digests and
                           state/purpose checks enforced
- downgrade 0c038982f158 -> all three gone, parent schema untouched
- upgrade head (again)  -> re-upgrade is clean (idempotent guards hold)

The scratch database is created under the repo root (not the system temp
dir) so test artifacts stay off the boot volume.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "0c038982f158"


def _alembic(*args: str, db: Path) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    proc = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"alembic {args} failed:\n{proc.stdout}\n{proc.stderr}"


def _table_names(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _agent_columns(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [row[1] for row in con.execute("PRAGMA table_info(agents)")]
    finally:
        con.close()


def _insert_challenge(con: sqlite3.Connection, *, code_digest: str, purpose: str = "mint") -> None:
    """Insert one challenge row (epoch/agents seed omitted — FKs off in raw sqlite3)."""
    con.execute(
        "INSERT INTO auth_challenges "
        "(agent_id, agent_email, purpose, state, code_digest, epoch, "
        " requested_at, challenged_at, expires_at, consumed_at) "
        "VALUES (1, 'a@herd.ai', ?, 'challenged', ?, 0, "
        " '2026-10-02 00:00:00', NULL, '2026-10-02 00:10:00', NULL)",
        (purpose, code_digest),
    )


def test_auth_migration_up_down() -> None:
    scratch = Path(tempfile.mkdtemp(prefix="mig-test-", dir=REPO_ROOT))
    db = scratch / "mig-test.db"
    try:
        # Up: full chain, landing on the new head.
        _alembic("upgrade", "head", db=db)
        tables = _table_names(db)
        assert "auth_challenges" in tables
        assert "auth_sessions" in tables
        assert "auth_epoch" in _agent_columns(db)

        # Behavioral constraint checks (SQLite names unique-constraint indexes
        # sqlite_autoindex_*, so assert enforcement, not index names).
        con = sqlite3.connect(db)
        try:
            _insert_challenge(con, code_digest="d" * 64)
            # Duplicate code digest is rejected (uq_challenge_code_digest).
            try:
                _insert_challenge(con, code_digest="d" * 64)
                raise AssertionError("duplicate code_digest was accepted")
            except sqlite3.IntegrityError:
                pass
            # Purpose CHECK constraint is enforced.
            try:
                _insert_challenge(con, code_digest="e" * 64, purpose="bogus")
                raise AssertionError("purpose='bogus' was accepted")
            except sqlite3.IntegrityError:
                pass
            # State CHECK constraint is enforced.
            try:
                con.execute(
                    "INSERT INTO auth_sessions "
                    "(agent_id, agent_email, token_digest, epoch, state, "
                    " minted_at, expires_at, revoked_at) "
                    "VALUES (1, 'a@herd.ai', ?, 0, 'zombie', "
                    " '2026-10-02 00:00:00', '2026-10-03 00:00:00', NULL)",
                    ("f" * 64,),
                )
                raise AssertionError("state='zombie' was accepted")
            except sqlite3.IntegrityError:
                pass
            # Duplicate token digest is rejected (uq_session_token_digest).
            first = True
            for _ in range(2):
                try:
                    con.execute(
                        "INSERT INTO auth_sessions "
                        "(agent_id, agent_email, token_digest, epoch, state, "
                        " minted_at, expires_at, revoked_at) "
                        "VALUES (1, 'a@herd.ai', ?, 0, 'verified', "
                        " '2026-10-02 00:00:00', '2026-10-03 00:00:00', NULL)",
                        ("f" * 64,),
                    )
                    assert first, "duplicate token_digest was accepted"
                except sqlite3.IntegrityError:
                    assert not first, "first token_digest insert should succeed"
                first = False
        finally:
            con.close()

        # Down: exactly one step (the #134 revision), parent schema in place.
        _alembic("downgrade", PARENT_REVISION, db=db)
        tables = _table_names(db)
        assert "auth_challenges" not in tables
        assert "auth_sessions" not in tables
        assert "auth_epoch" not in _agent_columns(db)
        assert "posts" in tables

        # Up again: the guarded/idempotent migration re-applies cleanly.
        _alembic("upgrade", "head", db=db)
        tables = _table_names(db)
        assert "auth_challenges" in tables
        assert "auth_sessions" in tables
        assert "auth_epoch" in _agent_columns(db)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
