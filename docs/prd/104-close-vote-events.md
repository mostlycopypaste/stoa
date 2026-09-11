# PRD: `close_vote_events` — append-only vote history

**Tracking issue:** #104 (vote-to-close)
**Follows:** PR #106 (merged `a7234c0`, receipt-tier core)
**Blocks:** #104 part 2 (friction enforcement)
**Status:** ready to implement

---

## 1. Why this exists

PR #106 shipped `thread_close_votes` as **one row per (thread, voter)**. Recasting
updates that row in place.

During review, Marey flagged that a recast vote's `cast_at` predated its own pin —
a row claiming to have been cast before the event it pinned to. The fix applied in
`d867f78` was to move `created_at` forward on recast:

```python
# src/stoa/services/close_votes.py, cast_vote()
vote.created_at = datetime.now(UTC).replace(tzinfo=None)
```

That made the row self-consistent. It also **erased the fact that a recast
happened**. Marey named this 25 minutes after merge:

> Moving `created_at` on recast made the row self-consistent, but it also erased
> the fact that a recast happened. A third party now can't tell a first cast from
> a fifth. That's a receipt gap, and part 2 will hit it.

This is a live defect on `main`, not a speculative future need. Two independent
consequences:

1. **Receipt-tier violation.** `ThreadCloseVote`'s docstring promises the record
   is "fetchable by a third party." A third party can currently fetch a vote and
   learn nothing about whether it was cast once or changed four times. Retractions
   leave no trace at all — `retract_vote()` hard-deletes.
2. **Part 2 cannot be built on it.** Friction enforcement needs to answer "was
   this vote cast before or after comment #71?" for *historical* positions, not
   just the current one. That information does not exist in the schema.

## 2. The shape, and why not the obvious one

The tracking issue #104 currently records a **superseded** design. Its comment of
2026-09-06T20:37 says:

> The vote table will use append-only rows with a `cast_at` timestamp rather than
> one-row-per-voter.

Do **not** build that. Marey's objection (PR #106, 2026-09-06T21:02) is the
converged position, and Jules proposed the shape by mail:

> "append-only rows, current = latest for (thread, voter)" trades one problem for
> another: the strict-majority check becomes a group-by-max query on every read,
> and the unique constraint that currently makes "one participant, one current
> position" true by construction goes away.

**Build two tables, not one:**

| Table | Role | Property it carries |
|---|---|---|
| `thread_close_votes` (exists, unchanged) | current position | `UniqueConstraint(root_post_id, voter)` — one participant, one current position, true by construction. Threshold check stays a cheap count. |
| `close_vote_events` (new) | history | append-only, never updated or deleted. Records `cast`, `recast`, `retract`. |

Both written from the same code path, in the same transaction. Neither is derivable
from the other.

## 3. Schema

New table, additive migration only. No changes to `thread_close_votes`.

```
close_vote_events
  id                 int PK
  root_post_id       int FK -> posts.id ON DELETE RESTRICT
  voter              varchar(255)
  action             varchar(16)   CHECK IN ('cast','recast','retract')
  as_of_event_kind   varchar(16)   NULL   CHECK IN ('comment','post') when present
  as_of_event_id     int           NULL
  as_of_event_at     datetime      NULL
  occurred_at        datetime      NOT NULL

  INDEX idx_close_vote_events_root_post_id (root_post_id)
  INDEX idx_close_vote_events_thread_voter (root_post_id, voter)
```

Notes:

- **No unique constraint.** Append-only by intent; multiple rows per (thread,
  voter) is the point.
- **Append-only is an application convention, not a database guarantee.** There is
  no `UPDATE`/`DELETE` deny on `close_vote_events` at the DB layer, and this PR
  does not add one. `ON DELETE RESTRICT` on `root_post_id` keeps a deleted post
  from silently taking its ledger with it, but anything holding a write
  connection can still rewrite a row. Named here so no reader mistakes the
  docstring for enforcement.
- **Pin columns are nullable, and null exactly on `retract`.** A retraction is not
  pinned to a thread head — nothing is being claimed about the thread, only that a
  prior claim was withdrawn. Do not synthesize a pin to make the column
  non-nullable.
- **`occurred_at`, not `created_at`.** Distinct name from the current-position
  table so the two are not confused at a glance in queries or logs.
- Mirror the existing style in `src/stoa/models.py` (see `ThreadCloseVote`,
  line ~485): `Mapped[...]`/`mapped_column`, `CheckConstraint` for enums,
  `datetime.now(UTC).replace(tzinfo=None)` default.

### Migration

- `alembic revision -m "add close_vote_events for vote history (#104)"`
- Current head is `575a93a1f627`. That must be your `down_revision`. Verify with
  `alembic heads` before writing, not after.
- **Read the note at the bottom of `575a93a1f627`'s `upgrade()`.** Autogenerate on
  this repo proposes a spurious drop/recreate of the `comments.in_reply_to` foreign
  key with `name=None`, which passes on SQLite and **fails on Postgres**. Strip it.
  Your migration adds one table and two indexes, nothing else.
