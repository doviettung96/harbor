# Repo agent contract

This file is canonical. `CLAUDE.md` is a thin pointer to it.

## Workflow

This repo uses an **agtx-style task board** for tracking work, not beads. Each task = one reviewable PR, runs in its own git worktree + tmux window, and carries explicit acceptance criteria the worker verifies before advancing.

Required flow:

1. **Brainstorm** — explore the problem with the user. Do not create tasks during brainstorming.
2. **Sweep** — invoke `agtx-sweep-with-acceptance`. It asks three questions per task (probe artifact, probe command, runtime target) and embeds answers as `## Acceptance Criteria` / `## Verification Probes` / `## Runtime Target` headers in the task description. No task is created without all three.
3. **Run** — when the user advances a task in agtx, a worker session spawns. The worker invokes `agtx-task-worker` → does the work → invokes `agtx-task-verify` before moving the task to Review.
4. **Review** — user reviews the PR. Resume the worker session if changes are needed.

## Hard rules

- Do NOT call `mcp__agtx__create_task` or `mcp__agtx__create_tasks_batch` from this conversation outside the sweep skill. The sweep flow exists to enforce per-task acceptance.
- Do NOT trust `pytest passed` as evidence a task is done. The task's `## Verification Probes` MUST run via `target-runtime-exec` and exit zero. `agtx-task-verify` is the only path that says "verification passed."
- Do NOT advance a task to Review with a failing probe. Stay in `Running`, append the failure to `<worktree>/.agtx/execute.md`, and either fix the probe or escalate.
- Do NOT bypass `target-runtime-exec` for commands that touch an emulator, ADB device, or game window. The runtime-target probe is a hard gate.
- Do NOT modify `.agtx/runtime-target.json` by hand. Use `python scripts/shared/target_runtime.py target set-...` so the schema is validated.
- Do NOT touch the bead-coupled harbor modules (`beads.py`, `epic.py`, `runner.py`, `mail.py`, `finalize.py`) unless the task is explicitly about removing them. They are deprecated.

## Runtime target

`.agtx/runtime-target.json` declares:
- `mode` — `local` (this machine) or `ssh` (sync + run on a remote host).
- `target.kind` — `local`, `emulator`, `device`, or `game_window`.
- The kind-specific subobject (emulator name + adb_port, device id + kind, game window title pattern, etc.).
- `target.probe_command` — user-defined readiness check. Required when `kind != local`.

The worker reads the active task's `## Runtime Target` and writes a worktree-local override at `<worktree>/.agtx/runtime-target.json` if the task overrides the repo default.

## Tools

- `agtx` MCP server — task board, task management, tmux pane spawning. Tools: `mcp__agtx__list_tasks`, `mcp__agtx__get_task`, `mcp__agtx__create_task`, `mcp__agtx__create_tasks_batch`, `mcp__agtx__update_task`, `mcp__agtx__move_task`, `mcp__agtx__read_pane_content`, `mcp__agtx__send_to_task`, `mcp__agtx__check_conflicts`.
- `target-runtime-exec` skill — runs project commands through `scripts/shared/target_runtime.py`.
- `build-and-test` skill — runs repo-default tests + per-task probes.

## Repo layout

- `harbor/` — Python package (orchestrator, webui, tmux helpers). Non-bead modules are stable; bead-coupled modules are deprecated.
- `skills/` — workflow skills used by Claude Code / Codex.
- `scripts/shared/` — `target_runtime.py`, `probe_target.py`.
- `.agtx/` — runtime-target config (committed default + example).
- `tests/` — harbor's existing 19 mock-based tests.
- `docs/` — Windows tmux notes, phase-1 validation log.
