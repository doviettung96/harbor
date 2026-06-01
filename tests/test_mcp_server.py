from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harbor import agtx_client as ac
from harbor.agtx_client import AgtxDb, Project, Task, hash_project_path, insert_test_task
from harbor.mcp_server import HarborMcpService, TOOL_NAMES, allowed_actions, create_mcp_server


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")


def _make_task(
    *,
    id: str,
    title: str,
    status: str,
    project_id: str,
    session_name: str | None = None,
    branch_name: str | None = None,
    referenced_tasks: str | None = None,
) -> Task:
    n = _now()
    return Task(
        id=id,
        title=title,
        description="",
        status=status,
        agent="codex",
        project_id=project_id,
        session_name=session_name,
        branch_name=branch_name,
        referenced_tasks=referenced_tasks,
        created_at=n,
        updated_at=n,
    )


@pytest.fixture
def project_db(tmp_path: Path, monkeypatch) -> tuple[Project, AgtxDb]:
    fake_config = tmp_path / "harbor-config"
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    global_db = AgtxDb(project_db_p=None, global_db_p=ac.global_db_path())  # type: ignore[arg-type]
    project = global_db.register_project(project_dir, name="harbor")
    db_path = fake_config / "projects" / f"{hash_project_path(project.path)}.db"
    return project, AgtxDb(project_db_p=db_path, global_db_p=ac.global_db_path())


def test_tools_list_registers_exact_13_tool_names():
    mcp = create_mcp_server(HarborMcpService(tmux=MagicMock()))
    result = mcp.list_tools()
    if inspect.isawaitable(result):
        result = asyncio.run(result)

    assert sorted(tool.name for tool in result) == sorted(TOOL_NAMES)


def test_create_list_get_move_and_transition_status(project_db):
    project, db = project_db
    service = HarborMcpService(tmux=MagicMock())

    projects = service.list_projects()
    assert any(p["id"] == project.id and p["name"] == "harbor" for p in projects)

    created = service.create_task(
        project_id=project.id,
        title="MCP smoke task",
        description="created by test",
    )
    assert created["status"] == "backlog"
    assert created["allowed_actions"]

    listed = service.list_tasks(project_id=project.id)
    assert any(t["id"] == created["id"] and t["title"] == "MCP smoke task" for t in listed)

    detail = service.get_task(project_id=project.id, task_id=created["id"])
    assert detail["allowed_actions"] == [
        "move_forward",
        "move_to_planning",
        "move_to_running",
        "research",
    ]

    moved = service.move_task(
        project_id=project.id,
        task_id=created["id"],
        action="move_forward",
    )
    assert moved["request_id"]
    pending = db.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].id == moved["request_id"]
    assert pending[0].action == "move_forward"

    status = service.get_transition_status(project_id=project.id, request_id=moved["request_id"])
    assert status == {"request_id": moved["request_id"], "status": "pending", "error": None}


def test_allowed_actions_by_status(project_db):
    project, db = project_db
    conn = db._connect_project()
    dep = _make_task(id="dep", title="Dep", status="planning", project_id=project.id)
    insert_test_task(conn, dep)
    blocked = _make_task(
        id="blocked",
        title="Blocked",
        status="backlog",
        project_id=project.id,
        referenced_tasks="dep",
    )
    conn.execute("DELETE FROM tasks")

    assert allowed_actions(_make_task(id="b", title="B", status="backlog", project_id=project.id)) == [
        "move_forward",
        "move_to_planning",
        "move_to_running",
        "research",
    ]
    insert_test_task(conn, dep)
    insert_test_task(conn, blocked)
    blocked_resolved = db.get_task("blocked")
    assert blocked_resolved is not None
    assert allowed_actions(blocked_resolved) == []
    assert allowed_actions(_make_task(id="p", title="P", status="planning", project_id=project.id)) == [
        "move_forward",
        "move_to_running",
        "escalate_to_user",
    ]
    assert allowed_actions(_make_task(id="r", title="R", status="running", project_id=project.id)) == [
        "move_forward",
        "move_to_review",
        "escalate_to_user",
    ]
    assert allowed_actions(_make_task(id="v", title="V", status="review", project_id=project.id)) == [
        "move_to_done",
        "resume",
    ]
    assert allowed_actions(_make_task(id="d", title="D", status="done", project_id=project.id)) == []


