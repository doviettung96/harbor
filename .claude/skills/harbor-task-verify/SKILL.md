---
name: harbor-task-verify
description: "Verify an in-progress Harbor task. Runs the project build first (always, from harbor.yml `harbor.build`), then the task's ## Verification Probes and ## Related Tests through target-runtime-exec, gates the task's move to Review on every check exiting zero, and writes failure summaries into <worktree>/.harbor/execute.md. Use after the worker finishes implementation, before moving the task forward."
---

# Harbor Task Verify

The single non-negotiable gate between Running and Review for a Harbor task. It (1) runs the project build **always**, (2) runs the task's `## Verification Probes`, and (3) runs the task's `## Related Tests` — all through `target-runtime-exec` — and refuses to advance the task on any non-zero exit.

This skill is what makes "I distrust pytest" enforceable. It is ALSO the only skill that should ever say "verification passed" for a Harbor task.

## Inputs

- The active task ID (from `$HARBOR_TASK_ID`, branch name, or asked).
- The current worktree (where the task's branch is checked out).

## Steps

1. **Fetch the task.** `mcp__harbor__get_task(task_id)`. Read the description.

2. **Parse `## Verification Probes` and `## Related Tests`.** Under `## Verification Probes`, each non-empty bullet line is one shell command — strip the leading `- ` and trailing whitespace. If that section is missing or empty, do NOT advance — escalate via `mcp__harbor__move_task(task_id, action="escalate_to_user", note="missing ## Verification Probes section")` and stop. Under `## Related Tests`, each non-empty bullet line is an existing test to also run; treat a lone `none` as empty. A bullet annotated `(update: ...)` should already have been updated by the worker during implementation — you only run it here.

3. **Build first — always.** Read `harbor.build` from the repo's `harbor.yml` (the `harbor:` mapping). If it is set, run it once through the runtime target:
   ```
   python scripts/shared/target_runtime.py run -- <harbor.build command>
   ```
   This kills the stale running app and rebuilds (e.g. pyinstaller), or is a no-op for a `python main.py` project. If `harbor.build` is unset or empty, skip this step. On a non-zero exit, append a failure record to `<worktree>/.harbor/execute.md`, emit `blocked classification=build`, do NOT call `move_task`, and stop — never test against a failed build.

4. **Probe target reachability.** Run `python scripts/shared/probe_target.py` from the worktree root. The runtime comes from `.harbor/runtime-target.json` — the repo default, or a worktree-local override the worker wrote if `## Worker Instructions` named a non-local target. If exit != 0, append the failure to `<worktree>/.harbor/execute.md` and emit `blocked classification=env`. Do NOT run tests against an unreachable target.

5. **Run each verification probe, then each related test.** For every command (probes first, then `## Related Tests`):
   ```
   python scripts/shared/target_runtime.py run -- <command>
   ```
   Capture stdout, stderr, and exit code.

6. **On any non-zero exit (probe or related test):**
   - Append a failure record to `<worktree>/.harbor/execute.md`:
     ```
     === <UTC timestamp> <probe|related test> failed ===
     command: <command>
     exit: <code>
     stderr (last 20 lines):
     <stderr tail>
     ```
   - Emit `blocked classification=acceptance`.
   - Do NOT call `move_task` — the task stays in `Running`.
   - Stop. Hand control back to the worker so they can fix and retry.

7. **On everything passing:**
   - Append a success record to `<worktree>/.harbor/execute.md`:
     ```
     === <UTC timestamp> all checks passed ===
     build: <harbor.build command, or "none">
     probes: <bulleted list of probe commands and exit codes>
     related: <bulleted list of related tests and exit codes, or "none">
     ```

8. **Verification passed:**
   - Print `verification passed` with the build + probe + related-test summary.
   - Return control to the worker, who will call `mcp__harbor__move_task(task_id, action="move_forward")`.

## Hard Rules

- Verify cannot skip the build. If `harbor.build` is set, it runs every time, before tests. Verify cannot reinterpret it as skippable because "nothing relevant changed".
- Verify cannot decide to skip a probe or a related test. If the task lists it, you run it. Exit code is law for all of them.
- Verify cannot decide a check "passed in spirit" if it exited non-zero.
- Verify never calls `move_task` itself — that is the worker's responsibility, gated on this skill's success report.
- Verify writes to `<worktree>/.harbor/execute.md`, never to `.harbor/runtime-target.json` or any other shared file.
- If the same probe or related test has flaked across runs, do not paper over it — escalate so the user can either fix it or accept the flake explicitly.

## Output Contract

Single trailing line, machine-readable:

- Success: `harbor-verify task=<id> build=<ok|none> probes=<N> related=<M> passed`
- Build failure: `harbor-verify task=<id> build=failed`
- Probe failure: `harbor-verify task=<id> probes=<N> failed=<idx> exit=<code>`
- Related-test failure: `harbor-verify task=<id> related=<M> failed=<idx> exit=<code>`
- Escalation: `harbor-verify task=<id> escalated reason=<short>`

These lines are what `harbor-task-worker` greps for to decide whether to call `move_task`. Any line that does not end in `passed` is a no-advance signal.
