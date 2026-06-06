# Slack "tee" bridge for channels — design

**Date:** 2026-06-05
**Status:** Approved (design) — ready for implementation plan
**Scope:** One-way mirroring of `agent-channels` messages to Slack channels.

## Summary

Add an opt-in, one-way bridge that mirrors messages posted to selected
`channels` channels into a mapped Slack channel. Posting stays effectively
instant: the `post` command writes locally as today, enqueues a delivery job
to a file spool, and spawns a fully detached worker that delivers to Slack
asynchronously over the Slack Web API. Slack outages never block or fail a
post. There is no always-on daemon — each post is self-healing because the
spool persists undelivered messages until some worker drains them.

One-way only for now: messages flow `channels → Slack`. Replies/threads are out
of scope (the bot-token path leaves the door open later, but we do not read
back).

## Goals

- Mirror new posts on a bridged channel to a mapped Slack channel.
- Keep `post` latency unchanged; never block the agent on the network.
- No persistent daemon, no broker, no server.
- Stay zero-dependency (Python stdlib only) and POSIX-only, matching the
  existing tool.
- No silent failures: delivery problems are observable.

## Non-goals (YAGNI)

- Reading Slack replies / threads / reactions.
- Retroactive backfill of messages posted before a bridge was added.
- Per-bridge message templating / Block Kit formatting.
- Windows support (the project is already POSIX-only: `fcntl`, `flock`).
- Exactly-once delivery (we accept at-least-once).

## Decisions (from brainstorming)

- **Trigger model:** inline enqueue that returns immediately, plus an async
  flush performed by a detached background worker.
- **Slack connection:** bot token via `chat.postMessage` (leaves room for a
  future threaded/two-way path that needs the returned message `ts`).
- **Secret storage:** layered token resolution, first hit wins —
  `$SLACK_BOT_TOKEN` → OS keychain when available → hard fail with a clear
  message. The token is never written to a plaintext config file by default.
- **Mappings:** non-secret `channel → slack channel id` mappings live in
  `~/.agent-channels/bridges.json`, managed via a `channels bridge`
  subcommand group.

## Architecture & data flow

The bridge rides on the existing reader/writer split. Channel writes stay pure
(flock + fsync, unchanged); Slack delivery is a detached, file-spooled side
channel.

### On-disk layout

All under the active data root (`~/.agent-channels/`, or the legacy
`~/.claude/channels/` root when that is the only existing store):

```
bridges.json                 # { "<channel>": {"slack_channel": "C0123", "label": "..."} }
outbox/                      # spool dir — one file per undelivered message
  <chan>.<seq>.<pid>.json    # {slack_channel, text, channel, seq, enqueued_ts}
  <chan>.<seq>.<pid>.json.sending   # transiently claimed entry being delivered
  worker.log                 # delivery errors (append-only; no silent failures)
```

`bridges.json` holds no secrets. The token is never persisted here.

### Post path (stays ~instant)

1. `post` writes the channel JSONL under the existing exclusive flock —
   unchanged from today.
2. After the write, `post` checks whether the canonical channel name is present
   in `bridges.json`. If not, it does nothing further (fast-path regression
   guard: a non-bridged post enqueues nothing and spawns no worker).
3. If bridged, `post` renders the Slack text and writes one spool file into
   `outbox/` via tmp-write + atomic `rename` (no lock, no network). The spool
   record captures the resolved `slack_channel` at enqueue time, so later
   mapping edits do not retroactively change already-queued messages.
4. `post` spawns a fully detached worker and returns immediately:

   ```
   subprocess.Popen(
       [sys.executable, "-m", "agent_channels", "bridge", "flush", "--quiet"],
       stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
       start_new_session=True, close_fds=True,
   )
   ```

   `start_new_session=True` calls `setsid`, detaching the worker from the
   controlling terminal and the agent shell's process group, so the shell never
   waits on it.

### Worker path (the async flush)

`bridge flush` drains the spool:

1. Scan `outbox/*.json`.
2. Claim each entry by atomic `rename` to `<name>.sending`. If the rename loses
   (file already claimed/delivered by another worker), skip it. This makes
   concurrent drain safe without a lock in the common case and prevents
   double-send.
3. Resolve the bot token once (env → keychain). If no token resolves, log to
   `worker.log` and exit without attempting delivery (entries remain as
   `.json` for a future attempt once a token is available).
