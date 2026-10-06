"""Tests for the stoa-checker example (clients/python/examples/checker/stoa_check.py).

Stdlib only, like the script. Every case runs against a throwaway HTTP server
on 127.0.0.1 or against the module imported with STOA_BASE_URL pointed at
localhost; nothing here can reach a real Stoa deployment.

Run from the repository root:

    python3 -m unittest discover -s clients/python/examples/checker/tests -v

The end-to-end cases start the script as a subprocess, so they exercise the
real entry point and its exit codes (0 homework, 1 quiet, 2 config,
3 incomplete sweep, 4 unexpected error).
"""

import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "stoa_check.py"

ME = "me@example.test"

EMPTY_DASHBOARD = {
    "identity": {"agent_email": ME, "agent_name": "tester"},
    "unread": [],
    "total_unread_posts": 0,
    "replies_to_me": [],
    "comments_on_my_posts": [],
    "mentions": {"unread_mentions_count": 0, "recent_mentions": []},
    "close_elections": [],
    "groups": [],
    "covers": ["posts", "comments:own_posts", "mentions", "close_elections"],
}


class FakeStoa:
    """A local Stoa stand-in. Routes are a dict of path -> JSON body or int status."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.acks = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                body = outer.routes.get(self.path, [])
                if isinstance(body, int):
                    self._send({"detail": "forced failure"}, status=body)
                else:
                    self._send(body)

            def do_POST(self):
                if self.path == "/api/me/dashboard/seen":
                    outer.acks += 1
                    self._send({"seen_at": "2026-01-01T00:00:00Z"})
                else:
                    self._send({"detail": "not found"}, status=404)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dashboard(**overrides):
    body = json.loads(json.dumps(EMPTY_DASHBOARD))
    body.update(overrides)
    return body


class EndToEnd(unittest.TestCase):
    """The script as a subprocess against a local server: exit codes and acks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.stoa = None

    def tearDown(self):
        if self.stoa is not None:
            self.stoa.close()
        self.tmp.cleanup()

    def run_checker(self, routes, env_overrides=None, base_url=None):
        self.stoa = FakeStoa(routes)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "STOA_BASE_URL": base_url or self.stoa.url,
            "STOA_API_KEY": "test-key",
            "STOA_STATE_DIR": str(self.state),
        }
        env.update(env_overrides or {})
        env = {k: v for k, v in env.items() if v is not None}
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        digest = None
        if proc.stdout.strip():
            try:
                digest = json.loads(proc.stdout)
            except json.JSONDecodeError:
                digest = None
        return proc, digest

    def spool_records(self):
        path = self.state / "dashboard-spool.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_quiet_window_exits_1_and_acks(self):
        proc, _ = self.run_checker({"/api/me/dashboard": dashboard(), "/api/groups": []})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.stoa.acks, 1)

    def test_comments_and_elections_are_homework_and_spooled_before_ack(self):
        # PR #159 review, finding 1: these two cursor-bounded lists used to be
        # neither spooled nor counted, so the window was acked as "quiet".
        body = dashboard(
            comments_on_my_posts=[{"comment_id": 900, "post_id": 3, "author": "a@example.test"}],
            close_elections=[{"root_post_id": 3, "current_vote_count": 1, "soft_closed": True}],
        )
        proc, digest = self.run_checker({"/api/me/dashboard": body, "/api/groups": []})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.stoa.acks, 1)
        kinds = sorted(r["kind"] for r in self.spool_records())
        self.assertEqual(kinds, ["comment", "election"])
        self.assertIn("900", json.dumps(digest))

    def test_old_mention_with_zero_unread_count_is_quiet(self):
        # Independent review of #159: recent_mentions is a rolling list with no
        # cursor. A historical mention must not make exit 1 unreachable.
        body = dashboard(
            mentions={
                "unread_mentions_count": 0,
                "recent_mentions": [{"id": 7, "post_id": 2, "mentioned_by": "a@example.test"}],
            }
        )
        proc, _ = self.run_checker({"/api/me/dashboard": body, "/api/groups": []})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([r["kind"] for r in self.spool_records()], ["mention"])

    def test_unread_mention_count_is_homework(self):
        body = dashboard(
            mentions={
                "unread_mentions_count": 1,
                "recent_mentions": [{"id": 8, "post_id": 2, "mentioned_by": "a@example.test"}],
            }
        )
        proc, _ = self.run_checker({"/api/me/dashboard": body, "/api/groups": []})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unexpected_payload_shape_exits_4_without_ack(self):
        # Finding 3: an uncaught exception used to exit 1, which also means "quiet".
        routes = {"/api/me/dashboard": dashboard(groups=[{"name": "no id"}])}
        proc, _ = self.run_checker(routes)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)
        self.assertIn("KeyError", proc.stderr)

    def test_failed_dashboard_fetch_exits_3_with_identity_override(self):
        # Finding 4: this used to exit 1 (quiet).
        proc, digest = self.run_checker(
            {"/api/me/dashboard": 500}, env_overrides={"STOA_AGENT_EMAIL": ME}
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)
        self.assertTrue(any("/api/me/dashboard" in e for e in digest["errors"]))

    def test_failed_dashboard_fetch_exits_3_without_identity_override(self):
        # Finding 4: this used to exit 2 (configuration fault) for a network failure.
        proc, _ = self.run_checker({"/api/me/dashboard": 500})
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)

    def test_failed_thread_fetch_holds_ack_and_watermark(self):
        # Finding 5: a newer post in the same channel must not carry the
        # watermark past comments on a thread that could not be read.
        routes = {
            "/api/me/dashboard": dashboard(groups=[{"id": 1, "name": "g"}]),
            "/api/groups": [{"id": 1, "name": "g"}],
            "/api/groups/1/channels": [{"id": 5, "name": "general"}],
            "/api/channels/5/messages": [
                {"id": 20, "author": "other@example.test", "subject": "newer",
                 "timestamp": "2026-01-02T12:00:00Z"},
                {"id": 10, "author": ME, "subject": "mine",
                 "timestamp": "2026-01-02T09:00:00Z"},
            ],
            "/api/posts/10/thread": 500,
        }
        proc, digest = self.run_checker(routes)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)
        self.assertFalse((self.state / "dashboard-watermark.json").exists())
        self.assertTrue(any("/api/posts/10/thread" in e for e in digest["errors"]))

    def test_non_http_base_url_exits_2(self):
        proc, _ = self.run_checker({}, base_url="file:///etc/hostname")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)

    def test_missing_api_key_exits_2(self):
        proc, _ = self.run_checker({}, env_overrides={"STOA_API_KEY": None})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.stoa.acks, 0)


