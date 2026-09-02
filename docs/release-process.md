# Stoa Release Process

This is the operator checklist for cutting an official release.

## Scope

Use this process for patch/minor releases (for example `v0.1.1`, `v0.2.0`).

## 1) Preflight (must be true)

- Target branch is `main`
- Working tree is clean (`git status`)
- Release candidate issues are resolved for the release scope
- No known high-severity regressions are open for the release surface

## 2) Version + changelog updates

1. Bump project version in `pyproject.toml`.
2. Update `CHANGELOG.md` with human-readable release notes.
3. (Optional) Regenerate changelog snapshot from commits:
   ```bash
   ./scripts/generate-changelog.sh <previous-tag> HEAD
   ```

## 3) Local quality gates

Run from repo root:

```bash
uv sync --all-extras
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/stoa/
uv run pytest tests/ --cov=stoa --cov-report=term-missing --cov-fail-under=60 -q
uv run pytest tests/test_security.py --cov=stoa.security --cov-fail-under=100 -q
```

## 4) Merge + CI/deploy verification

- Merge release-prep PR into `main`
- Confirm required checks are green on `main` (`test`, `SAST`, dependency scan)
- Confirm Fly deploy succeeded for the merged commit

## 5) Tag + release

```bash
git checkout main
git pull --ff-only

git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag triggers `.github/workflows/release.yml` to publish the GitHub release.

## 6) Post-release verification

- `gh release view vX.Y.Z --repo mostlycopypaste/stoa`
- Hit health endpoint on prod (`/health`)
- Smoke critical paths:
  - auth/register
  - authenticated posts read/write
  - public pinned read endpoints (if enabled)

## 7) Announce

- Post release summary in Stoa Builders
- Include: version, key changes, known follow-ups, rollback plan
