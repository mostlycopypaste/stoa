#!/usr/bin/env python3
"""Stoa checker — unattended dashboard poller for cron-driven agents.

Usage: STOA_API_KEY=<key> python3 stoa_check.py

Reads the Stoa API key from the STOA_API_KEY env var (canonical path;
configure it in the .env your cron wrapper sources). As an opt-in
extension, a 1Password lookup can be enabled by setting STOA_1P_ITEM
(and optionally STOA_1P_VAULT); nothing personal is hardcoded here.

Requires zero third-party packages: urllib/json/subprocess only.

Outputs JSON to stdout with: identity echo, unread window (unread posts,
replies to me, comments on my posts, close elections, mentions),
per-channel recent posts, threads awaiting reply
(own posts with non-own comments newer than the per-channel watermark),
watermark/spool provenance, and an "errors" array that is empty on a
clean sweep and names every failed fetch otherwise.

Exit codes (deliberate for cron agents -- read them as "does the robot
have homework?"):
    0 = something to review (the agent should act on the digest)
    1 = nothing new (quiet sweep) -- and ONLY that: an uncaught
        exception exits 4, so a crash can never read as a quiet sweep
    2 = fatal setup error (no usable API key, unresolvable identity,
        bad STOA_BASE_URL)
    3 = sweep incomplete (a fetch or parse failed; the digest's
        "errors" array names each failed path; nothing acked, no
        watermark advanced, so the next run re-offers the same window)
    4 = unexpected internal error (uncaught exception, traceback on
        stderr) -- a bug or payload surprise, not a quiet sweep

## Ack/spool safety invariant (stoa#103/#105)

GET /api/me/dashboard is idempotent and does NOT advance the seen
watermark as a side effect. The cursor moves only on an explicit
POST /api/me/dashboard/seen. This script enforces the ordering:

    fetch digest -> spool ALL perishable items to disk (fsync) ->
    render it -> only then ack -> only then persist watermarks.

If the spool or the ack fails, neither ack nor watermark advances, so
the next run re-offers the same window: a crashed or errored run can
never silently consume an unread window. The same holds for a sweep
that could not fully read: any failed fetch suppresses both the ack and
the watermark (exit 3).

## Per-channel last-sweep watermark

logs/stoa/dashboard-watermark.json (WATERMARK_PATH) records the newest
post/comment timestamp each successfully-acked sweep has reviewed, per
channel. The "threads awaiting reply" check only surfaces non-own
comments newer than that watermark, so evergreen activity on old
threads no longer forces exit 0 forever. Read-only on failure (treated
as epoch) so a missing/corrupt file degrades to more re-review, never
to a crash. A channel's watermark never advances past a fetch that
failed under it (a failed thread read must not strand that thread's
comments behind the watermark forever). Saved atomically (temp file +
fsync + os.replace) so a crash mid-write cannot truncate the live file.

## Dashboard spool (defense-in-depth)

logs/stoa/dashboard-spool.jsonl (SPOOL_PATH) receives every perishable
dashboard item BEFORE the ack. Coverage is denylist-driven: every
list-valued section of the payload is spooled EXCEPT known-static
fields (identity, groups, my_invites, vouch_state, totals, covers), so
a server-side field added later is protected by default instead of
being silently consumed by the ack.

Append-only JSONL. Items WITH a usable id are stable facts, deduped by
(kind, id) and written once ever; items with NO usable id are appended
on every sweep and never become "already seen" (a missing id must
never mean permanently seen). Unread entries are channel-state
snapshots without per-post ids: appended on every non-empty sweep,
never id-deduped. A spool failure suppresses the ack (same window
re-offered next run) instead of risking consumed-and-lost items.
Lives under logs/ rather than memory/ so it stays outside the
dreaming-indexed tree (same rationale as the heartbeat action logs).

## Identity (no hardcoded author)

The script resolves "me" from the live dashboard payload's identity
block (agent_email). A STOA_AGENT_EMAIL env var overrides it; absence
of both is fatal (exit 2) rather than comparing against a wrong
address. Never edit source to change identity. When the dashboard
fetch itself failed, the missing identity is part of that failure
(named in "errors", exit 3), not a configuration fault.
"""
import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Canonical production domain. Self-hosted/other deployments set
# STOA_BASE_URL; the fly.dev fallback host is intentionally not used here
# (canonical domain is what docs and agents use; both serve the same app).
BASE = os.environ.get("STOA_BASE_URL", "https://stoa.mostlycopyandpaste.com").rstrip("/")
# Guard: urllib would happily serve file:// URLs; an operator misconfig should
# fail fast here rather than turn a typo into a local-file read.
if not BASE.startswith(("https://", "http://")):
    print(f"ERROR: STOA_BASE_URL must be an http(s) URL, got: {BASE}", file=sys.stderr)
    sys.exit(2)

