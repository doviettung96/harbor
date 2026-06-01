# Harbor

Harbor is two things in one repo:

1. **A Python orchestrator** (`harbor/`) for running coding-agent sessions in tmux panes. Originally built to drive beads epics; today, the agent-per-task work is delegated to [agtx](https://github.com/fynnfluegge/agtx) and harbor's role is being refocused (webui dashboard for Harbor tasks; tmux/psmux fallbacks; bead-coupled modules are deprecated, not yet removed).

2. **A Harbor-style workflow template** (`skills/`, `scripts/`, `.harbor/`) for game-reverse work. brainstorm → sweep → one-agent-per-task, with **explicit per-task acceptance criteria** that the worker enforces via `build-and-test`. Designed for Windows + a working tmux build. Supports per-task game-RE runtime targeting (emulator, device, game window).

The two parts share one repo because harbor's webui is the natural future home for the Harbor dashboard, and harbor's tmux orchestrator is the fallback for any flow agtx doesn't cover.

## Quick Start (the Harbor-style workflow)

```bash
# 1. Install agtx and register its MCP server with your agent
agtx trust && agtx
claude mcp add agtx -- agtx mcp-serve

# 2. Configure this repo's runtime target (only needed for non-local targets)
python scripts/shared/target_runtime.py status
python scripts/shared/target_runtime.py target set-device \
  --id=127.0.0.1:5555 --kind=adb \
  --probe-command="adb -s 127.0.0.1:5555 get-state"

# 3. Brainstorm + sweep into Harbor tasks (use the customized sweep)
#    /harbor:brainstorm in your agent — explore freely, no code yet
#    /harbor-sweep-with-acceptance — the worker, three questions per task

# 4. Move a task forward in Harbor; a worker agent picks it up in its own
#    tmux window + git worktree. The worker uses harbor-task-worker → harbor-task-verify.
```

## Workflow Skills

| Skill | Purpose |
|---|---|
| `harbor-sweep-with-acceptance` | Sweep skill that asks 3 numbered acceptance questions per task and embeds answers as `## Acceptance Criteria` / `## Verification Probes` / `## Runtime Target` headers in the task description. |
| `harbor-task-worker` | Per-task worker for a Harbor-spawned tmux session. Parses the three headers, does the work, hands off to verify. |
| `harbor-task-verify` | Runs `## Verification Probes` via `target-runtime-exec` with hard-block on any failure. Writes failure summaries to `<worktree>/.harbor/execute.md`. |
| `runtime-target-config` | Interactive setup for `.harbor/runtime-target.json` (mode + target.kind + emulator/device/game_window subobject + probe_command). |
| `build-and-test` | Generic discovery-based test runner; ALSO reads task-scoped probes from the active Harbor task description. |
| `target-runtime-exec` | Routes runtime-dependent commands through `scripts/shared/target_runtime.py` so `target.kind` and probes are honored. |
| `brainstorming`, `verification-before-completion`, `systematic-debugging`, `writing-plans` | Carried over from the bead-era template; useful regardless of task tracker. |

## Runtime Target

`.harbor/runtime-target.json` is the source of truth for "where commands run" and "what they target." Schema:

```json
{
  "version": 1,
  "mode": "local",
  "target": {
    "kind": "device",
    "device": { "id": "127.0.0.1:5555", "kind": "adb", "transport": "tcp" },
    "probe_command": "adb -s 127.0.0.1:5555 get-state"
  }
}
```

See `.harbor/runtime-target.example.json` for a fully-populated example targeting LDPlayer + Blue Archive (JP).

When `target.kind != local`, the worker runs the probe before any command and refuses to proceed if it fails. Workers can write a worktree-local `<worktree>/.harbor/runtime-target.json` to override per-task without touching the repo default.

## The Harbor Python Package

`harbor/` is the original orchestrator. Several modules are bead-coupled and marked deprecated:

- `beads.py`, `epic.py`, `runner.py`, `mail.py`, `finalize.py`

These remain in the codebase but are not used by the Harbor-style workflow. The non-deprecated pieces (tmux, state, prompt, agent, orchestrator core, verify, webui) are still useful and are the candidates for the Harbor dashboard rebuild.

## Prerequisites

- Python 3.10+
- A working `tmux` on PATH (Windows: install a working build — psmux is no longer recommended; the original psmux notes are in `docs/WINDOWS_TMUX.md` for historical reference)
- `agtx` installed and trusted in this repo
- An agent CLI (`codex`, `claude`)

## Install

```bash
python -m pip install -e .
```

This exposes the `harbor` and `harbor-bead-runner` entry points. `harbor-bead-runner` is bead-coupled and will be removed; do not use it for new work.

## Status

- Workflow template: in active development.
- Harbor python package: stable on the non-bead modules; bead-coupled modules slated for removal once the agtx integration is verified end-to-end.
