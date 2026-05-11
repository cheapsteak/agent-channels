#!/usr/bin/env python3
"""SessionStart hook for agent-channels.

Reads the SessionStart JSON payload from stdin and writes
~/.claude/channels/sessions/<session_id>.json with the machine identity
fields. Display name is *not* captured here — agents supply --from on
first post and the binary caches it into this file.

Hook must never block a session start. Any failure exits 0 silently.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "channels" / "sessions"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    record = {
        "session_id": session_id,
        "cwd": payload.get("cwd", ""),
        "source": payload.get("source", ""),
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        target = SESSIONS_DIR / f"{session_id}.json"
        existing: dict = {}
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        if isinstance(existing.get("from"), str):
            record["from"] = existing["from"]

        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
