# Releasing

Version is tracked in two places that must stay in lockstep:
- `pyproject.toml` — read by uv/pipx installs.
- `.claude-plugin/plugin.json` — read by Claude Code's plugin manager.

## Release checklist

1. Decide the new version (semver: patch for fixes, minor for new commands, major for breaking changes).
2. Bump `version` in `pyproject.toml`.
3. Bump `version` in `.claude-plugin/plugin.json` to the same value.
4. Run `bash tests/smoke.sh` — must print PASS.
5. Commit with a conventional message describing the change.
6. Tag: `git tag v<version> && git push origin main --tags`.
7. (Optional) Verify a fresh install: `uv tool install --force git+https://github.com/cheapsteak/agent-channels` then `channels --help`.

The smoke test runs the binary directly from `bin/channels`. The pip-installed binary is the `[project.scripts]` entry point — they should produce identical output. If they diverge, the shim's `sys.path` injection or the entry-point wiring is broken.
