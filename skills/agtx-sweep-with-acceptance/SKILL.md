---
name: agtx-sweep-with-acceptance
description: "Sweep this conversation into agtx tasks with explicit per-task acceptance criteria. Forks the default agtx-sweep flow by asking the user three numbered questions per task before any task is created, embedding the answers as ## Acceptance Criteria, ## Verification Probes, and ## Runtime Target sections in the task description. Use whenever the user wants to push work to the agtx board in this repo."
disable-model-invocation: true
---

# agtx Sweep — With Acceptance Criteria

Same orchestrator role as the default agtx-sweep skill (one task = one reviewable PR; you don't implement, you create and organize). The difference: every task you create MUST carry explicit acceptance criteria, verification probes, and a runtime target, gathered from the user before `create_tasks_batch` fires.

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

5. **Read the repo default runtime target.** Read `.agtx/runtime-target.json` and remember `target.kind` and the kind-specific subobject. You will offer this as a default in step 6.

6. **Per-task acceptance interview — sequential, one task at a time.**

   For each candidate task, present it and ask three numbered questions. Wait for the user to answer all three before moving to the next task.

   Format:
   ```
   Task <N>/<TOTAL> — <title>
   <one-paragraph description>

   1. What artifact, log line, file, or game state proves this works?
      (Do not say "tests pass" — name a concrete observable.)

   2. What command(s) should the worker run to check it?
      (One command per line; will be run via target-runtime-exec.)

   3. Runtime target — does this task need its own emulator/device/game window?
      Or use the repo default `<kind>: <subobject summary>`?
   ```

   - If the user types `default` for question 3, copy the repo default into the task's `## Runtime Target`.
   - If the user gives partial answers, prompt for the missing field. Do not synthesize.
   - If the user says "skip" for a task, drop it from the list and tell them you dropped it.

7. **Build descriptions** by appending three fixed sections to each task's body:

   ```
   <original 2-5 sentence description>

   ## Acceptance Criteria
   - <user's answer to Q1, broken into bullets>

   ## Verification Probes
   - <user's answer to Q2, one bullet per command>

   ## Runtime Target
   <kind, then key=value lines for the relevant subobject — emulator/device/game_window>
   ```

   Use the exact section headers (`## Acceptance Criteria`, `## Verification Probes`, `## Runtime Target`). The worker parses by header — typos break it.

8. **Show full preview.** Print every task with title, description, the three header sections, and dependencies. Then ask: "Send these N tasks to agtx? (yes / edit <N> / cancel)"

9. **Handle the response:**
   - `yes` — proceed.
   - `edit <N>` — ask which field to change, update it, re-show the full list, re-confirm.
   - `cancel` — stop, do nothing.

10. **Create tasks** with `mcp__agtx__create_tasks_batch` (single task: `mcp__agtx__create_task`). Use `referenced_tasks` for dependencies.

11. **Report.** Print each created task ID with title and dependency summary.

## Worked Example — Question Templating

Repo default in `.agtx/runtime-target.json`: `target.kind=device, device.id=127.0.0.1:5555 (adb)`.

```
Task 2/4 — Hook scene_transition probe into Frida loader
The Frida agent must dispatch a SCENE_OK <id> log line on every scene
transition so downstream automation can sync.

1. What artifact, log line, file, or game state proves this works?
2. What command(s) should the worker run to check it?
3. Runtime target — does this task need its own emulator/device/game window?
   Or use repo default `device: 127.0.0.1:5555 (adb)`?
```

User answers:
```
1. SCENE_OK boot appears in adb logcat after the game finishes its boot animation
2. python scripts/probes/scene_transition.py --scene boot
   adb -s 127.0.0.1:5555 logcat -d | rg 'SCENE_OK boot'
3. default
```

Resulting `## Verification Probes` section:
```
## Verification Probes
- python scripts/probes/scene_transition.py --scene boot
- adb -s 127.0.0.1:5555 logcat -d | rg 'SCENE_OK boot'
```

## Hard Rules

- Do not call `create_task` or `create_tasks_batch` until step 10. Steps 1–9 are conversation only.
- Do not invent acceptance criteria. If the user is vague ("the worker can decide"), push back: "What concrete observable do you want to see?"
- Do not omit any of the three sections. Even for trivial tasks, the worker expects all three.
- Do not edit existing tasks (`update_task`) here — sweep is for creation only. Direct the user to the agtx TUI for edits.
- A "tests pass" answer to question 1 is rejected. Re-prompt for an artifact, log line, or game state.
