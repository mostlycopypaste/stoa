# Contributing to Herd-Inbox

Welcome! We're glad you're here.

This project is a little different — we're a mix of humans and AI agents working together on email infrastructure for herd communication. Some of us are online 24/7, others work in bursts across different time zones. We've designed our workflow to embrace that variety rather than fight it.

If you're new, start anywhere that interests you. Browse the [open issues](https://github.com/mostlycopypaste/herd-inbox/issues), ask questions, or jump straight into code. We're here to help.

## Before You Start

1. **Check for open issues** at [github.com/mostlycopypaste/herd-inbox/issues](https://github.com/mostlycopypaste/herd-inbox/issues)
2. **Comment on the issue** to signal you're working on it
3. **Create a feature branch** from `main`:
   - `feat/<short-description>` for new features
   - `fix/<short-description>` for bug fixes
   - `chore/<short-description>` for maintenance

## Local Development Setup

Get from `git clone` to a running local instance:

### Prerequisites

- **Python 3.11+** (3.11 or 3.12 recommended)
- **pip** or **uv** for dependency management
- **Git** + **gh CLI** (for PR workflow)

### Setup

```bash
# Clone the repo
git clone https://github.com/mostlycopypaste/herd-inbox.git
cd herd-inbox

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (including dev tools)
pip install -e ".[dev]"
```

### Running Locally

```bash
# Start the development server
uvicorn herd_inbox.main:app --reload --port 8000
```

The API is now available at `http://127.0.0.1:8000/`.

**Database:** SQLite — created automatically on first run at `./herd_inbox.db`. Migrations apply automatically on startup. No external database setup needed.

**Environment variables:** For local development, the app runs with sensible defaults. If you need to customize:

| Variable | Default | Purpose |
|----------|---------|--------|
| `HERD_INBOX_DB` | `./herd_inbox.db` | SQLite database path |
| `HERD_INBOX_ADMIN_KEY` | (none) | Admin API key (optional for dev) |
| `SECRET_KEY` | (auto-generated) | Session/token signing |

### Running Tests

```bash
# Full test suite with coverage
pytest tests/ -v --cov

# Lint + format check
ruff check src/ tests/
ruff format --check src/ tests/

# Type checking
mypy src/herd_inbox/
```

### Creating an API Key (local dev)

To test authenticated endpoints locally, generate a key:

```bash
# Start the server, then in another terminal:
curl -X POST -H "X-Admin-Key: your-admin-key" \
  "http://127.0.0.1:8000/api/admin/keys?agent_email=dev@localhost"
```

Or skip the admin key requirement by running tests — the test fixtures handle auth setup automatically.

---

## Development Workflow

### Test-Driven Development

We use test-driven development to keep the codebase healthy:

1. Pick an issue
2. Write tests **first** — this helps you think through the design before implementing
3. Implement the feature to make tests pass
4. Run `pytest tests/ -v --cov` locally
5. Open a PR

### Coverage Requirements

We maintain high test coverage to catch bugs early:

- **Security module (`security.py`):** 100% coverage (enforced by CI)
- **Overall project:** >80% coverage (enforced as ≥81%)

If your PR reduces coverage below these thresholds, CI will flag it — but we're happy to help you add the needed tests.

### CI Pipeline

All PRs must pass the CI pipeline (`.github/workflows/test.yml`):

- **Lint** — ruff check + format
- **Type check** — mypy on Python 3.11 and 3.12
- **Tests** — pytest with coverage gates on Python 3.11 and 3.12

## Pull Requests

### Creating a PR

```bash
# Push your branch
git push -u origin feat/my-feature

# Create PR (fills from commits)
gh pr create --fill
```

### Review Process

We use a collaborative review process:

- **2 approving reviews** required before merge
- **Security-related PRs** get extra attention — let us know if your PR touches security code
- All CI checks must pass (we'll help debug if they don't)

### PR Template

Help reviewers understand your work by including:

- What issue this closes
- What you changed and why
- How you tested it
- Any open questions or areas where you'd like specific feedback

## Abandoned and Stalled PRs

Because our contributors have different uptime patterns (agents can go offline, humans have day jobs, time zones vary), we've developed a collaborative approach to keeping PRs moving.

### For Reviewers

- If the author hasn't responded in **48 hours**, leave a friendly ping on the PR.
- After **72 hours** with no response, any org member with write access can help finish the PR by pushing to the branch (if it's not from a fork).

### For Authors

- If you need to step away, **push your work-in-progress** and leave a comment with your estimated return time.
- If you're working from a fork and can't continue, we may close your PR and open a new one to keep momentum going. You'll get full credit in the new PR description.

### Replacement PRs

When we need to carry a PR forward:

1. Close the original PR with a note explaining the situation
2. Open a new PR referencing the original (e.g., "Continues work from #12")
3. Credit the original author prominently in the description
4. The new PR goes through the same review process

## Code Style

We keep style consistent with automated tools:

- **Python 3.11+** (tested against 3.11 and 3.12)
- **Line length:** 100 characters max (ruff enforced)
- **Linting:** ruff check + ruff format
- **Type hints:** Required for all function signatures (mypy strict)

Before pushing, run these locally to catch issues early:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/herd_inbox/
pytest tests/ -v --cov
```

## Commit Messages

- Use clear, descriptive commit messages
- Prefix with type: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`
- Squash merge is the default — your PR will be squashed on merge

## Security

- **Never commit secrets** (API keys, passwords, tokens)
- **Always sanitize user input** before rendering HTML
- **Report security vulnerabilities** privately to a maintainer — don't open a public issue

## Questions?

Open an issue, comment on an existing one, or reach out to a maintainer:

- **Kevin** (`crackmac`) — project owner
- **O.C.** (`oc-mostlycopy`) — maintainer

We're friendly. Don't hesitate to ask.