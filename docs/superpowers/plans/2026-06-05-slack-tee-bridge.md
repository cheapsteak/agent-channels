# Slack tee bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror messages posted to selected `channels` channels into a mapped Slack channel, one-way, without slowing down `post`.

**Architecture:** `post` writes locally (unchanged), and for bridged channels drops a job file into a `outbox/` spool and spawns a fully detached worker. The worker (`channels bridge flush`) claims spool files by atomic rename, delivers them to Slack via `chat.postMessage` over stdlib `urllib`, unlinks on success, and retains + logs on failure. Mappings live in `bridges.json`; the bot token resolves env → keychain → hard fail.

**Tech Stack:** Python 3.9+ stdlib only (`urllib`, `subprocess`, `http.server`, `argparse`, `unittest`). POSIX-only. No third-party imports.

---

## Design reference

Full spec: `docs/superpowers/specs/2026-06-05-slack-tee-bridge-design.md`.

## File structure

- **Create `src/agent_channels/__main__.py`** — enables `python -m agent_channels`, which is how the detached worker is launched.
- **Create `src/agent_channels/bridge.py`** — the whole bridge subsystem: `bridges.json` config, token resolution (env/keychain), `slack_post` HTTP, the outbox spool (enqueue + flush/drain), message rendering, and the detached-worker spawn. Kept separate from `__init__.py` so the existing CLI module stays focused; `bridge.py` imports the data-root helpers from `__init__` lazily (inside functions) to avoid an import cycle.
- **Modify `src/agent_channels/__init__.py`** — `from . import bridge` at top; hook bridging into `cmd_post`; add the `bridge` subcommand group (`add`/`remove`/`list`/`set-token`/`flush`) to the argparse tree and their `cmd_bridge_*` handlers.
- **Create `tests/bridge_helpers.py`** — shared test infrastructure: a stub Slack HTTP server (stdlib `http.server`) and a `BridgeTestCase` base that isolates `$HOME` and disables keychain + worker-spawn.
- **Create `tests/test_bridge.py`** — `unittest` suite, run with `python3 tests/test_bridge.py`.
- **Modify `tests/smoke.sh`** — add a CLI round-trip: `bridge add` → `post` (no-spawn) → `bridge flush` against a stub server → assert delivered.
- **Modify `README.md`** — document the `bridge` subcommand group and token setup.

## Test seams (env vars honored by `bridge.py`)

These exist so tests are hermetic and deterministic; they are also operationally useful:

- `SLACK_API_BASE` — override Slack API base URL (default `https://slack.com/api`).
- `CHANNELS_BRIDGE_NO_KEYCHAIN=1` — skip all keychain access (env-token-only).
- `CHANNELS_BRIDGE_NO_SPAWN=1` — `post` enqueues but does not spawn the worker.
- `CHANNELS_BRIDGE_MAX_ATTEMPTS` — per-flush delivery attempts (default `3`).
- `CHANNELS_BRIDGE_BACKOFF` — base backoff seconds between attempts (default `0.5`).

---

## Task 1: `python -m agent_channels` entry point

**Files:**
- Create: `src/agent_channels/__main__.py`

- [ ] **Step 1: Write the failing test**

Run this one-off check (no test file yet — this is a manual gate):

Run: `python3 -m agent_channels list` from the repo root with `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3 -m agent_channels list
```

Expected now: FAIL — `No module named agent_channels.__main__; 'agent_channels' is a package and cannot be directly executed`.

- [ ] **Step 2: Create the module**

```python
# src/agent_channels/__main__.py
"""Enable `python -m agent_channels`, used to launch the detached Slack worker."""
import sys

from agent_channels import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run to verify it works**

Run: `PYTHONPATH=src python3 -m agent_channels list`
Expected: PASS — prints `(no channels)` or a channel table, exit 0.

- [ ] **Step 4: Commit**

```bash
git add src/agent_channels/__main__.py
git commit -m "feat: add python -m agent_channels entry point"
```

---

## Task 2: Shared test infrastructure

**Files:**
- Create: `tests/bridge_helpers.py`

- [ ] **Step 1: Create the helper module**

```python
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
```

- [ ] **Step 2: Verify it imports**

Run: `python3 -c "import sys; sys.path.insert(0,'tests'); import bridge_helpers; print('ok')"`
Expected: PASS — prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add tests/bridge_helpers.py
git commit -m "test: add bridge test infrastructure (stub Slack server + base case)"
```

