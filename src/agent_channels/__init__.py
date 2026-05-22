"""channels — Slack-style channels for cross-session AI agent messaging.

Source of truth is per-channel append-only JSONL at ~/.agent-channels/,
falling back to legacy ~/.claude/channels/ when that is the only existing
store. Writes go through this binary under fcntl.flock for cross-process
coordination, with os.fsync for durability. Reads open files directly.
"""

from __future__ import annotations

import argparse
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

NEUTRAL_ROOT_NAME = ".agent-channels"
LEGACY_ROOT_PARTS = (".claude", "channels")

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
    name = raw[1:] if raw.startswith("#") else raw
    name = name.lower()
    if not name:
        die(f"channel name is empty: {raw!r}")
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
    return channels_root() / f"{name}.jsonl"


def lock_path(name: str) -> Path:
    return channels_root() / f"{name}.lock"


# ---------- filesystem helpers ----------

def neutral_root() -> Path:
    return Path.home() / NEUTRAL_ROOT_NAME


def legacy_root() -> Path:
    return Path.home().joinpath(*LEGACY_ROOT_PARTS)


def channels_root() -> Path:
    """Return the active data root.

    Prefer the product-neutral root when it exists, or for fresh installs.
    Keep using the legacy Claude root when it is the only existing store so
    upgraded Claude-only installs do not lose sight of existing channels.
    """
    neutral = neutral_root()
    legacy = legacy_root()
    if neutral.exists() or not legacy.exists():
        return neutral
    return legacy


def archive_dir() -> Path:
    return channels_root() / "archive"


def sessions_dir() -> Path:
    return channels_root() / "sessions"



def ensure_dirs() -> None:
    root = channels_root()
    root.mkdir(parents=True, exist_ok=True)
    archive_dir().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- session / identity ----------


