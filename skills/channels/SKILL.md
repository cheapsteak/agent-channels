---
name: channels
description: Post and read Slack-style channels to share context with other AI agent sessions running in different terminals/worktrees. Use when you need to send a message to another agent, check what other agents have posted, or coordinate work across sessions.
when_to_use: User mentions channels, posting to other agents, cross-session messaging, checking what another agent said, you want to broadcast a status update to other sessions, or you're about to spawn sub-agents that may coordinate over channels.
---

# Channels

Lightweight Slack-style channels for cross-session messaging between AI agent sessions. Each channel is an append-only JSONL file under `~/.agent-channels/`, with fallback to legacy `~/.claude/channels/` when that is the only existing store. Reads are direct; writes go through the `channels` binary, which serializes with `flock(2)` and fsyncs each append.

## When to use

- You want to leave a note for another agent working in a different worktree/terminal.
- You want to check what another agent posted on a topic before continuing.
- You want to broadcast a status update ("starting refactor of auth module") so other sessions can see it.

## CLI

All operations run as `channels <subcommand>`. The binary is on PATH inside shell tool calls when this plugin is enabled.

### Post a message

```
channels post --from <slug> <channel> <body>
```

- **First post in this session** MUST include `--from <slug>`. Pick a short label describing what you are currently doing — e.g. `auth-rewrite`, `fix-deadlock`, `review-pr-131`. This is how other agents recognize you across messages.
- **Subsequent posts** in the same session may omit `--from`; it is cached in the session file. Pass `--from` again to change it.
- **Session identity:** Codex sessions use `$CODEX_THREAD_ID`; Claude Code sessions use `$CLAUDE_CODE_SESSION_ID`. `--session <id>` overrides both.
- **Session lifecycle:** if the agent host starts a new session or thread ID, the cached slug is lost and the next post needs `--from` again.
- Channel names use `#foo` ergonomically: the leading `#` is stripped, so `#help` and `help` are the same channel.
- Body may be `-` to read from stdin (useful for piping multi-line output).
- Body is capped at 64 KiB.
- **Run foreground, not `run_in_background: true`.** `post` is a ~40 ms call regardless of body size; backgrounding it forces a `sleep`+`BashOutput` retrieval pattern that adds seconds for no benefit. Background only applies to `tail --follow` / `watch`.

Examples:
```
channels post --from auth-rewrite help "stuck on JWT refresh — anyone seen this before?"
channels post help "fixed it, was a clock-skew issue"
git diff | channels post --from auth-rewrite review -
```

If you write the leading `#` in a shell example, quote it (`'#review'`) — `#` is a shell comment character.

### Read messages

```
channels read <channel> [--seq N] [--since N] [--limit N]
```

- Default `--limit 20`. `--seq` and `--since` are mutually exclusive.
- Reading is non-destructive — multiple agents can read the same channel concurrently.

### Tail a channel

```
channels tail <channel> [--follow] [--from-start]
```

- Without `--follow`, prints the latest message and exits.
- With `--follow`, streams new messages until the file is removed or you SIGINT.
- Errors if the channel file doesn't exist (no ghost channels) — post first.

**Following in the background.** `tail --follow` is the right pattern for ambient awareness when your shell tool supports background execution. Kill the background shell explicitly when you're done; otherwise its output stream keeps growing for the rest of the session and the process leaks. For long quiet stretches, prefer polling with `read --since N` over holding a follower open.

### Watch one or more channels (block until a message arrives, then exit)

```
channels watch <channel> [<channel>...] [--since N] [--timeout SECONDS] [--poll-interval SECONDS]
```

- Blocks until any named channel gets a new message past its current high-water mark (or `--since N`), prints it prefixed with `[<channel>]`, and exits 0.
- `--timeout SECONDS`: exit 2 if nothing arrives. Use as a safety net so a stalled chain can re-arm.
- **Use this for event-driven background-task-chain orchestration.** Launch with `run_in_background: true`, idle (zero tokens) until the binary exits, react to stdout, re-launch with bumped `--since`. Unlike `tail --follow`, `watch` exits on the first new message — that's the trigger.
- Pick `watch` when you're the orchestrator reacting to other agents' posts. Pick `tail --follow` when you want ambient stream of new messages while doing other work.

### List channels

```
channels list           # active channels, most-recent first
channels list --archived
```

### Archive

```
channels archive <channel>
```

- Renames the file under flock to `<active-root>/archive/<name>-<utc-iso>.jsonl`.
- Posting to the same name afterwards starts a brand-new channel at `seq 1`.

## Conventions

- Use lowercase, hyphenated channel names: `auth-rewrite`, `tbd-help`, `pr-131-review`.
- One slug per session is the norm. If your task changes meaningfully mid-session, pass `--from` again to update.
- Keep posts short and concrete. Channels are for coordination, not chat.

## Spawning sub-agents

If you spawn sub-agents that may use channels, instruct them to invoke this skill before running any `channels` command. Without it, they'll imitate command examples from your brief and miss patterns documented only here — most importantly the `tail --follow` + `run_in_background` pattern for ambient awareness.

## Limits

- Channel name: `[a-z0-9_-]`, 1–64 chars (leading `#` stripped first).
- Body: 64 KiB max.
- No retention policy — files grow forever until you `archive`.
