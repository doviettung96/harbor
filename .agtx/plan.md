# Plan: 51d5085d Harbor MCP server

## Task Contract Parsed

Status: planning

Title: Harbor MCP server (mcp__harbor__*, all 13 tools)

Acceptance Criteria:
- With `claude mcp add harbor -- python -m harbor mcp-serve`, a recorded smoke transcript saved to `.agtx/proofs/mcp-smoke.md` shows:
  - `mcp__harbor__list_projects` returns the harbor project.
  - `mcp__harbor__create_task` then `mcp__harbor__list_tasks` shows the new backlog task.
  - `mcp__harbor__get_task` returns it with a non-empty `allowed_actions` consistent with its status.
  - `mcp__harbor__move_task` returns a transition request id that `mcp__harbor__get_transition_status` reports on.
- All 13 tools are registered and callable; a stdio self-test issuing `tools/list` returns exactly the 13 tool names.
- `get_task.allowed_actions` matches harbor's transition rules for each status, with parity to the prior agtx-computed lists.
- `move_task` does not execute side effects itself; it only queues a `transition_request` for harbor's existing executor.

Verification Probes:
- `python -m pytest tests/test_mcp_server.py -q -v -s`
- `python scripts/probes/mcp_tools_list.py`

Runtime Target:
- Repo default `.agtx/runtime-target.json` is local.
- `## Worker Instructions` is `none`, so no worktree runtime override is needed.

Run Repo Defaults:
- yes

## Context Found

- `mcp` is installed locally at version `1.18.0`; `mcp.server.fastmcp.FastMCP` exposes `tool`, `list_tools`, `call_tool`, and stdio runners.
- Harbor currently has no MCP server module, no `mcp-serve` CLI subcommand, no `tests/test_mcp_server.py`, and no `scripts/probes/mcp_tools_list.py`.
- Existing reusable surfaces:
  - `harbor/agtx_client.py`: SQLite path resolution, task/project dataclasses, task CRUD, transition request queueing, notifications.
  - `harbor/agtx_transitions.py`: transition rules, session/worktree side effects, tmux helpers, plugin-aware phase behavior.
  - `harbor/plugin_loader.py`: plugin loading and phase prompt/command helpers.
  - `harbor/tmux.py`: pane capture and send-keys wrappers on the Harbor tmux server.
  - `harbor/webui/server.py`: existing web UI queueing behavior and dependency gate checks to mirror.
- `AgtxDb` already supports most database operations but needs small additions for MCP parity: lookup a transition request by id, consume notifications, delete backlog tasks, and batch create dependency wiring if implemented at the DB layer.
- The agtx reference keeps Backlog mostly user-triaged, but this task's smoke
  acceptance explicitly requires a newly-created Backlog task to expose
  non-empty `allowed_actions`. The MCP implementation should therefore follow
  Harbor's transition map for Backlog while still dependency-gating forward
  Backlog moves.

## Implementation Plan

1. Add an MCP service module, likely `harbor/mcp_server.py`.
   - Build a `FastMCP("harbor")` app and register exactly these 13 tools:
     `list_projects`, `list_tasks`, `get_task`, `move_task`, `get_transition_status`, `check_conflicts`, `get_notifications`, `read_pane_content`, `send_to_task`, `create_task`, `create_tasks_batch`, `update_task`, `delete_task`.
   - Keep global mode: every project-scoped tool requires `project_id`; `list_projects` is the discovery entrypoint.
   - Return structured JSON-serializable dict/list payloads, with error strings only where the existing agtx MCP contract does so.

2. Add CLI entrypoint wiring in `harbor/__main__.py`.
   - Add `mcp-serve` subcommand.
   - It should import the MCP app lazily and run stdio transport.
   - Do not change webui/daemon behavior.

3. Reuse Harbor's existing DB and transition code rather than reimplementing behavior.
   - Project resolution should use the global agtx index via `AgtxDb(...).list_projects()` and open project DBs by selected `project_id`.
   - `list_tasks` and `get_task` should map existing `Task` objects to response dicts including dependency state.
   - `move_task` should validate action names, verify the task exists, apply the same backlog dependency gate as `webui.server._queue_transition`, then call `AgtxDb.create_transition_request(...)` and return the request id.
   - It must not instantiate `TransitionWorker`, create worktrees, start tmux, push branches, or call transition side-effect functions.

