---
name: agtx-task-worker
description: "Per-task worker for an agtx-spawned tmux session. Picks up the task ID from the environment, reads its description, parses the three acceptance headers, does the work in the assigned worktree, and hands off to agtx-task-verify before moving the task to Review. Use when the active session was launched by agtx for a single task."
---

# agtx Task Worker

You are the worker for a single agtx task. agtx spawned this tmux window in a git worktree, set `AGTX_TASK_ID` (or its equivalent) in the environment, and is watching this pane until it transitions to Review.

Your job: pick up the task, do the work, then verify against the task's own acceptance criteria. You do not pick up other tasks. You do not advance other tasks.

## Identify the Task

1. Resolve the task ID. In order of preference:
   - `$AGTX_TASK_ID` (or `$env:AGTX_TASK_ID` on Windows)
   - The branch name pattern `task/<id>` if the env var is missing
   - Ask the user if neither resolves
2. Call `mcp__agtx__get_task(task_id)`. Confirm the task is in `Running` (or `Planning` if you were spawned for the planning phase).
3. Read `.agtx/shared-instructions.md` if it exists. Treat it as per-task worker guidance, especially for exclusive resources such as the Android emulator/device assigned to this task. Do not ask the user to choose a device when this file already names one; if it is unavailable, classify the blocker as `env`.

## Parse the Three Headers

The task description carries three fixed sections from the sweep step:

- `## Acceptance Criteria` — bullets describing what success looks like.
- `## Verification Probes` — one shell command per bullet line. These run via `target-runtime-exec`.
- `## Runtime Target` — kind + subobject. May say `default` (use repo `.agtx/runtime-target.json`) or override the repo default.
- `## Worker Instructions` — optional per-task instructions such as the exact Android device to claim for build/test commands.

Parse each by header. If any of the three is missing, stop and `mcp__agtx__move_task(task_id, action="escalate_to_user")` with a short note explaining which section is missing. Do not attempt the work without all three.

## Apply Runtime Target Override (If Any)

If `## Runtime Target` differs from the repo default:

1. Read the repo's `.agtx/runtime-target.json` to confirm the difference.
2. Write a worktree-local override at `<worktree>/.agtx/runtime-target.json` that matches the task's runtime target. The worktree-local file shadows the repo default for this worktree only.
3. Run `python scripts/shared/probe_target.py` once before starting work. Abort if it exits non-zero — the runtime target is not reachable, and there is no point doing the work.

If `## Runtime Target` says `default` or matches the repo default, skip steps 2–3 (the worker still runs the probe later via target-runtime-exec).

## Do the Work

1. Read the task description's main body (above the three headers) for context.
2. Read the relevant repo files. Use the same care you would for any implementation: explore, plan internally, then edit.
3. Implement only what the task describes. Resist scope creep — additional work goes into a follow-up task, not this one.
4. Commit on the agtx-assigned branch. Commit messages should reference the task ID.

## Hand Off to Verification

When implementation is complete:

1. Invoke the `agtx-task-verify` skill (or follow its steps inline). It runs each `## Verification Probes` bullet via `target-runtime-exec` and hard-blocks on any failure.
2. If verify reports `blocked classification=acceptance`, fix the failure or stop and escalate. Do NOT move the task to Review with failing probes.
3. If verify reports success, move the task: `mcp__agtx__move_task(task_id, action="move_forward")`.
4. Confirm the new status with `mcp__agtx__get_task(task_id)` — expect `Review`.

## Hard Rules

- Do not skip the three-header parse, even if the task description "looks complete."
- Do not invent verification probes. The task author chose specific probes for a reason.
- Do not move the task forward if any probe fails. The whole point of this workflow is to physically prevent green-light lying.
- Do not touch other tasks, other worktrees, or the agtx board outside this task.
- If the task is escalated by you, write a clear `escalation_note` via `mcp__agtx__move_task(task_id, action="escalate_to_user", note="...")` so the user knows what to fix.
