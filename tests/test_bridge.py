# tests/test_bridge.py
import contextlib as _ctx
import io
import json as _json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bridge_helpers import BridgeTestCase, stub_slack  # noqa: E402
from agent_channels import bridge  # noqa: E402
from agent_channels import main as channels_main  # noqa: E402


class ConfigTests(BridgeTestCase):
    def test_add_get_remove_roundtrip(self):
        self.assertIsNone(bridge.get_bridge("help"))
        bridge.add_bridge("help", "C0123", label="Help channel")
        got = bridge.get_bridge("help")
        self.assertIsNotNone(got)
        assert got is not None  # narrow for type-checkers
        self.assertEqual(got["slack_channel"], "C0123")
        self.assertEqual(got["label"], "Help channel")
        self.assertTrue(bridge.remove_bridge("help"))
        self.assertIsNone(bridge.get_bridge("help"))
        self.assertFalse(bridge.remove_bridge("help"))

    def test_load_bridges_missing_returns_empty(self):
        self.assertEqual(bridge.load_bridges(), {})

    def test_load_bridges_corrupt_returns_empty(self):
        from agent_channels import ensure_dirs

        ensure_dirs()
        bridge.bridges_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(bridge.load_bridges(), {})

    def test_concurrent_add_bridge_no_lost_update(self):
        # add_bridge serializes its read-modify-write under a lock, so parallel
        # adds must not clobber each other (all 20 mappings survive).
        import threading

        errors = []

        def add(i):
            try:
                bridge.add_bridge(f"chan{i}", f"C{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(bridge.load_bridges()), 20)


class RenderTests(BridgeTestCase):
    def test_render_text_format(self):
        record = {"seq": 7, "from": "auth-rewrite", "body": "stuck on JWT refresh"}
        text = bridge.render_text("help", record)
        self.assertEqual(text, "`auth-rewrite` in #help (#7)\nstuck on JWT refresh")

    def test_render_text_missing_fields(self):
        text = bridge.render_text("help", {})
        self.assertEqual(text, "`?` in #help (#?)\n")


class SlackPostTests(BridgeTestCase):
    def test_post_ok(self):
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            ok, retry_after = bridge.slack_post("xoxb-tok", "C0123", "hello")
        self.assertTrue(ok)
        self.assertIsNone(retry_after)
        self.assertEqual(len(server.requests), 1)
        req = server.requests[0]
        self.assertEqual(req["path"], "/chat.postMessage")
        self.assertEqual(req["auth"], "Bearer xoxb-tok")
        self.assertEqual(req["body"], {"channel": "C0123", "text": "hello"})

    def test_post_error_ok_false(self):
        with stub_slack("error") as (_server, base):
            os.environ["SLACK_API_BASE"] = base
            ok, retry_after = bridge.slack_post("xoxb-tok", "C0123", "hello")
        self.assertFalse(ok)
        self.assertIsNone(retry_after)

    def test_post_rate_limited_returns_retry_after(self):
        with stub_slack("rate_limit") as (_server, base):
            os.environ["SLACK_API_BASE"] = base
            ok, retry_after = bridge.slack_post("xoxb-tok", "C0123", "hello")
        self.assertFalse(ok)
        self.assertEqual(retry_after, 1.0)

    def test_post_connection_failure(self):
        # Nothing listening on this port -> connection refused, no exception out.
        os.environ["SLACK_API_BASE"] = "http://127.0.0.1:1"
        ok, retry_after = bridge.slack_post("xoxb-tok", "C0123", "hello")
        self.assertFalse(ok)
        self.assertIsNone(retry_after)


class TokenTests(BridgeTestCase):
    def test_env_token_wins(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-env"
        self.assertEqual(bridge.resolve_token(), ("xoxb-env", "env"))

    def test_missing_token_returns_none(self):
        # NO_KEYCHAIN is set by BridgeTestCase, so no keychain lookup happens.
        self.assertIsNone(bridge.resolve_token())

    def test_keychain_disabled_get_returns_none(self):
        self.assertIsNone(bridge.keychain_get())
        self.assertFalse(bridge.keychain_available())


class EnqueueTests(BridgeTestCase):
    def test_enqueue_writes_one_spool_file(self):
        record = {"seq": 7, "from": "auth-rewrite", "body": "stuck",
                  "ts": "2026-06-05T00:00:00Z"}
        path = bridge.enqueue("help", "C0123", record)
        self.assertTrue(path.exists())
        self.assertTrue(path.name.startswith("help.7."))
        self.assertTrue(path.name.endswith(".json"))
        payload = _json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["slack_channel"], "C0123")
        self.assertEqual(payload["channel"], "help")
        self.assertEqual(payload["seq"], 7)
        self.assertEqual(payload["text"], "`auth-rewrite` in #help (#7)\nstuck")
        self.assertIn("enqueued_ts", payload)
        # exactly one *.json job in the spool
        jobs = list(bridge.outbox_dir().glob("*.json"))
        self.assertEqual(len(jobs), 1)


class FlushTests(BridgeTestCase):
    def _enqueue_one(self):
        record = {"seq": 1, "from": "a", "body": "hi", "ts": "2026-06-05T00:00:00Z"}
        return bridge.enqueue("help", "C0123", record)

    def test_flush_delivers_and_unlinks(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        path = self._enqueue_one()
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            rc = bridge.flush(quiet=True)
        self.assertEqual(rc, 0)
        self.assertFalse(path.exists())
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(list(bridge.outbox_dir().glob("*.json")), [])

    def test_flush_error_retains_and_logs(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        os.environ["CHANNELS_BRIDGE_MAX_ATTEMPTS"] = "1"
        path = self._enqueue_one()
        with stub_slack("error") as (_server, base):
            os.environ["SLACK_API_BASE"] = base
            rc = bridge.flush(quiet=True)
        self.assertEqual(rc, 1)
        self.assertTrue(path.exists())  # retained for retry
        self.assertFalse(path.with_suffix(".json.sending").exists())
        log = bridge.worker_log_path().read_text(encoding="utf-8")
        self.assertIn("help.1.", log)

    def test_flush_rate_limited_retains_and_logs(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        os.environ["CHANNELS_BRIDGE_MAX_ATTEMPTS"] = "1"
        path = self._enqueue_one()
        with stub_slack("rate_limit") as (_server, base):
            os.environ["SLACK_API_BASE"] = base
            rc = bridge.flush(quiet=True)
        self.assertEqual(rc, 1)
        self.assertTrue(path.exists())

    def test_flush_no_token_leaves_queued(self):
        path = self._enqueue_one()  # no SLACK_BOT_TOKEN, keychain disabled
        rc = bridge.flush(quiet=True)
        self.assertEqual(rc, 1)
        self.assertTrue(path.exists())
        log = bridge.worker_log_path().read_text(encoding="utf-8")
        self.assertIn("no Slack token", log)

    def test_flush_empty_is_noop(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        self.assertEqual(bridge.flush(quiet=True), 0)

    def test_claim_resets_mtime_to_now(self):
        # A long-queued job, once claimed, must have a fresh (claim-time) mtime
        # so a concurrent worker's _reclaim_stale won't steal it mid-delivery.
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        path = self._enqueue_one()
        old = time.time() - 9999
        os.utime(path, (old, old))
        seen = {}
        real = bridge.slack_post

        def spy(token, ch, text):
            sending = list(bridge.outbox_dir().glob("*.sending"))
            seen["mtime"] = sending[0].stat().st_mtime if sending else None
            return (True, None)

        bridge.slack_post = spy
        try:
            bridge.flush(quiet=True)
        finally:
            bridge.slack_post = real
        self.assertIsNotNone(seen["mtime"])
        self.assertGreater(seen["mtime"], time.time() - 60)

    def test_concurrent_claim_delivers_once(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        for seq in range(1, 6):
            bridge.enqueue(
                "help", "C0123",
                {"seq": seq, "from": "a", "body": str(seq), "ts": "t"},
            )
        import threading

        # Two workers share one stub server; every job must be delivered
        # exactly once across both (claim-by-rename prevents double-delivery).
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            t1 = threading.Thread(target=lambda: bridge.flush(quiet=True))
            t2 = threading.Thread(target=lambda: bridge.flush(quiet=True))
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(len(server.requests), 5)
        self.assertEqual(list(bridge.outbox_dir().glob("*.json")), [])

    def test_flush_malformed_payload_dropped_without_crash(self):
        # A valid-JSON-but-wrong-shape spool file must be dropped, not crash the
        # worker (which would strand it and re-crash every future flush).
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        good = self._enqueue_one()
        bad = bridge.outbox_dir() / "help.999.1.json"
        bad.write_text('{"missing": "required keys"}', encoding="utf-8")
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            rc = bridge.flush(quiet=True)
        self.assertEqual(rc, 0)
        self.assertFalse(bad.exists())          # malformed -> dropped
        self.assertFalse(good.exists())         # valid -> delivered + unlinked
        self.assertEqual(len(server.requests), 1)
        log = bridge.worker_log_path().read_text(encoding="utf-8")
        self.assertIn("malformed", log)

    def test_flush_drains_jobs_enqueued_mid_drain(self):
        # The singleton worker must keep draining jobs that appear while it is
        # mid-flush, not leave them for the next post.
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        self._enqueue_one()  # seq 1
        real = bridge.slack_post
        state = {"added": False, "calls": 0}

        def spy(token, channel, text):
            state["calls"] += 1
            if not state["added"]:
                state["added"] = True
                bridge.enqueue(
                    "help", "C0123",
                    {"seq": 2, "from": "a", "body": "two", "ts": "t"},
                )
            return (True, None)

        bridge.slack_post = spy
        try:
            rc = bridge.flush(quiet=True)
        finally:
            bridge.slack_post = real
        self.assertEqual(rc, 0)
        self.assertEqual(state["calls"], 2)  # both the original and the new job
        self.assertEqual(list(bridge.outbox_dir().glob("*.json")), [])

    def test_flush_singleton_skips_when_lock_held(self):
        # While another worker holds the flush lock, flush() must no-op (return
        # 0) and leave the spool untouched rather than spawn a parallel drain.
        import fcntl as _fcntl

        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        path = self._enqueue_one()
        bridge._ensure_outbox()
        held = os.open(str(bridge._flush_lock_path()), os.O_RDWR | os.O_CREAT, 0o644)
        _fcntl.flock(held, _fcntl.LOCK_EX)
        try:
            with stub_slack("ok") as (server, base):
                os.environ["SLACK_API_BASE"] = base
                rc = bridge.flush(quiet=True)
            self.assertEqual(rc, 0)
            self.assertTrue(path.exists())      # not drained
            self.assertEqual(len(server.requests), 0)
        finally:
            _fcntl.flock(held, _fcntl.LOCK_UN)
            os.close(held)

    def test_outbox_stats_reports_depth_and_last_error(self):
        s0 = bridge.outbox_stats()
        self.assertEqual(s0["pending"], 0)
        self.assertEqual(s0["in_flight"], 0)
        self.assertIsNone(s0["last_error"])
        bridge.enqueue("help", "C0123", {"seq": 1, "from": "a", "body": "x", "ts": "t"})
        bridge.enqueue("help", "C0123", {"seq": 2, "from": "a", "body": "y", "ts": "t"})
        bridge._log_error("boom happened")
        s1 = bridge.outbox_stats()
        self.assertEqual(s1["pending"], 2)
        self.assertIn("boom happened", s1["last_error"])

    def test_flush_follow_drains_then_stops(self):
        import threading

        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        self._enqueue_one()
        stop = threading.Event()
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            t = threading.Thread(
                target=lambda: bridge.flush_follow(
                    interval=0.1, quiet=True, stop_event=stop
                )
            )
            t.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not server.requests:
                time.sleep(0.05)
            stop.set()
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(list(bridge.outbox_dir().glob("*.json")), [])


class SpawnTests(BridgeTestCase):
    def test_no_spawn_env_is_noop(self):
        # BridgeTestCase sets CHANNELS_BRIDGE_NO_SPAWN=1; must not raise/spawn.
        self.assertIsNone(bridge.spawn_worker())

    def test_spawn_delivers_end_to_end(self):
        del os.environ["CHANNELS_BRIDGE_NO_SPAWN"]
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        path = bridge.enqueue(
            "help", "C0123",
            {"seq": 1, "from": "a", "body": "hi", "ts": "t"},
        )
        import time as _t

        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            bridge.spawn_worker()
            deadline = _t.monotonic() + 10
            while _t.monotonic() < deadline and path.exists():
                _t.sleep(0.1)
        self.assertFalse(path.exists())
        self.assertEqual(len(server.requests), 1)


class PostHookTests(BridgeTestCase):
    def _post(self, channel, body):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-1"
        return channels_main(["post", "--from", "tester", channel, body])

    def test_bridged_post_enqueues_one_job(self):
        bridge.add_bridge("help", "C0123")
        rc = self._post("help", "hello world")
        self.assertEqual(rc, 0)
        jobs = list(bridge.outbox_dir().glob("*.json"))
        self.assertEqual(len(jobs), 1)
        payload = _json.loads(jobs[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["slack_channel"], "C0123")
        self.assertEqual(payload["text"], "`tester` in #help (#1)\nhello world")

    def test_unbridged_post_enqueues_nothing(self):
        rc = self._post("random", "nothing to mirror")
        self.assertEqual(rc, 0)
        self.assertFalse(bridge.outbox_dir().exists()
                         and list(bridge.outbox_dir().glob("*.json")))

    def test_bridge_failure_does_not_break_post(self):
        bridge.add_bridge("help", "C0123")
        real = bridge.enqueue

        def boom(*_a, **_k):
            raise OSError("disk full")

        bridge.enqueue = boom
        try:
            rc = self._post("help", "still durable")
        finally:
            bridge.enqueue = real
        # Post still succeeds despite the bridge failure...
        self.assertEqual(rc, 0)
        # ...and the message was durably written to the channel.
        from agent_channels import channel_path, iter_messages

        msgs = list(iter_messages(channel_path("help")))
        self.assertEqual(msgs[-1]["body"], "still durable")
        # ...and the failure was logged, not silent.
        log = bridge.worker_log_path().read_text(encoding="utf-8")
        self.assertIn("bridge enqueue failed", log)


class BridgeCliTests(BridgeTestCase):
    def _run(self, argv):
        out = io.StringIO()
        with _ctx.redirect_stdout(out):
            rc = channels_main(argv)
        return rc, out.getvalue()

    def test_add_list_remove(self):
        rc, _ = self._run(["bridge", "add", "help", "C0123", "--label", "Help"])
        self.assertEqual(rc, 0)
        entry = bridge.get_bridge("help")
        assert entry is not None
        self.assertEqual(entry["slack_channel"], "C0123")

        rc, out = self._run(["bridge", "list"])
        self.assertEqual(rc, 0)
        self.assertIn("help", out)
        self.assertIn("C0123", out)
        self.assertIn("MISSING", out)  # no token in test env

        rc, _ = self._run(["bridge", "remove", "help"])
        self.assertEqual(rc, 0)
        self.assertIsNone(bridge.get_bridge("help"))

    def test_list_shows_env_token_status_without_value(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-secret"
        self._run(["bridge", "add", "help", "C0123"])
        _rc, out = self._run(["bridge", "list"])
        self.assertIn("set (env)", out)
        self.assertNotIn("xoxb-secret", out)

    def test_flush_subcommand_delivers(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        bridge.enqueue("help", "C0123", {"seq": 1, "from": "a", "body": "x", "ts": "t"})
        with stub_slack("ok") as (server, base):
            os.environ["SLACK_API_BASE"] = base
            rc, _ = self._run(["bridge", "flush"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(server.requests), 1)

    def test_add_canonicalizes_channel_name(self):
        self._run(["bridge", "add", "#Help", "C0123"])
        self.assertIsNotNone(bridge.get_bridge("help"))

    def test_status_reports_queue_depth(self):
        os.environ["SLACK_BOT_TOKEN"] = "xoxb"
        bridge.enqueue("help", "C0123", {"seq": 1, "from": "a", "body": "x", "ts": "t"})
        rc, out = self._run(["bridge", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("set (env)", out)
        self.assertIn("queued: 1", out)


if __name__ == "__main__":
    unittest.main()
