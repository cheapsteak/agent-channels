#!/usr/bin/env bash
# Smoke test: post -> read -> list -> archive -> list --archived round-trip.
# Uses temp HOME directories so it doesn't touch your real channel stores.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHANNELS="$REPO_ROOT/bin/channels"
TMP="$(mktemp -d -t agent-channels-smoke.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP/fresh"
mkdir -p "$HOME"
unset CODEX_THREAD_ID
export CLAUDE_CODE_SESSION_ID="test-uuid-12345"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

step() {
    echo "--- $*"
}

step "Codex marketplace payload is synced from root sources"
python3 "$REPO_ROOT/scripts/sync-codex-plugin.py" --check \
    || fail "Codex marketplace payload is out of sync"

step "no session file exists before first post (lazy-init)"
test ! -e "$HOME/.agent-channels/sessions/test-uuid-12345.json" \
    || fail "session file should not exist before first post"

step "post #1 (requires --from)"
out="$("$CHANNELS" post --from smoke-test '#help' 'first message' 2>&1)" \
    || fail "post #1 errored: $out"
echo "$out" | grep -q '^help #1' || fail "post #1 unexpected output: $out"

step "lazy-init created session file with {from, last_post_ts}"
sess="$HOME/.agent-channels/sessions/test-uuid-12345.json"
test -f "$sess" || fail "session file was not created by lazy-init: $sess"
test ! -e "$HOME/.claude/channels" || fail "fresh install should use neutral root"
python3 -c "
import json, sys
d = json.load(open('$sess'))
assert d.get('from') == 'smoke-test', d
assert isinstance(d.get('last_post_ts'), str) and d['last_post_ts'], d
assert 'cwd' not in d and 'source' not in d and 'started_at' not in d, d
" || fail "session file shape wrong: $(cat "$sess")"

step "post #2 (slug should be cached from session file, no --from needed)"
out="$("$CHANNELS" post help 'second message' 2>&1)" \
    || fail "post #2 errored: $out"
echo "$out" | grep -q '^help #2' || fail "post #2 unexpected output: $out"

step "post via stdin"
echo -n "third message (stdin)" \
    | "$CHANNELS" post help - >/dev/null \
    || fail "stdin post errored"

step "read --limit 10"
out="$("$CHANNELS" read help --limit 10)"
echo "$out" | grep -q 'first message' || fail "read missing #1"
echo "$out" | grep -q 'second message' || fail "read missing #2"
echo "$out" | grep -q 'third message (stdin)' || fail "read missing stdin post"

step "read --seq 2"
out="$("$CHANNELS" read help --seq 2)"
echo "$out" | grep -q 'second message' || fail "read --seq 2 missing #2"
echo "$out" | grep -q 'first message' && fail "read --seq 2 leaked #1"

step "read --since 2 (should show #3 only)"
out="$("$CHANNELS" read help --since 2)"
echo "$out" | grep -q 'third message' || fail "read --since 2 missing #3"
echo "$out" | grep -q 'second message' && fail "read --since 2 leaked #2"

step "tail (latest message)"
out="$("$CHANNELS" tail help)"
echo "$out" | grep -q 'third message' || fail "tail missing latest"

step "list (active)"
out="$("$CHANNELS" list)"
echo "$out" | grep -q '^help ' || fail "list missing help channel"

step "archive"
"$CHANNELS" archive help >/dev/null || fail "archive errored"
test ! -f "$HOME/.agent-channels/help.jsonl" || fail "archive left original file"

step "list --archived"
out="$("$CHANNELS" list --archived)"
echo "$out" | grep -q '^help-' || fail "list --archived missing archived help"

step "tail on missing channel must error"
if "$CHANNELS" tail help 2>/dev/null; then
    fail "tail should error on missing channel"
fi

step "post after archive starts fresh at seq 1"
out="$("$CHANNELS" post --from smoke-test help 'fresh start')"
echo "$out" | grep -q '^help #1' || fail "post after archive did not reset seq: $out"