def test_create_batch_update_delete_backlog_only(project_db):
    project, _ = project_db
    service = HarborMcpService(tmux=MagicMock())

    created = service.create_tasks_batch(
        project_id=project.id,
        tasks=[
            {"title": "First"},
            {"title": "Second", "depends_on": [0]},
        ],
    )
    assert len(created) == 2
    assert created[1]["referenced_tasks"] == created[0]["id"]

    updated = service.update_task(project_id=project.id, task_id=created[0]["id"], title="Renamed")
    assert updated["title"] == "Renamed"

    deleted = service.delete_task(project_id=project.id, task_id=created[0]["id"])
    assert deleted == {"task_id": created[0]["id"], "deleted": True}

    running = service.create_task(project_id=project.id, title="Running")
    _, db = service._project_db(project.id)
    db.update_task(running["id"], status="running")
    with pytest.raises(ValueError, match="only supports Backlog"):
        service.update_task(project_id=project.id, task_id=running["id"], title="Nope")
    with pytest.raises(ValueError, match="only supports Backlog"):
        service.delete_task(project_id=project.id, task_id=running["id"])


def test_notifications_are_consumed(project_db):
    project, db = project_db
    db._connect_project().execute(
        "INSERT INTO notifications (id, message, created_at) VALUES (?, ?, ?)",
        ("n1", "hello", _now()),
    )
    service = HarborMcpService(tmux=MagicMock())

    first = service.get_notifications(project_id=project.id)
    second = service.get_notifications(project_id=project.id)

    assert first["notifications"][0]["message"] == "hello"
    assert second["notifications"] == []


def test_pane_read_and_send_use_existing_tmux_wrapper(project_db):
    project, db = project_db
    task = _make_task(
        id="t1",
        title="Live",
        status="running",
        project_id=project.id,
        session_name="task-live",
    )
    insert_test_task(db._connect_project(), task)
    tmux = MagicMock()
    tmux.capture_pane.return_value = "pane text"
    service = HarborMcpService(tmux=tmux)

    assert service.read_pane_content(project_id=project.id, task_id="t1", lines=25) == {
        "task_id": "t1",
        "session_name": "task-live",
        "content": "pane text",
        "lines_requested": 25,
    }
    tmux.capture_pane.assert_called_once_with("task-live", "", lines=25)

    sent = service.send_to_task(project_id=project.id, task_id="t1", message="continue")
    assert sent["sent"] is True
    tmux.send_keys_literal.assert_called_once_with("task-live", "", "continue", enter=True)


def test_check_conflicts_reports_review_task_without_branch(project_db):
    project, db = project_db
    insert_test_task(db._connect_project(), _make_task(
        id="review",
        title="Needs branch",
        status="review",
        project_id=project.id,
    ))
    service = HarborMcpService(tmux=MagicMock())

    result = service.check_conflicts(project_id=project.id)

    assert result["main_branch"] == "main"
    assert result["results"] == [{
        "task_id": "review",
        "title": "Needs branch",
        "branch_name": None,
        "has_conflicts": False,
        "conflicting_files": [],
        "error": "No branch name set for this task",
    }]


def test_move_task_rejects_blocked_backlog_without_queueing(project_db):
    project, db = project_db
    conn = db._connect_project()
    insert_test_task(conn, _make_task(id="dep", title="Dep", status="planning", project_id=project.id))
    insert_test_task(conn, _make_task(
        id="task",
        title="Blocked",
        status="backlog",
        project_id=project.id,
        referenced_tasks="dep",
    ))
    service = HarborMcpService(tmux=MagicMock())

    with pytest.raises(ValueError, match="blocked by dependencies"):
        service.move_task(project_id=project.id, task_id="task", action="move_forward")

    assert db.pending_transition_requests() == []
