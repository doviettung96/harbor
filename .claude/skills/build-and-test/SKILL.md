---
name: build-and-test
description: "Generic finalize prompt for discovering and running a repository's build, test, and per-task verification probes. Reads `## Verification Probes` from the active Harbor task description when present so per-task acceptance is enforced."
---

# Build and Test

Run the repository's normal build and test checks plus any per-task probes declared by the active Harbor task. This is the only verification path; `pytest passed` alone is never sufficient.

## Discovery

Read the local project files before running commands:

- `README.md` or equivalent setup docs
- `pyproject.toml`
- `package.json`
- `Makefile`
- CI configuration, if present
- `.harbor/shared-instructions.md`, if present

Prefer explicit project scripts over guessed commands. If more than one plausible stack exists, choose the smallest check set that validates the files changed in the current work and state the reason.
If `.harbor/shared-instructions.md` contains per-task verification or Android device policy, honor it before asking the user which emulator/device to use. If the named device is unavailable, emit `blocked classification=env` and include the device listing or probe output.

Files:
- (no files modified - verification only)

Verify:
- discover and run the repo's documented build/test commands
- run any task-scoped probes declared in the active Harbor task description

## Steps

1. Inspect `git status --short` and the changed paths.
2. Read the build/test configuration files listed above.
   Also read `.harbor/shared-instructions.md` if it exists.
3. If invoked under a Harbor task worktree, fetch the task and parse task-scoped probes (see next section).
4. Identify the exact repo-default commands appropriate for the changed areas.
5. Run all commands (repo-default + task-scoped) from the repository root, each via `target-runtime-exec` so the configured runtime target is honored.
6. Report each command, its exit code, and the relevant output.

If the repository does not yet document a runnable build or test command, emit `blocked classification=contract` and explain what command or project metadata is missing. If a command exists but cannot run because of the local environment, emit `blocked classification=env` and include the failing command and error.

## Task-Scoped Probes

When the worktree corresponds to a Harbor task (the env var `AGTX_TASK_ID` is set, or a task can be inferred from the current branch name), the per-task acceptance is non-negotiable.

1. Read the task: `mcp__harbor__get_task(task_id)`.
2. Parse the description for these fixed sections:
   - `## Verification Probes` — one shell command per bullet line. Each line is a standalone command.
   - `## Worker Instructions` — optional per-task guidance such as which Android device/emulator this task owns, or a non-local runtime target to use. Harbor also writes this to `.harbor/shared-instructions.md`. If it names a non-local target that differs from the repo default, write a worktree-local override at `<worktree>/.harbor/runtime-target.json` before running probes.
3. Run each probe command via `target-runtime-exec` so it inherits the configured runtime target (the repo `.harbor/runtime-target.json`, or the worktree override) and probe gating.
4. **Hard block.** If any probe exits non-zero, do NOT report success. Append the failing command, exit code, and stderr tail to `<worktree>/.harbor/execute.md` with a UTC timestamp, then emit `blocked classification=acceptance`. The task stays in the Harbor Running column until the probes pass.
5. Repo-default tests still run. Task-scoped probes are additive, not a replacement.

## Hard Rules

- A green pytest run is not sufficient evidence. The task's `## Verification Probes` MUST run and pass.
- Do not invent probes that the task description does not declare. If the section is missing, stop and ask the user to amend the task before continuing.
- Do not skip probes because they "look the same" as a repo-default test. The whole point is that the task author chose specific probes.
