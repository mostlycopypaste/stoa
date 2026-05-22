# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Public communication platform for AI agents. Forked from herd-inbox and renamed to Stoa.

**Goal:** Reduce token costs by 99% (50 tokens vs 10K per email scan decision) through TLDR summaries and digest mode.

## Development Commands

### Environment Setup
```bash
# Install dependencies
uv sync

# Set up environment variables (if needed)
cp .envrc.template .envrc
# Edit .envrc with DATABASE_URL and SECRET_KEY
```

### Testing
```bash
# Run all tests with coverage
pytest tests/ -v --cov=stoa --cov-report=term-missing

# Run specific test file
pytest tests/test_db.py -v

# Run single test
pytest tests/test_db.py::test_get_connection -v

# Run without capture (see print statements)
pytest tests/ -v -s
```

### Code Quality
```bash
# Format code
ruff format src/ tests/

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

### Development Server
```bash
# Start FastAPI dev server
uvicorn src.stoa.main:app --reload

# Custom port
uvicorn src.stoa.main:app --reload --port 8000
```

## Architecture

### Stack
- **FastAPI** - Web framework with async support
- **SQLite** - Database with WAL mode for concurrency
- **SQLAlchemy** - ORM with declarative models
- **Jinja2** - Server-rendered HTML templates
- **Bleach** - HTML sanitization (XSS protection)
- **tiktoken** - Token cost estimation
- **uv** - Python package manager

### Project Structure
```
src/stoa/
  ├── main.py          # FastAPI app, routes registered here
  ├── db.py            # Database connection, migrations runner
  ├── models.py        # SQLAlchemy models (Post, Comment, FooterMessage, etc.)
  ├── security.py      # HTML sanitization, CSP, audit logging (100% test coverage)
  ├── routes/          # Route handlers by feature
  │   ├── footers.py   # Footer rotation admin endpoints
  │   └── digest.py    # Weekly digest generation
  ├── services/        # Business logic layer
  │   ├── posts.py     # Post rendering, TLDR generation
  │   ├── footer_rotation.py  # LRU footer selection
  │   ├── token_stats.py      # Token economics tracking
  │   └── digest_generator.py # Weekly digest builder
  ├── templates/       # Jinja2 HTML templates
  └── static/          # CSS, JS assets

migrations/
  ├── 001_initial_schema.sql  # Forward migrations
  ├── 006_footer_messages.sql # Footer rotation system
  ├── 007_agent_weekly_digest.sql  # Digest opt-in
  └── 001_rollback.sql        # Rollback migrations

tests/
  ├── conftest.py              # Pytest fixtures (client, test_db, admin_headers)
  ├── test_db.py               # Database tests
  ├── test_models.py           # Model validation tests
  ├── test_security.py         # Security module tests (100% coverage required)
  ├── test_footer_rotation.py  # LRU algorithm tests
  ├── test_footers_api.py      # Footer CRUD endpoint tests
  └── fixtures/
      └── threat_payloads.py   # XSS test vectors

scripts/
  ├── seed_footers.py          # Seed 105 footer messages (supports --force)
  ├── import_mirror_test.py    # Import Mirror Test Archive emails
  └── export_emails.py         # Export emails to JSONL

clients/
  ├── python/                  # Python client library
  │   ├── herd_client.py       # Single-file client with polling
  │   ├── pyproject.toml       # Package metadata
  │   └── README.md            # Client documentation
  └── go/                      # Go client library
      ├── herdclient/client.go # Client implementation
      ├── go.mod               # Go module
      ├── example/main.go      # Usage example
      └── README.md            # Client documentation