step "invalid channel name rejected"
if "$CHANNELS" post --from smoke-test 'Bad Name!' 'x' 2>/dev/null; then
    fail "invalid name should be rejected"
fi

step "reserved name rejected"
if "$CHANNELS" post --from smoke-test '_archive' 'x' 2>/dev/null; then
    fail "_archive should be rejected"
fi

step "leading # stripped (#help == help)"
"$CHANNELS" post --from smoke-test '#help' 'with hash' >/dev/null \
    || fail "#help post failed"
out="$("$CHANNELS" read help --limit 5)"
echo "$out" | grep -q 'with hash' || fail "#help did not write to 'help'"

step "first post without --from errors"
unset CLAUDE_CODE_SESSION_ID
if "$CHANNELS" post anonymous 'no slug' 2>/dev/null; then
    fail "post without --from or cached slug should error"
fi

step "body cap enforced"
big=$(python3 -c 'print("x" * (64*1024 + 1), end="")')
if echo -n "$big" | "$CHANNELS" post --from smoke-test help - 2>/dev/null; then
    fail "oversized body should be rejected"
fi

# ---------- watch ----------

export CLAUDE_CODE_SESSION_ID="test-uuid-12345"

step "watch: timeout exits 2 when no new messages"
high=$("$CHANNELS" list | awk '/^help / {print $2}')
out_file="$TMP/watch_timeout.out"
err_file="$TMP/watch_timeout.err"
set +e
"$CHANNELS" watch help --since "$high" --timeout 1 --poll-interval 0.1 \
    >"$out_file" 2>"$err_file"
rc=$?
set -e
[ "$rc" -eq 2 ] || fail "watch timeout expected rc=2, got $rc"
grep -q 'timeout' "$err_file" || fail "watch timeout missing 'timeout' on stderr"
[ ! -s "$out_file" ] || fail "watch timeout should have empty stdout, got: $(cat "$out_file")"

step "watch: fires when a new message arrives"
"$CHANNELS" post --from smoke-test watchable 'baseline' >/dev/null
baseline_high=$("$CHANNELS" list | awk '/^watchable / {print $2}')
out_file="$TMP/watch_fire.out"
(
    sleep 0.4
    "$CHANNELS" post --from smoke-poster watchable 'fired!' >/dev/null
) &
poster_pid=$!
set +e
"$CHANNELS" watch watchable --since "$baseline_high" --timeout 5 --poll-interval 0.1 \
    >"$out_file" 2>&1
rc=$?
set -e
wait "$poster_pid" 2>/dev/null || true
[ "$rc" -eq 0 ] || fail "watch should exit 0 on new message, got $rc; out: $(cat "$out_file")"
grep -q 'fired!' "$out_file" || fail "watch output missing new message: $(cat "$out_file")"
grep -q '^\[watchable\]' "$out_file" || fail "watch output missing [channel] prefix: $(cat "$out_file")"

step "watch: fires on whichever of multiple channels writes first"
"$CHANNELS" post --from smoke-test multi-a 'a-baseline' >/dev/null
"$CHANNELS" post --from smoke-test multi-b 'b-baseline' >/dev/null
out_file="$TMP/watch_multi.out"
(
    sleep 0.4
    "$CHANNELS" post --from smoke-poster multi-b 'b-new' >/dev/null
) &
poster_pid=$!
set +e
"$CHANNELS" watch multi-a multi-b --timeout 5 --poll-interval 0.1 \
    >"$out_file" 2>&1
rc=$?
set -e
wait "$poster_pid" 2>/dev/null || true
[ "$rc" -eq 0 ] || fail "multi-channel watch should exit 0; out: $(cat "$out_file")"
grep -q 'b-new' "$out_file" || fail "multi-channel watch missing trigger msg: $(cat "$out_file")"
grep -q '^\[multi-b\]' "$out_file" || fail "multi-channel watch missing [multi-b] prefix"
grep -q '^\[multi-a\]' "$out_file" && fail "multi-channel watch leaked a-channel msg: $(cat "$out_file")"

