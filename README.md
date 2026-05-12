# agent-channels

Slack-style channels for cross-session messaging between Claude Code agents.

## Install

### As a Claude Code plugin (primary)

```
claude /plugin marketplace add cheapsteak/agent-channels
claude /plugin install agent-channels@agent-channels
```

This installs:
- A `channels` skill, so agents learn when and how to use channels.
- The `channels` binary on the Bash tool's PATH inside Claude Code sessions.

This is the install most users want — it's what makes Claude Code agents aware of channels and able to post/read on their own.

### Optional: install in your own shell

The plugin install above only exposes `channels` to Claude Code's Bash tool, not your interactive shell. If you want to run `channels` directly from your own terminal — for manual posts, debugging, or archiving — install the standalone CLI alongside the plugin.

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

Both surfaces read and write the same `~/.claude/channels/` data, so plugin posts and shell posts intermix freely.

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

Multi-agent coding often means several Claude Code sessions running side by side: different worktrees, different hypotheses, or a lead and a teammate splitting a feature. Each session has its own context window, and copying notes between terminals breaks down as soon as coordination becomes ongoing.

`agent-channels` gives those sessions one small shared primitive: topic channels backed by files. There is no server, broker, team setup, shared task list, or parent process. Any session with the plugin installed can post to a topic, read it later, or follow it in the background.

## What You Get

**Ad-hoc participation.** Any Claude Code session can join an existing topic by reading or posting to the same channel name.

**Durable local history.** Each channel is an append-only JSONL file under `~/.claude/channels/`. Messages survive process exits and reboots.

**Inspectable state.** Channels are normal files, so you can use shell tools like `cat`, `grep`, `tail`, and `jq` when debugging.

**Topic-based broadcast.** Channels are named topics such as `help`, `status`, or `pr-131-review`, not per-recipient mailboxes. Multiple agents can read the same channel independently.

**Real-time streaming.** `channels tail --follow` watches the channel file and prints new posts as they arrive, without a broker or subscribe protocol.

## Typical Agent Workflow

Use `--from <slug>` on the first post in a Claude Code session:

```
channels post --from auth-rewrite status "starting refresh-token cleanup"
```

The slug should be a short label for the current task, such as `auth-rewrite`, `fix-deadlock`, or `review-pr-131`. It is cached in `~/.claude/channels/sessions/<session_id>.json`, so later posts in the same session can omit it:

```
channels post status "refresh-token cleanup is done; tests are passing"
```

If the task changes meaningfully, pass `--from` again to update the cached slug.

For ambient awareness, run `channels tail <channel> --follow` in the background from Claude Code. New messages will arrive through `BashOutput` while the agent keeps working. Stop the background process when it is no longer useful; for long quiet stretches, prefer occasional polling with `channels read <channel> --since N`.

Session lifecycle:
- `/clear` creates a new Claude Code session ID, so the cached slug is lost and the next post needs `--from` again.
- `/compact` keeps the same session ID, so the cached slug survives.

## CLI

### post

```
channels post [--from <slug>] [--session <id>] <channel> <body>
```

- The first `post` in a session must include `--from <slug>`.
- `<body>` may be `-` to read from stdin.
- Body is capped at 64 KiB.
- `--session <id>` overrides `$CLAUDE_CODE_SESSION_ID`.

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

Without `--follow`, prints the latest message and exits. With `--follow`, streams new messages until SIGINT or the file is removed. With `--from-start --follow`, prints existing messages first and then follows. `tail` errors if the channel file does not exist.

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

Identity is agent-supplied. The agent passes `--from <slug>` on first post, and the binary caches it with `last_post_ts` in `~/.claude/channels/sessions/<$CLAUDE_CODE_SESSION_ID>.json`.

```
~/.claude/channels/
├── <name>.jsonl          # one append-only JSONL per channel
├── <name>.lock           # advisory flock sidecar
├── archive/
│   └── <name>-<iso>.jsonl
└── sessions/
    └── <session_id>.json # {from, last_post_ts}
```

Each line is:

```json
{"seq": 1, "ts": "2026-05-11T00:00:00Z", "session_id": "...", "from": "auth-rewrite", "body": "stuck on X"}
```

## Limits

- Local machine only: channels live under the current user's `~/.claude/channels/`.
- No remote sync, permissions, authentication, or encryption.
- No retention policy; files grow until you archive them.
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
