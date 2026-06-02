# Repo agent contract

This file is canonical. `CLAUDE.md` is a thin pointer to it.

## Workflow

This repo uses a **Harbor-style task board** for tracking work, not beads. Each task = one reviewable PR, runs in its own git worktree + tmux window, and carries explicit acceptance criteria the worker verifies before advancing.

Required flow:

1. **Brainstorm** — explore the problem with the user. Do not create tasks during brainstorming.
2. **Sweep** — invoke `harbor-sweep-with-acceptance`. It asks the acceptance questions per task (probe artifact, probe command, related tests, optional worker instructions) and embeds answers as `## Acceptance Criteria` / `## Verification Probes` / `## Related Tests` / `## Worker Instructions` headers in the task description. No task is created without acceptance criteria and probes. Build and runtime target are NOT sweep questions — build lives in `harbor.yml` as `harbor.build`; runtime target lives in `.harbor/runtime-target.json`.
3. **Run** — when the user advances a task in Harbor, a worker session spawns. The worker invokes `harbor-task-worker` → does the work (including writing the task's test and updating any `(update: ...)`-flagged related tests) → invokes `harbor-task-verify` before moving the task to Review.
4. **Review** — user reviews the PR. The agent presents the task's new test and **recommends** whether it should join the permanent suite (`tests/`) or was a one-time gate to drop before merge; the user decides. Resume the worker session if changes are needed.

## Hard rules

- Do NOT call `mcp__harbor__create_task` or `mcp__harbor__create_tasks_batch` from this conversation outside the sweep skill. The sweep flow exists to enforce per-task acceptance.
- Do NOT trust `pytest passed` as evidence a task is done. `harbor-task-verify` runs the build (always, from `harbor.yml` `harbor.build`), then the task's `## Verification Probes` and `## Related Tests` via `target-runtime-exec`; every one must exit zero. It is the only path that says "verification passed."
- Do NOT advance a task to Review while the build, any probe, or any related test fails. Stay in `Running`, append the failure to `<worktree>/.harbor/execute.md`, and either fix it or escalate.
- Do NOT weaken or delete a related test to make it pass. A `(update: ...)`-flagged test is updated to match the new intended behavior, justified against the acceptance criteria; any other failure means fix the code, not the test.
- Do NOT bypass `target-runtime-exec` for commands that touch an emulator, ADB device, or game window. The runtime-target probe is a hard gate.
- Do NOT modify `.harbor/runtime-target.json` by hand. Use `python scripts/shared/target_runtime.py target set-...` so the schema is validated.
- Do NOT touch the bead-coupled harbor modules (`beads.py`, `epic.py`, `runner.py`, `mail.py`, `finalize.py`) unless the task is explicitly about removing them. They are deprecated.

## Runtime target

`.harbor/runtime-target.json` declares:
- `mode` — `local` (this machine) or `ssh` (sync + run on a remote host).
- `target.kind` — `local`, `emulator`, `device`, or `game_window`.
- The kind-specific subobject (emulator name + adb_port, device id + kind, game window title pattern, etc.).
- `target.probe_command` — user-defined readiness check. Required when `kind != local`.

`.harbor/runtime-target.json` is the single source of truth, and `target-runtime-exec` reads it directly. Almost all work is local. When a single task needs a non-local target, the user says so in plain text under `## Worker Instructions`; the worker writes a worktree-local override at `<worktree>/.harbor/runtime-target.json` from that instruction.

## Build

The project build is a single shell command in `harbor.yml` under `harbor.build`. `harbor-task-verify` (and `build-and-test`) run it **always**, before any tests, via `target-runtime-exec` — so tests run against current code, not a stale running instance.

- It is project-agnostic: for a pyinstaller app, the command typically kills the running processes and rebuilds the exe; for a `python main.py` app it is usually unset (a no-op).
- It is NOT a task field and NOT a sweep question — it is repo-wide config, parsed by `harbor.agent.load_config` and preserved across settings-UI saves.
- Set it by editing `harbor.yml`:
  ```yaml
  harbor:
    build: pwsh scripts/rebuild.ps1
  ```
- A non-zero build exit blocks the task (`classification=build`); the task stays in `Running`.

## Tools

- `harbor` MCP server — task board, task management, tmux pane spawning. Tools: `mcp__harbor__list_tasks`, `mcp__harbor__get_task`, `mcp__harbor__create_task`, `mcp__harbor__create_tasks_batch`, `mcp__harbor__update_task`, `mcp__harbor__move_task`, `mcp__harbor__read_pane_content`, `mcp__harbor__send_to_task`, `mcp__harbor__check_conflicts`.
- `harbor webui` — Windows-friendly substitute for the agtx ratatui TUI. Reads the same SQLite DB agtx writes (`%APPDATA%/agtx/config/projects/<sha256_8>.db`) and processes `transition_requests` itself, since the TUI process — which normally executes them — is unusable on Windows. Run with `python -m harbor webui --project-path <repo>` and visit `http://127.0.0.1:8765/`.
- Workflow plugin (`plugins/harbor-workflow-template/plugin.toml`) — defines per-phase slash commands, prompts, artifacts, and auto-dismiss patterns for harbor and agtx alike. Conforms to agtx's `WorkflowPlugin` schema so the same plugin.toml works in both runtimes. Reference it from harbor.yml as `harbor.plugin: harbor-workflow-template` or pass `--plugin <name>` on the CLI.
- `target-runtime-exec` skill — runs project commands through `scripts/shared/target_runtime.py`.
- `build-and-test` skill — runs the project build (from `harbor.yml` `harbor.build`) + the task's probes and related tests.

## Repo layout

- `harbor/` — Python package (orchestrator, webui, tmux helpers). Non-bead modules are stable; bead-coupled modules are deprecated.
- `.claude/skills/` — canonical workflow skills (single source of truth). Auto-discovered by harbor's own Claude Code sessions; deployed into task worktrees by the transition worker; bundled into the `harbor-workflow-template` plugin by its `install.py`.
- `scripts/shared/` — `target_runtime.py`, `probe_target.py`.
- `.harbor/` — runtime-target config (committed default + example).
- `tests/` — harbor's existing 19 mock-based tests.
- `docs/` — Windows tmux notes, phase-1 validation log.
