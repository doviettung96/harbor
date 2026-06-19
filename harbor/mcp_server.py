from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import agtx_client as ac
from .agtx_client import AgtxDb, Project, Task, strip_extended_length_prefix
from .tmux import Tmux


TOOL_NAMES: tuple[str, ...] = (
    "list_projects",
    "list_tasks",
    "get_task",
    "move_task",
    "get_transition_status",
    "check_conflicts",
    "get_notifications",
    "read_pane_content",
    "send_to_task",
    "create_task",
    "create_tasks_batch",
    "update_task",
    "delete_task",
    "list_resources",
    "acquire_runtime",
    "release_runtime",
)

VALID_MOVE_ACTIONS = frozenset({
    "research",
    "move_forward",
    "move_to_planning",
    "move_to_running",
    "move_to_review",
    "move_to_done",
    "resume",
    "escalate_to_user",
})


class HarborMcpService:
    def __init__(self, *, tmux: Tmux | None = None) -> None:
        self.tmux = tmux or Tmux()

    # ---- DB/project resolution -----------------------------------------

    def _global_db(self) -> AgtxDb:
        return AgtxDb(project_db_p=Path("__global_only__"), global_db_p=ac.global_db_path())

    def _projects(self) -> list[Project]:
        return self._global_db().list_projects()

    def _project(self, project_id: str | None) -> Project:
        if not project_id:
            raise ValueError("project_id is required in global mode; call list_projects first")
        for project in self._projects():
            if project.id == project_id or project.name == project_id:
                return project
        raise ValueError(f"project not found: {project_id}")

    def _project_db(self, project_id: str | None) -> tuple[Project, AgtxDb]:
        project = self._project(project_id)
        db_path = ac.harbor_data_dir() / "projects" / f"{ac.hash_project_path(project.path)}.db"
        return project, AgtxDb(project_db_p=db_path, global_db_p=ac.global_db_path())

    # ---- Response mapping ----------------------------------------------

    def _task_summary(self, task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "agent": task.agent,
            "branch_name": task.branch_name,
            "pr_url": task.pr_url,
            "plugin": task.plugin,
            "referenced_tasks": task.referenced_tasks,
            "base_branch": task.base_branch,
            "deps_satisfied": task.deps_satisfied,
        }

    def _task_detail(self, task: Task) -> dict[str, Any]:
        data = self._task_summary(task)
        data.update({
            "project_id": task.project_id,
            "session_name": task.session_name,
            "worktree_path": task.worktree_path,
            "pr_number": task.pr_number,
            "cycle": task.cycle,
            "escalation_note": task.escalation_note,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "blocking_tasks": [
                {"id": dep.id, "title": dep.title, "status": dep.status}
                for dep in task.blocking_dependencies
            ],
            "allowed_actions": allowed_actions(task),
        })
        return data

    # ---- Tools ----------------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        return [asdict(project) for project in self._projects()]

    def list_tasks(self, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        _, db = self._project_db(project_id)
        return [self._task_summary(task) for task in db.list_tasks(status=status)]

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        return self._task_detail(task)

    def move_task(
        self,
        project_id: str,
        task_id: str,
        action: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if action not in VALID_MOVE_ACTIONS:
            raise ValueError(
                f"invalid action: {action!r}; valid actions: {', '.join(sorted(VALID_MOVE_ACTIONS))}"
            )
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if action in {"move_forward", "move_to_planning", "move_to_running", "research"}:
            if task.status == "backlog" and not task.deps_satisfied:
                blockers = ", ".join(
                    f"{dep.short_id} {dep.title} [{dep.status}]"
                    for dep in task.blocking_dependencies
                )
                raise ValueError(f"task is blocked by dependencies: {blockers}")
        request_id = db.create_transition_request(
            task_id=task_id,
            action=action,
            reason=reason or None,
        )
        return {
            "request_id": request_id,
            "message": (
                f"Transition {action!r} queued for task {task_id}. "
                "Harbor's existing transition executor will process it."
            ),
        }

    def get_transition_status(self, project_id: str, request_id: str) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        req = db.get_transition_request(request_id)
        if req is None:
            raise ValueError(f"transition request not found: {request_id}")
        if req.processed_at is None:
            status = "pending"
        elif req.error:
            status = "error"
        else:
            status = "completed"
        return {"request_id": req.id, "status": status, "error": req.error}

    def check_conflicts(
        self,
        project_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        project, db = self._project_db(project_id)
        project_path = strip_extended_length_prefix(project.path)
        main_branch = _detect_main_branch(project_path)
        tasks = [db.get_task(task_id)] if task_id else db.list_tasks(status="review")
        results: list[dict[str, Any]] = []
        for task in tasks:
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            results.append(_check_task_conflicts(project_path, main_branch, task))
        return {"main_branch": main_branch, "results": results}

    def get_notifications(self, project_id: str) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        return {
            "notifications": [
                {"message": n.message, "created_at": n.created_at}
                for n in db.consume_notifications()
            ]
        }

    def read_pane_content(
        self,
        project_id: str,
        task_id: str,
        lines: int = 50,
    ) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if not task.session_name:
            raise ValueError(f"task {task_id} has no session_name")
        requested = max(1, min(int(lines or 50), 10000))
        return {
            "task_id": task_id,
            "session_name": task.session_name,
            "content": self.tmux.capture_pane(task.session_name, "", lines=requested),
            "lines_requested": requested,
        }

    def send_to_task(
        self,
        project_id: str,
        task_id: str,
        message: str,
    ) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if not task.session_name:
            raise ValueError(f"task {task_id} has no session_name")
        self.tmux.send_keys_literal(task.session_name, "", message, enter=True)
        return {"task_id": task_id, "session_name": task.session_name, "sent": True}

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        project, db = self._project_db(project_id)
        task = db.create_task(
            title=title,
            description=description or "",
            project_id=project.id,
            agent=project.default_agent or "codex",
            status="backlog",
            plugin=plugin,
            referenced_tasks=referenced_tasks,
            base_branch=base_branch,
        )
        return self._task_detail(task)

    def create_tasks_batch(
        self,
        project_id: str,
        tasks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        created: list[Task] = []
        for idx, item in enumerate(tasks):
            depends_on = item.get("depends_on") or []
            if any(not isinstance(dep, int) or dep < 0 or dep >= idx for dep in depends_on):
                raise ValueError("depends_on entries must reference earlier task indices")
            referenced = ",".join(created[dep].id for dep in depends_on)
            if item.get("referenced_tasks"):
                referenced = item["referenced_tasks"]
            created_task = self.create_task(
                project_id=project_id,
                title=str(item["title"]),
                description=item.get("description"),
                plugin=item.get("plugin"),
                referenced_tasks=referenced or None,
                base_branch=item.get("base_branch"),
            )
            _, db = self._project_db(project_id)
            task = db.get_task(created_task["id"])
            if task is None:
                raise RuntimeError(f"created task not found: {created_task['id']}")
            created.append(task)
        return [self._task_detail(task) for task in created]

    def update_task(
        self,
        project_id: str,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if task.status != "backlog":
            raise ValueError("update_task only supports Backlog tasks")
        fields = {
            key: value
            for key, value in {
                "title": title,
                "description": description,
                "plugin": plugin,
                "referenced_tasks": referenced_tasks,
                "base_branch": base_branch,
            }.items()
            if value is not None
        }
        db.update_task(task_id, **fields)
        updated = db.get_task(task_id)
        if updated is None:
            raise RuntimeError(f"updated task not found: {task_id}")
        return self._task_detail(updated)

    def delete_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        _, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        if task.status != "backlog":
            raise ValueError("delete_task only supports Backlog tasks")
        db.delete_task(task_id)
        return {"task_id": task_id, "deleted": True}

    # ---- runtime resources (global pool) -------------------------------

    def list_resources(self) -> dict[str, Any]:
        """Per-kind summary of the global runtime pool: free/held + queue depth.

        An agent calls this before ``acquire_runtime`` to pick the kind its tests
        need and gauge contention.
        """
        db = self._global_db()
        permits = db.list_permits()
        waiters = db.list_waiters()
        kinds: dict[str, dict[str, Any]] = {}
        for permit in permits:
            agg = kinds.setdefault(
                permit.kind,
                {"kind": permit.kind, "total": 0, "free": 0, "held": 0, "queued": 0,
                 "instances": []},
            )
            agg["total"] += 1
            agg["free" if permit.state == "free" else "held"] += 1
            if permit.instance_name:
                agg["instances"].append(permit.instance_name)
        for waiter in waiters:
            kinds.setdefault(
                waiter.kind,
                {"kind": waiter.kind, "total": 0, "free": 0, "held": 0, "queued": 0,
                 "instances": []},
            )["queued"] += 1
        return {"resources": [kinds[k] for k in sorted(kinds)]}

    def acquire_runtime(
        self,
        project_id: str,
        task_id: str,
        kind: str,
        n: int = 1,
    ) -> dict[str, Any]:
        """Atomically hold `n` permits of `kind` for a task, or park it (FIFO).

        Granted ⇒ the instance target is written into the task worktree's
        runtime-target override and returned. Busy ⇒ the task is enqueued and the
        agent should end its turn; the supervisor wakes it on grant.
        """
        if n <= 0:
            raise ValueError("n must be >= 1")
        project, db = self._project_db(project_id)
        task = db.get_task(task_id)
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        lease_db = self._global_db()

        # Idempotent: already holding enough of this kind ⇒ re-affirm the grant.
        held = [p for p in lease_db.held_permits_for_task(task_id) if p.kind == kind]
        if len(held) >= n:
            return self._granted(project, task, held[:n], already_held=True)

        label = task.branch_name or f"task/{task.id[:8]}"
        permits = lease_db.acquire_permits(
            kind=kind, n=n, task_id=task_id, project_id=project.id, label=label,
        )
        if permits is not None:
            return self._granted(project, task, permits, already_held=False)

        lease_db.enqueue_waiter(
            task_id=task_id,
            project_id=project.id,
            kind=kind,
            n=n,
            session_name=task.session_name,
        )
        position = lease_db.waiter_position(task_id, kind)
        return {
            "status": "queued",
            "kind": kind,
            "n": n,
            "position": position,
            "message": (
                f"No free {kind!r} permit. You are #{position} in line. End your "
                "turn now and do nothing further — Harbor will message this "
                "session when the resource is reserved for you."
            ),
        }

    def release_runtime(
        self,
        project_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Free every permit held by a task and drop any pending waiter for it."""
        lease_db = self._global_db()
        released = lease_db.release_permits_for_task(task_id)
        lease_db.delete_waiters_for_task(task_id)
        return {"task_id": task_id, "released": released}

    def _granted(
        self, project: Project, task: Task, permits: list[Any], *, already_held: bool,
    ) -> dict[str, Any]:
        from .agtx_transitions import write_target_override

        target = None
        for permit in permits:
            if permit.target_json:
                try:
                    target = json.loads(permit.target_json)
                except (ValueError, TypeError):
                    target = None
                break
        if target is not None and task.worktree_path:
            write_target_override(
                Path(strip_extended_length_prefix(project.path)),
                Path(task.worktree_path),
                target,
            )
        return {
            "status": "granted",
            "already_held": already_held,
            "permits": [
                {"permit_id": p.permit_id, "kind": p.kind, "instance": p.instance_name}
                for p in permits
            ],
            "target": target,
            "message": (
                "Resource reserved. Its target is written to "
                ".harbor/runtime-target.json. Run your build + probes + related "
                "tests, then call release_runtime the instant they finish."
            ),
        }


def allowed_actions(task: Task) -> list[str]:
    if task.status == "backlog":
        if not task.deps_satisfied:
            return []
        return ["move_forward", "move_to_planning", "move_to_running", "research"]
    if task.status == "planning":
        return ["move_forward", "move_to_running", "escalate_to_user"]
    if task.status == "running":
        return ["move_forward", "move_to_review", "escalate_to_user"]
    if task.status == "review":
        return ["move_to_done", "resume"]
    return []


def create_mcp_server(service: HarborMcpService | None = None) -> FastMCP:
    svc = service or HarborMcpService()
    mcp = FastMCP("harbor")

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        return svc.list_projects()

    @mcp.tool()
    def list_tasks(project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        return svc.list_tasks(project_id=project_id, status=status)

    @mcp.tool()
    def get_task(project_id: str, task_id: str) -> dict[str, Any]:
        return svc.get_task(project_id=project_id, task_id=task_id)

    @mcp.tool()
    def move_task(
        project_id: str,
        task_id: str,
        action: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return svc.move_task(project_id=project_id, task_id=task_id, action=action, reason=reason)

    @mcp.tool()
    def get_transition_status(project_id: str, request_id: str) -> dict[str, Any]:
        return svc.get_transition_status(project_id=project_id, request_id=request_id)

    @mcp.tool()
    def check_conflicts(project_id: str, task_id: str | None = None) -> dict[str, Any]:
        return svc.check_conflicts(project_id=project_id, task_id=task_id)

    @mcp.tool()
    def get_notifications(project_id: str) -> dict[str, Any]:
        return svc.get_notifications(project_id=project_id)

    @mcp.tool()
    def read_pane_content(project_id: str, task_id: str, lines: int = 50) -> dict[str, Any]:
        return svc.read_pane_content(project_id=project_id, task_id=task_id, lines=lines)

    @mcp.tool()
    def send_to_task(project_id: str, task_id: str, message: str) -> dict[str, Any]:
        return svc.send_to_task(project_id=project_id, task_id=task_id, message=message)

    @mcp.tool()
    def create_task(
        project_id: str,
        title: str,
        description: str | None = None,
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        return svc.create_task(
            project_id=project_id,
            title=title,
            description=description,
            plugin=plugin,
            referenced_tasks=referenced_tasks,
            base_branch=base_branch,
        )

    @mcp.tool()
    def create_tasks_batch(project_id: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return svc.create_tasks_batch(project_id=project_id, tasks=tasks)

    @mcp.tool()
    def update_task(
        project_id: str,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        return svc.update_task(
            project_id=project_id,
            task_id=task_id,
            title=title,
            description=description,
            plugin=plugin,
            referenced_tasks=referenced_tasks,
            base_branch=base_branch,
        )

    @mcp.tool()
    def delete_task(project_id: str, task_id: str) -> dict[str, Any]:
        return svc.delete_task(project_id=project_id, task_id=task_id)

    @mcp.tool()
    def list_resources() -> dict[str, Any]:
        return svc.list_resources()

    @mcp.tool()
    def acquire_runtime(
        project_id: str, task_id: str, kind: str, n: int = 1,
    ) -> dict[str, Any]:
        return svc.acquire_runtime(project_id=project_id, task_id=task_id, kind=kind, n=n)

    @mcp.tool()
    def release_runtime(project_id: str, task_id: str) -> dict[str, Any]:
        return svc.release_runtime(project_id=project_id, task_id=task_id)

    return mcp


def run_stdio() -> None:
    create_mcp_server().run(transport="stdio")


def _detect_main_branch(repo_root: Path) -> str:
    for candidate in ("main", "master"):
        cp = _run_git(repo_root, "rev-parse", "--verify", "--quiet", candidate)
        if cp.returncode == 0:
            return candidate
    cp = _run_git(repo_root, "symbolic-ref", "--short", "HEAD")
    return cp.stdout.strip() or "main"


def _check_task_conflicts(repo_root: Path, main_branch: str, task: Task) -> dict[str, Any]:
    if not task.branch_name:
        return {
            "task_id": task.id,
            "title": task.title,
            "branch_name": None,
            "has_conflicts": False,
            "conflicting_files": [],
            "error": "No branch name set for this task",
        }
    base = _run_git(repo_root, "merge-base", main_branch, task.branch_name)
    if base.returncode != 0:
        return _conflict_error(task, f"merge-base failed: {base.stderr.strip()}")
    tree = _run_git(repo_root, "merge-tree", base.stdout.strip(), main_branch, task.branch_name)
    if tree.returncode not in (0, 1):
        return _conflict_error(task, f"merge-tree failed: {tree.stderr.strip()}")
    output = tree.stdout or ""
    files = _conflicting_files_from_merge_tree(output)
    return {
        "task_id": task.id,
        "title": task.title,
        "branch_name": task.branch_name,
        "has_conflicts": bool(files),
        "conflicting_files": files,
        "error": None,
    }


def _conflict_error(task: Task, error: str) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "title": task.title,
        "branch_name": task.branch_name,
        "has_conflicts": False,
        "conflicting_files": [],
        "error": error,
    }


def _conflicting_files_from_merge_tree(output: str) -> list[str]:
    files: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("changed in both"):
            current = None
        elif line.startswith("  our    ") or line.startswith("  their  "):
            parts = line.split()
            if parts:
                current = parts[-1]
        elif line.startswith("<<<<<<<") and current:
            files.append(current)
            current = None
    return sorted(set(files))


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
