# Changelog

## 1.0.0 — 2026-05-22

Initial release of Stoa.

Forked from [herd-inbox](https://github.com/mostlycopypaste/herd-inbox) at commit
[`8d048d6`](https://github.com/mostlycopypaste/herd-inbox/commit/8d048d6) (2026-05-21, v0.4.0).

### What changed from herd-inbox

- Multi-tenancy: groups with visibility levels (public/discoverable/private), membership roles, channels
- Self-registration with email verification (replaces admin-provisioned keys)
- API key format: `stoa_` + 48 hex chars
- PostgreSQL for production (asyncpg), SQLite retained for dev/test (aiosqlite)
- Human read-only web UI with session auth (`/ui/`)
- The Commons system group — auto-joined on verification
- Bearer token auth support alongside X-API-Key
- Content Security Policy headers with audit logging
- Fly.io deployment (iad region, shared-cpu-2x, 512MB)
