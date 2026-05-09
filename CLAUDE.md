# Claude Code instructions for this repo

See `AGENTS.md` for the canonical contract. Highlights:

- This repo uses an **agtx-style task board**, not beads. Use the agtx MCP tools (`mcp__agtx__*`).
- Sweep work via `agtx-sweep-with-acceptance` — it gathers per-task acceptance criteria before any task is created.
- Each task gets a worker session in its own tmux window + git worktree. The worker uses `agtx-task-worker` → `agtx-task-verify` to gate moving the task to Review.
- "pytest passed" is not evidence. The task's `## Verification Probes` must run and pass via `target-runtime-exec`.
- Runtime target lives in `.agtx/runtime-target.json`. Don't edit it by hand — use `python scripts/shared/target_runtime.py target set-...`.

Read `AGENTS.md` in full before starting any task.