def session_file(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.json"


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
    return (
        os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
    )


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


# ---------- write helpers (shared by post + edit) ----------


def _read_body_arg(body_arg: Optional[str]) -> str:
    if body_arg is None:
        die("body is required")
    body = sys.stdin.read() if body_arg == "-" else body_arg
    n = len(body.encode("utf-8"))
    if n > MAX_BODY_BYTES:
        die(f"body too large: {n} bytes (max {MAX_BODY_BYTES})")
    return body


def _resolve_slug_for_write(args: argparse.Namespace) -> tuple[str, Optional[str], dict]:
    """Resolve (slug, session_id, session_data) for a write op.

    Reads cached slug from the session file if --from is omitted; dies if no
    slug is available. Caller is responsible for writing session_data back
    after a successful append.
    """
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
    return slug, session_id, session_data


# ---------- POST ----------


def cmd_post(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    body = _read_body_arg(args.body)
    slug, session_id, session_data = _resolve_slug_for_write(args)

    ensure_dirs()

    lp = lock_path(name)
    lock_fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        cp = channel_path(name)
        data_fd = os.open(str(cp), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
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


# ---------- EDIT ----------


def cmd_edit(args: argparse.Namespace) -> int:
    name = canonical_name(args.name)
    target_seq = args.seq
    if target_seq < 1:
        die(f"seq must be >= 1, got {target_seq}")
    body = _read_body_arg(args.body)
    slug, session_id, session_data = _resolve_slug_for_write(args)

    ensure_dirs()

    lp = lock_path(name)
    lock_fd = os.open(str(lp), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        cp = channel_path(name)
        if not cp.exists():
            die(f"channel {name!r} has no messages yet")

        data_fd = os.open(str(cp), os.O_RDWR | os.O_APPEND, 0o644)
        try:
            last_good_end, highest_seq = scan_last_good(data_fd)
            if last_good_end != os.fstat(data_fd).st_size:
                os.ftruncate(data_fd, last_good_end)

            target: Optional[dict] = None
            for m in iter_messages(cp):
                if m.get("seq") == target_seq:
                    target = m
                    break
            if target is None:
                die(f"no message with seq {target_seq} in channel {name!r}")
            if "edit_of" in target:
                die(
                    f"#{target_seq} is itself an edit record (of "
                    f"#{target['edit_of']}); edit the original instead"
                )

            next_seq = highest_seq + 1
            record = {
                "seq": next_seq,
                "ts": now_iso(),
                "session_id": session_id or "",
                "from": slug,
                "body": body,
                "edit_of": target_seq,
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

        print(f"{name} #{next_seq} (edit of #{target_seq})")
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
    suffix = " (edited)" if m.get("edited") else ""
    return f"#{seq} [{ts}] {frm}: {body}{suffix}"


def format_edit(m: dict) -> str:
    seq = m.get("seq", "?")
    ts = m.get("ts", "")
    frm = m.get("from", "?")
    target = m.get("edit_of", "?")
    body = m.get("body", "")
    return f"#{seq} [{ts}] {frm}: edit of #{target}: {body}"


def format_record(m: dict) -> str:
    return format_edit(m) if "edit_of" in m else format_message(m)


def fold_edits(messages: list[dict]) -> tuple[list[dict], dict[int, dict]]:
    """Collapse edit records into their target originals.

    Returns (folded_originals, latest_edit_by_target_seq). Each folded
    original is a shallow copy with its body replaced by the latest edit's
    body and `edited=True` set. Edit records are dropped from the originals
    list but kept in the lookup so callers can surface "old message edited"
    notifications.
    """
    originals: list[dict] = []
    latest_edit: dict[int, dict] = {}
    for m in messages:
        if "edit_of" in m:
            target = m.get("edit_of")
            if not isinstance(target, int):
                continue
            cur = latest_edit.get(target)
            seq = m.get("seq", 0)
            if cur is None or seq > cur.get("seq", 0):
                latest_edit[target] = m
        else:
            originals.append(m)
    folded: list[dict] = []
    for orig in originals:
        seq = orig.get("seq")
        edit = latest_edit.get(seq) if isinstance(seq, int) else None
        if edit:
            merged = dict(orig)
            merged["body"] = edit.get("body", orig.get("body", ""))
            merged["edited"] = True
            folded.append(merged)
        else:
            folded.append(orig)
    return folded, latest_edit


# ---------- READ ----------


def cmd_read(args: argparse.Namespace) -> int:
    if args.seq is not None and args.since is not None:
        die("--seq and --since are mutually exclusive")
    name = canonical_name(args.name)
    path = channel_path(name)
    if not path.exists():
        die(f"channel {name!r} has no messages yet")

    all_messages = [
        m for m in iter_messages(path) if isinstance(m.get("seq"), int)
    ]
    folded, latest_edit = fold_edits(all_messages)

    if args.seq is not None:
        for m in all_messages:
            if m.get("seq") == args.seq:
                if "edit_of" in m:
                    print(format_edit(m))
                else:
                    for f in folded:
                        if f.get("seq") == args.seq:
                            print(format_message(f))
                            break
                return 0
        die(f"no message with seq {args.seq} in channel {name!r}")

    selected: list[dict]
    if args.since is not None:
        since = args.since
        selected = [f for f in folded if f.get("seq", 0) > since]
        for target_seq, edit in latest_edit.items():
            if target_seq <= since and edit.get("seq", 0) > since:
                selected.append(edit)
        selected.sort(key=lambda m: m.get("seq", 0))
    else:
        selected = folded

    if args.limit is not None and args.limit > 0:
        selected = selected[-args.limit :]

    for m in selected:
        print(format_record(m))
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
            print(format_record(m), flush=True)
        last_size = path.stat().st_size
    else:
        msgs = list(iter_messages(path))
        if msgs and not args.follow:
            print(format_record(msgs[-1]), flush=True)
        last_size = path.stat().st_size

    if not args.follow:
        return 0

    buf = b""
    with path.open("rb") as f:
        f.seek(last_size)
        try:
            starting_inode = os.fstat(f.fileno()).st_ino
        except OSError:
            starting_inode = None
        stop = {"v": False}

        def _sigint(*_):
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
            try:
                current_inode = path.stat().st_ino
            except OSError:
                current_inode = None
            if (
                starting_inode is not None
                and current_inode is not None
                and current_inode != starting_inode
            ):
                print(
                    f"channels: channel {name!r} file replaced (likely archived + reposted) — exiting tail",
                    file=sys.stderr,
                )
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
                    print(format_record(m), flush=True)
            else:
                time.sleep(0.25)
    return 0


# ---------- WATCH ----------


def _highest_seq(path: Path) -> int:
    high = 0
    for m in iter_messages(path):
        seq = m.get("seq")
        if isinstance(seq, int) and seq > high:
            high = seq
    return high


def cmd_watch(args: argparse.Namespace) -> int:
    """Block until a new message arrives on any named channel, then exit.

    Designed for the background-task-chain pattern: launch with the agent's
    background tool, idle (zero tokens) until the binary exits, react to
    stdout, re-launch with bumped --since. Unlike `tail --follow`, this
    exits on the first new message — that's the trigger.
    """
    names = [canonical_name(n) for n in args.names]
    if not names:
        die("at least one channel name required")

    poll_interval = max(0.05, args.poll_interval)
    deadline: Optional[float] = (
        time.monotonic() + args.timeout if args.timeout and args.timeout > 0 else None
    )

    baselines: dict[str, int] = {}
    for name in names:
        if args.since is not None:
            baselines[name] = args.since
        else:
            path = channel_path(name)
            baselines[name] = _highest_seq(path) if path.exists() else 0

    stop = {"v": False}

    def _sigint(_signum, _frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, _sigint)

    while not stop["v"]:
        new_msgs: list[tuple[str, dict]] = []
        for name in names:
            path = channel_path(name)
            if not path.exists():
                continue
            for m in iter_messages(path):
                seq = m.get("seq")
                if isinstance(seq, int) and seq > baselines[name]:
                    new_msgs.append((name, m))

        if new_msgs:
            new_msgs.sort(key=lambda nm: (nm[1].get("ts", ""), nm[1].get("seq", 0)))
            for name, m in new_msgs:
                print(f"[{name}] {format_record(m)}", flush=True)
            return 0

        if deadline is not None and time.monotonic() >= deadline:
            print("channels: watch timeout", file=sys.stderr)
            return 2

        time.sleep(poll_interval)

    return 130


# ---------- LIST ----------


def cmd_list(args: argparse.Namespace) -> int:
    ensure_dirs()
    if args.archived:
        rows = []
        for p in sorted(archive_dir().glob("*.jsonl")):
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
    for p in sorted(channels_root().glob("*.jsonl")):
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
        target = archive_dir() / f"{name}-{ts}.jsonl"
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
            os.close(lock_fd)


# ---------- argparse ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="channels",
        description="Slack-style channels for cross-session AI agent messaging.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_post = sub.add_parser("post", help="post a message to a channel")
    p_post.add_argument(
        "--from",
        dest="from_slug",
        default=None,
        help="agent slug describing this session's task",
    )
    p_post.add_argument(
        "--session",
        dest="session",
        default=None,
        help="session id (defaults to $CODEX_THREAD_ID or $CLAUDE_CODE_SESSION_ID)",
    )
    p_post.add_argument("name")
    p_post.add_argument("body", help="message body, or '-' to read from stdin")
    p_post.set_defaults(func=cmd_post)

    p_edit = sub.add_parser(
        "edit",
        help="edit a previously posted message (appends an edit record)",
    )
    p_edit.add_argument(
        "--from",
        dest="from_slug",
        default=None,
        help="agent slug (defaults to cached value from session)",
    )
    p_edit.add_argument(
        "--session",
        dest="session",
        default=None,
        help="session id (defaults to $CLAUDE_CODE_SESSION_ID)",
    )
    p_edit.add_argument("name")
    p_edit.add_argument("seq", type=int, help="seq of the message to edit")
    p_edit.add_argument("body", help="new body, or '-' to read from stdin")
    p_edit.set_defaults(func=cmd_edit)

    p_read = sub.add_parser("read", help="read messages from a channel")
    p_read.add_argument("name")
    p_read.add_argument(
        "--seq", type=int, default=None, help="show only the message with this seq"
    )
    p_read.add_argument(
        "--since", type=int, default=None, help="show messages with seq > N"
    )
    p_read.add_argument(
        "--limit", type=int, default=20, help="max messages to show (default 20)"
    )
    p_read.set_defaults(func=cmd_read)

    p_tail = sub.add_parser("tail", help="tail the latest message; --follow to stream")
    p_tail.add_argument("name")
    p_tail.add_argument(
        "--follow", action="store_true", help="stream new messages as they arrive"
    )
    p_tail.add_argument(
        "--from-start",
        action="store_true",
        help="print all existing messages before following",
    )
    p_tail.set_defaults(func=cmd_tail)

    p_watch = sub.add_parser(
        "watch",
        help="block until a new message arrives on any channel, then exit",
    )
    p_watch.add_argument("names", nargs="+", help="one or more channel names")
    p_watch.add_argument(
        "--since",
        type=int,
        default=None,
        help="fire on messages with seq > N (applies to all channels). "
        "Default: each channel's current high-water mark at start.",
    )
    p_watch.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="exit with code 2 if no message arrives within N seconds (default: no timeout)",
    )
    p_watch.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        dest="poll_interval",
        help="seconds between file checks (default 1.0; min 0.05)",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_list = sub.add_parser("list", help="list channels")
    p_list.add_argument(
        "--archived", action="store_true", help="list archived channels instead"
    )
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
