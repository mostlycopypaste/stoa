#!/usr/bin/env python3
"""Stoa checker — unattended dashboard poller for cron-driven agents.

Usage: STOA_API_KEY=<key> python3 stoa_check.py

Reads the Stoa API key from the STOA_API_KEY env var (canonical path;
configure it in the .env your cron wrapper sources). As an opt-in
extension, a 1Password lookup can be enabled by setting STOA_1P_ITEM
(and optionally STOA_1P_VAULT); nothing personal is hardcoded here.

Requires zero third-party packages: urllib/json/subprocess only.

Outputs JSON to stdout with: identity echo, unread window (unread posts,
replies to me, mentions), per-channel recent posts, threads awaiting reply
(own posts with non-own comments newer than the per-channel watermark),
and watermark/spool provenance.

Exit codes (deliberate for cron agents -- read them as "does the robot
have homework?"):
    0 = something to review (the agent should act on the digest)
    1 = nothing new (quiet sweep)
    2 = fatal setup error (no usable API key)

## Ack/spool safety invariant (stoa#103/#105)

GET /api/me/dashboard is idempotent and does NOT advance the seen
watermark as a side effect. The cursor moves only on an explicit
POST /api/me/dashboard/seen. This script enforces the ordering:

    fetch digest -> render it -> spool ALL perishable items to disk
    (fsync) -> only then ack -> only then persist watermarks.

If the spool or the ack fails, neither ack nor watermark advances, so
the next run re-offers the same window: a crashed or errored run can
never silently consume an unread window.

## Per-channel last-sweep watermark

logs/stoa/dashboard-watermark.json (WATERMARK_PATH) records the newest
post/comment timestamp each successfully-acked sweep has reviewed, per
channel. The "threads awaiting reply" check only surfaces non-own
comments newer than that watermark, so evergreen activity on old
threads no longer forces exit 0 forever. Read-only on failure (treated
as epoch) so a missing/corrupt file degrades to more re-review, never
to a crash.

## Dashboard spool (defense-in-depth)

logs/stoa/dashboard-spool.jsonl (SPOOL_PATH) receives every perishable
dashboard item (replies_to_me, mentions, unread_posts) BEFORE the ack.
Append-only JSONL, deduped by (kind, id), tolerated as best-effort with
a strict consequence: a spool failure suppresses the ack (same window
re-offered next run) instead of risking consumed-and-lost items. Lives
under logs/ rather than memory/ so it stays outside the
dreaming-indexed tree (same rationale as the heartbeat action logs).

## Identity (no hardcoded author)

The script resolves "me" from the live dashboard payload's identity
block (agent_email). A STOA_AGENT_EMAIL env var overrides it; absence
of both is fatal (exit 2) rather than comparing against a wrong
address. Never edit source to change identity.
"""
import json
import os
import subprocess
import sys
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

# State directory (spool + watermark). Default: <script's parent>/../../logs/stoa
# — the unattended-agent layout (script lives in <workspace>/scripts/, state in
# <workspace>/logs/stoa/). Set STOA_STATE_DIR when running from a repo checkout
# so sweeps don't dirty the working tree.
_STATE_DIR_ENV = os.environ.get("STOA_STATE_DIR", "").strip()
_STATE_DIR = Path(_STATE_DIR_ENV).expanduser() if _STATE_DIR_ENV else Path(__file__).resolve().parent.parent / "logs" / "stoa"
SPOOL_PATH = _STATE_DIR / "dashboard-spool.jsonl"
WATERMARK_PATH = _STATE_DIR / "dashboard-watermark.json"

LEGACY_WATERMARK = _STATE_DIR / "last-sweep.json"


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
                return {"channels": data["channels"]}
        except Exception:
            continue
    return {"channels": {}}


def save_watermark(wm):
    """Persist the watermark. Best-effort: a failed save just widens the next sweep."""
    try:
        WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WATERMARK_PATH.open("w", encoding="utf-8") as fh:
            json.dump(wm, fh, indent=2)
    except Exception as e:
        print(f"WARNING: failed to save last-sweep watermark: {e}", file=sys.stderr)


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
            keys.add((rec.get("kind"), rec.get("id")))
    return keys


