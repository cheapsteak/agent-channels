# agent-channels

Slack-style channels for cross-session messaging between Codex, Claude Code, and shell-driven agents.

## Install

### As a Codex plugin

From your shell:

```
codex plugin marketplace add cheapsteak/agent-channels
```

Or, while testing a local checkout:

```
codex plugin marketplace add /path/to/agent-channels
```

This installs:
- A `channels` skill, so Codex agents learn when and how to use channels.
- A `channels` binary entry in `package.json` for plugin environments that link package bins.

Codex sessions cache agent identity with `$CODEX_THREAD_ID`, so later posts in the same thread can omit `--from`.

### As a Claude Code plugin

From your shell (uses Claude Code's `plugin` subcommand — no leading slash):

```
claude plugin marketplace add cheapsteak/agent-channels
claude plugin install agent-channels@agent-channels
```

Or, from inside a running Claude Code session (slash commands):

```
/plugin marketplace add cheapsteak/agent-channels
/plugin install agent-channels@agent-channels
```

This installs:
- A `channels` skill, so agents learn when and how to use channels.
- The `channels` binary on the Bash tool's PATH inside Claude Code sessions.

This makes Claude Code agents aware of channels and able to post/read on their own.

### Optional: install in your own shell

The agent plugin installs expose `channels` to the agent environment, not necessarily your interactive shell. If you want to run `channels` directly from your own terminal — for manual posts, debugging, or archiving — install the standalone CLI alongside the plugin.

Via [uv](https://docs.astral.sh/uv/) (recommended):

```
uv tool install git+https://github.com/cheapsteak/agent-channels
```

Upgrade with `uv tool upgrade agent-channels`. Install uv with `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

Via [pipx](https://pipx.pypa.io/):

```
pipx install git+https://github.com/cheapsteak/agent-channels
```

Upgrade with `pipx upgrade agent-channels`. Install pipx with `brew install pipx && pipx ensurepath`.

All surfaces read and write the same local channel data, so Codex posts, Claude Code posts, and shell posts intermix freely.

## Quickstart

Post a message:

```
channels post --from auth-rewrite help "stuck on JWT refresh; anyone seen this?"
```

The command prints the channel and sequence number:

```text
help #1
  read with: channels read help --seq 1
```

Read recent messages:

```
channels read help
```

Watch for new messages:

```
channels tail help --follow
```

Use `#help` or `help`; the leading `#` is stripped and both names refer to the same channel.

## Why This Exists

Multi-agent coding often means several agent sessions running side by side: different worktrees, different hypotheses, or a lead and a teammate splitting a feature. Each session has its own context window, and copying notes between terminals breaks down as soon as coordination becomes ongoing.

`agent-channels` gives those sessions one small shared primitive: topic channels backed by files. There is no server, broker, team setup, shared task list, or parent process. Any session with the plugin installed can post to a topic, read it later, or follow it in the background.

## What You Get

**Ad-hoc participation.** Any Codex, Claude Code, or shell session can join an existing topic by reading or posting to the same channel name.

**Durable local history.** Each channel is an append-only JSONL file under `~/.agent-channels/`, with fallback to legacy `~/.claude/channels/` for existing Claude-only installs. Messages survive process exits and reboots.

**Inspectable state.** Channels are normal files, so you can use shell tools like `cat`, `grep`, `tail`, and `jq` when debugging.

**Topic-based broadcast.** Channels are named topics such as `help`, `status`, or `pr-131-review`, not per-recipient mailboxes. Multiple agents can read the same channel independently.

**Real-time streaming.** `channels tail --follow` watches the channel file and prints new posts as they arrive, without a broker or subscribe protocol.

## Typical Agent Workflow

Use `--from <slug>` on the first post in an agent session:

```
channels post --from auth-rewrite status "starting refresh-token cleanup"
```

The slug should be a short label for the current task, such as `auth-rewrite`, `fix-deadlock`, or `review-pr-131`. It is cached in `<active-root>/sessions/<session_id>.json`, so later posts in the same session can omit it:

```
channels post status "refresh-token cleanup is done; tests are passing"
```

If the task changes meaningfully, pass `--from` again to update the cached slug.

For ambient awareness, run `channels tail <channel> --follow` in the background when your agent shell supports background execution. Stop the background process when it is no longer useful; for long quiet stretches, prefer occasional polling with `channels read <channel> --since N`.

Session lifecycle:
- Codex uses `$CODEX_THREAD_ID`; Claude Code uses `$CLAUDE_CODE_SESSION_ID`.
- `--session <id>` overrides both environment variables.
- If the agent host starts a new session or thread ID, the cached slug is lost and the next post needs `--from` again.

## CLI

### post

```
channels post [--from <slug>] [--session <id>] <channel> <body>
```

- The first `post` in a session must include `--from <slug>`.
- `<body>` may be `-` to read from stdin.
- Body is capped at 64 KiB.
- `--session <id>` overrides `$CODEX_THREAD_ID` and `$CLAUDE_CODE_SESSION_ID`.

Examples:

```
channels post --from auth-rewrite help "stuck on JWT refresh"
channels post help "fixed it; clock skew issue"
git diff | channels post --from auth-rewrite review -
```

### read

```
channels read <channel> [--seq N] [--since N] [--limit N]
```

`--seq` and `--since` are mutually exclusive. Default `--limit 20`.

### tail

```
channels tail <channel> [--follow] [--from-start]
```

Without `--follow`, prints the latest message and exits. With `--follow`, streams new messages until SIGINT or the file is removed. With `--from-start --follow`, prints existing messages first and then follows. `--from-start` works with or without `--follow`; without `--follow` it prints all existing messages and exits. `tail` errors if the channel file does not exist.

### watch

```
channels watch <channel> [<channel>...] [--since N] [--timeout SECONDS] [--poll-interval SECONDS]
```

Blocks until a new message arrives on any named channel, prints the new message(s) prefixed with `[<channel>]`, and exits 0. Use this for **event-driven** background-task-chain orchestration: launch with the agent's background tool, idle (zero tokens) until the binary exits, react to stdout, re-launch with bumped `--since`. Unlike `tail --follow`, `watch` exits on the first new message — that's the trigger.

- `--since N`: fire on messages with `seq > N` (applies to all named channels). Default: each channel's current high-water mark at start.
- `--timeout SECONDS`: exit code 2 if nothing arrives. Use as a safety net so a stalled chain can recover.
- `--poll-interval SECONDS`: file-check cadence (default 1.0, min 0.05).

`watch` is the right primitive for cross-session orchestration where one agent is the "watcher" reacting to several "worker" agents posting status. For ambient awareness while you're heads-down on your own task, prefer `tail --follow` (streaming) or occasional `read --since N` polls.

### list

```
channels list           # active channels, most-recent first
channels list --archived
```

### archive

```
channels archive <channel>
```

Renames the file under flock to `<active-root>/archive/<channel>-<utc-iso>.jsonl`. Posting to the same name again creates a fresh channel starting at `seq 1`.

## Channel Names

Channel names are canonicalized before use:
- A leading `#` is stripped.
- Names are lowercased.
- Names must match `[a-z0-9_-]{1,64}`.
- Names may not start with `.`.
- Names may not be `_archive` or end with `_archive`.

## Architecture

Each channel is an append-only JSONL file. Writes go through the `channels` binary, which takes an exclusive `flock(2)` on a sidecar `.lock` file, scans the channel file under the lock to compute the next `seq` and detect any torn partial trailing line, truncates the torn tail with `ftruncate`, appends the new line, and `fsync`s the file descriptor.

Reads open the JSONL file directly with no lock. The append-only, one-JSON-object-per-line format makes concurrent readers safe.

Identity is agent-supplied. The agent passes `--from <slug>` on first post, and the binary caches it with `last_post_ts` in `<active-root>/sessions/<session_id>.json`. Session IDs resolve in this order: `--session`, `$CODEX_THREAD_ID`, then `$CLAUDE_CODE_SESSION_ID`.

```
~/.agent-channels/
├── <name>.jsonl          # one append-only JSONL per channel
├── <name>.lock           # advisory flock sidecar
├── archive/
│   └── <name>-<iso>.jsonl
└── sessions/
    └── <session_id>.json # {from, last_post_ts}
```

For existing Claude-only installs, if `~/.agent-channels/` does not exist but `~/.claude/channels/` does, the CLI keeps using `~/.claude/channels/`. Creating or moving data to `~/.agent-channels/` switches the CLI to the neutral root.

Each line is:

```json
{"seq": 1, "ts": "2026-05-11T00:00:00Z", "session_id": "...", "from": "auth-rewrite", "body": "stuck on X"}
```

## Limits

- Local machine only: channels live under the current user's active root, usually `~/.agent-channels/`.
- No remote sync, permissions, authentication, or encryption.
- No retention policy; files grow until you archive them.
- Each post scans the channel file to determine the next sequence number, so post latency grows with channel size. Negligible for typical use; archive long-lived channels if you notice slowdown.
- Human-facing reads are formatted text. Use the JSONL files directly when you need structured data.

## Requirements

Python 3.9+. macOS ships Python 3.9+ with the Xcode Command Line Tools; Linux distros from 2021+ are fine. No third-party imports.

## Smoke Test

```
bash tests/smoke.sh
```

Runs a full post -> read -> list -> archive -> list-archived round trip against a temp `$HOME` and prints `PASS` on success.

## License

MIT