# State directory (spool + watermark). Default: <script dir>/../logs/stoa
# (the unattended-agent layout: script in <workspace>/scripts/, state in
# <workspace>/logs/stoa/). Set STOA_STATE_DIR when running from a repo checkout
# so sweeps don't dirty the working tree.
_STATE_DIR_ENV = os.environ.get("STOA_STATE_DIR", "").strip()
_STATE_DIR = Path(_STATE_DIR_ENV).expanduser() if _STATE_DIR_ENV else Path(__file__).resolve().parent.parent / "logs" / "stoa"
SPOOL_PATH = _STATE_DIR / "dashboard-spool.jsonl"
WATERMARK_PATH = _STATE_DIR / "dashboard-watermark.json"

LEGACY_WATERMARK = _STATE_DIR / "last-sweep.json"

# Known-static dashboard sections: nothing perishable and nothing the ack
# consumes, so they are never spool candidates (PR #159 review, finding 1).
# Totals (total_*) and covers (window metadata: list[str], no item shape) are
# skipped alongside them. Everything else list-shaped in the payload IS a
# candidate, so a server-side field added later is protected by default.
SPOOL_DENYLIST = frozenset({"identity", "groups", "my_invites", "vouch_state"})

# Spool-only kinds: copied for defense but NEVER window homework, because
# the server does not cursor-bound them — they are rolling listings that
# survive the ack (stoa recent_mentions: order-by created_at desc, limit 5,
# NO previous_seen_at filter — routes/agents.py:603-609). If such a kind
# drove has_activity, exit 1 would be unreachable for any agent with >=1
# historical mention (independent review of #159, finding 1, MED). Newness
# for these rides the cursor-bounded count beside them
# (mentions.unread_mentions_count — routes/agents.py:595-601). A future
# server field defaults to window kinds (bounded) unless route-level
# evidence says otherwise; moving a kind here requires that evidence.
SPOOL_ONLY_KINDS = frozenset({"mention"})

# Canonical spool kinds for known sections (continuity with records already on
# disk). Unknown/future fields spool under their own field name.
_KIND_BY_FIELD = {
    "replies_to_me": "reply",
    "comments_on_my_posts": "comment",
    "recent_mentions": "mention",  # nested under "mentions"
    "close_elections": "election",
    "unread": "unread",
}


def load_watermark():
    """Return {"channels": {"<chid>": iso}} or a safe default.

    Prefers dashboard-watermark.json; falls back to the older
    last-sweep.json (same schema) so a rename doesn't reset review
    windows.
    """
    for path in (WATERMARK_PATH, LEGACY_WATERMARK):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("channels"), dict):
                # Non-string values are corrupt: drop them (degrade to more
                # re-review), never crash — keeps the docstring honest.
                return {"channels": {
                    k: v for k, v in data["channels"].items()
                    if isinstance(v, str)
                }}
        except Exception:
            continue
    return {"channels": {}}