4. For each claimed entry, call `chat.postMessage` over `urllib`.
   - Success → `unlink` the `.sending` file.
   - Failure → rename back to `.json`, append a line to `worker.log`, retry
     with backoff. Honor HTTP `429` `Retry-After`. After a bounded number of
     attempts, exit and leave remaining entries for the next post's worker.
5. A `.sending` file older than a reclaim timeout (crashed worker) may be
   reclaimed by a later worker, detected via file mtime.

### Why a spool directory (not a single outbox file)

Concurrent posts simply drop new files in — no rewrite race, no lock contention
on enqueue, and claim-by-rename makes concurrent drain safe. It is the
maildir-style pattern and keeps with "files are the source of truth."

### Delivery semantics

At-least-once. A crash between a successful POST and the `unlink` can re-send a
message; Slack does not dedupe, so a rare duplicate is possible. This is
acceptable for one-way mirroring.

## CLI surface

New `bridge` subcommand group. Mappings are non-secret; the token value is
never printed.

```
channels bridge add <channel> <slack-channel-id> [--label <text>]
        # register a one-way mirror; writes to bridges.json

channels bridge remove <channel>
        # remove a mapping

channels bridge list
        # show channel -> slack id + label, and whether a token resolves:
        # "token: set (keychain)" / "set (env)" / "MISSING" — never the value

channels bridge set-token
        # store $SLACK_BOT_TOKEN (or prompt) into the OS keychain when
        # available; otherwise error telling the user to export $SLACK_BOT_TOKEN

channels bridge flush [--quiet]
        # drain the outbox now, in the foreground; also what the detached
        # worker runs. Useful for manual retries and for tests.
```

- `post` gains no new flags. Bridging is transparent, driven by `bridges.json`.
- Forwarding applies only to **new** posts after `bridge add` (no backfill).

### Token resolution (first hit wins)

1. `$SLACK_BOT_TOKEN` environment variable (explicit override; ideal for CI /
   containers with no keyring).
2. OS keychain when available:
   - macOS: `security find-generic-password -w -s <service> -a <account>`
   - Linux: `secret-tool lookup <attrs>` if `secret-tool`/libsecret is present.
3. Otherwise: hard fail with a clear message. The token is never written to a
   plaintext config file by default.

The detached worker inherits `post`'s environment, so an exported
`$SLACK_BOT_TOKEN` carries through automatically.

## Message format

One `chat.postMessage` per channel message. Default rendered text: a context
line (`from` slug · source channel · seq) followed by the body. Example:

```
`auth-rewrite` in #help (#7)
stuck on JWT refresh; anyone seen this?
```

Fixed format for now; no per-bridge templating. The bot-token path leaves room
for Block Kit later.

## Failure visibility

- Delivery errors append to `outbox/worker.log`.
- The failed spool file is retained for retry.
- Signals that something is backed up: a non-empty `outbox/` and
  `bridge list` output. Nothing fails silently.

## Testing

### Seam (no real Slack in tests)

- All HTTP goes through one function `slack_post(token, channel, text)` built on
  `urllib.request`.
- The Slack API base is overridable via `$SLACK_API_BASE` (default
  `https://slack.com/api`). Tests point it at a tiny local stub HTTP server
  (stdlib `http.server`) that records requests and returns canned
  `{"ok": true}` / error / `429` responses.
- `bridge flush` runs the drain synchronously in the foreground, so tests
  exercise enqueue → claim → POST → unlink deterministically without spawning
  detached processes or sleeping.

### Cases to cover

1. `bridge add` then `post` enqueues exactly one spool file with the correct
   `slack_channel` and rendered text.
2. `bridge flush` against the stub delivers it and unlinks the spool file.
3. Stub returns an error / `429` → spool file retained, `worker.log` gets an
   entry, no crash.
4. Concurrent claim: two `flush` runs over the same spool do not double-deliver
   (claim-by-rename).
5. Token resolution order: env beats keychain; missing token → clear failure
   with no network attempt.
6. Non-bridged channel `post` enqueues nothing and spawns no worker
   (fast-path regression guard).

## Build / packaging

- Add `src/agent_channels/__main__.py` so `python -m agent_channels` works —
  the spawn mechanism for the detached worker, independent of how `channels`
  was installed.
- Zero third-party imports remain: `urllib`, `subprocess`, `http.server` are
  all stdlib. POSIX-only is unchanged.

## Open questions / future work

- Optional two-way (threaded replies) using the `ts` returned by
  `chat.postMessage`.
- Per-bridge message templating / Block Kit.
- Outbox retention / pruning policy if delivery stays broken for a long time.
