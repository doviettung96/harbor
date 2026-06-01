# Plan: 97d7e970 final cutover to harbor-only

## Task Contract Parsed

Status: planning

Title: Final cutover to harbor-only (drop agtx runtime dep)

Acceptance Criteria:
- With agtx unregistered (only `mcp__harbor__*` tools present), a real task is driven backlog->planning->running->review->done: a worktree is created under `.worktrees/`, a tmux session spawns the agent CLI, a PR opens on the Running->Review transition, and the task lands in Done. Captured in `.harbor/proofs/cutover-e2e.md`.
- `rg "_is_worktree_project" harbor/` returns no matches and its test file is removed.
- `rg "agtx mcp-serve" README.md` returns nothing; install docs register harbor.

Verification Probes:
- `python -m pytest tests/test_webui_agtx.py tests/test_agtx_transitions.py -q -v -s`
- `python scripts/probes/residual_agtx_scan.py`

Worker Instructions:
- Run the end-to-end validation against a THROWAWAY project, not a real one.
- Ensure no `agtx` process is running during validation (`Get-Process agtx` is empty).
- Test-file names may have changed in task 3; current tree still has `tests/test_webui_agtx.py` and `tests/test_agtx_transitions.py`.

Run Repo Defaults:
- yes

Runtime Target:
- Repo default `.agtx/runtime-target.json` is local; no worktree runtime override is needed.

## Context Found

- The current worktree still documents agtx MCP registration in `README.md` and `docs/INSTALL-WINDOWS.md`: `claude mcp add agtx -- agtx mcp-serve`.
- `harbor/__main__.py` currently has `webui`, `daemon`, `webui-diagnose`, `run-bead`, and `run-epic`, but no `mcp-serve` CLI subcommand in this checkout.
- No `harbor/mcp_server.py` file is present in this checkout, so the MCP-server dependency may need to be brought in or recreated from the predecessor task before the cutover docs can be made truthful.
- `_is_worktree_project` currently has no matches under `harbor/webui`; the remaining worktree references are legitimate UI/transition behavior, not that filter.
- `tests/test_webui_agtx.py` and `tests/test_agtx_transitions.py` still exist, matching the first probe path.
- `scripts/probes/residual_agtx_scan.py` does not exist yet, so this task must add it.
- Bootstrap still writes `.agtx` plugin/runtime files and seeds agtx-style tasks through `harbor/bootstrap.py`; the task is about removing the external agtx runtime/MCP registration, not necessarily renaming every internal compatibility module or `.agtx` workflow artifact.
- The end-to-end proof must be run against a disposable project and must not rely on an agtx TUI/process to execute transitions.

## Implementation Plan

1. Confirm predecessor state before editing.
   - Search for the harbor MCP server implementation and tests again after the branch is in Running; if still absent, add the missing Harbor MCP server surface needed by `python -m harbor mcp-serve`.
   - Keep the public tool registration target as `claude mcp add harbor -- python -m harbor mcp-serve`.
   - Do not touch deprecated bead-coupled modules unless a direct reference blocks the cutover.

2. Add or wire the Harbor MCP server CLI.
   - Add a `mcp-serve` subcommand in `harbor/__main__.py`.
   - Implement/reuse a Harbor MCP server module that exposes the task-board operations previously expected from `mcp__agtx__*`, but under `mcp__harbor__*` when registered as `harbor`.
   - Reuse existing Harbor DB/transition code (`harbor/agtx_client.py`, `harbor/agtx_transitions.py`, `harbor/webui/server.py`) rather than introducing a separate board store.
   - Add focused tests for the CLI/server registration path if the predecessor task did not already add them.

3. Update install and runtime docs to the harbor-only path.
   - Replace README quick-start instructions so new projects register Harbor MCP, not agtx MCP.
   - Update `docs/INSTALL-WINDOWS.md` and any bootstrap/plugin docs that instruct users to run `agtx mcp-serve`.
   - Remove wording that says an agtx process/TUI must be running for normal Windows operation; keep "agtx-style" only where it describes the workflow format or existing `.agtx` files.

4. Remove the obsolete worktree-project filter surface.
   - Verify whether `_is_worktree_project` is already gone because a dependency landed it.
   - If a dedicated test file for that filter still exists under a new name, remove it; otherwise leave current worktree UI tests intact.
   - Run `rg "_is_worktree_project" harbor/` as an explicit acceptance check.

5. Add the residual agtx scan probe.
   - Create `scripts/probes/residual_agtx_scan.py`.
   - Make it fail on external-runtime residues that the task forbids, especially `agtx mcp-serve` and docs that tell users to register agtx MCP.
   - Make the allowlist explicit for intentional compatibility names: `.agtx` task artifacts, `agtx-style` workflow wording, agtx schema/client compatibility module names, and the existing test filenames in the required probe.
   - Include checks for `README.md`, install docs, CLI help text, and any harbor-managed registration/bootstrap text.

6. Validate end-to-end on a throwaway project.
   - Create a disposable project under a repo-local temp/proof area rather than using a real project.
   - Before starting validation, run `Get-Process agtx -ErrorAction SilentlyContinue` and record that it returns empty in `.harbor/proofs/cutover-e2e.md`.
   - Register Harbor MCP for the disposable validation context as `harbor` with `python -m harbor mcp-serve`; do not start or depend on any `agtx` process.
   - Drive one real task backlog->planning->running->review->done through Harbor's MCP/webui transition processing so the evidence shows `.worktrees/` creation, tmux session launch, PR creation on Running->Review, and Done.
   - Keep any GitHub/PR work scoped to the throwaway project; if real remote PR creation is not possible in the disposable repo, capture the exact blocker and do not claim the acceptance item passed.

7. Final verification before Review.
   - Re-read `git diff` against the merge-base with `main` and remove unrelated changes.
   - Run probes via the task verifier path:
     - `python scripts/shared/target_runtime.py run -- python -m pytest tests/test_webui_agtx.py tests/test_agtx_transitions.py -q -v -s`
     - `python scripts/shared/target_runtime.py run -- python scripts/probes/residual_agtx_scan.py`
   - Because `## Run Repo Defaults` is `yes`, also expect repo-default tests from `agtx-task-verify`.
   - If any probe fails, stay in Running and write the failure summary to `.agtx/execute.md` instead of advancing.

## Planning Notes

- Current `.agtx/plan.md` was stale and belonged to task `0b7d5ded`; it has been replaced with this task-specific plan.
- Current local dirty state before planning was only `.agtx/shared-instructions.md` plus the stale plan replacement.
- No implementation code has been changed in the Planning phase.
