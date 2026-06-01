# Harbor-Only Cutover E2E Proof

Validation date: 2026-06-01

Scope: disposable project under `.harbor/proofs/cutover-e2e-work/throwaway-project`.

## Preconditions

- `Get-Process agtx -ErrorAction SilentlyContinue` was empty during validation.
- Harbor MCP stdio server was probed with `python scripts/probes/mcp_tools_list.py`.
- MCP probe output: `mcp tools/list ok`.
- No real project was used. Harbor data was redirected to `.harbor/proofs/cutover-e2e-work/harbor-config`.

## Validation Path

The validation registered the throwaway project in Harbor's isolated data store, created a real board task through `HarborMcpService.create_task`, then drove the task with `HarborMcpService.move_task` plus `TransitionWorker.process_once`.

The throwaway git project used a local bare `origin` remote, so `git worktree add` and `git push -u origin task/<id>` ran against disposable local repositories. The PR opener was shimmed in-process to avoid creating or touching a real GitHub repository; the shim returned `https://example.invalid/harbor-cutover/pull/1` and verified Harbor called the PR-open branch on Running->Review.

## Evidence

Task ID: `7959f33c-1cdc-4aec-917b-465ac46503d9`

Worktree created under `.worktrees`: yes

Tmux session live after Planning: yes

Final status: `done`

Final PR URL stored on task: `https://example.invalid/harbor-cutover/pull/1`

Transition states:

```json
[
  {
    "action": "move_forward",
    "processed": 1,
    "status": "planning",
    "session_name": "task-7959f33c--1e14edda-4765-49f4-ad68-499c0df72ce0--throwaway-cutover-ta",
    "worktree_path": "D:\\Projects\\harbor\\.worktrees\\task-97d7e970\\.harbor\\proofs\\cutover-e2e-work\\throwaway-project\\.worktrees\\task-7959f33c",
    "branch_name": "task/7959f33c",
    "pr_url": null
  },
  {
    "action": "move_forward",
    "processed": 1,
    "status": "running",
    "session_name": "task-7959f33c--1e14edda-4765-49f4-ad68-499c0df72ce0--throwaway-cutover-ta",
    "worktree_path": "D:\\Projects\\harbor\\.worktrees\\task-97d7e970\\.harbor\\proofs\\cutover-e2e-work\\throwaway-project\\.worktrees\\task-7959f33c",
    "branch_name": "task/7959f33c",
    "pr_url": null
  },
  {
    "action": "move_forward",
    "processed": 1,
    "status": "review",
    "session_name": "task-7959f33c--1e14edda-4765-49f4-ad68-499c0df72ce0--throwaway-cutover-ta",
    "worktree_path": "D:\\Projects\\harbor\\.worktrees\\task-97d7e970\\.harbor\\proofs\\cutover-e2e-work\\throwaway-project\\.worktrees\\task-7959f33c",
    "branch_name": "task/7959f33c",
    "pr_url": "https://example.invalid/harbor-cutover/pull/1",
    "pr_number": 1,
    "escalation_note": null
  },
  {
    "action": "move_forward",
    "processed": 1,
    "status": "done",
    "session_name": "task-7959f33c--1e14edda-4765-49f4-ad68-499c0df72ce0--throwaway-cutover-ta",
    "worktree_path": null,
    "branch_name": "task/7959f33c",
    "pr_url": "https://example.invalid/harbor-cutover/pull/1",
    "pr_number": 1,
    "escalation_note": null
  }
]
```
