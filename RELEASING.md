# Releasing

Version is tracked in four places that must stay in lockstep:
- `pyproject.toml` — read by uv/pipx installs.
- `.claude-plugin/plugin.json` — read by Claude Code's plugin manager.
- `.codex-plugin/plugin.json` — read by Codex's plugin manager.
- `package.json` — read by package-bin based plugin installs.

## Release checklist

1. Decide the new version (semver: patch for fixes, minor for new commands, major for breaking changes).
2. Bump `version` in `pyproject.toml`.
3. Bump `version` in `.claude-plugin/plugin.json` to the same value.
4. Bump `version` in `.codex-plugin/plugin.json` to the same value.
5. Bump `version` in `package.json` to the same value.
6. Run `python3 scripts/sync-codex-plugin.py` to refresh `plugins/agent-channels` from root sources.
7. Run `python3 scripts/sync-codex-plugin.py --check` to verify the generated payload is current.
8. Run `bash tests/smoke.sh` — must print PASS.
9. Commit with a conventional message describing the change.
10. Tag: `git tag v<version> && git push origin main --tags`.
11. (Optional) Verify a fresh install: `uv tool install --force git+https://github.com/cheapsteak/agent-channels` then `channels --help`.

The smoke test runs the binary directly from `bin/channels`. The pip-installed binary is the `[project.scripts]` entry point — they should produce identical output. If they diverge, the shim's `sys.path` injection or the entry-point wiring is broken.

`plugins/agent-channels` is a generated Codex marketplace payload. Codex marketplace plugins must live under `./plugins/<plugin-name>` and must contain real files, not symlinks, so keep editing the root files and refresh the payload with `scripts/sync-codex-plugin.py`.
