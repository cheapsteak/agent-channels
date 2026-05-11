#!/usr/bin/env bash
# Smoke test: post -> read -> list -> archive -> list --archived round-trip.
# Uses a temp HOME so it doesn't touch your real ~/.claude/channels.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHANNELS="$REPO_ROOT/bin/channels"
TMP="$(mktemp -d -t agent-channels-smoke.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

export HOME="$TMP"
export CLAUDE_SESSION_ID="test-session-$$"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

step() {
    echo "--- $*"
}

step "post #1 (requires --from)"
out="$("$CHANNELS" post --from smoke-test '#help' 'first message' 2>&1)" \
    || fail "post #1 errored: $out"
echo "$out" | grep -q '^help #1' || fail "post #1 unexpected output: $out"

step "post #2 (slug should be cached, no --from needed)"
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
test ! -f "$HOME/.claude/channels/help.jsonl" || fail "archive left original file"

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
unset CLAUDE_SESSION_ID
if "$CHANNELS" post anonymous 'no slug' 2>/dev/null; then
    fail "post without --from or cached slug should error"
fi

step "body cap enforced"
big=$(python3 -c 'print("x" * (64*1024 + 1), end="")')
if echo -n "$big" | "$CHANNELS" post --from smoke-test help - 2>/dev/null; then
    fail "oversized body should be rejected"
fi

echo
echo "PASS"