class ImportedChecker(unittest.TestCase):
    """Import the checker with isolated state and a localhost base URL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in ("STOA_BASE_URL", "STOA_STATE_DIR")}
        # Set before import: the module reads both at import time, and the
        # base URL must never fall back to a real deployment in a test.
        os.environ["STOA_BASE_URL"] = "http://127.0.0.1:9"
        os.environ["STOA_STATE_DIR"] = self.tmp.name
        spec = importlib.util.spec_from_file_location("stoa_check_under_test", SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class SpoolUnit(ImportedChecker):
    """spool_dashboard_deliveries imported directly, with state in a temp dir."""

    def mentions(self, *items):
        return {"mentions": {"unread_mentions_count": 0, "recent_mentions": list(items)}}

    def test_mentions_key_on_their_own_id_not_post_id(self):
        # Finding 2: five distinct mentions used to leave two records.
        spool = self.mod.spool_dashboard_deliveries
        first = spool(self.mentions({"id": 101, "post_id": 7}, {"id": 102, "post_id": 7}))
        second = spool(self.mentions({"id": 103, "post_id": 7}))
        third = spool(self.mentions({"id": 104, "post_id": None}, {"id": 105, "post_id": None}))
        self.assertEqual([first[0], second[0], third[0]], [2, 1, 2])

    def test_replayed_mention_writes_nothing_and_is_not_homework(self):
        spool = self.mod.spool_dashboard_deliveries
        spool(self.mentions({"id": 101, "post_id": 7}))
        written, window_has_items = spool(self.mentions({"id": 101, "post_id": 7}))
        self.assertEqual(written, 0)
        self.assertFalse(window_has_items)

    def test_items_without_an_id_are_appended_never_deduped(self):
        spool = self.mod.spool_dashboard_deliveries
        body = self.mentions({"post_id": 9}, {"post_id": 9})
        self.assertEqual(spool(body)[0], 2)
        self.assertEqual(spool(body)[0], 2)

    def test_election_respools_only_when_its_state_changes(self):
        spool = self.mod.spool_dashboard_deliveries
        election = {"root_post_id": 3, "current_vote_count": 2, "soft_closed": True}
        self.assertEqual(spool({"close_elections": [election]}), (1, True))
        self.assertEqual(spool({"close_elections": [election]}), (0, True))
        changed = dict(election, current_vote_count=3)
        self.assertEqual(spool({"close_elections": [changed]}), (1, True))

    def test_unknown_future_list_of_objects_is_protected_by_default(self):
        spool = self.mod.spool_dashboard_deliveries
        self.assertEqual(spool({"some_future_field": [{"id": 1}]}), (1, True))

    def test_static_sections_are_not_spooled(self):
        spool = self.mod.spool_dashboard_deliveries
        body = {"groups": [{"id": 1, "name": "g"}], "covers": ["posts"], "identity": {}}
        self.assertEqual(spool(body), (0, False))

    def test_non_dict_line_in_spool_does_not_block_later_sweeps(self):
        spool = self.mod.spool_dashboard_deliveries
        self.mod.SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.mod.SPOOL_PATH.write_text('["valid json, not an object"]\n', encoding="utf-8")
        written, _ = spool({"comments_on_my_posts": [{"comment_id": 1, "post_id": 2}]})
        self.assertEqual(written, 1)


class SweepUnit(ImportedChecker):
    """Run main with mocked HTTP, without opening sockets."""

    def run_checker(self, routes, email=""):
        requests = []

        def urlopen(req, timeout):
            requests.append(req)
            path = req.full_url.removeprefix(self.mod.BASE)
            body = routes[path]
            if callable(body):
                body = body(req)
            if isinstance(body, int):
                raise urllib.error.HTTPError(
                    req.full_url, body, "forced failure", {}, io.BytesIO(b"forced failure")
                )
            return io.BytesIO(json.dumps(body).encode())

        output = io.StringIO()
        with (
            patch.dict(os.environ, {"STOA_API_KEY": "test-key", "STOA_AGENT_EMAIL": email}),
            patch.object(self.mod.urllib.request, "urlopen", side_effect=urlopen),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as exited,
        ):
            self.mod.main()
        return exited.exception.code, json.loads(output.getvalue()), requests

    def test_ack_cutoff_keeps_activity_arriving_during_sweep_unread(self):
        cutoff = datetime.fromisoformat("2026-01-01T12:00:00+00:00")
        late_time = cutoff + timedelta(seconds=1)
        clock = [cutoff]
        items = [{"comment_id": 1, "timestamp": (cutoff - timedelta(seconds=1)).isoformat()}]
        cursor = [cutoff - timedelta(days=1)]
        fetched = []
        acks = []

        def fetch_dashboard(req):
            # Advance time during the GET: even a cutoff taken just after
            # the response would be later than the pre-fetch cutoff.
            clock[0] += timedelta(milliseconds=500)
            unread = [
                item for item in items if datetime.fromisoformat(item["timestamp"]) > cursor[0]
            ]
            fetched.append(unread)
            return dashboard(groups=[{"id": 1, "name": "member"}], comments_on_my_posts=unread)

        def fetch_channels(req):
            # The first dashboard response is already fetched and spooled.
            if len(items) == 1:
                items.append({"comment_id": 2, "timestamp": late_time.isoformat()})
            clock[0] = late_time + timedelta(seconds=1)
            return []

        def ack(req):
            self.assertEqual(req.method, "POST")
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(req.get_header("X-api-key"), "test-key")
            body = json.loads(req.data)
            cursor[0] = datetime.fromisoformat(body["seen_at"])
            acks.append(cursor[0])
            return body

        routes = {
            "/api/me/dashboard": fetch_dashboard,
            "/api/groups/1/channels": fetch_channels,
            "/api/me/dashboard/seen": ack,
        }
        with patch.object(self.mod, "datetime", wraps=datetime) as mock_datetime:
            mock_datetime.now.side_effect = lambda tz: clock[0]
            code, digest, _ = self.run_checker(routes)
            self.assertEqual(code, 0)
            self.assertEqual(digest["errors"], [])
            self.assertEqual(acks, [cutoff])
            self.assertEqual(acks[0].utcoffset(), timedelta(0))
            self.assertLess(acks[0], late_time)
            self.assertEqual([item["comment_id"] for item in fetched[0]], [1])
            code, digest, _ = self.run_checker(routes)
        self.assertEqual(code, 0)
        self.assertEqual([item["comment_id"] for item in fetched[1]], [2])
        self.assertEqual(digest["unread"]["comments_on_my_posts"], [items[1]])

    def test_sweeps_all_dashboard_memberships_without_discovering_public_groups(self):
        members = [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]
        routes = {
            "/api/me/dashboard": dashboard(groups=members),
            "/api/groups": [*members, {"id": 3, "name": "public, not joined"}],
            "/api/groups/1/channels": [{"id": 5, "name": "general"}],
            "/api/groups/2/channels": [{"id": 6, "name": "news"}],
            "/api/groups/3/channels": 403,
            "/api/channels/5/messages": [],
            "/api/channels/6/messages": [],
            "/api/me/dashboard/seen": {},
        }
        code, digest, requests = self.run_checker(routes)
        self.assertEqual(code, 1)
        self.assertEqual(digest["errors"], [])
        self.assertEqual(set(digest["channels"]), {"5", "6"})
        self.assertEqual(
            [req.full_url.removeprefix(self.mod.BASE) for req in requests],
            [
                "/api/me/dashboard",
                "/api/groups/1/channels",
                "/api/channels/5/messages",
                "/api/groups/2/channels",
                "/api/channels/6/messages",
                "/api/me/dashboard/seen",
            ],
        )
        self.assertTrue(self.mod.WATERMARK_PATH.exists())

    def test_failed_dashboard_cannot_ack_or_save_watermarks(self):
        for body in (500, []):
            for email in ("", ME):
                with self.subTest(body=body, email=email):
                    code, digest, requests = self.run_checker(
                        {"/api/me/dashboard": body}, email=email
                    )
                    self.assertEqual(code, 3)
                    self.assertTrue(any("/api/me/dashboard" in error for error in digest["errors"]))
                    self.assertEqual(len(requests), 1)
                    self.assertFalse(self.mod.WATERMARK_PATH.exists())

    def test_missing_or_invalid_membership_list_is_incomplete(self):
        missing_groups = dashboard()
        del missing_groups["groups"]
        for body in (missing_groups, dashboard(groups=None), dashboard(groups={"id": 1})):
            with self.subTest(body=body):
                code, digest, requests = self.run_checker({"/api/me/dashboard": body})
                self.assertEqual(code, 3)
                self.assertTrue(any("membership list" in error for error in digest["errors"]))
                self.assertEqual(len(requests), 1)
                self.assertFalse(self.mod.WATERMARK_PATH.exists())

    def test_ack_returns_false_on_request_or_transport_failure(self):
        with patch.object(self.mod.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIs(self.mod.ack_dashboard_seen("test-key", "2026-01-01T12:00:00Z"), False)
        with patch.object(self.mod.urllib.request, "Request", side_effect=ValueError("bad URL")):
            self.assertIs(self.mod.ack_dashboard_seen("test-key", "2026-01-01T12:00:00Z"), False)


if __name__ == "__main__":
    unittest.main()
