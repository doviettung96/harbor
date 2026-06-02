---
name: harbor-sweep-with-acceptance
description: "Sweep this conversation into Harbor tasks with explicit per-task acceptance criteria. Forks the default sweep flow by asking Q1 as the primary task-scoped acceptance question, deriving Q2 verification probes for confirmation, defaulting Q3 worker instructions, and proposing Q4 related tests, then embedding the answers as fixed task-description sections. Use whenever the user wants to push work to the Harbor board in this repo."
disable-model-invocation: true
---

# Harbor Sweep - With Acceptance Criteria

Same orchestrator role as the default sweep skill (one task = one reviewable PR; you don't implement, you create and organize). The difference: every task you create MUST carry explicit acceptance criteria, verification probes, related tests, and per-task worker instructions, confirmed with the user before `create_tasks_batch` fires.

The user does not trust pytest "green = done." Each task's worker reads the embedded `## Verification Probes` and `build-and-test` runs them. If you skip the acceptance step, the worker has nothing to verify against and the task ships broken.

Build is NOT a sweep question. The project's build command lives in `harbor.yml` as `harbor.build` (a single shell command — e.g. a script that kills the running app's processes and rebuilds the exe via pyinstaller; absent / no-op for a `python main.py` project). `harbor-task-verify` runs it **always**, before any tests, via `target-runtime-exec`, so tests run against current code rather than a stale running instance. The sweep never asks about it and never restates it in a task.

Runtime target is NOT a sweep question. The repo's `.harbor/runtime-target.json` is the single source of truth for where commands run, and `target-runtime-exec` reads it directly — it does not need the task description to restate it. Almost all work is local. When a single task genuinely needs a different target (a remote SSH host, a specific emulator/device/game window), say so in plain language under `## Worker Instructions`; the worker translates that into a worktree-local override.

## When to Use

- User says "sweep this", "push these to the board", "make tasks for all of this", "decompose this into tasks", or similar.
- A brainstorming or planning conversation has settled on a list of work items.

Do not invoke for casual chatter or single-task requests where direct execution is appropriate.

## Flow

1. **Verify MCP connection.** Call `mcp__harbor__list_projects`. If it fails, instruct the user to install Harbor and register the MCP server, then stop.

2. **Identify the project.** Resolve the Harbor project for the current working directory. If ambiguous, ask the user.

3. **List existing tasks.** Call `mcp__harbor__list_tasks` with the project_id. Skip anything that duplicates an existing task.

4. **Extract candidate tasks** from the conversation. One task = one PR. Atomic. Wire dependencies in your head, not in the task list yet.