---

## Task 3: Bridge config (`bridges.json`)

**Files:**
- Create: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bridge.py
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bridge_helpers import BridgeTestCase, stub_slack  # noqa: E402
from agent_channels import bridge  # noqa: E402


class ConfigTests(BridgeTestCase):
    def test_add_get_remove_roundtrip(self):
        self.assertIsNone(bridge.get_bridge("help"))
        bridge.add_bridge("help", "C0123", label="Help channel")
        got = bridge.get_bridge("help")
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py ConfigTests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_channels.bridge'`.

- [ ] **Step 3: Create `bridge.py` with constants, paths, and config**

```python
# src/agent_channels/bridge.py
"""Slack tee bridge: config, token resolution, outbox spool, worker.

One-way mirror of channel messages to Slack. Imports data-root helpers from
the package lazily (inside functions) to avoid an import cycle with __init__.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

BRIDGES_FILE = "bridges.json"
OUTBOX_DIRNAME = "outbox"
WORKER_LOG = "worker.log"

KEYCHAIN_SERVICE = "agent-channels"
KEYCHAIN_ACCOUNT = "slack-bot-token"

DEFAULT_SLACK_API_BASE = "https://slack.com/api"
RECLAIM_TIMEOUT_S = 60.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 0.5


# ---------- paths (lazy import to avoid cycle) ----------


def _root() -> Path:
    from agent_channels import channels_root

    return channels_root()


def bridges_path() -> Path:
    return _root() / BRIDGES_FILE


def outbox_dir() -> Path:
    return _root() / OUTBOX_DIRNAME


def worker_log_path() -> Path:
    return outbox_dir() / WORKER_LOG


# ---------- bridges.json config (non-secret) ----------


def load_bridges() -> dict:
    try:
        return json.loads(bridges_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def save_bridges(data: dict) -> None:
    from agent_channels import channels_root

    root = channels_root()
    root.mkdir(parents=True, exist_ok=True)
    p = bridges_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def get_bridge(name: str) -> Optional[dict]:
    entry = load_bridges().get(name)
    return entry if isinstance(entry, dict) else None


def add_bridge(name: str, slack_channel: str, label: str = "") -> None:
    data = load_bridges()
    data[name] = {"slack_channel": slack_channel, "label": label}
    save_bridges(data)


def remove_bridge(name: str) -> bool:
    data = load_bridges()
    if name in data:
        del data[name]
        save_bridges(data)
        return True
    return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py ConfigTests -v`
Expected: PASS — 3 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add bridge config (bridges.json) layer"
```

---

## Task 4: Message rendering

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
class RenderTests(BridgeTestCase):
    def test_render_text_format(self):
        record = {"seq": 7, "from": "auth-rewrite", "body": "stuck on JWT refresh"}
        text = bridge.render_text("help", record)
        self.assertEqual(text, "`auth-rewrite` in #help (#7)\nstuck on JWT refresh")

    def test_render_text_missing_fields(self):
        text = bridge.render_text("help", {})
        self.assertEqual(text, "`?` in #help (#?)\n")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py RenderTests -v`
Expected: FAIL — `AttributeError: module 'agent_channels.bridge' has no attribute 'render_text'`.

- [ ] **Step 3: Add `render_text` to `bridge.py`**

Add after the config section:

```python
# ---------- message rendering ----------


def render_text(channel: str, record: dict) -> str:
    frm = record.get("from", "?")
    seq = record.get("seq", "?")
    body = record.get("body", "")
    return f"`{frm}` in #{channel} (#{seq})\n{body}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py RenderTests -v`
Expected: PASS — 2 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add Slack message rendering"
```

---

## Task 5: Slack HTTP delivery (`slack_post`)

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py SlackPostTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'slack_post'`.

- [ ] **Step 3: Add HTTP delivery to `bridge.py`**

Add imports at the top of `bridge.py` (alongside existing imports):

```python
import urllib.error
import urllib.request
```

Add this section:

```python
# ---------- Slack HTTP ----------


def slack_api_base() -> str:
    return os.environ.get("SLACK_API_BASE", DEFAULT_SLACK_API_BASE)


def slack_post(token: str, slack_channel: str, text: str) -> tuple:
    """POST one message to chat.postMessage.

    Returns (ok, retry_after_seconds). retry_after is set only on HTTP 429.
    Never raises — network/HTTP failures return (False, ...).
    """
    url = slack_api_base().rstrip("/") + "/chat.postMessage"
    payload = json.dumps({"channel": slack_channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return (bool(body.get("ok")), None)
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            raw = exc.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw is not None else None
            except ValueError:
                retry_after = None
        return (False, retry_after)
    except (urllib.error.URLError, OSError, ValueError):
        return (False, None)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py SlackPostTests -v`
Expected: PASS — 4 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add Slack chat.postMessage delivery over urllib"
```

---

## Task 6: Token resolution (env -> keychain -> none)

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py TokenTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'resolve_token'`.

- [ ] **Step 3: Add token resolution to `bridge.py`**

Add imports at the top (alongside existing):

```python
import shutil
import subprocess
import sys as _sys
```

Add this section:

```python
# ---------- token resolution ----------


def _keychain_disabled() -> bool:
    return os.environ.get("CHANNELS_BRIDGE_NO_KEYCHAIN") == "1"


def _keychain_tool() -> Optional[str]:
    """Return the keychain CLI for this platform, or None if unavailable."""
    if _keychain_disabled():
        return None
    if _sys.platform == "darwin":
        return shutil.which("security")
    if _sys.platform.startswith("linux"):
        return shutil.which("secret-tool")
    return None


def keychain_available() -> bool:
    return _keychain_tool() is not None


def keychain_get() -> Optional[str]:
    tool = _keychain_tool()
    if not tool:
        return None
    if _sys.platform == "darwin":
        cmd = [tool, "find-generic-password", "-s", KEYCHAIN_SERVICE,
               "-a", KEYCHAIN_ACCOUNT, "-w"]
    else:  # linux secret-tool
        cmd = [tool, "lookup", "service", KEYCHAIN_SERVICE,
               "account", KEYCHAIN_ACCOUNT]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    token = proc.stdout.strip()
    return token or None


def keychain_set(token: str) -> bool:
    tool = _keychain_tool()
    if not tool:
        return False
    try:
        if _sys.platform == "darwin":
            cmd = [tool, "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                   "-a", KEYCHAIN_ACCOUNT, "-w", token]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        else:  # linux secret-tool reads the secret from stdin
            cmd = [tool, "store", "--label", "agent-channels Slack bot token",
                   "service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT]
            proc = subprocess.run(cmd, input=token, capture_output=True,
                                  text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def resolve_token() -> Optional[tuple]:
    """Return (token, source) with source in {"env","keychain"}, or None."""
    env = os.environ.get("SLACK_BOT_TOKEN")
    if env:
        return (env, "env")
    kc = keychain_get()
    if kc:
        return (kc, "keychain")
    return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py TokenTests -v`
Expected: PASS — 3 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add layered Slack token resolution (env -> keychain)"
```

---

## Task 7: Outbox enqueue (spool file)

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
import json as _json


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py EnqueueTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'enqueue'`.

- [ ] **Step 3: Add enqueue to `bridge.py`**

Add this section:

```python
# ---------- outbox spool ----------


def _ensure_outbox() -> Path:
    od = outbox_dir()
    od.mkdir(parents=True, exist_ok=True)
    return od


def _spool_name(channel: str, seq) -> str:
    return f"{channel}.{seq}.{os.getpid()}.json"


def enqueue(channel: str, slack_channel: str, record: dict) -> Path:
    """Write one delivery job to the outbox via tmp + atomic rename."""
    from agent_channels import now_iso

    od = _ensure_outbox()
    payload = {
        "slack_channel": slack_channel,
        "text": render_text(channel, record),
        "channel": channel,
        "seq": record.get("seq"),
        "enqueued_ts": now_iso(),
    }
    final = od / _spool_name(channel, record.get("seq"))
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, final)
    return final
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py EnqueueTests -v`
Expected: PASS — 1 test OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add outbox enqueue (spool file per message)"
```

---

## Task 8: Flush / drain the outbox

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py FlushTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'flush'`.

- [ ] **Step 3: Add flush + logging to `bridge.py`**

Add `import time` at the top (alongside existing imports), then add:

```python
# ---------- flush / drain ----------

SENDING_SUFFIX = ".sending"


def _max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("CHANNELS_BRIDGE_MAX_ATTEMPTS", "")))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def _backoff() -> float:
    try:
        return max(0.0, float(os.environ.get("CHANNELS_BRIDGE_BACKOFF", "")))
    except ValueError:
        return DEFAULT_BACKOFF_S


def _log_error(msg: str) -> None:
    from agent_channels import now_iso

    try:
        _ensure_outbox()
        with worker_log_path().open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
    except OSError:
        pass


def _reclaim_stale(od: Path) -> None:
    """Return orphaned *.sending files (crashed worker) to *.json."""
    now = time.time()
    for s in od.glob("*" + SENDING_SUFFIX):
        try:
            if now - s.stat().st_mtime < RECLAIM_TIMEOUT_S:
                continue
        except OSError:
            continue
        target = s.with_suffix("")  # strip ".sending" -> "....json"
        try:
            os.rename(s, target)
        except OSError:
            pass


def _deliver(token: str, payload: dict) -> bool:
    """Attempt delivery with bounded retries + backoff. Returns ok."""
    attempts = _max_attempts()
    backoff = _backoff()
    for i in range(attempts):
        ok, retry_after = slack_post(
            token, payload["slack_channel"], payload["text"]
        )
        if ok:
            return True
        if i == attempts - 1:
            break
        delay = retry_after if retry_after is not None else backoff * (i + 1)
        time.sleep(min(delay, 30.0))
    return False


def flush(quiet: bool = True) -> int:
    """Drain the outbox to Slack. Returns 0 if nothing remains, else 1."""
    od = outbox_dir()
    if not od.exists():
        return 0
    _reclaim_stale(od)
    jobs = sorted(od.glob("*.json"))
    if not jobs:
        return 0

    token_info = resolve_token()
    if token_info is None:
        _log_error(f"no Slack token; leaving {len(jobs)} message(s) queued")
        if not quiet:
            print("channels: no Slack token (set $SLACK_BOT_TOKEN or run "
                  "`channels bridge set-token`)", file=_sys.stderr)
        return 1
    token = token_info[0]

    remaining = 0
    for job in jobs:
        sending = Path(str(job) + SENDING_SUFFIX)
        try:
            os.rename(job, sending)  # claim; loser of a race raises/ skips
            os.utime(sending, None)  # reset mtime to claim time so the reclaim
                                     # window is measured from ownership, not enqueue
        except OSError:
            continue
        try:
            payload = json.loads(sending.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _log_error(f"{sending.name}: unreadable spool file; dropping")
            try:
                sending.unlink()
            except OSError:
                pass
            continue
        if _deliver(token, payload):
            try:
                sending.unlink()
            except OSError:
                pass
        else:
            remaining += 1
            _log_error(f"{job.name}: delivery failed; retained for retry")
            try:
                os.rename(sending, job)  # release for a later worker
            except OSError:
                _log_error(f"{job.name}: could not release claim back to .json")

    return 1 if remaining else 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py FlushTests -v`
Expected: PASS — 6 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add outbox flush/drain with claim-by-rename + retry"
```

---

## Task 9: Detached worker spawn

**Files:**
- Modify: `src/agent_channels/bridge.py`
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py SpawnTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'spawn_worker'`.

- [ ] **Step 3: Add `spawn_worker` to `bridge.py`**

Add this section:

```python
# ---------- detached worker spawn ----------


def spawn_worker() -> None:
    """Launch a fully detached `bridge flush` worker, then return immediately.

    No-op when CHANNELS_BRIDGE_NO_SPAWN=1. Sets PYTHONPATH so `python -m
    agent_channels` works from both an installed package and an in-repo
    checkout. The worker inherits this process's env (so $SLACK_BOT_TOKEN and
    $SLACK_API_BASE carry through).
    """
    if os.environ.get("CHANNELS_BRIDGE_NO_SPAWN") == "1":
        return

    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        pkg_parent + (os.pathsep + existing if existing else "")
    )
    try:
        devnull = subprocess.DEVNULL
        subprocess.Popen(
            [_sys.executable, "-m", "agent_channels", "bridge", "flush", "--quiet"],
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except OSError:
        # If we can't spawn, the message stays queued; the next post retries.
        _log_error("failed to spawn delivery worker")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 tests/test_bridge.py SpawnTests -v`
Expected: PASS — 2 tests OK.

- [ ] **Step 5: Commit**