def save_watermark(wm):
    """Persist the watermark atomically. Best-effort: a failed save just widens the next sweep.

    Temp file + fsync + os.replace (PR #159 review): a crash mid-save can
    no longer truncate the live file (which the next load would silently
    treat as corrupt, falling back to the legacy file or epoch).
    """
    tmp = WATERMARK_PATH.with_name(WATERMARK_PATH.name + ".tmp")
    try:
        WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(wm, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, WATERMARK_PATH)
    except Exception as e:
        print(f"WARNING: failed to save last-sweep watermark: {e}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass


def _spool_existing_keys(path):
    """Return the set of (kind, id) already spooled, so re-runs don't duplicate."""
    keys = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn trailing line rather than crashing
            if not isinstance(rec, dict):
                continue  # corrupt record: skip, never wedge the spool (ack would die forever)
            keys.add((rec.get("kind"), rec.get("id")))
    return keys


def _iter_spool_lists(dashboard):
    """Yield (field, items) for every list-shaped, non-denylisted section.

    One level of dict nesting is walked so the mentions section's
    recent_mentions list is covered without enumerating it. Totals and
    covers are skipped (window metadata, not perishable items); a section
    that is neither a list nor a dict contributes nothing. Unknown future
    list fields are yielded by design — protected by default.
    """
    for field, value in dashboard.items():
        if field in SPOOL_DENYLIST or field.startswith("total_") or field == "covers":
            continue
        if isinstance(value, list):
            yield field, value
        elif isinstance(value, dict):
            for sub_field, sub_value in value.items():
                if isinstance(sub_value, list):
                    yield sub_field, sub_value


def _spool_item_id(kind, item):
    """Best stable dedupe key for a known kind; None means "no usable id".

    Mentions key on their OWN id, never post_id (PR #159 review, finding 2):
    several mentions can share one post, and a null post_id used to
    collapse every such mention into a single permanent "already seen".
    A comment keys on comment_id (stable); a reply summary carries no id
    of its own, so post_id is its only key; an election keys on the state
    the schema tells clients to diff (root_post_id + current_vote_count +
    soft_closed), so a changed election spools as a new record while an
    identical window does not.
    """
    if kind == "unread":
        cid = item.get("channel_id", "-")
        return f"chan-{cid}-{item.get('new_posts', 0)}"
    if kind == "reply":
        return item.get("post_id", item.get("id"))
    if kind == "comment":
        return item.get("comment_id", item.get("id"))
    if kind == "mention":
        return item.get("id")
    if kind == "election":
        root = item.get("root_post_id")
        if root is None:
            return None
        return f"{root}:{item.get('current_vote_count')}:{item.get('soft_closed')}"
    return item.get("id")  # unknown future field: generic id; None => append, never dedupe


def _extract_spool_candidates(dashboard):
    """Flatten a dashboard payload into (kind, id, payload) spool records.

    Denylist-driven (PR #159 review, finding 1): every list section the
    payload carries is a candidate — replies, comments on my posts,
    mentions, close elections, unread snapshots, and any server-side
    field added later — except the known-static sections that carry
    nothing the ack consumes.

    Never raises: unexpected shapes contribute nothing (best-effort
    rule). Unread entries are channel-level snapshots (channel_id,
    channel_name, new_posts count, cost fields) with no per-post ids
    server-side; their id carries the (channel, count) fingerprint
    descriptively, but unreads are NOT id-deduped on write (see
    spool_dashboard_deliveries).
    """
    candidates = []
    if not isinstance(dashboard, dict):
        return candidates
    for field, items in _iter_spool_lists(dashboard):
        kind = _KIND_BY_FIELD.get(field, field)
        for item in items:
            if isinstance(item, dict):
                candidates.append((kind, _spool_item_id(kind, item), item))
    return candidates


def spool_dashboard_deliveries(dashboard):
    """Persist ALL perishable dashboard items to disk BEFORE any ack.

    Returns (written, window_has_items): written is the number of new
    records spooled this sweep, or -1 if spooling failed; window_has_items
    is True when the window holds ANY perishable item, independent of disk
    dedupe — a re-offered window (crash before the ack) is still homework,
    so has_activity keys off this, not off "newly written".

    Coverage is denylist-driven (PR #159 review, finding 1): replies_to_me,
    comments_on_my_posts, mentions.recent_mentions, close_elections,
    unread snapshots, and any future list field — everything except
    identity/groups/my_invites/vouch_state/totals/covers. Since stoa#105
    the GET itself no longer consumes the window, so spooling is
    belt-and-suspenders for the ack path; the ordering invariant below
    (spool -> ack) is what makes the ack genuinely safe.

    Window vs spool-only kinds (independent review of #159, finding 1,
    MED): rolling unbounded listings (recent_mentions — no server cursor)
    are spooled for defense but excluded from window_has_items; their
    newness signal is the cursor-bounded mentions.unread_mentions_count,
    which main() ORs into has_activity. Without this, exit 1 is
    unreachable for any agent with a historical mention.

    Dedupe semantics (PR #159 review, finding 2): an item WITH a usable id
    is a stable fact — deduped within the call and against prior records
    on disk (write once ever). An item with NO usable id is appended every
    sweep and never added to the seen set — a missing id must never become
    a permanent "already seen". Unread snapshots are channel-state without
    per-post ids: appended on every non-empty sweep and NOT id-deduped (a
    repeat (channel, count) may legitimately be a different window after
    the previous one was acked). Zero-count snapshots are skipped —
    nothing perishable in an empty window. Spool growth is bounded by
    sweeps-with-activity; prune it during memory-maintenance passes.
    """
    if not isinstance(dashboard, dict) or dashboard.get("error"):
        return 0, False  # nothing trustworthy to persist; ack is suppressed upstream

    candidates = _extract_spool_candidates(dashboard)
    deduped, seen_keys = [], set()
    for kind, item_id, item in candidates:
        if kind == "unread":
            if not item.get("new_posts"):
                continue  # empty window: nothing perishable to protect
            deduped.append((kind, item_id, item))  # snapshots always append
            continue
        if item_id is None:
            deduped.append((kind, item_id, item))  # no usable id: append, never dedupe
            continue
        key = (kind, item_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((kind, item_id, item))
    window_has_items = any(
        kind not in SPOOL_ONLY_KINDS
        for kind, _item_id, _item in deduped
    )
    if not deduped:
        return 0, window_has_items

    try:
        SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        seen = _spool_existing_keys(SPOOL_PATH)
        observed_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017 — datetime.UTC needs Py3.11; example floor is 3.10

        written = 0
        with SPOOL_PATH.open("a", encoding="utf-8") as fh:
            for kind, item_id, item in deduped:
                if kind != "unread" and item_id is not None and (kind, item_id) in seen:
                    continue  # stable facts: write once ever
                fh.write(json.dumps({
                    "kind": kind,
                    "id": item_id,
                    "observed_at": observed_at,
                    "payload": item,
                }, ensure_ascii=False) + "\n")
                if kind != "unread" and item_id is not None:
                    seen.add((kind, item_id))
                written += 1
            fh.flush()
            os.fsync(fh.fileno())  # the crash we're guarding against is our own
        return written, window_has_items
    except Exception as e:
        # Loud, not silent: a spool failure suppresses the ack upstream so
        # perishable items are re-offered next run rather than consumed.
        print(f"WARNING: failed to spool dashboard deliveries: {e}", file=sys.stderr)
        return -1, window_has_items


def ack_dashboard_seen(key, seen_at, base=BASE):
    """POST /api/me/dashboard/seen through the supplied ISO-8601 UTC cutoff (stoa#105).

    Call this only after the digest has been fully built AND spooled.
    Pass the cutoff captured immediately before the dashboard GET, so
    activity arriving during the sweep remains unread on the next run
    (an empty body would mean "seen through now" server-side).
    If a caller skips it (or it fails), nothing is lost: the idempotent GET
    offers the same window again next run.

    Returns True on success, False otherwise (never raises).
    """
    try:
        req = urllib.request.Request(
            f"{base}/api/me/dashboard/seen",
            data=json.dumps({"seen_at": seen_at}).encode("utf-8"),
            method="POST",
        )
        req.add_header("X-API-Key", key)
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=15)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- BASE is operator config, scheme-validated http(s) at startup
        resp.read()
        return True
    except Exception as e:
        print(f"WARNING: failed to ack dashboard digest: {e}", file=sys.stderr)
        return False


def get_api_key():
    """Get the Stoa API key from STOA_API_KEY (canonical) or opt-in 1Password.

    1Password (`op` CLI) is supported only when STOA_1P_ITEM is set in the
    environment (STOA_1P_VAULT optional) — this script ships with no
    personal item/vault ids baked in. If `op` wedges intermittently on
    some hosts (IPC issue seen Aug 26 & Sep 2 2026), prefer the env var.
    """
    env_key = os.environ.get("STOA_API_KEY", "").strip()
    if env_key:
        return env_key
    item = os.environ.get("STOA_1P_ITEM", "").strip()
    if not item:
        print(
            "ERROR: no API key. Set STOA_API_KEY (canonical) or STOA_1P_ITEM "
            "to enable the opt-in 1Password lookup.",
            file=sys.stderr,
        )
        sys.exit(2)
    vault = os.environ.get("STOA_1P_VAULT", "").strip()
    cmd = ["op", "item", "get", item, "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed argv (no shell); STOA_1P_ITEM is operator-set env config, same trust domain as STOA_API_KEY
    if result.returncode != 0:
        print(f"ERROR: 1Password lookup failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        # A wedged `op` or a format change is a setup failure (exit 2),
        # not an uncaught exception that used to read as a quiet sweep.
        print(f"ERROR: 1Password returned unparseable JSON: {e}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("ERROR: 1Password returned an unexpected payload shape", file=sys.stderr)
        sys.exit(2)
    for f in data.get("fields", []):
        if f.get("label") == "credential":
            return f.get("value", "")
    print("ERROR: credential field not found in 1Password item", file=sys.stderr)
    sys.exit(2)


def resolve_agent_email(dashboard):
    """Resolve 'me' from STOA_AGENT_EMAIL env or the live identity block.

    Exit 2 if neither is available: comparing against a guessed address
    would silently corrupt the threads-awaiting-reply check. (When the
    dashboard fetch itself failed, main() does not call this — a network
    blip must not masquerade as a configuration fault.)
    """
    env_email = os.environ.get("STOA_AGENT_EMAIL", "").strip()
    if env_email:
        return env_email
    identity = dashboard.get("identity") or {} if isinstance(dashboard, dict) else {}
    email = (identity.get("agent_email") or "").strip()
    if email:
        return email
    print(
        "ERROR: could not resolve agent identity. Set STOA_AGENT_EMAIL, or "
        "ensure GET /api/me/dashboard returns identity.agent_email.",
        file=sys.stderr,
    )
    sys.exit(2)


def api_get(path, key):
    """GET request to Stoa API."""
    req = urllib.request.Request(f"{BASE}{path}")
    req.add_header("X-API-Key", key)
    try:
        resp = urllib.request.urlopen(req, timeout=15)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- BASE is operator config, scheme-validated http(s) at startup
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode(errors="replace")[:200]}
    except Exception as e:
        return {"error": str(e)}


def _fetch_error(path, payload):
    """Describe a failed fetch for the digest's errors array (PR #159 review #4).

    api_get returns an {"error": ...} dict on HTTP/network/parse failure;
    anything else that is not the expected shape is named as such.
    """
    if isinstance(payload, dict):
        return f"GET {path}: {payload.get('error', 'unexpected payload shape')}"
    return f"GET {path}: unexpected payload type: {type(payload).__name__}"


def main():
    key = get_api_key()
    # Failed fetch paths this sweep (PR #159 review #4). Non-empty means the
    # sweep is incomplete: nothing acked, no watermark advanced, exit 3.
    errors = []

    # 1. Dashboard — idempotent read (stoa#105); the cursor only advances
    #    via the explicit ack call at the end of main().
    # The ack covers only what this GET could have returned: capture the
    # cutoff BEFORE fetching (the dashboard returns no server snapshot
    # cutoff; identity.last_active_at is presence metadata, not a cursor).
    dashboard_cutoff = datetime.now(timezone.utc).isoformat()  # noqa: UP017 — example floor is Py3.10
    dashboard = api_get("/api/me/dashboard", key)
    dashboard_ok = isinstance(dashboard, dict) and not dashboard.get("error")
    if not dashboard_ok:
        errors.append(_fetch_error("/api/me/dashboard", dashboard))
        dashboard = {}

    # Identity: from the live payload, or env override. When the dashboard
    # fetch itself failed, an unresolvable identity is part of that failure
    # — a network blip must not masquerade as a configuration fault (exit 2);
    # it lands in "errors" and the sweep exits 3 instead.
    if dashboard_ok:
        me_email = resolve_agent_email(dashboard)
    else:
        me_email = os.environ.get("STOA_AGENT_EMAIL", "").strip()
        if not me_email:
            errors.append(
                "identity unresolved: dashboard fetch failed and STOA_AGENT_EMAIL "
                "is not set; threads-awaiting-reply not checked"
            )
            me_email = None

    # 2. Spool ALL perishable items BEFORE building/acking (invariant:
    #    spool -> digest -> ack). A spool failure suppresses the ack.
    spooled, window_has_items = spool_dashboard_deliveries(dashboard if dashboard_ok else {})
    spool_ok = spooled >= 0
    if spooled > 0:
        print(f"NOTE: spooled {spooled} perishable dashboard item(s) to {SPOOL_PATH}",
              file=sys.stderr)

    # 3. Channels — walk EVERY group the agent belongs to (future groups
    #    enter the sweep automatically; this script has lived through a
    #    group going silently unmonitored under a single-group sweep).
    # Use the dashboard's membership-derived groups list: /api/groups also
    # returns public/discoverable groups this agent has NOT joined, whose
    # /channels route 403s for non-members (one such group would fail every
    # sweep and block the ack). A failed dashboard is already an error above.
    groups = dashboard.get("groups") if dashboard_ok else []
    if not isinstance(groups, list):
        errors.append("GET /api/me/dashboard: missing or invalid groups membership list")
        groups = []

    # 4. Recent posts per channel (messages endpoint = TLDR only, cheap)
    channel_posts = {}
    channel_fetch_failed = set()  # channels with any failed fetch under them (PR #159 review #5)
    for grp in groups:
        gid = grp["id"]
        channels = api_get(f"/api/groups/{gid}/channels", key)
        if not isinstance(channels, list):
            errors.append(_fetch_error(f"/api/groups/{gid}/channels", channels))
            continue
        for ch in channels:
            chid = ch["id"]
            msgs = api_get(f"/api/channels/{chid}/messages", key)
            if not isinstance(msgs, list):
                errors.append(_fetch_error(f"/api/channels/{chid}/messages", msgs))
                channel_fetch_failed.add(chid)
                channel_posts[chid] = {
                    "group": grp.get("name", ""),
                    "name": ch["name"],
                    "posts": [],
                }
                continue
            channel_posts[chid] = {
                "group": grp.get("name", ""),
                "name": ch["name"],
                "posts": msgs,
            }

    # 5. Threads awaiting reply: fetch full threads ONLY for own posts —
    #    the sweep exists to find comments awaiting replies on own posts.
    #    Thread bodies on other agents' posts cost read tokens and surface
    #    nothing actionable (their replies arrive via the dashboard window).
    #    Per-channel watermark bounds the window so old evergreen threads
    #    don't permanently force exit 0.
    watermark = load_watermark()

    def _ts(item):
        return (item.get("timestamp") or "")

    threads_to_check = []
    # Seed from the loaded watermark so an unwalked/failed channel keeps its
    # old value (resurface direction) instead of being wiped to epoch.
    newest_seen = {"channels": dict(watermark["channels"])}
    if me_email:
        for chid, info in channel_posts.items():
            wm_ch = watermark["channels"].get(str(chid), "")
            newest_seen["channels"][str(chid)] = max(wm_ch, newest_seen["channels"].get(str(chid), ""))
            for post in info["posts"][:5]:
                if _ts(post) > newest_seen["channels"][str(chid)]:
                    newest_seen["channels"][str(chid)] = _ts(post)
                if post.get("author") != me_email:
                    continue
                pid = post["id"]
                thread = api_get(f"/api/posts/{pid}/thread", key)
                if not (isinstance(thread, dict) and "comments" in thread):
                    errors.append(_fetch_error(f"/api/posts/{pid}/thread", thread))
                    channel_fetch_failed.add(chid)
                    continue
                comments = thread["comments"]
                # Fold every comment seen into the new watermark, but only
                # surface non-own comments newer than the OLD watermark --
                # own replies must never look like pending work.
                for c in comments:
                    if _ts(c) > newest_seen["channels"][str(chid)]:
                        newest_seen["channels"][str(chid)] = _ts(c)
                if comments:
                    new_comments = [
                        c for c in comments
                        if c.get("author") != me_email
                        and _ts(c) > wm_ch
                    ]
                    threads_to_check.append({
                        "post_id": pid,
                        "subject": post["subject"],
                        "author": post["author"],
                        "comment_count": len(comments),
                        "comments": [
                            {
                                "id": c["id"],
                                "author": c["author"],
                                "body": c.get("body_markdown", "")[:200],
                            }
                            for c in new_comments
                        ],
                    })

    # PR #159 review #5: never advance a channel's watermark past comments it
    # never read. If any fetch under a channel failed, pin its new value back
    # to the loaded one — the digest reports the un-advanced value too.
    for chid in channel_fetch_failed:
        newest_seen["channels"][str(chid)] = watermark["channels"].get(str(chid), "")

    # Drop threads whose comments are all older than the watermark.
    threads_to_check = [t for t in threads_to_check if t["comments"]]

    identity = dashboard.get("identity") or {} if isinstance(dashboard, dict) else {}
    mentions_block = dashboard.get("mentions") or {} if isinstance(dashboard, dict) else {}
    output = {
        "timestamp": identity.get("last_active_at", ""),
        "errors": errors,
        "identity": {
            "agent_email": me_email,
            "agent_name": identity.get("agent_name", ""),
            "tier": identity.get("verification_tier", ""),
            "role": groups[0].get("role", "") if groups else "",
            "post_count": identity.get("post_count", 0),
        },
        "unread": {
            "total_unread_posts": dashboard.get("total_unread_posts", 0),
            "unread_posts": dashboard.get("unread", []),
            "replies_to_me": dashboard.get("replies_to_me", []),
            "comments_on_my_posts": dashboard.get("comments_on_my_posts", []),
            "close_elections": dashboard.get("close_elections", []),
            "mentions": {
                "unread_count": mentions_block.get("unread_mentions_count", 0),
                "recent": mentions_block.get("recent_mentions", []),
            },
        },
        "channels": channel_posts,
        "threads_with_comments": threads_to_check,
        "watermark": {
            "path": str(WATERMARK_PATH),
            "loaded_channels": watermark.get("channels", {}),
            "new_channels": newest_seen.get("channels", {}),
            "note": "per-channel last-reviewed timestamps; non-own comments older than these are not re-surfaced; advances only after a fully-successful sweep (ack ok, no failed fetch under the channel)",
        },
        "spool": {
            "path": str(SPOOL_PATH),
            "new_records": spooled,
            "covers": dashboard.get("covers", []) if isinstance(dashboard, dict) else [],
            "note": "append-only JSONL of every perishable dashboard item (denylist-driven: all list sections except identity/groups/my_invites/vouch_state/totals/covers), fsynced BEFORE the ack; items with a usable id are written once ever, items without one are appended; spool failure suppresses ack so the same window is re-offered next run; covers lists what the server says the window surveys",
        },
    }

    # Something to act on: any perishable item in the cursor-bounded window
    # (spool-candidate driven — PR #159 review #1: comments_on_my_posts,
    # close_elections and any future list field count now, independent of
    # disk dedupe) — EXCLUDING spool-only kinds whose server listing is not
    # cursor-bounded (rolling recent_mentions; see SPOOL_ONLY_KINDS) — or
    # new mentions by the bounded unread count, or any non-own comment
    # surfaced by the thread walk.
    has_activity = (
        window_has_items
        or (mentions_block.get("unread_mentions_count", 0) or 0) > 0
        or any(t["comments"] for t in threads_to_check)
    )

    print(json.dumps(output, indent=2))
    # The ack follows a write that actually reached stdout: if the consumer
    # closed the pipe, the digest never fully rendered and the window must
    # not be consumed (BrokenPipeError exits 4 via the entry-point wrapper).
    sys.stdout.flush()

    # PR #159 review #4: a failed fetch is a THIRD state — "I could not
    # read" — never a quiet sweep (1) and never a config fault (2).
    # Nothing is acked and no watermark advances; the idempotent GET
    # re-offers the same window next run.
    if errors:
        print(
            f"ERROR: sweep incomplete: {len(errors)} failed fetch(es) named in the "
            "digest errors array; nothing acked, no watermark advanced",
            file=sys.stderr,
        )
        sys.exit(3)

    # Ordering invariant (stoa#105 + Oct 2 near-miss): ack only after the
    # digest is fully built AND the spool write succeeded. If the dashboard
    # fetch itself errored there is nothing to acknowledge; if spooling
    # failed, suppressing the ack re-offers the same window next run.
    if dashboard_ok and spool_ok:
        acked = ack_dashboard_seen(key, dashboard_cutoff)
        if not acked:
            print("WARNING: dashboard digest was not acknowledged; next run will "
                  "see the same window again (idempotent GET, no data loss).",
                  file=sys.stderr)
        else:
            # Advance the last-sweep watermark only after a successful ack,
            # so a failed sweep re-reviews the same window next run.
            save_watermark(newest_seen)

    sys.exit(0 if has_activity else 1)


if __name__ == "__main__":
    # PR #159 review #3: exit 1 must mean ONLY "quiet sweep". Python's
    # default exit status for an uncaught exception is also 1, so a crash
    # used to read as a quiet sweep to any cron wrapper. Anything
    # unexpected now exits 4 — distinct from 1 (quiet), 2 (config) and
    # 3 (incomplete sweep) — with the traceback on stderr. Deliberate
    # sys.exit() paths raise SystemExit, which is a BaseException and
    # passes through this handler untouched.
    try:
        main()
    except BrokenPipeError:
        # stdout closed before the digest fully rendered. Point the fd at
        # /dev/null first so the interpreter's exit-time flush cannot
        # mask the exit code, then report on stderr and exit 4.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            print("ERROR: stdout closed before the digest was written", file=sys.stderr)
        except Exception:
            pass
        sys.exit(4)
    except Exception:
        try:
            traceback.print_exc()
        except Exception:
            pass
        sys.exit(4)
