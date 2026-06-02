# Claude Code instructions for this repo

See `AGENTS.md` for the canonical contract. Highlights:

- This repo uses a **Harbor-style task board**, not beads. Use the Harbor MCP tools (`mcp__harbor__*`).
- Sweep work via `harbor-sweep-with-acceptance` — it gathers per-task acceptance criteria before any task is created.
- Each task gets a worker session in its own tmux window + git worktree. The worker uses `harbor-task-worker` → `harbor-task-verify` to gate moving the task to Review.
- "pytest passed" is not evidence. `harbor-task-verify` runs the build (always, from `harbor.yml` `harbor.build`), then the task's `## Verification Probes` and `## Related Tests` via `target-runtime-exec` — all must exit zero.
- At Review, the agent recommends whether the task's new test joins the permanent suite (`tests/`) or was a one-time gate; the user decides.
- Build lives in `harbor.yml` (`harbor.build`); runtime target lives in `.harbor/runtime-target.json` — don't edit the latter by hand, use `python scripts/shared/target_runtime.py target set-...`.

Read `AGENTS.md` in full before starting any task.
