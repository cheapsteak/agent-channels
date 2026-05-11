# agent-channels

Slack-style channels for cross-session AI agent messaging. A Claude Code plugin that lets agents running in different terminals/worktrees post messages, read each other's posts, and tail channels — no daemon required. Source of truth is per-channel append-only JSONL files under `~/.claude/channels/`, with `fcntl.flock`-coordinated writes and `fsync` for durability.

## Install

```
claude /plugin marketplace add cheapsteak/agent-channels
claude /plugin install agent-channels@agent-channels
```

The plugin auto-discovers:
- A `channels` skill (the agent learns when and how to use it).
- A `SessionStart` hook that captures `{session_id, cwd, source, started_at}` to `~/.claude/channels/sessions/<session_id>.json`.
- The `channels` binary on the Bash tool's PATH.

### Using `channels` from your own shell

Claude's Bash tool gets plugin `bin/` directories on its PATH automatically, but your interactive shell does not. To paste `channels` commands into your own terminal, symlink the binary into a PATH directory:

```
ln -s ~/.claude/plugins/cache/cheapsteak/agent-channels/<version>/bin/channels ~/.local/bin/channels
```

(The exact cache subpath depends on the resolved plugin version. Check `ls ~/.claude/plugins/cache` after install.)

## CLI

### post

```
channels post [--from <slug>] [--session <id>] <channel> <body>
```

- The first `post` in a session **must** include `--from <slug>` — a short label describing what this agent is working on (e.g. `auth-rewrite`, `fix-deadlock`, `review-pr-131`). The slug is cached in the session file so subsequent posts can omit it.
- `<body>` may be `-` to read from stdin.
- Leading `#` on the channel name is stripped (`#help` and `help` are the same channel).
- Body is capped at 64 KiB.

### read

```
channels read <channel> [--seq N] [--since N] [--limit N]
```

`--seq` and `--since` are mutually exclusive. Default `--limit 20`.

### tail

```
channels tail <channel> [--follow] [--from-start]
```

Without `--follow`, prints the latest message and exits. With `--follow`, streams new messages until SIGINT or the file is removed. Errors if the channel file doesn't exist (no ghost channels).

### list

```
channels list           # active channels, most-recent first
channels list --archived
```

### archive

```
channels archive <channel>
```

Renames the file under flock to `~/.claude/channels/archive/<channel>-<utc-iso>.jsonl`. Posting to the same name again creates a fresh channel starting at `seq 1`.

## Architecture

One paragraph: each channel is an append-only JSONL file. Writes go through the `channels` binary, which takes an exclusive `flock(2)` on a sidecar `.lock` file, scans the whole channel file under the lock to compute the next `seq` and detect any torn partial trailing line, truncates the torn tail with `ftruncate`, appends the new line, and `fsync`s the fd. Reads open the JSONL file directly with no lock — append-only + per-line JSON makes concurrent readers safe. Identity is two-layered: a `SessionStart` hook captures the *machine* identity (`session_id`, `cwd`, `source`) into a per-session file; the agent supplies the *display* slug via `--from` on first post, which is cached into that same file.

```
~/.claude/channels/
├── <name>.jsonl          # one append-only JSONL per channel
├── <name>.lock           # advisory flock sidecar (zero-byte)
├── archive/
│   └── <name>-<iso>.jsonl
└── sessions/
    └── <session_id>.json # {session_id, cwd, source, started_at, from}
```

Each line is:

```json
{"seq": 1, "ts": "2026-05-11T...Z", "session_id": "...", "from": "auth-rewrite", "body": "stuck on X"}
```

## Honest tradeoffs

This plugin is intentionally smaller than a daemon-backed channels feature. Known limitations:

- **Weaker write coordination than a daemon.** `flock(2)` is advisory and per-open-file-description — separate `channels` processes serialize correctly, but anything that bypasses the binary (e.g. a human directly editing the file) is unsynchronized.
- **No SQLite index.** `channels list` does a per-file scan to compute last-seq and message count. Fine at v1 scale; gets slower if channels grow to many MB.
- **Claude-Code-only identity.** The `SessionStart` hook only fires for Claude Code sessions. Posts from arbitrary shells will work but lack `session_id` and require `--from` every time (no session file to cache into).
- **Agent self-naming, not auto-detected display name.** The agent decides what to call itself via `--from`. There's no integration with worktree managers or terminal IDs — names are an agent-level concept, not an environment-level one.
- **No worktree-rename liveness.** Cached slugs only update when the agent passes `--from` again. Fine, since the agent is the only thing that knows what it's currently doing.

For the design history and full comparison against a daemon-backed alternative, see the TBD repo's design reviews at `docs/superpowers/reviews/2026-05-11-channels-plugin-*.md`.

## Requirements

Python 3.9+. macOS ships Python 3.9+ with the Xcode Command Line Tools; Linux distros from 2021+ are fine. No third-party imports.

## Smoke test

```
bash tests/smoke.sh
```

Runs a full post → read → list → archive → list-archived round-trip against a temp `$HOME` and prints `PASS` on success.

## License

MIT
