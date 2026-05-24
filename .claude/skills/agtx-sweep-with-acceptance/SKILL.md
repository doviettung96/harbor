---
name: agtx-sweep-with-acceptance
description: "Sweep this conversation into agtx tasks with explicit per-task acceptance criteria. Forks the default agtx-sweep flow by asking Q1 as the primary task-scoped acceptance question, deriving Q2/Q5 proposals for confirmation, defaulting Q3/Q4 unless overrides are needed, and embedding the answers as fixed task-description sections. Use whenever the user wants to push work to the agtx board in this repo."
disable-model-invocation: true
---

# agtx Sweep - With Acceptance Criteria

Same orchestrator role as the default agtx-sweep skill (one task = one reviewable PR; you don't implement, you create and organize). The difference: every task you create MUST carry explicit acceptance criteria, verification probes, runtime target, and per-task worker instructions, confirmed with the user before `create_tasks_batch` fires.

The user does not trust pytest "green = done." Each task's worker reads the embedded `## Verification Probes` and `build-and-test` runs them. If you skip the acceptance step, the worker has nothing to verify against and the task ships broken.

## When to Use

- User says "sweep this", "push these to the board", "make tasks for all of this", "decompose this into tasks", or similar.
- A brainstorming or planning conversation has settled on a list of work items.

Do not invoke for casual chatter or single-task requests where direct execution is appropriate.

## Flow

1. **Verify MCP connection.** Call `mcp__agtx__list_projects`. If it fails, instruct the user to install agtx and register the MCP server, then stop.

2. **Identify the project.** Resolve the agtx project for the current working directory. If ambiguous, ask the user.

3. **List existing tasks.** Call `mcp__agtx__list_tasks` with the project_id. Skip anything that duplicates an existing task.

4. **Extract candidate tasks** from the conversation. One task = one PR. Atomic. Wire dependencies in your head, not in the task list yet.

5. **Read the repo default runtime target.** Read `.agtx/runtime-target.json` and remember `target.kind` and the kind-specific subobject. This is the default runtime for Q3 unless the task plausibly needs an override.

6. **Per-task acceptance interview - sequential, one task at a time.**

   For each candidate task, present it and ask Q1 as the primary question. Q1 is mandatory and user-authored. The orchestrator may derive Q2 from Q1, propose Q5 from project context, and use defaults for Q3/Q4 when no override is needed. Wait for the user to confirm the proposed fields before moving to the next task.

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

   Runtime target defaults to the repo default from step 5. Ask Q3 only when the task plausibly needs a per-task emulator, device, game window, or SSH override:

   ```
   Q3. Runtime target override? Default is `<kind>: <subobject summary>`.
   ```

   Worker instructions default to `none`. Ask Q4 only when the task needs an exclusive resource claim or special task-scoped prompt guidance:

   ```
   Q4. Worker instructions override? Default is `none`.
   ```

   Propose Q5 and ask for confirmation:

   ```
   Proposed Q5 run repo-default build/tests during verification: <yes|no>
   Reason: <harbor: re-serve harbor + run Q1 tests; other repos: rerun project build + Q1 tests, or explain why probes-only is enough>

   OK? (yes / edit)
   ```

   - Q1 stays primary and must come from the user. Do not invent it.
   - Q2 is derived from Q1 by the orchestrator and proposed for user confirmation. If the user edits it, use the edited command(s). If no reliable command can be derived from Q1, ask Q2 directly.
   - Q3 defaults to the repo runtime target. If the user accepts the default, copy the repo default into the task's `## Runtime Target`.
   - Q4 defaults to `none`. If no exclusive resource or special prompt guidance is needed, write `none` under `## Worker Instructions`.
   - Q5 is proposed by the orchestrator and confirmed by the user. For harbor, the project-specific default is "re-serve harbor + run Q1 tests." For other repos, the generic default is "rerun project build + Q1 tests." Accept `yes`/`y`/`true` as opt-in and `no`/`n`/`false` as opt-out.
   - If the user gives partial answers or rejects a proposal, prompt only for the missing or rejected field.
   - If the user says "skip" for a task, drop it from the list and tell them you dropped it.

7. **Build descriptions** by appending five fixed sections to each task's body:

   ```
   <original 2-5 sentence description>

   ## Acceptance Criteria
   - <user's answer to Q1, broken into bullets>

   ## Verification Probes
   - <confirmed Q2 command, one bullet per command>

   ## Runtime Target
   <repo default, or confirmed override: kind plus key=value lines for emulator/device/game_window>

   ## Worker Instructions
   <confirmed Q4 instructions, or `none`>

   ## Run Repo Defaults
   <confirmed yes or no, normalized from Q5>
   ```

   Use the exact section headers (`## Acceptance Criteria`, `## Verification Probes`, `## Runtime Target`, `## Worker Instructions`, `## Run Repo Defaults`). The worker and verify skills parse by header - typos break them.

8. **Show full preview.** Print every task with title, description, all five fixed sections, and dependencies. Then ask: "Send these N tasks to agtx? (yes / edit <N> / cancel)"

9. **Handle the response:**
   - `yes` - proceed.
   - `edit <N>` - ask which field to change, update it, re-show the full list, re-confirm.
   - `cancel` - stop, do nothing.

10. **Create tasks** with `mcp__agtx__create_tasks_batch` (single task: `mcp__agtx__create_task`). Use `referenced_tasks` for dependencies.

11. **Report.** Print each created task ID with title and dependency summary.

## Worked Example - Question Templating

Repo default in `.agtx/runtime-target.json`: `target.kind=device, device.id=127.0.0.1:5555 (adb)`.

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

Q3 runtime target: use repo default `device: 127.0.0.1:5555 (adb)`.
Q4 worker instructions: Claim device 127.0.0.1:5555 for this task. For ADB commands, pass `-s 127.0.0.1:5555` or set `ANDROID_SERIAL=127.0.0.1:5555`.
Proposed Q5 run repo defaults: yes
Reason: rerun project build + Q1 tests after the task probes pass.

OK? (yes / edit)
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

## Runtime Target
device
id=127.0.0.1:5555
kind=adb

## Worker Instructions
Claim device 127.0.0.1:5555 for this task. For ADB commands, pass `-s 127.0.0.1:5555` or set `ANDROID_SERIAL=127.0.0.1:5555`.

## Run Repo Defaults
yes
```

## Hard Rules

- Do not call `create_task` or `create_tasks_batch` until step 10. Steps 1-9 are conversation only.
- Do not invent Q1 acceptance criteria. If the user is vague ("the worker can decide"), push back: "What concrete observable do you want to see?" You may propose Q2 verification probes and Q5 repo-default behavior from Q1 and project context, but the user must confirm them before task creation.
- Do not omit any of the five sections. Even for trivial tasks, the worker expects the section layout.
- Do not edit existing tasks (`update_task`) here - sweep is for creation only. Direct the user to the agtx TUI for edits.
- A "tests pass" answer to Q1 is rejected. Re-prompt for an artifact, log line, or game state.