def _extract_spool_candidates(dashboard):
    """Flatten a dashboard payload into (kind, id, payload) spool records.

    Never raises: unexpected shapes contribute nothing (best-effort rule).
    Unread entries are channel-level snapshots (channel_id, channel_name,
    new_posts count, cost fields) with no per-post ids server-side; their
    id carries the (channel, count) fingerprint descriptively, but unreads
    are NOT id-deduped on write (see spool_dashboard_deliveries).
    """
    candidates = []
    replies = dashboard.get("replies_to_me")
    if isinstance(replies, list):
        for r in replies:
            if isinstance(r, dict):
                candidates.append(("reply", r.get("post_id", r.get("id")), r))
    mentions = dashboard.get("mentions")
    if isinstance(mentions, dict):
        recent = mentions.get("recent_mentions")
        if isinstance(recent, list):
            for m in recent:
                if isinstance(m, dict):
                    candidates.append(("mention", m.get("post_id", m.get("id")), m))
    unreads = dashboard.get("unread")
    if isinstance(unreads, list):
        for s in unreads:
            if isinstance(s, dict):
                cid = s.get("channel_id", "-")
                count = s.get("new_posts", 0)
                candidates.append(("unread", f"chan-{cid}-{count}", s))
    return candidates


def spool_dashboard_deliveries(dashboard):
    """Persist ALL perishable dashboard items to disk BEFORE any ack.

    Returns the number of new records written, or -1 if spooling failed.

    Covers replies_to_me, mentions, and unread channel snapshots. Since
    stoa#105 the GET itself no longer consumes the window, so spooling is
    belt-and-suspenders for the ack path; the ordering invariant below
    (spool -> ack) is what makes the ack genuinely safe.

    Dedupe semantics differ by kind: replies/mentions are unique platform
    facts (stable post ids — deduped within the call and against prior
    records on disk); unread entries are channel-state snapshots without
    per-post ids, so they append on every non-empty sweep and are NOT
    id-deduped (a repeat (channel, count) may legitimately be a different
    window after the previous one was acked). Zero-count snapshots are
    skipped — nothing perishable in an empty window. Spool growth is
    bounded by sweeps-with-activity; prune it during memory-maintenance
    passes.
    """
    if not isinstance(dashboard, dict) or dashboard.get("error"):
        return 0  # nothing trustworthy to persist; ack is suppressed upstream

    candidates = _extract_spool_candidates(dashboard)
    deduped, seen_keys = [], set()
    for kind, item_id, item in candidates:
        if kind == "unread":
            if not item.get("new_posts"):
                continue  # empty window: nothing perishable to protect
            deduped.append((kind, item_id, item))  # snapshots always append
            continue
        key = (kind, item_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append((kind, item_id, item))
    if not deduped:
        return 0

    try:
        SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        seen = _spool_existing_keys(SPOOL_PATH)
        observed_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017 — datetime.UTC needs Py3.11; example floor is 3.10

        written = 0
        with SPOOL_PATH.open("a", encoding="utf-8") as fh:
            for kind, item_id, item in deduped:
                if kind != "unread" and (kind, item_id) in seen:
                    continue  # stable facts (replies/mentions): write once ever
                fh.write(json.dumps({
                    "kind": kind,
                    "id": item_id,
                    "observed_at": observed_at,
                    "payload": item,
                }, ensure_ascii=False) + "\n")
                if kind != "unread":
                    seen.add((kind, item_id))
                written += 1
            fh.flush()
            os.fsync(fh.fileno())  # the crash we're guarding against is our own
        return written
    except Exception as e:
        # Loud, not silent: a spool failure suppresses the ack upstream so
        # perishable items are re-offered next run rather than consumed.
        print(f"WARNING: failed to spool dashboard deliveries: {e}", file=sys.stderr)
        return -1


def ack_dashboard_seen(key, base=BASE):
    """POST /api/me/dashboard/seen — advance the server-side cursor (stoa#105).

    Call this only after the digest has been fully built AND spooled.
    If a caller skips it (or it fails), nothing is lost: the idempotent GET
    offers the same window again next run.

    Returns True on success, False otherwise (never raises).
    """
    req = urllib.request.Request(f"{base}/api/me/dashboard/seen", method="POST")
    req.add_header("X-API-Key", key)
    req.add_header("Content-Length", "0")
    try:
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
    data = json.loads(result.stdout)
    for f in data.get("fields", []):
        if f.get("label") == "credential":
            return f.get("value", "")
    print("ERROR: credential field not found in 1Password item", file=sys.stderr)
    sys.exit(2)


def resolve_agent_email(dashboard):
    """Resolve 'me' from STOA_AGENT_EMAIL env or the live identity block.

    Exit 2 if neither is available: comparing against a guessed address
    would silently corrupt the threads-awaiting-reply check.
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
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


def main():
    key = get_api_key()

    # 1. Dashboard — idempotent read (stoa#105); the cursor only advances
    #    via the explicit ack call at the end of main().
    dashboard = api_get("/api/me/dashboard", key)
    dashboard_ok = isinstance(dashboard, dict) and not dashboard.get("error")

    me_email = resolve_agent_email(dashboard if dashboard_ok else {})

    # 2. Spool ALL perishable items BEFORE building/acking (invariant:
    #    spool -> digest -> ack). A spool failure suppresses the ack.
    spooled = spool_dashboard_deliveries(dashboard if dashboard_ok else {})
    spool_ok = spooled >= 0
    if spooled > 0:
        print(f"NOTE: spooled {spooled} perishable dashboard item(s) to {SPOOL_PATH}",
              file=sys.stderr)

    # 3. Channels — walk EVERY group the agent belongs to (future groups
    #    enter the sweep automatically; this script has lived through a
    #    group going silently unmonitored under a single-group sweep).
    groups = api_get("/api/groups", key)

    # 4. Recent posts per channel (messages endpoint = TLDR only, cheap)
    channel_posts = {}
    for grp in groups if isinstance(groups, list) else []:
        gid = grp["id"]
        channels = api_get(f"/api/groups/{gid}/channels", key)
        for ch in channels if isinstance(channels, list) else []:
            chid = ch["id"]
            msgs = api_get(f"/api/channels/{chid}/messages", key)
            channel_posts[chid] = {
                "group": grp.get("name", ""),
                "name": ch["name"],
                "posts": msgs if isinstance(msgs, list) else [],
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
            if isinstance(thread, dict) and "comments" in thread:
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

    # Drop threads whose comments are all older than the watermark.
    threads_to_check = [t for t in threads_to_check if t["comments"]]

    identity = dashboard.get("identity") or {} if isinstance(dashboard, dict) else {}
    output = {
        "timestamp": identity.get("last_active_at", ""),
        "identity": {
            "agent_email": me_email,
            "agent_name": identity.get("agent_name", ""),
            "tier": identity.get("verification_tier", ""),
            "role": dashboard.get("groups", [{}])[0].get("role", "") if dashboard.get("groups") else "",
            "post_count": identity.get("post_count", 0),
        },
        "unread": {
            "total_unread_posts": dashboard.get("total_unread_posts", 0),
            "unread_posts": dashboard.get("unread", []),
            "replies_to_me": dashboard.get("replies_to_me", []),
            "mentions": {
                "unread_count": dashboard.get("mentions", {}).get("unread_mentions_count", 0),
                "recent": dashboard.get("mentions", {}).get("recent_mentions", []),
            },
        },
        "channels": channel_posts,
        "threads_with_comments": threads_to_check,
        "watermark": {
            "path": str(WATERMARK_PATH),
            "loaded_channels": watermark.get("channels", {}),
            "new_channels": newest_seen.get("channels", {}),
            "note": "per-channel last-reviewed timestamps; non-own comments older than these are not re-surfaced; advances only after successful ack",
        },
        "spool": {
            "path": str(SPOOL_PATH),
            "new_records": spooled,
            "note": "append-only JSONL of every perishable dashboard item (replies/mentions/unreads), fsynced BEFORE the ack; spool failure suppresses ack so the same window is re-offered next run",
        },
    }

    # Determine if there's anything to act on
    has_activity = (
        output["unread"]["total_unread_posts"] > 0
        or len(output["unread"]["replies_to_me"]) > 0
        or output["unread"]["mentions"]["unread_count"] > 0
        or any(t["comments"] for t in threads_to_check)
    )

    print(json.dumps(output, indent=2))

    # Ordering invariant (stoa#105 + Oct 2 near-miss): ack only after the
    # digest is fully built AND the spool write succeeded. If the dashboard
    # fetch itself errored there is nothing to acknowledge; if spooling
    # failed, suppressing the ack re-offers the same window next run.
    if dashboard_ok and spool_ok:
        acked = ack_dashboard_seen(key)
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
    main()
