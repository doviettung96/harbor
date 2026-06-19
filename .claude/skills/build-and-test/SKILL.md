---
name: build-and-test
description: "Finalize prompt that runs the project build (always, from harbor.yml `harbor.build`) then the active Harbor task's `## Verification Probes` and `## Related Tests`, all via target-runtime-exec. The build refreshes the running app so tests run against current code; there is no whole-suite run."
---

# Build and Test

Run the project **build** (always), then the active task's **verification probes** and **related tests**. This is the only verification path; `pytest passed` alone is never sufficient. There is no "run the whole documented test suite" step — tests are exactly the task's own probes plus the `## Related Tests` it names.

## Build (always)

Read `harbor.build` from the repo's `harbor.yml` (the `harbor:` mapping). It is a single shell command:

- If set, run it once via `target-runtime-exec`. It typically kills the project's running processes and rebuilds the exe (pyinstaller); for a `python main.py` project it is a no-op or unset.
- If unset/empty, skip the build — there is nothing to compile.

On a non-zero build exit, append the failure to `<worktree>/.harbor/execute.md`, emit `blocked classification=build`, and stop. Never run tests against a failed build.

Also read `.harbor/shared-instructions.md` if it exists — honor any per-task verification or Android device policy there before asking the user which emulator/device to use. If a named device is unavailable, emit `blocked classification=env` and include the device listing.

## Steps

1. Inspect `git status --short` and the changed paths.
2. Run the build (above) if `harbor.build` is set.
3. Fetch the task and parse the task-scoped sections (next section).
4. Run each `## Verification Probes` command, then each `## Related Tests` command, from the repository root via `target-runtime-exec` so the configured runtime target is honored.
5. Report each command, its exit code, and the relevant output.

If a command cannot run because of the local environment, emit `blocked classification=env` and include the failing command and error.

## Task-Scoped Probes and Related Tests

When the worktree corresponds to a Harbor task (the env var `HARBOR_TASK_ID` is set, or a task can be inferred from the current branch name), the per-task acceptance is non-negotiable.

1. Read the task: `mcp__harbor__get_task(task_id)`.
2. Parse the description for these fixed sections:
   - `## Verification Probes` — one shell command per bullet line. Each line is a standalone command.
   - `## Related Tests` — existing tests to also run (or `none`). Run each one; ignore a lone `none`. A bullet annotated `(update: ...)` is updated by the worker, not here — you only run it.
   - `## Worker Instructions` — optional per-task guidance such as which Android device/emulator this task owns, or a non-local runtime target to use. Harbor also writes this to `.harbor/shared-instructions.md`. If it names a non-local target that differs from the repo default, write a worktree-local override at `<worktree>/.harbor/runtime-target.json` before running.
3. Run each probe, then each related test, via `target-runtime-exec` so it inherits the configured runtime target (the repo `.harbor/runtime-target.json`, or the worktree override) and probe gating.
4. **Hard block.** If any probe or related test exits non-zero, do NOT report success. Append the failing command, exit code, and stderr tail to `<worktree>/.harbor/execute.md` with a UTC timestamp, then emit `blocked classification=acceptance`. The task stays in the Harbor Running column until everything passes.

## Hard Rules

- A green pytest run is not sufficient evidence. The build runs first, then the task's `## Verification Probes` and `## Related Tests` MUST run and pass.
- Do not invent probes or related tests that the task description does not declare. If `## Verification Probes` is missing, stop and ask the user to amend the task before continuing.
- Do not skip a probe because it "looks the same" as another test. The task author chose specific probes for a reason.
- Do not run the whole test suite as a stand-in. Tests are the task's probes plus its named `## Related Tests` — nothing more, nothing less.
