# Plan: 0b7d5ded /all cross-project web UI board

## Task Contract Parsed

Status: planning

Title: Webui: flat cross-project task view at /all with phase columns, project sub-groups, and signal vocabulary

Acceptance Criteria:
- `GET /all` returns HTTP 200 and renders a 5-column kanban: Backlog, Planning, Running, Review, Done.
- Inside each column, tasks are grouped by project under a project header showing project name + count and a coloured left-bar; project order is stable across columns.
- In Backlog/Planning/Running/Review, every registered project's slot is rendered even when empty with placeholder `-`; Done hides empty project slots.
- Cards in Planning/Running/Review each render exactly one signal glyph on the card head:
  - blue pulse when the task's tmux session is alive
  - amber bang `!` when the session ended or a sentinel was hit
  - red bang `!` when `task.escalation_note` is set
- Cards in Backlog and Done render no signal glyph.
- A sidebar link `All tasks` navigates to `/all`.
- Clicking a card on `/all` opens the existing task drawer via `?task=<id>` with task fields from that task's owning project DB.
- The board refreshes via htmx every 4 seconds, same cadence as the per-project board.

Verification Probes:
- `python -m pytest tests/test_webui_all.py -q -v -s`
- `python -m pytest tests/test_webui_agtx.py -q -v -s`

Runtime Target:
- `local`
- Repo default `.agtx/runtime-target.json` is local, so no worktree runtime override is needed.

Worker Instructions:
- none

Run Repo Defaults:
- yes

## Context Found

- The current `/` and `/projects/{project_id}` board flows render a single selected project through `board.html` plus `_board_partial.html`.
- `GlobalWebuiState.refresh_projects()` already returns all registered project contexts in a stable order from the project provider or agtx global index.
- `AgtxDb.list_tasks()` resolves dependencies and returns per-project tasks; no schema change is needed for aggregation.
- Existing task drawer routes are project-scoped at `/projects/{project_id}/_partials/task/{task_id}` and compatibility routes default to the selected project.
- Existing cards use `data-task-id`, `data-partial-url`, and JS in `board.html` to fetch a drawer partial and update `?task=<id>`.
- Existing tmux state helpers in `server.py` can determine whether a session is live and can capture pane text for non-live sessions.
- Signal state needs a small derived view model because the task card must distinguish live sessions, dead sessions, sentinel-hit panes, escalation, and non-signal statuses.

## Implementation Plan

1. Add a cross-project board view model in `harbor/webui/server.py`.
   - Keep it read-only and derive everything from existing project contexts and task DB rows.
   - Build columns in the existing `COLUMNS` order.
   - For each column, include project groups in `state.refresh_projects()` order.
   - For Backlog/Planning/Running/Review, include every registered and initialized project even when that project has no tasks in the column.
   - For Done, omit project groups that have zero Done tasks.
   - Decide how to handle uninitialized project DBs consistently: show empty placeholder groups for active columns if the project is registered but has no readable task rows; do not fail `/all` because one project DB is missing.

2. Add signal derivation for cross-project cards.
   - Only compute signals for Planning/Running/Review cards.
   - Red wins when `task.escalation_note` is set.
   - Blue wins when `task.session_name` exists and `state.tmux.has_session(...)` returns true.
   - Amber applies when a task has a `session_name` but the tmux session is not live.
   - Amber also applies when captured pane text indicates a completion/blocker sentinel. I will verify the current expected marker before coding; likely candidates are `agtx-verify ... passed/failed/escalated` for agtx tasks, with optional compatibility for `HARBOR-DONE: <task-id> ...` via `harbor.prompt.parse_sentinel`.
   - Backlog and Done cards get no signal even if old session/escalation fields exist.

3. Add `/all` and `/all/_partials/board` routes.
   - `GET /all` renders a new template with the cross-project board and optional drawer.
   - The board section uses `hx-get="/all/_partials/board"` and `hx-trigger="every 4s"`.
   - If `?task=<id>` is present, resolve the owning project by searching initialized project DBs for that task ID, then reuse `_task_detail_context(...)`.
   - If the same task ID appears in more than one project, pick the first project in stable project order.
   - Return 404 if no owning project contains the task.

4. Add templates for the flat board.
   - Create `all.html` and `_all_board_partial.html`, reusing the visual language from `board.html` without introducing write controls.
   - Render five columns with project subgroups, project header count, stable color left-bar, and placeholder `-` for empty active-column groups.
   - Render cards with links like `/all?task=<id>` and `data-partial-url="/projects/<project_id>/_partials/task/<task_id>"` so the existing drawer endpoint returns the correct project task.
   - Add minimal CSS for project groups, project color bars, signal glyphs, and pulse animation inside the `/all` template.

5. Update sidebar navigation in `base.html`.
   - Add an `All tasks` link near the project list.
   - Keep existing project links unchanged.

6. Add `tests/test_webui_all.py`.
   - Build an app with multiple in-memory project DBs and fake tmux, using existing `Project`, `AgtxDb`, `init_test_db`, and `insert_test_task` helpers.
   - Cover HTTP 200, five column labels, project grouping, stable project order, active-column placeholders, Done hiding empty project slots, sidebar link, htmx 4-second refresh, and card drawer URLs.
   - Cover signal precedence and absence:
     - live Planning/Running/Review task gets blue pulse
     - dead session or sentinel-hit task gets amber bang
     - escalation gets red bang
     - Backlog/Done cards get no signal
   - Cover `/all?task=<id>` preloads the drawer from the owning project DB.

7. Run verification in the Running phase through `agtx-task-verify`.
   - `python -m pytest tests/test_webui_all.py -q -v -s`
   - `python -m pytest tests/test_webui_agtx.py -q -v -s`
   - Because `Run Repo Defaults` is `yes`, let `agtx-task-verify` run the repo default build/test path after these probes pass.

## Notes / Risks

- The amber "sentinel hit" definition is the one ambiguous implementation detail. I will ground it in existing harbor/agtx output parsing before editing, and tests will pin the chosen marker vocabulary.
- The existing `/all` task query only carries `task=<id>`, not project ID, so route-side owner resolution is required. The card partial URL can still be project-scoped for click-time drawer fetches.
- This task is UI aggregation only. I will not add write routes, DB columns, or change per-project board behavior except where shared template helpers make that unavoidable.
