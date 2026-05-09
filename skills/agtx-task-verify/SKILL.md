---
name: agtx-task-verify
description: "Verify an in-progress agtx task against the ## Verification Probes embedded in its description. Runs each probe through target-runtime-exec, gates the task's move to Review on success, and writes failure summaries into <worktree>/.agtx/execute.md. Use after the worker finishes implementation, before moving the task forward."
---

# agtx Task Verify

The single non-negotiable gate between Running and Review for an agtx task. Reads the task's `## Verification Probes`, runs each one through `target-runtime-exec`, and refuses to advance the task on any non-zero exit.

This skill is what makes "I distrust pytest" enforceable. It is ALSO the only skill that should ever say "verification passed" for an agtx task.

## Inputs

- The active task ID (from `$AGTX_TASK_ID`, branch name, or asked).
- The current worktree (where the task's branch is checked out).

## Steps

1. **Fetch the task.** `mcp__agtx__get_task(task_id)`. Read the description.

2. **Parse `## Verification Probes`.** Each non-empty bullet line under the header is one shell command. Strip the leading `- ` and any trailing whitespace. If the section is missing or empty, do NOT advance — escalate via `mcp__agtx__move_task(task_id, action="escalate_to_user", note="missing ## Verification Probes section")` and stop.

3. **Confirm runtime target.** Parse `## Runtime Target`. If it differs from the repo default in `.agtx/runtime-target.json`, the worker should already have written a worktree-local override; verify the override exists at `<worktree>/.agtx/runtime-target.json`. If the override is required but missing, stop and escalate.

4. **Probe target reachability first.** Run `python scripts/shared/probe_target.py` from the worktree root. If exit != 0, append the failure to `<worktree>/.agtx/execute.md` and emit `blocked classification=env`. Do NOT run task probes against an unreachable target.

5. **Run each probe.** For each parsed command:
   ```
   python scripts/shared/target_runtime.py run -- <probe command>
   ```
   Capture stdout, stderr, and exit code.

6. **On any non-zero exit:**
   - Append a failure record to `<worktree>/.agtx/execute.md`:
     ```
     === <UTC timestamp> probe failed ===
     command: <probe command>
     exit: <code>
     stderr (last 20 lines):
     <stderr tail>
     ```
   - Emit `blocked classification=acceptance`.
   - Do NOT call `move_task` — the task stays in `Running`.
   - Stop. Hand control back to the worker so they can fix and retry.

7. **On all probes passing:**
   - Append a success record to `<worktree>/.agtx/execute.md`:
     ```
     === <UTC timestamp> all probes passed ===
     <bulleted list of probe commands and exit codes>
     ```
   - Print "verification passed" with the probe summary.
   - Return control to the worker, who will call `mcp__agtx__move_task(task_id, action="move_forward")`.

## Hard Rules

- Verify cannot decide to skip a probe. If the task author wrote a probe, you run it.
- Verify cannot decide a probe "passed in spirit" if it exited non-zero. Exit code is law.
- Verify never calls `move_task` itself — that is the worker's responsibility, gated on this skill's success report.
- Verify writes to `<worktree>/.agtx/execute.md`, never to `.agtx/runtime-target.json` or any other shared file.
- If the same probe has flaked across runs, do not paper over it — escalate so the user can either fix the probe or accept the flake explicitly.

## Output Contract

Single trailing line, machine-readable:

- Success: `agtx-verify task=<id> probes=<N> passed`
- Failure: `agtx-verify task=<id> probes=<N> failed=<idx> exit=<code>`
- Escalation: `agtx-verify task=<id> escalated reason=<short>`

These lines are what `agtx-task-worker` greps for to decide whether to call `move_task`.
