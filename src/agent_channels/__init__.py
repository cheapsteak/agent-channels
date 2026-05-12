"""channels — Slack-style channels for cross-session AI agent messaging.

Source of truth is per-channel append-only JSONL at
~/.claude/channels/<name>.jsonl. Writes go through this binary under
fcntl.flock for cross-process coordination, with os.fsync for durability.
Reads open files directly.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

CHANNELS_ROOT = Path.home() / ".claude" / "channels"
ARCHIVE_DIR = CHANNELS_ROOT / "archive"
SESSIONS_DIR = CHANNELS_ROOT / "sessions"

MAX_BODY_BYTES = 64 * 1024
NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
RESERVED_SUFFIX = "_archive"


# ---------- errors / exits ----------

def die(msg: str, code: int = 1) -> None:
    print(f"channels: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- channel name ----------

def canonical_name(raw: str) -> str:
    """Strip leading '#', case-fold to lowercase, validate.

    Allowed: [a-z0-9_-], length 1-64.
    Rejected: starts with '.', equals reserved suffix.
    """
    if raw is None:
        die("channel name is required")
    name = raw[1:] if raw.startswith("#") else raw
    name = name.lower()
    if not name:
        die("channel name is empty")
    if name.startswith("."):
        die(f"channel name may not start with '.': {raw!r}")
    if name == RESERVED_SUFFIX or name.endswith(RESERVED_SUFFIX):
        die(f"channel name reserved: {raw!r}")
    if not NAME_RE.match(name):
        die(
            f"invalid channel name {raw!r}: "
            "must match [a-z0-9_-]{1,64} after lowercasing and stripping leading '#'"
        )
    return name


def channel_path(name: str) -> Path:
    return CHANNELS_ROOT / f"{name}.jsonl"


def lock_path(name: str) -> Path:
    return CHANNELS_ROOT / f"{name}.lock"


# ---------- filesystem helpers ----------

def ensure_dirs() -> None:
    CHANNELS_ROOT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- session / identity ----------

def session_file(session_id: str) -> Path:
    return SESSIONS_DIR / f"{session_id}.json"


def read_session(session_id: str) -> dict:
    p = session_file(session_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def write_session(session_id: str, data: dict) -> None:
    ensure_dirs()
    p = session_file(session_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def resolve_session_id(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    return os.environ.get("CLAUDE_CODE_SESSION_ID")


# ---------- JSONL scan / torn-line recovery ----------

def scan_last_good(fd: int) -> tuple[int, int]:
    """Scan the entire file. Return (last_good_end, highest_seq).

    last_good_end is the byte offset of the last newline + 1
    (i.e. the position to truncate to before appending). Any bytes
    past last_good_end are a torn partial line and must be dropped.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    size = os.fstat(fd).st_size
    last_good_end = 0
    highest_seq = 0
    remainder = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data = remainder + chunk
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            remainder = data
            continue
        complete = data[: last_nl + 1]
        remainder = data[last_nl + 1 :]
        for line in complete.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = obj.get("seq")
            if isinstance(seq, int) and seq > highest_seq:
                highest_seq = seq
        last_good_end = size - len(remainder)
    if not remainder:
        last_good_end = size
    return last_good_end, highest_seq


# ---------- POST ----------

def cmd_post(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)

    body = args.body
    if body == "-":
        body = sys.stdin.read()
    if body is None:
        die("body is required")
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > MAX_BODY_BYTES:
        die(
            f"body too large: {len(body_bytes)} bytes (max {MAX_BODY_BYTES})"
        )

    session_id = resolve_session_id(args.session)

    slug = args.from_slug
    session_data: dict = {}
    if session_id:
        session_data = read_session(session_id)
        if not slug:
            slug = session_data.get("from")
        elif session_data.get("from") and session_data["from"] != slug:
            session_data["from"] = slug

    if not slug:
        die(
            "first post in this session requires --from <slug> "
            "(a short label describing this agent's current task, e.g. 'auth-rewrite'). "
            "It will be cached so subsequent posts can omit it."
        )

    ensure_dirs()

    lp = lock_path(name)
    lock_fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        cp = channel_path(name)
        data_fd = os.open(
            str(cp), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644
        )
        try:
            last_good_end, highest_seq = scan_last_good(data_fd)
            if last_good_end != os.fstat(data_fd).st_size:
                os.ftruncate(data_fd, last_good_end)

            next_seq = highest_seq + 1
            record = {
                "seq": next_seq,
                "ts": now_iso(),
                "session_id": session_id or "",
                "from": slug,
                "body": body,
            }
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            os.write(data_fd, line)
            os.fsync(data_fd)
        finally:
            os.close(data_fd)

        if session_id:
            session_data["from"] = slug
            session_data["last_post_ts"] = record["ts"]
            write_session(session_id, session_data)

        print(f"{name} #{next_seq}")
        print(f"  read with: channels read {name} --seq {next_seq}")
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# ---------- READ helpers ----------

