# Deployment & CI/CD Setup

## GitHub Repository Configuration

### 1. Required Secrets

Add these in **Settings → Secrets and variables → Actions**:

- `FLY_API_TOKEN` — Fly.io deploy token
  ```bash
  fly auth token
  ```

### 2. Branch Protection (Recommended)

Enable on `main` branch in **Settings → Branches → Branch protection rules**:

**Required status checks** (enforce CI before merge):
- ✅ Lint (ruff)
- ✅ Type check (mypy, py3.11)
- ✅ Type check (mypy, py3.12)
- ✅ Tests (py3.11)
- ✅ Tests (py3.12)
- ✅ Dependency vulnerability scan
- ✅ Semgrep security scan
- ✅ Review dependency changes (PR only)

**Additional rules**:
- ✅ Require branches to be up to date before merging
- ✅ Require linear history (no merge commits)
- ✅ Do not allow bypassing the above settings

## CI/CD Workflows

### Automated on every PR and push to main:

1. **test.yml** — Comprehensive testing
   - Lint (ruff check + format)
   - Type checking (mypy on Python 3.11 & 3.12)
   - Tests with coverage (81% overall, 100% on security.py)
   - Dependency vulnerability scan (pip-audit)

2. **sast.yml** — Static security analysis
   - Semgrep with Python security rules
   - SQL injection, XSS, secrets detection
   - ~30 second scan

3. **dependency-review.yml** — PR dependency check
   - Flags vulnerable dependencies
   - License compliance check
   - Auto-comments on PRs

### Automated on push to main:

4. **fly-deploy.yml** — Production deployment
   - Deploys to `stoa-murmur.fly.dev`
   - Runs after all CI checks (if branch protection enabled)

## Manual Deploy

If CI is blocked or you need emergency deploy:

```bash
fly deploy --app stoa-murmur
```

## Security Hardening Applied

All workflows follow these practices:

- ✅ Actions pinned to commit SHA (supply-chain defense)
- ✅ Minimal permissions (`contents: read`)
- ✅ Timeout limits on every job
- ✅ No credential persistence in checkouts
- ✅ Concurrency cancellation (cost optimization)
- ✅ No user-controlled input in shell commands

## Monitoring

- **Fly.io dashboard**: https://fly.io/apps/stoa-murmur
- **GitHub Actions**: https://github.com/mostlycopypaste/stoa/actions
- **Coverage reports**: Available as artifacts on test runs
