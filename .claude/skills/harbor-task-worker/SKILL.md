---
name: harbor-task-worker
description: "Per-task worker for a Harbor-spawned tmux session. Picks up the task ID from the environment, reads its description, parses the acceptance headers, does the work in the assigned worktree, and hands off to harbor-task-verify before moving the task to Review. Use when the active session was launched by Harbor for a single task."
---

# Harbor Task Worker

You are the worker for a single Harbor task. Harbor spawned this tmux window in a git worktree, set `HARBOR_TASK_ID` (or its equivalent) in the environment, and is watching this pane until it transitions to Review.

Your job: pick up the task, do the work, then verify against the task's own acceptance criteria. You do not pick up other tasks. You do not advance other tasks.

## Identify the Task

1. Resolve the task ID. In order of preference:
   - `$HARBOR_TASK_ID` (or `$env:HARBOR_TASK_ID` on Windows)
   - The branch name pattern `task/<id>` if the env var is missing
   - Ask the user if neither resolves
2. Call `mcp__harbor__get_task(task_id)`. Confirm the task is in `Running` (or `Planning` if you were spawned for the planning phase).
3. Read `.harbor/shared-instructions.md` if it exists. Treat it as per-task worker guidance, especially for exclusive resources such as the Android emulator/device assigned to this task. Do not ask the user to choose a device when this file already names one; if it is unavailable, classify the blocker as `env`.

## Parse the Headers

The task description carries fixed sections from the sweep step:

- `## Acceptance Criteria` — bullets describing what success looks like.
- `## Verification Probes` — one shell command per bullet line. These run via `target-runtime-exec`.
- `## Related Tests` — existing tests to run alongside this task's probe (or `none`). A bullet annotated `(update: <what must change>)` means this task makes that test stale: you must UPDATE it as part of your work (see "Do the Work"), justified against `## Acceptance Criteria` — never just weaken it to pass.
- `## Worker Instructions` — optional per-task instructions, such as an exclusive resource to claim, a non-local runtime target to use, or special task-scoped guidance.

Parse each by header. If `## Acceptance Criteria` or `## Verification Probes` is missing, stop and `mcp__harbor__move_task(task_id, action="escalate_to_user")` with a short note explaining which section is missing. Do not attempt the work without those two. `## Related Tests` (`none` ok) and `## Worker Instructions` are optional — proceed without them.

Note: the project **build** is not a task section — it lives in `harbor.yml` as `harbor.build`, and `harbor-task-verify` runs it for you before tests. You do not run the build yourself.

## Runtime Target

The repo's `.harbor/runtime-target.json` is the runtime for every task — `target-runtime-exec` reads it directly, so there is nothing per-task to apply for the common (local, repo-default) case.

**If Harbor's auto-orchestrator leased a runtime slot for this task, it has already written `<worktree>/.harbor/runtime-target.json` for you** (pinning this task to its own emulator / app instance so parallel tasks don't collide). If that file already exists in your worktree, treat it as authoritative and do **not** overwrite it — it is the slot you were assigned.

Only when `## Worker Instructions` names a *non-local* runtime target (an SSH host, a specific emulator, device, or game window that differs from the repo default) **and** no orchestrator-written override is already present:

1. Read the repo's `.harbor/runtime-target.json` to see the current default.
2. Write a worktree-local override at `<worktree>/.harbor/runtime-target.json` that matches the target described in `## Worker Instructions`. The worktree-local file shadows the repo default for this worktree only. Use `python scripts/shared/target_runtime.py target set-...` so the schema is validated.
3. Run `python scripts/shared/probe_target.py` once before starting work. Abort if it exits non-zero — the runtime target is not reachable, and there is no point doing the work.

If `## Worker Instructions` says `none` or names no runtime target, do nothing here (the worker still runs probes later via target-runtime-exec, which uses the repo default).

## Do the Work

1. Read the task description's main body (above the section headers) for context.
2. Read the relevant repo files. Use the same care you would for any implementation: explore, plan internally, then edit.
3. Implement only what the task describes. Resist scope creep — additional work goes into a follow-up task, not this one.
4. **Write the task's test.** If `## Verification Probes` names a test that does not exist yet (e.g. a new `pytest tests/test_<x>.py::test_<y>`), create it from `## Acceptance Criteria` so the probe has something to run.
5. **Update stale tests.** For each `## Related Tests` bullet flagged `(update: ...)`, change that test to match the behavior this task alters — justified against `## Acceptance Criteria` and visible in your commit. Never weaken or delete a test just to make it green.
6. Commit on the Harbor-assigned branch. Commit messages should reference the task ID.

## Hand Off to Verification

When implementation is complete:

1. Invoke the `harbor-task-verify` skill (or follow its steps inline). It runs the build (always, from `harbor.yml`), then each `## Verification Probes` and `## Related Tests` command via `target-runtime-exec`, and hard-blocks on any failure.
2. If verify reports `blocked` (classification `build`, `env`, or `acceptance`), fix the failure or stop and escalate. Do NOT move the task to Review while any check fails.
3. If verify reports success, move the task: `mcp__harbor__move_task(task_id, action="move_forward")`.
4. Confirm the new status with `mcp__harbor__get_task(task_id)` — expect `Review`.

## Hard Rules

- Do not skip the header parse, even if the task description "looks complete."
- Do not invent verification probes. The task author chose specific probes for a reason.
- Do not move the task forward if the build, any probe, or any related test fails. The whole point of this workflow is to physically prevent green-light lying.
- Do not weaken or delete a related test to make it pass. If a `## Related Tests` entry is flagged `(update: ...)`, change it to match the new intended behavior, justified against `## Acceptance Criteria`; if it fails for any other reason, fix the code, not the test.
- Do not touch other tasks, other worktrees, or the Harbor board outside this task.
- If the task is escalated by you, write a clear `escalation_note` via `mcp__harbor__move_task(task_id, action="escalate_to_user", note="...")` so the user knows what to fix.