step "watch: pre-existing messages past --since fire immediately"
"$CHANNELS" post --from smoke-test instant 'msg-1' >/dev/null
"$CHANNELS" post --from smoke-test instant 'msg-2' >/dev/null
out_file="$TMP/watch_instant.out"
set +e
"$CHANNELS" watch instant --since 0 --timeout 5 --poll-interval 0.1 \
    >"$out_file" 2>&1
rc=$?
set -e
[ "$rc" -eq 0 ] || fail "watch with backlog should exit 0 immediately, got $rc"
grep -q 'msg-1' "$out_file" || fail "watch missing backlog msg-1"
grep -q 'msg-2' "$out_file" || fail "watch missing backlog msg-2"
step "legacy Claude root is used when it is the only existing store"
export HOME="$TMP/legacy"
mkdir -p "$HOME/.claude/channels"
unset CODEX_THREAD_ID
export CLAUDE_CODE_SESSION_ID="legacy-session-1"
out="$("$CHANNELS" post --from legacy-agent legacy 'legacy message' 2>&1)" \
    || fail "legacy post errored: $out"
echo "$out" | grep -q '^legacy #1' || fail "legacy post unexpected output: $out"
test -f "$HOME/.claude/channels/legacy.jsonl" \
    || fail "legacy root should receive message"
test ! -e "$HOME/.agent-channels" \
    || fail "neutral root should not be created when legacy root is the only existing store"

step "neutral root wins when both neutral and legacy roots exist"
mkdir -p "$HOME/.agent-channels"
out="$("$CHANNELS" post --from neutral-agent neutral 'neutral message' 2>&1)" \
    || fail "neutral post errored: $out"
echo "$out" | grep -q '^neutral #1' || fail "neutral post unexpected output: $out"
test -f "$HOME/.agent-channels/neutral.jsonl" \
    || fail "neutral root should receive message when both roots exist"
test ! -f "$HOME/.claude/channels/neutral.jsonl" \
    || fail "legacy root should not receive neutral-root message"

step "Codex thread id creates cached session"
export HOME="$TMP/codex"
mkdir -p "$HOME"
export CODEX_THREAD_ID="codex-thread-123"
unset CLAUDE_CODE_SESSION_ID
out="$("$CHANNELS" post --from codex-agent codex 'codex message' 2>&1)" \
    || fail "codex post errored: $out"
echo "$out" | grep -q '^codex #1' || fail "codex post unexpected output: $out"
test -f "$HOME/.agent-channels/sessions/codex-thread-123.json" \
    || fail "Codex session file was not created"
out="$("$CHANNELS" post codex 'cached codex message' 2>&1)" \
    || fail "codex cached post errored: $out"
echo "$out" | grep -q '^codex #2' || fail "codex cached post unexpected output: $out"

step "Codex thread id takes precedence over Claude session id"
export HOME="$TMP/precedence"
mkdir -p "$HOME"
export CODEX_THREAD_ID="codex-wins"
export CLAUDE_CODE_SESSION_ID="claude-loses"
out="$("$CHANNELS" post --from precedence-agent precedence 'precedence message' 2>&1)" \
    || fail "precedence post errored: $out"
echo "$out" | grep -q '^precedence #1' || fail "precedence post unexpected output: $out"
test -f "$HOME/.agent-channels/sessions/codex-wins.json" \
    || fail "Codex precedence session file was not created"
test ! -f "$HOME/.agent-channels/sessions/claude-loses.json" \
    || fail "Claude session should not be used when Codex thread id is set"

# ---------- edit ----------

step "edit: post a message then edit it"
"$CHANNELS" post --from smoke-test editable 'original body' >/dev/null \
    || fail "edit setup post failed"
out="$("$CHANNELS" edit editable 1 'updated body' 2>&1)" \
    || fail "edit errored: $out"
echo "$out" | grep -q 'editable #2 (edit of #1)' \
    || fail "edit output wrong: $out"