```bash
git add src/agent_channels/bridge.py tests/test_bridge.py
git commit -m "feat: add detached delivery-worker spawn"
```

---

## Task 10: Hook bridging into `post`

**Files:**
- Modify: `src/agent_channels/__init__.py:1-21` (add `from . import bridge`)
- Modify: `src/agent_channels/__init__.py` (`cmd_post`, around the print block)
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
from agent_channels import main as channels_main  # noqa: E402


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py PostHookTests -v`
Expected: FAIL — `test_bridged_post_enqueues_one_job` fails (0 jobs, no bridging hook yet).

- [ ] **Step 3: Add the import**

In `src/agent_channels/__init__.py`, after the existing `from typing import ...` line (around line 21), add:

```python
from . import bridge
```

- [ ] **Step 4: Hook bridging into `cmd_post`**

In `cmd_post`, locate this existing block (the success path inside the lock, after the session write):

```python
        if session_id:
            session_data["from"] = slug
            session_data["last_post_ts"] = record["ts"]
            write_session(session_id, session_data)

        print(f"{name} #{next_seq}")
        print(f"  read with: channels read {name} --seq {next_seq}")
        return 0
```

Replace it with:

```python
        if session_id:
            session_data["from"] = slug
            session_data["last_post_ts"] = record["ts"]
            write_session(session_id, session_data)

        mapping = bridge.get_bridge(name)
        if mapping:
            bridge.enqueue(name, mapping["slack_channel"], record)
            bridge.spawn_worker()

        print(f"{name} #{next_seq}")
        print(f"  read with: channels read {name} --seq {next_seq}")
        return 0
```

- [ ] **Step 5: Run to verify it passes**

Run: `python3 tests/test_bridge.py PostHookTests -v`
Expected: PASS — 2 tests OK.

- [ ] **Step 6: Run the full bridge suite + existing smoke test (no regressions)**

Run: `python3 tests/test_bridge.py -v && bash tests/smoke.sh`
Expected: all bridge tests PASS; smoke test prints `PASS`.

- [ ] **Step 7: Commit**

```bash
git add src/agent_channels/__init__.py tests/test_bridge.py
git commit -m "feat: mirror bridged posts to the Slack outbox"
```

---

## Task 11: `bridge` CLI subcommands

**Files:**
- Modify: `src/agent_channels/__init__.py` (add `cmd_bridge_*` handlers + argparse)
- Test: `tests/test_bridge.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_bridge.py`:

```python
import io
import contextlib as _ctx


class BridgeCliTests(BridgeTestCase):
    def _run(self, argv):
        out = io.StringIO()
        with _ctx.redirect_stdout(out):
            rc = channels_main(argv)
        return rc, out.getvalue()

    def test_add_list_remove(self):
        rc, _ = self._run(["bridge", "add", "help", "C0123", "--label", "Help"])
        self.assertEqual(rc, 0)
        self.assertEqual(bridge.get_bridge("help")["slack_channel"], "C0123")

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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 tests/test_bridge.py BridgeCliTests -v`
Expected: FAIL — argparse exits non-zero / `invalid choice: 'bridge'`.

- [ ] **Step 3: Add `cmd_bridge_*` handlers to `__init__.py`**

Add these functions just before `build_parser` in `src/agent_channels/__init__.py`:

```python
# ---------- BRIDGE ----------


