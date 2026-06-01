---
name: harbor-task-worker
description: "Per-task worker for a Harbor-spawned tmux session. Picks up the task ID from the environment, reads its description, parses the acceptance headers, does the work in the assigned worktree, and hands off to harbor-task-verify before moving the task to Review. Use when the active session was launched by Harbor for a single task."
---

# Harbor Task Worker

You are the worker for a single Harbor task. Harbor spawned this tmux window in a git worktree, set `AGTX_TASK_ID` (or its equivalent) in the environment, and is watching this pane until it transitions to Review.

Your job: pick up the task, do the work, then verify against the task's own acceptance criteria. You do not pick up other tasks. You do not advance other tasks.

## Identify the Task

1. Resolve the task ID. In order of preference:
   - `$AGTX_TASK_ID` (or `$env:AGTX_TASK_ID` on Windows)
   - The branch name pattern `task/<id>` if the env var is missing
   - Ask the user if neither resolves
2. Call `mcp__harbor__get_task(task_id)`. Confirm the task is in `Running` (or `Planning` if you were spawned for the planning phase).
3. Read `.harbor/shared-instructions.md` if it exists. Treat it as per-task worker guidance, especially for exclusive resources such as the Android emulator/device assigned to this task. Do not ask the user to choose a device when this file already names one; if it is unavailable, classify the blocker as `env`.

## Parse the Headers

The task description carries fixed sections from the sweep step:

- `## Acceptance Criteria` — bullets describing what success looks like.
- `## Verification Probes` — one shell command per bullet line. These run via `target-runtime-exec`.
- `## Worker Instructions` — optional per-task instructions, such as an exclusive resource to claim, a non-local runtime target to use, or special task-scoped guidance.
- `## Run Repo Defaults` — optional `yes`/`no` toggle. You don't act on this directly; `harbor-task-verify` reads it during the Running→Review gate to decide whether to also invoke `build-and-test` after the probes pass.

Parse each by header. If `## Acceptance Criteria` or `## Verification Probes` is missing, stop and `mcp__harbor__move_task(task_id, action="escalate_to_user")` with a short note explaining which section is missing. Do not attempt the work without those two. `## Worker Instructions` and `## Run Repo Defaults` are optional — proceed without them.

## Runtime Target

The repo's `.harbor/runtime-target.json` is the runtime for every task — `target-runtime-exec` reads it directly, so there is nothing per-task to apply for the common (local, repo-default) case.

Only when `## Worker Instructions` names a *non-local* runtime target (an SSH host, a specific emulator, device, or game window that differs from the repo default):

1. Read the repo's `.harbor/runtime-target.json` to see the current default.
2. Write a worktree-local override at `<worktree>/.harbor/runtime-target.json` that matches the target described in `## Worker Instructions`. The worktree-local file shadows the repo default for this worktree only. Use `python scripts/shared/target_runtime.py target set-...` so the schema is validated.
3. Run `python scripts/shared/probe_target.py` once before starting work. Abort if it exits non-zero — the runtime target is not reachable, and there is no point doing the work.

If `## Worker Instructions` says `none` or names no runtime target, do nothing here (the worker still runs probes later via target-runtime-exec, which uses the repo default).

## Do the Work

1. Read the task description's main body (above the section headers) for context.
2. Read the relevant repo files. Use the same care you would for any implementation: explore, plan internally, then edit.
3. Implement only what the task describes. Resist scope creep — additional work goes into a follow-up task, not this one.
4. Commit on the Harbor-assigned branch. Commit messages should reference the task ID.

## Hand Off to Verification

When implementation is complete:

1. Invoke the `harbor-task-verify` skill (or follow its steps inline). It runs each `## Verification Probes` bullet via `target-runtime-exec` and hard-blocks on any failure.
2. If verify reports `blocked classification=acceptance`, fix the failure or stop and escalate. Do NOT move the task to Review with failing probes.
3. If verify reports success, move the task: `mcp__harbor__move_task(task_id, action="move_forward")`.
4. Confirm the new status with `mcp__harbor__get_task(task_id)` — expect `Review`.

## Hard Rules

- Do not skip the header parse, even if the task description "looks complete."
- Do not invent verification probes. The task author chose specific probes for a reason.
- Do not move the task forward if any probe fails. The whole point of this workflow is to physically prevent green-light lying.
- Do not touch other tasks, other worktrees, or the Harbor board outside this task.
- If the task is escalated by you, write a clear `escalation_note` via `mcp__harbor__move_task(task_id, action="escalate_to_user", note="...")` so the user knows what to fix.