5. **Per-task acceptance interview - sequential, one task at a time.**

   For each candidate task, present it and ask Q1 as the primary question. Q1 is mandatory and user-authored. The orchestrator may derive Q2 from Q1, default Q3 to `none`, and propose Q4 (related tests) from the task description. Wait for the user to confirm the proposed fields before moving to the next task.

   Format:
   ```
   Task <N>/<TOTAL> - <title>
   <one-paragraph description>

   Q1. What artifact, log line, file, or game state proves this works?
      (Do not say "tests pass" - name a concrete observable.)
   ```

   After Q1 is answered, derive Q2 from Q1 and ask for confirmation:

   ```
   Proposed Q2 verification probe(s), derived from Q1:
   - <one command per line; will be run via target-runtime-exec>

   OK? (yes / edit)
   ```

   Worker instructions default to `none`. Ask Q3 only when the task needs an exclusive resource claim, a non-local runtime target, or special task-scoped prompt guidance:

   ```
   Q3. Worker instructions override? Default is `none`.
      (Use this for an exclusive resource, a non-local runtime target such as
       an SSH host / emulator / device, or any special task-scoped guidance.)
   ```

   Propose Q4 (related tests) and ask for confirmation. Derive these from the task
   description — the existing test files/areas this change plausibly affects and
   should be re-run alongside the task's own probe:

   ```
   Proposed Q4 related tests to run alongside this task's probe (from the description):
   - tests/test_<area>.py — <one-line reason it's related>
   (flag any with `(update: <what must change>)` if this task makes that test stale)

   OK? (yes / drop <file> / update <file> / edit / none)
   ```

   - Q1 stays primary and must come from the user. Do not invent it.
   - Q2 is derived from Q1 by the orchestrator and proposed for user confirmation. If the user edits it, use the edited command(s). If no reliable command can be derived from Q1, ask Q2 directly.
   - Q3 defaults to `none`. If no exclusive resource, non-local target, or special prompt guidance is needed, write `none` under `## Worker Instructions`. If the task needs a non-local runtime target, describe it here in plain language (e.g. "Run probes against SSH host gpu-box" or "Claim emulator Pixel_7"); the worker writes the worktree-local `.harbor/runtime-target.json` override from this instruction.
   - Q4 is the related-tests list, proposed by the orchestrator from the task description and confirmed by the user. The user may `drop` any (it won't run) or flag any with `(update: ...)` — meaning this task makes that test stale, so the worker updates it as part of the task and then runs it. If no existing test is plausibly affected, write `none`. Do NOT propose running the whole suite; name specific related tests only. Build is not asked here (it lives in `harbor.yml` and always runs).
   - If the user gives partial answers or rejects a proposal, prompt only for the missing or rejected field.
   - If the user says "skip" for a task, drop it from the list and tell them you dropped it.

6. **Build descriptions** by appending four fixed sections to each task's body:

   ```
   <original 2-5 sentence description>

   ## Acceptance Criteria
   - <user's answer to Q1, broken into bullets>

   ## Verification Probes
   - <confirmed Q2 command, one bullet per command>

   ## Related Tests
   <confirmed Q4 list, one bullet per test (optionally `(update: ...)`), or `none`>

   ## Worker Instructions
   <confirmed Q3 instructions, or `none`>
   ```

   Use the exact section headers (`## Acceptance Criteria`, `## Verification Probes`, `## Related Tests`, `## Worker Instructions`). The worker and verify skills parse by header - typos break them.

7. **Show full preview.** Print every task with title, description, all four fixed sections, and dependencies. Then ask: "Send these N tasks to Harbor? (yes / edit <N> / cancel)"

8. **Handle the response:**
   - `yes` - proceed.
   - `edit <N>` - ask which field to change, update it, re-show the full list, re-confirm.
   - `cancel` - stop, do nothing.

9. **Create tasks** with `mcp__harbor__create_tasks_batch` (single task: `mcp__harbor__create_task`). Use `referenced_tasks` for dependencies.

10. **Report.** Print each created task ID with title and dependency summary.

## Worked Example - Question Templating

Repo default in `.harbor/runtime-target.json`: `target.kind=device, device.id=127.0.0.1:5555 (adb)`. This is the runtime for every task; the sweep never restates it.

```
Task 2/4 - Hook scene_transition probe into Frida loader
The Frida agent must dispatch a SCENE_OK <id> log line on every scene
transition so downstream automation can sync.

Q1. What artifact, log line, file, or game state proves this works?
```

User answers Q1:
```
SCENE_OK boot appears in adb logcat after the game finishes its boot animation
```

Orchestrator proposes:
```
Proposed Q2 verification probe(s), derived from Q1:
- python scripts/probes/scene_transition.py --scene boot
- adb -s 127.0.0.1:5555 logcat -d | rg 'SCENE_OK boot'

Q3 worker instructions: Claim device 127.0.0.1:5555 for this task. For ADB commands, pass `-s 127.0.0.1:5555` or set `ANDROID_SERIAL=127.0.0.1:5555`.
Proposed Q4 related tests to run alongside this task's probe (from the description):
- tests/test_frida_loader.py — this task changes the Frida loader's dispatch path

OK? (yes / drop <file> / update <file> / edit / none)
```

User confirms:
```
yes
```

Resulting trailing sections:
```
## Acceptance Criteria
- SCENE_OK boot appears in adb logcat after the game finishes its boot animation

## Verification Probes
- python scripts/probes/scene_transition.py --scene boot
- adb -s 127.0.0.1:5555 logcat -d | rg 'SCENE_OK boot'

## Related Tests
- tests/test_frida_loader.py — this task changes the Frida loader's dispatch path

## Worker Instructions
Claim device 127.0.0.1:5555 for this task. For ADB commands, pass `-s 127.0.0.1:5555` or set `ANDROID_SERIAL=127.0.0.1:5555`.
```

## Hard Rules

- Do not call `create_task` or `create_tasks_batch` until step 9. Steps 1-8 are conversation only.
- Do not invent Q1 acceptance criteria. If the user is vague ("the worker can decide"), push back: "What concrete observable do you want to see?" You may propose Q2 verification probes and Q4 related tests from Q1, the task description, and project context, but the user must confirm them before task creation.
- Do not omit any of the four sections (`## Acceptance Criteria`, `## Verification Probes`, `## Related Tests`, `## Worker Instructions`). Even for trivial tasks, the worker expects the section layout. If a section has no content, write `none` (Related Tests / Worker Instructions) — never leave it blank.
- Do not add a `## Run Repo Defaults` section or ask whether to run the whole suite — that mechanism is removed. Build always runs (from `harbor.yml`); tests are the task's own probe plus the named `## Related Tests`.
- Do not add a `## Runtime Target` section. The repo `.harbor/runtime-target.json` is the source of truth; non-local overrides live in `## Worker Instructions` as plain text.
- Do not edit existing tasks (`update_task`) here - sweep is for creation only. Direct the user to the Harbor UI for edits.
- A "tests pass" answer to Q1 is rejected. Re-prompt for an artifact, log line, or game state.