def cmd_bridge_add(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    bridge.add_bridge(name, args.slack_channel, label=args.label or "")
    print(f"bridged {name} -> {args.slack_channel}")
    return 0


def cmd_bridge_remove(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    if bridge.remove_bridge(name):
        print(f"removed bridge for {name}")
        return 0
    die(f"no bridge for channel {name!r}")
    return 1


def cmd_bridge_list(args: argparse.Namespace) -> int:
    bridges = bridge.load_bridges()
    token_info = bridge.resolve_token()
    if token_info is None:
        print("token: MISSING (set $SLACK_BOT_TOKEN or run `channels bridge set-token`)")
    else:
        print(f"token: set ({token_info[1]})")
    if not bridges:
        print("(no bridges)")
        return 0
    print(f"{'CHANNEL':<24} {'SLACK':<16} LABEL")
    for name in sorted(bridges):
        entry = bridges[name]
        slack = entry.get("slack_channel", "?")
        label = entry.get("label", "")
        print(f"{name:<24} {slack:<16} {label}")
    return 0


def cmd_bridge_set_token(args: argparse.Namespace) -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        import getpass

        token = getpass.getpass("Slack bot token (xoxb-...): ").strip()
    if not token:
        die("no token provided")
    if not bridge.keychain_available():
        die(
            "OS keychain unavailable; export SLACK_BOT_TOKEN in the environment "
            "that runs posts instead (macOS needs `security`, Linux needs `secret-tool`)"
        )
    if bridge.keychain_set(token):
        print("stored Slack bot token in the OS keychain")
        return 0
    die("failed to store token in the keychain")
    return 1


def cmd_bridge_flush(args: argparse.Namespace) -> int:
    return bridge.flush(quiet=args.quiet)
```

- [ ] **Step 4: Wire the `bridge` subparser into `build_parser`**

In `build_parser`, just before `return p`, add:

```python
    p_bridge = sub.add_parser("bridge", help="manage one-way Slack mirrors")
    bsub = p_bridge.add_subparsers(dest="bridge_cmd", required=True)

    b_add = bsub.add_parser("add", help="mirror a channel to a Slack channel")
    b_add.add_argument("name")
    b_add.add_argument("slack_channel", help="Slack channel id, e.g. C0123ABC")
    b_add.add_argument("--label", default=None, help="optional human label")
    b_add.set_defaults(func=cmd_bridge_add)

    b_rm = bsub.add_parser("remove", help="remove a channel's Slack mirror")
    b_rm.add_argument("name")
    b_rm.set_defaults(func=cmd_bridge_remove)

    b_ls = bsub.add_parser("list", help="list Slack mirrors and token status")
    b_ls.set_defaults(func=cmd_bridge_list)

    b_tok = bsub.add_parser("set-token", help="store the bot token in the OS keychain")
    b_tok.set_defaults(func=cmd_bridge_set_token)

    b_flush = bsub.add_parser("flush", help="drain queued messages to Slack now")
    b_flush.add_argument("--quiet", action="store_true", help="suppress stderr notices")
    b_flush.set_defaults(func=cmd_bridge_flush)
```

- [ ] **Step 5: Run to verify it passes**

Run: `python3 tests/test_bridge.py BridgeCliTests -v`
Expected: PASS — 4 tests OK.

- [ ] **Step 6: Run the full suite**

Run: `python3 tests/test_bridge.py -v && bash tests/smoke.sh`
Expected: all bridge tests PASS; smoke test prints `PASS`.

- [ ] **Step 7: Commit**

```bash
git add src/agent_channels/__init__.py tests/test_bridge.py
git commit -m "feat: add `channels bridge` subcommands"
```

---

## Task 12: Smoke-test round-trip + README

**Files:**
- Modify: `tests/smoke.sh`
- Modify: `README.md`

- [ ] **Step 1: Add a bridge round-trip to `tests/smoke.sh`**

Insert before the final `echo "PASS"` block. It starts a stdlib stub Slack server, points the CLI at it, and verifies an end-to-end deliver:

```bash
# ---------- slack bridge ----------

export HOME="$TMP/bridge"
mkdir -p "$HOME"
unset CODEX_THREAD_ID
export CLAUDE_CODE_SESSION_ID="bridge-session-1"
export SLACK_BOT_TOKEN="xoxb-smoke"
export CHANNELS_BRIDGE_NO_KEYCHAIN=1
export CHANNELS_BRIDGE_NO_SPAWN=1

step "bridge: stub Slack server captures a delivered message"
STUB_OUT="$TMP/stub_requests.log"
python3 - "$STUB_OUT" <<'PYSTUB' &
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer

out_path = sys.argv[1]

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(raw.decode("utf-8") + "\n")
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

srv = HTTPServer(("127.0.0.1", 0), H)
with open(out_path + ".port", "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
PYSTUB
STUB_PID=$!

# wait for the stub to report its port
for _ in $(seq 1 50); do
    [ -f "$STUB_OUT.port" ] && break
    sleep 0.1
done
[ -f "$STUB_OUT.port" ] || fail "stub Slack server did not start"
export SLACK_API_BASE="http://127.0.0.1:$(cat "$STUB_OUT.port")"

"$CHANNELS" bridge add slacktest C0SMOKE >/dev/null || fail "bridge add errored"
"$CHANNELS" bridge list | grep -q 'slacktest' || fail "bridge list missing entry"
"$CHANNELS" post --from smoke-bridge slacktest 'mirror me' >/dev/null \
    || fail "bridged post errored"
"$CHANNELS" bridge flush --quiet || fail "bridge flush errored"

kill "$STUB_PID" 2>/dev/null || true
wait "$STUB_PID" 2>/dev/null || true

grep -q 'mirror me' "$STUB_OUT" || fail "Slack stub did not receive the message"
grep -q '"channel": "C0SMOKE"' "$STUB_OUT" || fail "Slack stub missing channel id"

unset SLACK_BOT_TOKEN SLACK_API_BASE CHANNELS_BRIDGE_NO_KEYCHAIN CHANNELS_BRIDGE_NO_SPAWN
```

- [ ] **Step 2: Run the smoke test**

Run: `bash tests/smoke.sh`
Expected: prints `PASS` (now including the bridge round-trip).

- [ ] **Step 3: Document the bridge in `README.md`**

Add a new section after the `### archive` subsection (before `## Channel Names`):

````markdown
### bridge (Slack mirror)

Mirror a channel one-way into a Slack channel. Messages are queued locally and
delivered asynchronously by a detached worker, so posting stays fast and a
Slack outage never blocks an agent.

```
channels bridge add <channel> <slack-channel-id> [--label <text>]
channels bridge remove <channel>
channels bridge list
channels bridge set-token
channels bridge flush [--quiet]
```

Setup:

1. Create a Slack app with a bot token (`xoxb-...`) that has `chat:write`, and
   invite the bot to the target channel. Note the channel id (e.g. `C0123ABC`).
2. Make the token available. Either export it where your agents run:

   ```
   export SLACK_BOT_TOKEN=xoxb-...
   ```

   or store it in the OS keychain (macOS `security`, Linux `secret-tool`):

   ```
   channels bridge set-token
   ```

   Token resolution is env first, then keychain. If neither is available,
   delivery is skipped and the message stays queued.
3. Bridge a channel and post:

   ```
   channels bridge add status C0123ABC
   channels post --from auth-rewrite status "refresh-token cleanup is done"
   ```

The Slack message looks like:

```
`auth-rewrite` in #status (#4)
refresh-token cleanup is done
```

Notes:
- One-way only: replies in Slack are not read back.
- Only messages posted after `bridge add` are mirrored (no backfill).
- Undelivered messages are retained under `~/.agent-channels/outbox/` and
  retried on the next post; delivery errors are logged to `outbox/worker.log`.
- Delivery is at-least-once (a crash mid-delivery can re-send).
````

- [ ] **Step 4: Commit**

```bash
git add tests/smoke.sh README.md
git commit -m "test: add Slack bridge smoke round-trip; docs: document bridge"
```

---

## Final verification

- [ ] **Run the full test suite**

Run: `python3 tests/test_bridge.py -v && bash tests/smoke.sh`
Expected: all bridge unit tests PASS; smoke test prints `PASS`.

- [ ] **Confirm zero third-party imports**

Run: `grep -nE "^\s*(import|from)\s" src/agent_channels/bridge.py | grep -vE "agent_channels|^\s*(import|from)\s+(json|os|sys|time|shutil|subprocess|urllib|getpass|pathlib|typing)" || echo "stdlib-only OK"`
Expected: prints `stdlib-only OK`.

---

## Self-review notes (author)

- **Spec coverage:** trigger model (Tasks 7-10), bot-token `chat.postMessage` (Task 5), layered token env→keychain→fail (Task 6, `cmd_bridge_set_token`), `bridges.json` mappings (Task 3), spool dir + claim-by-rename + at-least-once (Tasks 7-8), detached worker via `python -m agent_channels` (Tasks 1, 9), `__main__.py` (Task 1), message format (Task 4), failure visibility via `worker.log` (Task 8), testing seam with `SLACK_API_BASE` + stub server + foreground `flush` (Tasks 2, 8, 11-12), reclaim of stale `.sending` (Task 8). All spec sections map to a task.
- **No silent caps:** `flush` logs queued-but-undelivered counts and reasons to `worker.log`.
- **Type consistency:** `resolve_token() -> (token, source)`; `slack_post(token, slack_channel, text) -> (ok, retry_after)`; `enqueue(channel, slack_channel, record) -> Path`; `get_bridge(name) -> dict|None`. These signatures are used consistently across Tasks 5-11.
