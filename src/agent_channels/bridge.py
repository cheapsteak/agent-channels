# src/agent_channels/bridge.py
"""Slack tee bridge: config, token resolution, outbox spool, worker.

One-way mirror of channel messages to Slack. Imports data-root helpers from
the package lazily (inside functions) to avoid an import cycle with __init__.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys as _sys
import time
import urllib.error
import urllib.request
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


# ---------- Slack HTTP ----------


def slack_api_base() -> str:
    return os.environ.get("SLACK_API_BASE", DEFAULT_SLACK_API_BASE)


def slack_post(token: str, slack_channel: str, text: str) -> tuple:
    """POST one message to chat.postMessage.

    Returns (ok, retry_after_seconds). retry_after is set only on HTTP 429.
    Never raises — network/HTTP failures return (False, ...).
    """
    url = slack_api_base().rstrip("/") + "/chat.postMessage"
    try:
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
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        return (False, None)


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


# ---------- message rendering ----------


def render_text(channel: str, record: dict) -> str:
    frm = record.get("from", "?")
    seq = record.get("seq", "?")
    body = record.get("body", "")
    return f"`{frm}` in #{channel} (#{seq})\n{body}"
