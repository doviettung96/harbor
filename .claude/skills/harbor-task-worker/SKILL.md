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

The repo's `.harbor/runtime-target.json` is the default runtime — `target-runtime-exec` reads it directly, so for the common local / repo-default case there is nothing to apply.

### Reserve a shared runtime before you test (resource broker)

Runtime resources — emulators, app instances, game windows — are shared across **all** Harbor projects on this machine. There is **no pre-declared pool**: you *discover* what exists and reserve one, so two tasks never collide on the same device. Do all your **implementation freely** (coding needs no resource). But **before anything that touches an exclusive runtime** — the build and the `## Verification Probes` / `## Related Tests` that `harbor-task-verify` runs — reserve it:

1. **Discover** the candidates for the kind your task needs. For an Android emulator: run `adb devices` and collect every running serial (e.g. `emulator-5554`, `emulator-5556`). Use the serial as the canonical identity (`key`).
2. **Reserve** one — pass ALL discovered candidates in a single call:
   ```
   mcp__harbor__acquire_runtime(
     project_id=<this task's project>, task_id=<this task's id>, kind="emulator",
     candidates=[
       {"key": "emulator-5554", "target": {"kind": "emulator",
          "emulator": {"name": "emulator-5554", "adb_port": 5554},
          "probe_command": "adb -s emulator-5554 get-state"}},
       {"key": "emulator-5556", "target": {"kind": "emulator",
          "emulator": {"name": "emulator-5556", "adb_port": 5556},
          "probe_command": "adb -s emulator-5556 get-state"}}])
   ```
   The broker atomically locks the first candidate not already held by another task and returns it — never busy-loops, never silently fails:
   - `{"status": "granted", "key": ..., "target": ...}` → the target is already written into your worktree `.harbor/runtime-target.json`. Proceed to verification.
   - `{"status": "queued"}` → every candidate is in use and you are in line. **STOP: end your turn and do nothing further.** Harbor parks you and will message this pane when one is reserved for you (it writes your worktree override, then tells you to resume); pick up at "Hand Off to Verification" then.
3. `mcp__harbor__release_runtime(project_id=..., task_id=...)` the **instant** verification finishes — pass or fail — so the next queued task can proceed.

`mcp__harbor__list_resources()` shows what's currently held / who's waiting if you want to gauge contention first. If you crash or the task leaves Running, Harbor reclaims your reservation automatically. **Never overwrite a `.harbor/runtime-target.json` that Harbor wrote for you** — it is the instance you were granted. Either run your probes via `target-runtime-exec` (it reads that file and exports `AWT_TARGET_*`) or use the granted serial directly.

### Broker disabled / manual runtime

If the resource broker is disabled, there is nothing to acquire — `target-runtime-exec` uses the repo default. Only when `## Worker Instructions` names a *non-local* runtime target (an SSH host, a specific emulator, device, or game window that differs from the repo default) **and** no Harbor-written override is already present:

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

1. **Reserve your runtime first** (see "Runtime Target"). If the resource broker is enabled, discover candidates and call `mcp__harbor__acquire_runtime(...)` for the kind your tests need. If it returns `queued`, **stop and end your turn** — resume here once Harbor wakes you. If `granted` (or the broker is disabled), continue.
2. Invoke the `harbor-task-verify` skill (or follow its steps inline). It runs the build (always, from `harbor.yml`), then each `## Verification Probes` and `## Related Tests` command via `target-runtime-exec`, and hard-blocks on any failure.
3. **Release your runtime** as soon as verify finishes — pass or fail: `mcp__harbor__release_runtime(project_id=..., task_id=...)`. Do this before you escalate, fix-and-retry, or move forward, so a parked task isn't blocked while you iterate. (Re-acquire before the next verify run.)
4. If verify reports `blocked` (classification `build`, `env`, or `acceptance`), fix the failure or stop and escalate. Do NOT move the task to Review while any check fails.
5. If verify reports success, move the task: `mcp__harbor__move_task(task_id, action="move_forward")`.
6. Confirm the new status with `mcp__harbor__get_task(task_id)` — expect `Review`.

## Hard Rules

- Do not skip the header parse, even if the task description "looks complete."
- Do not invent verification probes. The task author chose specific probes for a reason.
- Do not move the task forward if the build, any probe, or any related test fails. The whole point of this workflow is to physically prevent green-light lying.
- Do not weaken or delete a related test to make it pass. If a `## Related Tests` entry is flagged `(update: ...)`, change it to match the new intended behavior, justified against `## Acceptance Criteria`; if it fails for any other reason, fix the code, not the test.
- If the resource broker is enabled, never run the build/probes without first discovering candidates and `acquire_runtime`-ing one, and always `release_runtime` the moment verification finishes. When `acquire_runtime` returns `queued`, stop and end your turn — do not poll, busy-wait, or proceed without the resource.
- Do not touch other tasks, other worktrees, or the Harbor board outside this task.
- If the task is escalated by you, write a clear `escalation_note` via `mcp__harbor__move_task(task_id, action="escalate_to_user", note="...")` so the user knows what to fix.