4. Add shared helpers needed for response parity.
   - `allowed_actions(task)` helper in the MCP module or a small shared location; tests will pin all five statuses.
   - `blocking_tasks` serialization from `task.blocking_dependencies`.
   - DB helpers in `agtx_client.py` only where they are generally useful:
     `get_transition_request`, `consume_notifications`, `delete_task`, and possibly a dependency-aware batch create helper.
   - Keep `ALLOWED_TASK_UPDATE_COLUMNS` as the guard for `update_task`; validate Backlog-only updates/deletes in the MCP layer to match the tool descriptions.

5. Implement operational tools with existing wrappers.
   - `get_transition_status`: read one transition request and report `pending`, `completed`, or `error`.
   - `get_notifications`: consume pending notifications for the selected project. If the current DB helper only lists, add consume semantics as a delete-after-read operation.
   - `read_pane_content`: load the task session name and use `Tmux.capture_pane(..., lines=N)`.
   - `send_to_task`: load the task session name and use `Tmux.send_keys_literal(...)`.
   - `check_conflicts`: perform a read-only git conflict check for one task or all Review tasks. Prefer a helper using `git merge-base`, `git merge-tree`, and/or a temporary index/worktree-free strategy so the repository files are not modified.

6. Implement creation/update/delete tools.
   - `create_task`: call `AgtxDb.create_task(...)` with Backlog status and project defaults where available.
   - `create_tasks_batch`: create tasks in input order and translate index-based `depends_on` into comma-separated created task IDs; reject forward references.
   - `update_task`: only allow Backlog tasks; update title, description, plugin, referenced tasks, and base branch fields.
   - `delete_task`: only allow Backlog tasks; remove the task row without touching worktrees or sessions.

7. Add focused tests in `tests/test_mcp_server.py`.
   - Assert `tools/list` returns exactly the 13 expected tool names.
   - Exercise list/create/list/get/move/status against in-memory or temp SQLite DBs.
   - Pin `allowed_actions` for Backlog, Planning, Running, Review, and Done.
   - Assert `move_task` queues exactly one transition request and does not call transition side-effect code.
   - Cover batch dependency wiring, update/delete Backlog restrictions, pane read/send via mocked `Tmux`, notification consume behavior, and conflict-check response shape.

8. Add the required stdio probe script `scripts/probes/mcp_tools_list.py`.
   - Start `python -m harbor mcp-serve` as a subprocess.
   - Issue MCP initialize plus `tools/list` over stdio using the installed SDK or raw JSON-RPC if the SDK test client is not convenient.
   - Assert the tool names exactly match the 13-tool contract and exit non-zero on mismatch.

9. Produce the smoke transcript in `.agtx/proofs/mcp-smoke.md`.
   - Capture the exact manual or scripted interaction showing `claude mcp add harbor -- python -m harbor mcp-serve` and calls to the required `mcp__harbor__*` tools.
   - Use a harmless backlog smoke task title so it is easy to identify and delete later if needed.
   - Include the returned transition request id and the matching transition status output.

10. Verification in Running phase.
   - Run `agtx-task-verify`, which must execute:
     - `python -m pytest tests/test_mcp_server.py -q -v -s`
     - `python scripts/probes/mcp_tools_list.py`
   - Because `Run Repo Defaults` is `yes`, let `agtx-task-verify` also run repo defaults after the probes pass.

## Risks / Open Details

- The installed Python `mcp` SDK is available, but I will keep the stdio probe resilient by testing the real `python -m harbor mcp-serve` subprocess rather than only calling FastMCP in process.
- The task requires both "allowed_actions consistent with harbor's transition rules" and a non-empty Backlog smoke result. I will prioritize the task's Harbor transition-map wording for Backlog and keep tests explicit.
- `check_conflicts` needs careful read-only git handling. I will test it with mocked subprocess calls first and avoid any command that writes into the user's worktree.
- The smoke proof writes under `.agtx/proofs/`, which is explicitly required by acceptance and should be the only non-code artifact added besides the probe script and tests.