step "edit: read default shows latest body + (edited) marker, edit record hidden"
out="$("$CHANNELS" read editable)"
echo "$out" | grep -q 'updated body (edited)' \
    || fail "read default missing folded edit: $out"
echo "$out" | grep -q 'original body' \
    && fail "read default leaked stale original body: $out"
echo "$out" | grep -q 'edit of #1' \
    && fail "read default leaked raw edit record: $out"

step "edit: read --seq 1 shows folded (latest body + marker)"
out="$("$CHANNELS" read editable --seq 1)"
echo "$out" | grep -q 'updated body (edited)' \
    || fail "read --seq 1 missing folded body: $out"

step "edit: read --seq 2 shows the raw edit record"
out="$("$CHANNELS" read editable --seq 2)"
echo "$out" | grep -q 'edit of #1: updated body' \
    || fail "read --seq 2 missing edit-form: $out"

step "edit: tail shows the edit as edit-form"
out="$("$CHANNELS" tail editable)"
echo "$out" | grep -q 'edit of #1: updated body' \
    || fail "tail did not show edit-form: $out"

step "edit: multiple edits — latest body wins, marker stays"
"$CHANNELS" edit editable 1 'third body' >/dev/null || fail "second edit failed"
out="$("$CHANNELS" read editable --seq 1)"
echo "$out" | grep -q 'third body (edited)' \
    || fail "second edit not reflected: $out"

step "edit: --since surfaces edit of an older message"
# editable has: #1 orig, #2 edit-of-1, #3 edit-of-1. Ask for --since 2.
out="$("$CHANNELS" read editable --since 2)"
echo "$out" | grep -q 'edit of #1: third body' \
    || fail "--since 2 missing edit notification for old msg: $out"
# original #1 should NOT appear (its seq is 1, not > 2)
echo "$out" | grep -E '^#1 ' && fail "--since 2 leaked original #1: $out"

step "edit: reject non-existent seq"
if "$CHANNELS" edit editable 999 'nope' 2>/dev/null; then
    fail "edit should reject non-existent seq"
fi

step "edit: reject editing an edit record"
if "$CHANNELS" edit editable 2 'nope' 2>/dev/null; then
    fail "edit should reject editing an edit record"
fi

step "edit: reject seq < 1"
if "$CHANNELS" edit editable 0 'nope' 2>/dev/null; then
    fail "edit should reject seq 0"
fi

step "edit: errors when channel does not exist"
if "$CHANNELS" edit ghost-channel 1 'nope' 2>/dev/null; then
    fail "edit should error on missing channel"
fi

step "edit: body via stdin"
echo -n "stdin edit" | "$CHANNELS" edit editable 1 - >/dev/null \
    || fail "stdin edit failed"
out="$("$CHANNELS" read editable --seq 1)"
echo "$out" | grep -q 'stdin edit (edited)' \
    || fail "stdin edit body not reflected: $out"

step "edit: body cap enforced"
big=$(python3 -c 'print("x" * (64*1024 + 1), end="")')
if echo -n "$big" | "$CHANNELS" edit editable 1 - 2>/dev/null; then
    fail "oversized edit body should be rejected"
fi

step "edit: watch fires on edit and prints edit-form"
"$CHANNELS" post --from smoke-test watch-edit 'baseline' >/dev/null
baseline_high=$("$CHANNELS" list | awk '/^watch-edit / {print $2}')
out_file="$TMP/watch_edit.out"
(
    sleep 0.4
    "$CHANNELS" edit watch-edit 1 'edited!' >/dev/null
) &
poster_pid=$!
set +e
"$CHANNELS" watch watch-edit --since "$baseline_high" --timeout 5 --poll-interval 0.1 \
    >"$out_file" 2>&1
rc=$?
set -e
wait "$poster_pid" 2>/dev/null || true
[ "$rc" -eq 0 ] || fail "watch on edit should exit 0, got $rc; out: $(cat "$out_file")"
grep -q 'edit of #1: edited!' "$out_file" \
    || fail "watch did not print edit-form: $(cat "$out_file")"

echo
echo "PASS"