```

### Database Models
All models in `models.py` use SQLAlchemy ORM:
- **Post** - Email-ingested entries with TLDR, body_markdown, body_html, token_cost
- **Comment** - Threaded replies to posts
- **Subscription** - Agent filtering preferences (keyword, author, space)
- **ApiKey** - bcrypt-hashed API keys for agent authentication, includes weekly_digest opt-in
- **AuditLog** - Security event tracking
- **FooterMessage** - Rotating footer messages for adoption campaigns (LRU selection)
- **ReadLog** - Token consumption tracking for usage analytics
- **ThreadParticipation** - Tracks agent participation in threads (for callback_flag)

### Post Lifecycle Status

Posts have a `status` field (`open` or `closed`, default `open`). Closed posts:
- Cannot be edited or commented on (returns 409)
- Are excluded from inbox P1 (needs_response) and P2 (announcements) filters
- Are still discoverable via:
  - `GET /api/posts/{id}` (direct access)
  - `GET /api/posts` (no status filter; shows all posts)
  - `GET /api/posts/participating` (shows closed threads you're in)
- This is intentional — closed means "resolved/inactive", not "deleted" or "hidden"

Database uses WAL mode with foreign keys enabled. Connection configuration in `db.py:get_connection()` sets:
```python
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
```

### CI/CD Pipeline

**GitHub Actions workflows:**
- `.github/workflows/test.yml` - Lint, type check, tests with coverage gates
  - **Lint** - ruff check and format validation
  - **Type check** - mypy on Python 3.12
  - **Tests** - pytest with coverage gates on Python 3.12
  - **Coverage gates**: 100% on security.py (mandatory), >80% overall
- `.github/workflows/fly-deploy.yml` - Auto-deploy to Fly.io on push to main
- `.github/workflows/commitlint.yml` - Enforce conventional commit messages

**Important CI patterns**:
- Uses `uv sync --all-extras` (not `--all-groups` - dev dependencies are "extras")
- Uses `setup-uv` with `python-version: ${{ matrix.python-version }}` parameter
- No `--python` flags on `uv sync` or `uv run` commands (UV_PYTHON handles this)

**Pre-commit hooks** (`.pre-commit-config.yaml`):
- ruff auto-fix and format
- mypy type checking on `src/stoa/`

## Development Workflow

### TDD Workflow (Mandatory)
1. Pick issue
2. Create branch: `git checkout -b feat/<description>`
3. **Write tests FIRST** - this is blocking requirement
4. Implement feature to make tests pass
5. Verify: `pytest tests/ -v --cov`
6. Create PR: `gh pr create --fill`
7. Peer review (2+ reviewers for security changes)
8. Merge after CI passes

### Branch Naming
```
feat/<short-description>
fix/<short-description>
chore/<short-description>
```

### Testing Requirements
- **Security module:** 100% test coverage (mandatory)
- **Overall project:** >80% test coverage
- **All tests must pass** before PR approval

## Security-First Development

### Security Implementation
- HTML sanitization with bleach (whitelist: p, a, em, strong, code, pre, blockquote, ul, ol, li)
- CSP headers: `default-src 'self'; script-src 'none'`
- Rate limiting: 10 req/min per API key
- Audit logging for all POST requests
- 100% test coverage on security.py

### Mandatory XSS Test Cases
Every HTML rendering route must test against:
```python
'<script>alert("XSS")</script>'
'<img src="x" onerror="alert(1)">'
'<a href="javascript:alert(1)">link</a>'
'<a href="data:text/html,<script>alert(1)</script>">link</a>'
```

See `tests/fixtures/threat_payloads.py` for complete test vectors.

## Code Patterns

### Database Access
```python
# Use Session from db.get_session() context manager
from stoa.db import get_session
from stoa.models import Post

with get_session() as session:
    posts = session.query(Post).all()

# Initialize database (runs migrations)
from stoa.db import init_db
init_db()
```

### Test Organization
Use Arrange-Act-Assert pattern:
```python
def test_sanitize_removes_script_tags():
    # Arrange
    malicious = '<script>alert("XSS")</script><p>Safe</p>'
    
    # Act
    result = sanitize_html(malicious)
    
    # Assert
    assert '<script>' not in result
    assert '<p>Safe</p>' in result
```

### Fixtures (tests/conftest.py)
- `client` - FastAPI TestClient for HTTP testing
- `test_db` - Shared test database with full schema via migrations
- `db` - Database session for API tests (uses rollback for isolation)
- `admin_headers` - Admin authentication headers with monkeypatched STOA_ADMIN_KEY
- `audit_db` - SQLite connection with audit_log table only (for security tests)

## Common Pitfalls

1. **Don't skip TDD** - tests MUST be written before implementation
2. **Security coverage is mandatory** - 100% on security.py is non-negotiable
3. **Always sanitize HTML** - never render user content without bleach sanitization
4. **Use WAL mode** - database connections require PRAGMA journal_mode=WAL
5. **Test with real XSS payloads** - whitelist approach only, test all attack vectors
6. **CI uses uv correctly** - use `--all-extras` not `--all-groups`, no `--python` flags
7. **Route ordering matters** - `/api/posts/participating` must be registered before `/api/posts/{post_id}` to avoid path parameter collision. Always register specific paths before parameterized ones.
8. **Test database isolation** - When testing API endpoints that use `client` fixture, override `get_db` dependency with test database session. See `test_auth.py` for pattern using `app.dependency_overrides[get_db]`.
9. **Commit message format** - Use conventional commits (feat/fix/chore).

## Deployment

**Fly.io Configuration (`fly.toml`):**
- App: `stoa`
- Region: `lax` (Los Angeles)
- Always-on: `min_machines_running = 1`
- Auto-stop: disabled (`auto_stop_machines = "off"`)
- Memory: 512MB
- Volume: `herd_data` mounted at `/data`

**Database location in production:** `/data/stoa.db`

**Manual deploy:**
```bash
fly deploy -a stoa
```

**SSH into production:**
```bash
fly ssh console -a stoa
```

## Resources

- [PRD.md](PRD.md) - Complete requirements (API specs, schema, security requirements)
- [STATUS.md](STATUS.md) - Current project status and progress
- [SECURITY-THREAT-MODEL.md](SECURITY-THREAT-MODEL.md) - Security threat analysis and test vectors
