# tests/bridge_helpers.py
"""Shared infrastructure for bridge tests: stub Slack server + isolated TestCase."""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Make the in-repo package importable without installation (mirrors bin/channels).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class _StubSlackHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(raw.decode("utf-8")) if raw else None,
            }
        )
        mode = self.server.mode
        if mode == "ok":
            self._json(200, {"ok": True, "ts": "1700000000.000100"})
        elif mode == "rate_limit":
            body = b'{"ok":false,"error":"ratelimited"}'
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:  # "error"
            self._json(200, {"ok": False, "error": "channel_not_found"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # silence per-request logging


@contextlib.contextmanager
def stub_slack(mode="ok"):
    """Run a stub Slack API on localhost. Yields (server, base_url).

    mode: "ok" -> {"ok": true}; "error" -> {"ok": false}; "rate_limit" -> HTTP 429.
    server.requests is a list of received requests.
    """
    server = HTTPServer(("127.0.0.1", 0), _StubSlackHandler)
    server.requests = []
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, base
    finally:
        server.shutdown()
        thread.join(timeout=2)


class BridgeTestCase(unittest.TestCase):
    """Isolates $HOME, disables keychain and worker-spawn for hermetic tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ac-bridge-test-")
        self._saved_env = dict(os.environ)
        os.environ["HOME"] = self.tmp
        os.environ["CHANNELS_BRIDGE_NO_KEYCHAIN"] = "1"
        os.environ["CHANNELS_BRIDGE_NO_SPAWN"] = "1"
        for key in (
            "SLACK_BOT_TOKEN",
            "SLACK_API_BASE",
            "CODEX_THREAD_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CHANNELS_BRIDGE_MAX_ATTEMPTS",
            "CHANNELS_BRIDGE_BACKOFF",
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