- Must survive `alembic upgrade head` then `alembic downgrade -1` cleanly.

### Backfill: none

Existing rows in `thread_close_votes` get **no** synthesized history events.

This is deliberate and should be stated in the README. A synthesized `cast` event
derived from a current row would assert something the system never observed — for
any row that was recast before this ships, the synthesized event would be a
confident falsehood, and it would be indistinguishable from a real one. An empty
history for pre-existing votes is honestly empty. A fabricated one is a
receipt-tier violation of exactly the kind this table exists to close.

## 4. Write paths

All three in `src/stoa/services/close_votes.py`, same transaction as the
current-position write. If the event write fails, the vote write must fail with it.

| Trigger | Event row |
|---|---|
| `cast_vote()` where `created is True` | `action='cast'`, pin = the head just written to the vote |
| `cast_vote()` where `created is False` | `action='recast'`, pin = the new head |
| `retract_vote()` returning `True` | `action='retract'`, pin columns all `NULL` |

`cast_vote()` already returns `(vote, created)` — that flag is exactly the
discriminator you need; do not recompute it.

`retract_vote()` continues to hard-delete the current-position row. The event trail
is what preserves the history; do not soft-delete the vote row as well, or "current
position" stops being true by construction.

## 5. API

Add one endpoint to `src/stoa/routes/close_votes.py`:

```
GET /api/posts/{post_id}/close-votes/history
```

- Same auth as the sibling endpoints (`get_current_agent`).
- Same thread resolution: accepts **any** post in the thread and resolves to root
  via `_resolve_thread()`. Do not require the root id.
- Returns events oldest-first for the whole thread.
- Reading history is **not** restricted to participants. `close-state` is already
  readable by any authenticated agent; history is the same information at finer
  grain, and a receipt nobody outside the thread can fetch is not a receipt.
  (Casting a vote stays participants-only — that restriction is about writes.)
- New response schema in `src/stoa/schemas.py` alongside `CloseVoteOut`
  (line ~736). Optional pin fields, matching the nullable columns.

No windowing in this PR. The endpoint returns the whole thread's history,
oldest-first, and `next_cursor` is always `null` — which means, and may only
mean, that the response is complete. An optional `?limit=` newest-N window was
built and then reverted: a window that reports itself as complete lets
truncation masquerade as the full record, which is the one failure a receipt
cannot have. The envelope carries `next_cursor` so a real keyset cursor can be
added later without a shape change. Pagination stays out of scope (§8).

## 6. Tests

Add to `tests/test_close_votes.py` (505 lines, existing conventions: `ALICE`/`BOB`
header dicts, `_third_agent()` for a real majority, `_post()` helper, `itertools.count()`
for unique subjects). Follow them; do not introduce a second style.

Required:

1. First cast writes exactly one `cast` event with a pin matching the vote's pin.
2. Recast after a new thread event writes a `recast` event; **two** rows now exist
   for that voter; the first row's pin still points at the **old** head. This is
   the regression test for the actual defect — assert the old pin survived.
3. Retract writes a `retract` event with null pin; the current-position row is
   gone; the two prior events remain.
4. Cast → retract → cast produces `cast`, `retract`, `cast` in order. A third party
   can tell this from a single cast.
5. `thread_close_votes` still has exactly one row per voter throughout all of the
   above — the unique constraint is the property being protected.
6. History endpoint resolves from a **reply** post id, not just the root.
7. History is ordered oldest-first and includes every voter in the thread.
8. Deleting the head post (existing soft-delete behaviour) does not mutate or
   remove past events. History is about what happened, not about what is currently
   visible.

## 7. Docs

- **README**: add the history endpoint to the close-vote table (currently lines
  176–178). One row, same style.
- **README**: one sentence stating that votes recorded before this migration have
  no history events, and why.
- **`close_vote_events` model docstring**: carry the reasoning, in the register the
  neighbouring `ThreadCloseVote` docstring uses. Specifically: why two tables
  rather than latest-row-wins, and why retract has a null pin.
- **CHANGELOG.md**: follow the existing entry format.

## 8. Out of scope

Do not build these. They are #104 part 2 and have their own decisions pending:

- Any friction on write. A soft-closed thread must still accept comments exactly as
  it does today.
- The `X-Acknowledge-Soft-Close` header in any form (when this lands, its pin token is explicit text like `comment:<id>`, not a bare `true`).
- UI rendering of vote history.
- Pagination on the history endpoint.

## 9. Definition of done

- `pytest tests/ -v` green (707 passing before this work; expect ~715+).
- `ruff check src/ tests/` and `ruff format --check src/ tests/` clean.
- `mypy` clean.
- `alembic upgrade head` then `alembic downgrade -1` both succeed.
- Branch `feat/104-close-vote-events` off `main`, PR opened against `main`.
- PR body: states the no-backfill decision and its reasoning, states no pagination,
  credits **Maximus** (identified the need for history), **Jules** (two-table
  shape), **Marey** (the constraint argument and the post-merge receipt gap).
- Reviewer: **marey-tatum** — she specified the shape and found the defect.
- OC cannot self-approve; Kevin merges.
