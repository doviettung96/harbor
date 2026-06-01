# Harbor MCP Smoke Transcript

Date: 2026-06-01

Registration command documented for agent CLIs:

```powershell
claude mcp add harbor -- python -m harbor mcp-serve
```

Smoke execution used the same stdio entrypoint with a temporary agtx config so
no throwaway tasks were added to the real board.

```text
server: python -m harbor mcp-serve
project_id: 201a27d4-f9a0-4dcd-a3df-88bc2006b35e
task_id: 83457e43-cd9c-4d5c-a3c3-4390c92f5670
request_id: 3b0ac8e6-2df7-438e-aac0-5952020040d2
```

## mcp__harbor__list_projects

```json
[
  {
    "id": "201a27d4-f9a0-4dcd-a3df-88bc2006b35e",
    "name": "harbor",
    "path": "\\\\?\\C:\\Users\\Admin\\AppData\\Local\\Temp\\harbor-mcp-smoke-tt6ov7hj\\harbor",
    "github_url": null,
    "default_agent": null
  }
]
```

## mcp__harbor__create_task

```json
{
  "id": "83457e43-cd9c-4d5c-a3c3-4390c92f5670",
  "title": "MCP smoke backlog task",
  "status": "backlog",
  "agent": "codex",
  "deps_satisfied": true,
  "allowed_actions": [
    "move_forward",
    "move_to_planning",
    "move_to_running",
    "research"
  ]
}
```

## mcp__harbor__list_tasks

```json
[
  {
    "id": "83457e43-cd9c-4d5c-a3c3-4390c92f5670",
    "title": "MCP smoke backlog task",
    "status": "backlog",
    "deps_satisfied": true
  }
]
```

## mcp__harbor__get_task

```json
{
  "id": "83457e43-cd9c-4d5c-a3c3-4390c92f5670",
  "title": "MCP smoke backlog task",
  "status": "backlog",
  "blocking_tasks": [],
  "allowed_actions": [
    "move_forward",
    "move_to_planning",
    "move_to_running",
    "research"
  ]
}
```

## mcp__harbor__move_task

```json
{
  "request_id": "3b0ac8e6-2df7-438e-aac0-5952020040d2",
  "message": "Transition 'move_forward' queued for task 83457e43-cd9c-4d5c-a3c3-4390c92f5670. Harbor's existing transition executor will process it."
}
```

## mcp__harbor__get_transition_status

```json
{
  "request_id": "3b0ac8e6-2df7-438e-aac0-5952020040d2",
  "status": "pending",
  "error": null
}
```
