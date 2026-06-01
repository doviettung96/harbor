---
name: harbor-task-verify
description: "Verify an in-progress Harbor task against the ## Verification Probes embedded in its description. Runs each probe through target-runtime-exec, optionally invokes build-and-test for repo-default checks when ## Run Repo Defaults is yes, gates the task's move to Review on success, and writes failure summaries into <worktree>/.harbor/execute.md. Use after the worker finishes implementation, before moving the task forward."
---

# Harbor Task Verify

The single non-negotiable gate between Running and Review for a Harbor task. Reads the task's `## Verification Probes`, runs each one through `target-runtime-exec`, and refuses to advance the task on any non-zero exit.

This skill is what makes "I distrust pytest" enforceable. It is ALSO the only skill that should ever say "verification passed" for a Harbor task.

## Inputs

- The active task ID (from `$AGTX_TASK_ID`, branch name, or asked).
- The current worktree (where the task's branch is checked out).

## Steps

1. **Fetch the task.** `mcp__harbor__get_task(task_id)`. Read the description.

2. **Parse `## Verification Probes`.** Each non-empty bullet line under the header is one shell command. Strip the leading `- ` and any trailing whitespace. If the section is missing or empty, do NOT advance — escalate via `mcp__harbor__move_task(task_id, action="escalate_to_user", note="missing ## Verification Probes section")` and stop.

3. **Probe target reachability first.** Run `python scripts/shared/probe_target.py` from the worktree root. The runtime comes from `.harbor/runtime-target.json` — the repo default, or a worktree-local override the worker wrote if `## Worker Instructions` named a non-local target. If exit != 0, append the failure to `<worktree>/.harbor/execute.md` and emit `blocked classification=env`. Do NOT run task probes against an unreachable target.

4. **Run each probe.** For each parsed command:
   ```
   python scripts/shared/target_runtime.py run -- <probe command>
   ```
   Capture stdout, stderr, and exit code.

5. **On any non-zero exit:**
   - Append a failure record to `<worktree>/.harbor/execute.md`:
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

6. **On all probes passing:**
   - Append a success record to `<worktree>/.harbor/execute.md`:
     ```
     === <UTC timestamp> all probes passed ===
     <bulleted list of probe commands and exit codes>
     ```
   - Continue to step 7 before declaring victory.

7. **Repo-default gate (optional).** Parse `## Run Repo Defaults` from the task description (case-insensitive).
   - Treat `yes`, `y`, `true`, `1`, `on`, `enabled` as opt-in. Anything else (including a missing section) is opt-out — preserving backwards compat with tasks created before this header existed.
   - **If opt-out:** skip to step 8.
   - **If opt-in:** invoke the `build-and-test` skill (or follow its discovery + run steps inline). It reads the repo's documented build/test commands from `README.md`, `pyproject.toml`, `package.json`, `Makefile`, or CI config and runs each via `target-runtime-exec`. Do not re-run the task's `## Verification Probes` here — they already passed in step 4.
   - **On any failure in build-and-test:**
     - Append the failing command, exit code, and stderr tail to `<worktree>/.harbor/execute.md`:
       ```
       === <UTC timestamp> repo defaults failed ===
       command: <command>
       exit: <code>
       stderr (last 20 lines):
       <stderr tail>
       ```
     - Emit `blocked classification=acceptance`.
     - Do NOT call `move_task` — the task stays in `Running`. Hand control back to the worker.
   - **On all repo-default commands passing:** append a success record to `<worktree>/.harbor/execute.md`:
     ```
     === <UTC timestamp> repo defaults passed ===
     <bulleted list of build-and-test commands and exit codes>
     ```

8. **Verification passed:**
   - Print `verification passed` with the probe summary (and `+ repo defaults` if step 7 ran).
   - Return control to the worker, who will call `mcp__harbor__move_task(task_id, action="move_forward")`.

## Hard Rules

- Verify cannot decide to skip a probe. If the task author wrote a probe, you run it.
- Verify cannot decide a probe "passed in spirit" if it exited non-zero. Exit code is law.
- Verify never calls `move_task` itself — that is the worker's responsibility, gated on this skill's success report.
- Verify writes to `<worktree>/.harbor/execute.md`, never to `.harbor/runtime-target.json` or any other shared file.
- Verify cannot weaken the repo-defaults gate. If `## Run Repo Defaults` is `yes`, build-and-test MUST run and pass. Verify cannot reinterpret it as `no` because the suite is slow or some test "looks unrelated".
- If the same probe has flaked across runs, do not paper over it — escalate so the user can either fix the probe or accept the flake explicitly.

## Output Contract

Single trailing line, machine-readable:

- Success (probes only): `harbor-verify task=<id> probes=<N> passed`
- Success (probes + repo defaults): `harbor-verify task=<id> probes=<N> passed repo_defaults=passed`
- Probe failure: `harbor-verify task=<id> probes=<N> failed=<idx> exit=<code>`
- Repo-default failure: `harbor-verify task=<id> probes=<N> passed repo_defaults=failed`
- Escalation: `harbor-verify task=<id> escalated reason=<short>`

These lines are what `harbor-task-worker` greps for to decide whether to call `move_task`. Any line that does not end in `passed` (with optional `repo_defaults=passed`) is a no-advance signal.
