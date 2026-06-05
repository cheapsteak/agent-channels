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