def iter_messages(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def format_message(m: dict) -> str:
    seq = m.get("seq", "?")
    ts = m.get("ts", "")
    frm = m.get("from", "?")
    body = m.get("body", "")
    return f"#{seq} [{ts}] {frm}: {body}"


# ---------- READ ----------

def cmd_read(args: argparse.Namespace) -> int:
    if args.seq is not None and args.since is not None:
        die("--seq and --since are mutually exclusive")
    name = canonical_name(args.name)
    path = channel_path(name)
    if not path.exists():
        die(f"channel {name!r} has no messages yet")

    selected: list[dict] = []
    for m in iter_messages(path):
        seq = m.get("seq")
        if not isinstance(seq, int):
            continue
        if args.seq is not None and seq != args.seq:
            continue
        if args.since is not None and seq <= args.since:
            continue
        selected.append(m)

    if args.limit is not None and args.limit > 0:
        selected = selected[-args.limit :]

    for m in selected:
        print(format_message(m))
    return 0


# ---------- TAIL ----------

def cmd_tail(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    path = channel_path(name)
    if not path.exists():
        die(
            f"channel {name!r} has no messages yet — post to it first "
            f"(e.g. `channels post --from <slug> {name} 'hello'`)"
        )

    if args.from_start:
        for m in iter_messages(path):
            print(format_message(m), flush=True)
        last_size = path.stat().st_size
    else:
        msgs = list(iter_messages(path))
        if msgs and not args.follow:
            print(format_message(msgs[-1]), flush=True)
        last_size = path.stat().st_size

    if not args.follow:
        return 0

    buf = b""
    with path.open("rb") as f:
        f.seek(last_size)
        stop = {"v": False}

        def _sigint(_signum, _frame):
            stop["v"] = True

        signal.signal(signal.SIGINT, _sigint)
        while not stop["v"]:
            try:
                if not path.exists():
                    print(
                        f"channels: channel {name!r} file gone — exiting tail",
                        file=sys.stderr,
                    )
                    return 0
            except OSError:
                return 0
            chunk = f.read()
            if chunk:
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line = buf[:nl].decode("utf-8", errors="replace")
                    buf = buf[nl + 1 :]
                    if not line.strip():
                        continue
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    print(format_message(m), flush=True)
            else:
                time.sleep(0.25)
    return 0


# ---------- LIST ----------

def cmd_list(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.archived:
        rows = []
        for p in sorted(ARCHIVE_DIR.glob("*.jsonl")):
            rows.append((p.name, p.stat().st_mtime))
        if not rows:
            print("(no archived channels)")
            return 0
        print(f"{'NAME':<60} ARCHIVED-AT")
        for name, mtime in rows:
            ts = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
            print(f"{name:<60} {ts}")
        return 0

    rows = []
    for p in sorted(CHANNELS_ROOT.glob("*.jsonl")):
        last_seq = 0
        last_ts = ""
        count = 0
        for m in iter_messages(p):
            seq = m.get("seq")
            if isinstance(seq, int):
                count += 1
                if seq > last_seq:
                    last_seq = seq
                    last_ts = m.get("ts", "")
        rows.append((p.stem, last_seq, last_ts, count))

    if not rows:
        print("(no channels)")
        return 0
    rows.sort(key=lambda r: r[2], reverse=True)
    print(f"{'NAME':<32} {'LAST-SEQ':>8}  {'COUNT':>6}  LAST-MESSAGE-AT")
    for name, last_seq, last_ts, count in rows:
        print(f"{name:<32} {last_seq:>8}  {count:>6}  {last_ts}")
    return 0


# ---------- ARCHIVE ----------

def cmd_archive(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    ensure_dirs()
    cp = channel_path(name)
    if not cp.exists():
        die(f"channel {name!r} does not exist")

    lp = lock_path(name)
    lock_fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        ts = (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            .replace(":", "-")
        )
        target = ARCHIVE_DIR / f"{name}-{ts}.jsonl"
        os.replace(cp, target)
        try:
            lp.unlink()
        except FileNotFoundError:
            pass
        print(f"archived: {target}")
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(lock_fd)
            except OSError as e:
                if e.errno != errno.EBADF:
                    raise


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="channels",
        description="Slack-style channels for cross-session AI agent messaging.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_post = sub.add_parser("post", help="post a message to a channel")
    p_post.add_argument("--from", dest="from_slug", default=None,
                        help="agent slug describing this session's task")
    p_post.add_argument("--session", dest="session", default=None,
                        help="session id (defaults to $CLAUDE_CODE_SESSION_ID)")
    p_post.add_argument("name")
    p_post.add_argument("body", help="message body, or '-' to read from stdin")
    p_post.set_defaults(func=cmd_post)

    p_read = sub.add_parser("read", help="read messages from a channel")
    p_read.add_argument("name")
    p_read.add_argument("--seq", type=int, default=None,
                        help="show only the message with this seq")
    p_read.add_argument("--since", type=int, default=None,
                        help="show messages with seq > N")
    p_read.add_argument("--limit", type=int, default=20,
                        help="max messages to show (default 20)")
    p_read.set_defaults(func=cmd_read)

    p_tail = sub.add_parser("tail", help="tail the latest message; --follow to stream")
    p_tail.add_argument("name")
    p_tail.add_argument("--follow", action="store_true",
                        help="stream new messages as they arrive")
    p_tail.add_argument("--from-start", action="store_true",
                        help="print all existing messages before following")
    p_tail.set_defaults(func=cmd_tail)

    p_list = sub.add_parser("list", help="list channels")
    p_list.add_argument("--archived", action="store_true",
                        help="list archived channels instead")
    p_list.set_defaults(func=cmd_list)

    p_arch = sub.add_parser("archive", help="archive a channel")
    p_arch.add_argument("name")
    p_arch.set_defaults(func=cmd_archive)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
