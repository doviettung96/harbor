"""Smoke tests for the Harbor-targeted webui.

Spins up a TestClient against `create_app(...)` with an in-memory AgtxDb so we
exercise routes/templates without touching the real ~/.config/agtx tree or
spawning the background TransitionWorker.
"""
from __future__ import annotations

import sqlite3
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient

from harbor import agtx_client as ac
import harbor.webui.server as server_mod
from harbor.agtx_client import AgtxDb, Project, Task, init_test_db, insert_test_task
from harbor.webui.server import create_app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")


def _make_task(
    *, id: str = "t1", title: str = "do thing", status: str = "backlog",
    project_id: str = "p1", agent: str = "claude",
    session_name: str | None = None, description: str | None = None,
    referenced_tasks: str | None = None,
) -> Task:
    n = _now()
    return Task(
        id=id, title=title, description=description, status=status, agent=agent,
        project_id=project_id, session_name=session_name,
        referenced_tasks=referenced_tasks,
        created_at=n, updated_at=n,
    )


@pytest.fixture
def memdb() -> AgtxDb:
    # check_same_thread=False because TestClient runs the app in a worker thread
    # while the test fixture seeds rows from the main thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_test_db(conn, kind="project")
    return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]


@pytest.fixture
def app_client(tmp_path: Path, memdb: AgtxDb):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    fake_tmux.attach_command.return_value = "tmux -L harbor attach -t fake"
    fake_tmux.capture_pane.return_value = "(no live pane)"
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(tmp_path, db=memdb, autostart_worker=False)
        with TestClient(app) as client:
            yield client, memdb, fake_tmux


# ---- read pages -----------------------------------------------------------


def test_board_renders_columns_and_tasks(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t-back", title="Backy", status="backlog"))
    insert_test_task(memdb._connect_project(), _make_task(id="t-plan", title="Planny", status="planning"))
    insert_test_task(memdb._connect_project(), _make_task(id="t-done", title="Donny", status="done"))

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    for col in ("Backlog", "Planning", "Running", "Review", "Done"):
        assert col in body
    assert "Backy" in body
    assert "Planny" in body
    assert "Donny" in body
    assert "New Manual Session" in body


def test_board_and_detail_render_short_ids_and_dependencies(app_client):
    client, memdb, _ = app_client
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="planning",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Blocked Task", status="backlog", referenced_tasks=dep_id,
    ))

    r = client.get(f"/?task={task_id}")

    assert r.status_code == 200
    assert "bbbbbbbb" in r.text
    assert "Blocked by:" in r.text
    assert "aaaaaaaa" in r.text
    assert "Dependency Task" in r.text
    assert "[planning]" in r.text
    assert 'title="Blocked by: aaaaaaaa Dependency Task [planning]"' in r.text
    assert '<button type="submit" disabled' in r.text


def test_sidebar_renders_track_project_form(app_client):
    client, _, _ = app_client

    r = client.get("/")

    assert r.status_code == 200
    assert "Track Project" in r.text
    assert 'action="/projects/init"' in r.text
    assert 'action="/projects/init/browse"' in r.text
    assert "Browse and track" in r.text


def test_board_partial_renders(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="abcdef12-3456-7890-abcd-ef1234567890", title="Hello"))
    r = client.get("/_partials/board")
    assert r.status_code == 200
    assert "Hello" in r.text
    assert "<code>abcdef12</code>" in r.text
    assert 'data-partial-url="/_partials/task/abcdef12-3456-7890-abcd-ef1234567890"' in r.text
    assert 'href="/projects/default?task=abcdef12-3456-7890-abcd-ef1234567890"' in r.text


def test_board_preloads_task_drawer_from_query(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", title="Drawer target"))
    r = client.get("/?task=t1")
    assert r.status_code == 200
    assert 'id="task-drawer"' in r.text
    assert 'task-drawer open' in r.text
    assert "Drawer target" in r.text


def test_task_partial_renders_drawer_detail_for_non_live_task(app_client):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Non-live target", session_name="task-fake",
    ))
    fake_tmux.has_session.return_value = False
    r = client.get("/_partials/task/t1")
    assert r.status_code == 200
    assert "Non-live target" in r.text
    assert "cannot see a live tmux session" in r.text
    assert "data-terminal-root" not in r.text


def test_task_partial_renders_embedded_terminal_for_live_task(app_client):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Live target", session_name="task-live",
    ))
    fake_tmux.has_session.return_value = True
    r = client.get("/_partials/task/t1")
    assert r.status_code == 200
    assert "Live target" in r.text
    assert "data-terminal-root" in r.text
    assert 'data-ws-url="/ws/tmux/t1"' in r.text


def test_board_query_renders_task_detail_in_drawer(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Detail target", status="planning", session_name="task-fake",
    ))
    r = client.get("/?task=t1")
    assert r.status_code == 200
    assert "Detail target" in r.text
    assert "task-fake" in r.text  # session name surfaced
    assert "Worker Instructions" in r.text
    assert 'data-task-detail="t1"' in r.text


def test_standalone_task_page_is_removed(app_client):
    client, _, _ = app_client
    assert client.get("/task/t1").status_code == 404


def test_pane_partial_returns_capture(app_client):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name="task-fake"))
    fake_tmux.has_session.return_value = True
    fake_tmux.capture_pane.return_value = "live output here"
    r = client.get("/_partials/pane/t1")
    assert r.status_code == 200
    assert "live output here" in r.text


def test_pane_partial_empty_when_no_session(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name=None))
    r = client.get("/_partials/pane/t1")
    assert r.status_code == 200
    assert "<pre" in r.text  # rendered, just empty


def test_resume_control_renders_for_dead_running_task_with_worktree(app_client, tmp_path: Path):
    client, memdb, fake_tmux = app_client
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Resume target", status="running", session_name="task-fake",
    ))
    memdb.update_task("t1", worktree_path=str(worktree))
    fake_tmux.has_session.return_value = False

    r = client.get("/?task=t1")

    assert r.status_code == 200
    assert "Resume target" in r.text
    assert "cannot see a live tmux session" in r.text
    assert "data-resume-control" in r.text
    assert 'action="/actions/move/t1"' in r.text
    assert 'name="action" value="resume"' in r.text
    assert ">Resume</button>" in r.text


@pytest.mark.parametrize("status", ["planning", "running", "review"])
def test_resume_control_hidden_when_worktree_missing(app_client, status: str):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status=status, session_name="task-fake",
    ))
    fake_tmux.has_session.return_value = False

    r = client.get("/?task=t1")

    assert r.status_code == 200
    assert "data-resume-control" not in r.text
    assert 'value="resume"' not in r.text


def test_resume_control_hidden_for_live_session(app_client, tmp_path: Path):
    client, memdb, fake_tmux = app_client
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="running", session_name="task-live",
    ))
    memdb.update_task("t1", worktree_path=str(worktree))
    fake_tmux.has_session.return_value = True

    r = client.get("/?task=t1")

    assert r.status_code == 200
    assert "data-terminal-root" in r.text
    assert "data-resume-control" not in r.text
    assert 'value="resume"' not in r.text


@pytest.mark.parametrize("status", ["backlog", "done"])
def test_resume_control_hidden_for_backlog_and_done(app_client, tmp_path: Path, status: str):
    client, memdb, fake_tmux = app_client
    worktree = tmp_path / status
    worktree.mkdir()
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status=status, session_name="task-fake",
    ))
    memdb.update_task("t1", worktree_path=str(worktree))
    fake_tmux.has_session.return_value = False

    r = client.get("/?task=t1")

    assert r.status_code == 200
    assert "data-resume-control" not in r.text
    assert 'value="resume"' not in r.text


def test_post_resume_queues_existing_move_action(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="running"))

    r = client.post(
        "/actions/move/t1",
        data={"action": "resume"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].task_id == "t1"
    assert pending[0].action == "resume"


def test_board_query_renders_planning_session_drawer(app_client):
    client, _, fake_tmux = app_client
    fake_tmux.has_session.return_value = True
    session_name = "plan-default-20260516010203-123456789"

    r = client.get(f"/?planning={session_name}")

    assert r.status_code == 200
    assert "Manual Session" in r.text
    assert session_name in r.text
    assert "Minimize" in r.text
    assert 'data-terminal-root' in r.text
    assert f'data-ws-url="/projects/default/ws/planning/{session_name}"' in r.text


def test_board_renders_live_planning_sessions_as_reopenable_links(app_client):
    client, _, fake_tmux = app_client
    first = "plan-default-20260516010203-123456789"
    second = "plan-default-20260516020203-987654321"
    fake_tmux.list_sessions.return_value = [
        "task-not-planning",
        first,
        second,
        "plan-other-20260516030203-111111111",
    ]

    r = client.get(f"/?planning={first}")

    assert r.status_code == 200
    assert "Manual Sessions" in r.text
    assert first in r.text
    assert second in r.text
    assert "task-not-planning" not in r.text
    assert "plan-other-20260516030203-111111111" not in r.text
    assert f'data-partial-url="/projects/default/_partials/planning/{first}"' in r.text
    assert f'data-partial-url="/projects/default/_partials/planning/{second}"' in r.text
    assert 'class="planning-session-link active"' in r.text


# ---- actions --------------------------------------------------------------


def test_post_move_queues_transition_request(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))

    r = client.post(
        "/actions/move/t1",
        data={"action": "move_forward"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?task=t1"

    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].action == "move_forward"


def test_post_move_rejects_blocked_backlog_task_without_queuing(app_client):
    client, memdb, _ = app_client
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="planning",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Blocked Task", status="backlog", referenced_tasks=dep_id,
    ))

    r = client.post(
        f"/actions/move/{task_id}",
        data={"action": "move_forward"},
        follow_redirects=False,
    )

    assert r.status_code == 409
    assert "aaaaaaaa Dependency Task [planning]" in r.text
    assert memdb.pending_transition_requests() == []


def test_post_move_allows_unblocked_backlog_task(app_client):
    client, memdb, _ = app_client
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="done",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Unblocked Task", status="backlog", referenced_tasks=dep_id,
    ))

    r = client.post(
        f"/actions/move/{task_id}",
        data={"action": "move_forward"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].task_id == task_id
    assert pending[0].action == "move_forward"


def test_post_move_after_backlog_ignores_unsatisfied_dependencies(app_client):
    client, memdb, _ = app_client
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="planning",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Already Planning", status="planning", referenced_tasks=dep_id,
    ))

    r = client.post(
        f"/actions/move/{task_id}",
        data={"action": "move_forward"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].task_id == task_id


def test_post_move_rejects_unknown_action(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    r = client.post(
        "/actions/move/t1",
        data={"action": "fly_to_moon"},
    )
    assert r.status_code == 400


def test_post_move_404_when_task_missing(app_client):
    client, _, _ = app_client
    r = client.post(
        "/actions/move/missing",
        data={"action": "move_forward"},
    )
    assert r.status_code == 404


def test_post_send_keys_invokes_tmux(app_client):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", session_name="task-fake",
    ))
    r = client.post(
        "/actions/send-keys/t1",
        data={"text": "hello"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    fake_tmux.send_keys.assert_called_once_with("task-fake", "", "hello")


def test_post_send_keys_409_when_no_session(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name=None))
    r = client.post(
        "/actions/send-keys/t1",
        data={"text": "hello"},
    )
    assert r.status_code == 409


def test_post_kill_invokes_tmux_kill(app_client):
    client, memdb, fake_tmux = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", session_name="task-fake",
    ))
    r = client.post("/actions/kill/t1", follow_redirects=False)
    assert r.status_code == 303
    fake_tmux.kill_session.assert_called_once_with("task-fake")


# ---- cleanup-worktree endpoint -------------------------------------------


def _insert_done_task_with_worktree(memdb):
    insert_test_task(memdb._connect_project(), _make_task(
        id="t-done", title="Wrapped task", status="done",
    ))
    memdb.update_task(
        "t-done",
        worktree_path="/repo/.worktrees/task-t-done",
        branch_name="task/t-done",
    )


def test_cleanup_worktree_removes_and_clears_fields(app_client):
    client, memdb, _ = app_client
    _insert_done_task_with_worktree(memdb)
    fake_git = MagicMock()
    with patch.object(server_mod, "GitOps", return_value=fake_git):
        r = client.post(
            "/actions/task/t-done/cleanup-worktree",
            follow_redirects=False,
        )
    assert r.status_code == 303
    fake_git.remove_worktree.assert_called_once()
    fake_git.delete_branch.assert_called_once()
    t = memdb.get_task("t-done")
    assert t.worktree_path is None
    assert t.branch_name is None


def test_cleanup_worktree_rejects_non_done(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review", session_name="task-t1",
    ))
    memdb.update_task("t1", worktree_path="/repo/.worktrees/t1")
    fake_git = MagicMock()
    with patch.object(server_mod, "GitOps", return_value=fake_git):
        r = client.post(
            "/actions/task/t1/cleanup-worktree",
            follow_redirects=False,
        )
    assert r.status_code == 409
    fake_git.remove_worktree.assert_not_called()


def test_cleanup_worktree_rejects_when_nothing_to_clean(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(
        id="t-done", status="done",
    ))  # no worktree or branch
    fake_git = MagicMock()
    with patch.object(server_mod, "GitOps", return_value=fake_git):
        r = client.post(
            "/actions/task/t-done/cleanup-worktree",
            follow_redirects=False,
        )
    assert r.status_code == 409


def test_done_view_renders_pr_url_and_cleanup_button(app_client):
    client, memdb, _ = app_client
    _insert_done_task_with_worktree(memdb)
    memdb.update_task(
        "t-done",
        pr_url="https://github.com/owner/repo/pull/77",
        pr_number=77,
    )
    r = client.get("/_partials/task/t-done")
    assert r.status_code == 200
    body = r.text
    assert "https://github.com/owner/repo/pull/77" in body
    assert "Cleanup worktree" in body
    assert "/actions/task/t-done/cleanup-worktree" in body


def test_project_init_registers_project_from_ui(tmp_path: Path, monkeypatch):
    fake_config = tmp_path / "harbor-config"
    project_dir = tmp_path / "new-project"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")

    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )
    with TestClient(app) as client:
        r = client.post(
            "/projects/init",
            data={"project_path": str(project_dir), "project_name": "New Project"},
            follow_redirects=False,
        )

        assert r.status_code == 303
        assert r.headers["location"].startswith("/projects/")
        board = client.get(r.headers["location"])
        assert board.status_code == 200
        assert "New Project" in board.text

    projects = AgtxDb(
        project_db_p=fake_config / "unused.db",
        global_db_p=ac.global_db_path(),
    ).list_projects()
    assert len(projects) == 1
    assert projects[0].name == "New Project"
    project_db = fake_config / "projects" / f"{ac.hash_project_path(projects[0].path)}.db"
    assert project_db.exists()
    assert AgtxDb(project_db_p=project_db).is_initialized() is True


def test_project_init_rejects_missing_path(tmp_path: Path, monkeypatch):
    fake_config = tmp_path / "harbor-config"
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    with TestClient(app) as client:
        r = client.post(
            "/projects/init",
            data={"project_path": str(tmp_path / "missing")},
        )

    assert r.status_code == 400
    assert not ac.global_db_path().exists()


def test_project_folder_browser_lists_and_tracks_folder(tmp_path: Path, monkeypatch):
    fake_config = tmp_path / "harbor-config"
    root = tmp_path / "root"
    child = root / "child-project"
    child.mkdir(parents=True)
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    with TestClient(app) as client:
        r = client.get("/projects/init/browse", params={"path": str(root)})
        assert r.status_code == 200
        assert "Browse folders" in r.text
        assert "child-project" in r.text
        assert "Track this folder" in r.text

        r = client.post(
            "/projects/init/browse/register",
            data={"project_path": str(child), "project_name": "Child Project"},
            follow_redirects=False,
        )

    assert r.status_code == 303
    projects = AgtxDb(
        project_db_p=fake_config / "unused.db",
        global_db_p=ac.global_db_path(),
    ).list_projects()
    assert len(projects) == 1
    assert projects[0].name == "Child Project"


def test_project_init_pick_folder_registers_selected_folder(
    tmp_path: Path,
    monkeypatch,
):
    fake_config = tmp_path / "harbor-config"
    project_dir = tmp_path / "picked-project"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    with patch.object(server_mod, "_pick_folder_with_native_dialog", return_value=str(project_dir)):
        with TestClient(app) as client:
            r = client.post("/projects/init/pick-folder", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/projects/")
    projects = AgtxDb(
        project_db_p=fake_config / "unused.db",
        global_db_p=ac.global_db_path(),
    ).list_projects()
    assert len(projects) == 1
    assert projects[0].name == "picked-project"


def test_project_init_pick_folder_get_registers_selected_folder(
    tmp_path: Path,
    monkeypatch,
):
    fake_config = tmp_path / "harbor-config"
    project_dir = tmp_path / "picked-by-url"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    with patch.object(server_mod, "_pick_folder_with_native_dialog", return_value=str(project_dir)):
        with TestClient(app) as client:
            r = client.get("/projects/init/pick-folder", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/projects/")
    projects = AgtxDb(
        project_db_p=fake_config / "unused.db",
        global_db_p=ac.global_db_path(),
    ).list_projects()
    assert len(projects) == 1
    assert projects[0].name == "picked-by-url"


def test_project_init_pick_folder_cancel_is_noop(tmp_path: Path, monkeypatch):
    fake_config = tmp_path / "harbor-config"
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")
    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    with patch.object(server_mod, "_pick_folder_with_native_dialog", return_value=None):
        with TestClient(app) as client:
            r = client.post("/projects/init/pick-folder", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert not ac.global_db_path().exists()


def test_start_planning_session_launches_configured_agent_and_mutates_no_tasks(
    tmp_path: Path,
    memdb: AgtxDb,
):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            agent_command=["codex", "--ask-for-approval", "never"],
        )
        with TestClient(app) as client:
            r1 = client.post("/projects/default/planning-sessions", follow_redirects=False)
            r2 = client.post("/projects/default/planning-sessions", follow_redirects=False)

    assert r1.status_code == 303
    assert r2.status_code == 303
    first = r1.headers["location"].split("planning=", 1)[1]
    second = r2.headers["location"].split("planning=", 1)[1]
    assert first.startswith("plan-default-")
    assert second.startswith("plan-default-")
    assert first != second
    default_shell = app.state.harbor.runtime.cfg.default_shell
    fake_tmux.ensure_session.assert_any_call(
        first,
        str(tmp_path.resolve()),
        default_shell=default_shell,
    )
    fake_tmux.send_keys_literal.assert_any_call(
        first,
        "",
        server_mod._planning_launcher(
            str(tmp_path.resolve()),
            ["codex", "--ask-for-approval", "never"],
            default_shell,
        ),
        enter=True,
    )
    assert memdb.list_tasks() == []
    assert memdb.pending_transition_requests() == []


def test_start_planning_session_defaults_to_claude(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client

    # Claude is launched with a caller-supplied --session-id so the manual
    # session can later be resumed by id despite the shared project-root cwd.
    with patch.object(server_mod.uuid, "uuid4", return_value="fixed-sid"):
        r = client.post("/projects/default/planning-sessions", follow_redirects=False)

    assert r.status_code == 303
    session_name = r.headers["location"].split("planning=", 1)[1]
    default_shell = client.app.state.harbor.runtime.cfg.default_shell
    fake_tmux.send_keys_literal.assert_called_with(
        session_name,
        "",
        server_mod._planning_launcher(
            str(tmp_path.resolve()),
            ["claude", "--session-id", "fixed-sid", "--dangerously-skip-permissions"],
            default_shell,
        ),
        enter=True,
    )


def test_planning_session_agent_selector_renders_for_multiple_configured_agents(
    tmp_path: Path,
    memdb: AgtxDb,
):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
            agent_command_by_agent={
                "codex": ["codex", "--enable", "goals"],
                "claude": ["claude", "--dangerously-skip-permissions"],
            },
        )
        with TestClient(app) as client:
            r = client.get("/projects/default")

    assert r.status_code == 200
    assert "New Manual Session" in r.text
    assert 'role="dialog" aria-label="New manual session"' in r.text
    assert '<select id="manual-session-agent" name="agent">' in r.text
    assert '<option value="">Global default</option>' in r.text
    assert '<option value="claude">claude</option>' in r.text
    assert '<option value="codex">codex</option>' in r.text


def test_planning_session_agent_selector_hidden_for_single_configured_agent(
    tmp_path: Path,
    memdb: AgtxDb,
):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
            agent_command_by_agent={"codex": ["codex", "--enable", "goals"]},
        )
        with TestClient(app) as client:
            r = client.get("/projects/default")

    assert r.status_code == 200
    assert "New Manual Session" in r.text
    assert '<select id="manual-session-agent" name="agent">' not in r.text
    assert 'method="post" action="/projects/default/planning-sessions" class="inline"' in r.text


def test_start_planning_session_uses_selected_configured_agent_command(
    tmp_path: Path,
    memdb: AgtxDb,
):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
            agent_command=["claude", "--dangerously-skip-permissions"],
            agent_command_by_agent={
                "codex": ["codex", "--enable", "goals", "-m", "gpt-5.5"],
                "claude": ["claude", "--dangerously-skip-permissions"],
            },
        )
        with TestClient(app) as client:
            r = client.post(
                "/projects/default/planning-sessions",
                data={"agent": "codex"},
                follow_redirects=False,
            )

    assert r.status_code == 303
    session_name = r.headers["location"].split("planning=", 1)[1]
    fake_tmux.send_keys_literal.assert_called_with(
        session_name,
        "",
        server_mod._planning_launcher(
            str(tmp_path.resolve()),
            ["codex", "--enable", "goals", "-m", "gpt-5.5"],
            app.state.harbor.runtime.cfg.default_shell,
        ),
        enter=True,
    )


def test_start_planning_session_falls_back_for_empty_unknown_or_unmapped_agent(
    tmp_path: Path,
    memdb: AgtxDb,
):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    with patch.object(server_mod, "Tmux", return_value=fake_tmux), \
         patch.object(server_mod.uuid, "uuid4", return_value="fixed-sid"):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
            agent_command=["claude", "--dangerously-skip-permissions"],
            agent_command_by_agent={"codex": ["codex", "--enable", "goals"]},
        )
        with TestClient(app) as client:
            empty = client.post(
                "/projects/default/planning-sessions",
                data={"agent": ""},
                follow_redirects=False,
            )
            unknown = client.post(
                "/projects/default/planning-sessions",
                data={"agent": "gemini"},
                follow_redirects=False,
            )

    assert empty.status_code == 303
    assert unknown.status_code == 303
    expected = server_mod._planning_launcher(
        str(tmp_path.resolve()),
        ["claude", "--session-id", "fixed-sid", "--dangerously-skip-permissions"],
        app.state.harbor.runtime.cfg.default_shell,
    )
    sent_commands = [call.args[2] for call in fake_tmux.send_keys_literal.call_args_list]
    assert sent_commands == [expected, expected]


def test_planning_launcher_folds_cwd_into_launch_line_with_bash():
    # Regression: a manual session must carry its own `cd` in the launch line so
    # a cold tmux server that swallows ensure_session's typed `cd` cannot strand
    # the agent in ~ instead of the project (it used to send a bare command).
    launcher = server_mod._planning_launcher(
        r"D:\Projects\harbor",
        ["claude", "--dangerously-skip-permissions"],
        "C:/Program Files/Git/bin/bash.exe",
    )
    assert launcher == (
        '"C:/Program Files/Git/bin/bash.exe" -lc '
        "\"cd 'D:/Projects/harbor' && exec claude --dangerously-skip-permissions\""
    )


def test_planning_launcher_folds_cwd_without_bash():
    launcher = server_mod._planning_launcher(
        "/home/me/proj", ["codex", "resume"], None,
    )
    assert launcher == "cd /home/me/proj && exec codex resume"


def test_kill_planning_session_invokes_tmux_kill(app_client):
    client, _, fake_tmux = app_client
    session_name = "plan-default-20260516010203-123456789"

    r = client.post(
        f"/projects/default/planning-sessions/{session_name}/kill",
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/projects/default"
    fake_tmux.kill_session.assert_called_once_with(session_name)


def test_kill_planning_session_rejects_non_harbor_name(app_client):
    client, _, fake_tmux = app_client

    r = client.post("/projects/default/planning-sessions/task-live/kill")

    assert r.status_code == 400
    fake_tmux.kill_session.assert_not_called()


# ---- manual-session resume ------------------------------------------------


def _sidecar(tmp_path: Path) -> Path:
    return tmp_path / ".harbor" / "manual-sessions.json"


def _start_manual_session(client, *, session_id: str = "fixed-sid") -> str:
    with patch.object(server_mod.uuid, "uuid4", return_value=session_id):
        r = client.post("/projects/default/planning-sessions", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("planning=", 1)[1]


def test_start_planning_session_persists_resumable_record(app_client, tmp_path: Path):
    client, _, _ = app_client
    session_name = _start_manual_session(client, session_id="sid-persist")

    records = json.loads(_sidecar(tmp_path).read_text(encoding="utf-8"))["sessions"]
    assert len(records) == 1
    rec = records[0]
    assert rec["session_name"] == session_name
    assert rec["agent_kind"] == "claude"
    assert rec["session_id"] == "sid-persist"
    assert rec["launch_argv"] == [
        "claude", "--session-id", "sid-persist", "--dangerously-skip-permissions",
    ]
    assert rec["cwd"] == str(tmp_path.resolve())


def test_dead_manual_session_renders_resume_pill_after_reboot(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client
    session_name = _start_manual_session(client, session_id="sid-reboot")

    # Reboot: the tmux server is empty, but the persisted record remains.
    fake_tmux.list_sessions.return_value = []
    fake_tmux.has_session.return_value = False

    board = client.get("/projects/default")
    assert board.status_code == 200
    assert session_name in board.text
    assert '<span class="pill warn">resume</span>' in board.text


def test_dead_manual_session_detail_offers_resume_and_dismiss(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client
    session_name = _start_manual_session(client, session_id="sid-detail")
    fake_tmux.has_session.return_value = False

    detail = client.get(f"/projects/default/_partials/planning/{session_name}")
    assert detail.status_code == 200
    assert f"/planning-sessions/{session_name}/resume" in detail.text
    assert f"/planning-sessions/{session_name}/dismiss" in detail.text
    # A dead session offers Dismiss, not Kill.
    assert f"/planning-sessions/{session_name}/kill" not in detail.text


def test_resume_planning_session_relaunches_claude_by_id(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client
    session_name = _start_manual_session(client, session_id="sid-xyz")
    default_shell = client.app.state.harbor.runtime.cfg.default_shell

    fake_tmux.ensure_session.reset_mock()
    fake_tmux.send_keys_literal.reset_mock()
    fake_tmux.has_session.return_value = False  # reboot: session gone

    r = client.post(
        f"/projects/default/planning-sessions/{session_name}/resume",
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == f"/projects/default?planning={session_name}"
    fake_tmux.ensure_session.assert_called_once_with(
        session_name, str(tmp_path.resolve()), default_shell=default_shell,
    )
    fake_tmux.send_keys_literal.assert_called_once_with(
        session_name,
        "",
        server_mod._planning_launcher(
            str(tmp_path.resolve()),
            ["claude", "--resume", "sid-xyz", "--dangerously-skip-permissions"],
            default_shell,
        ),
        enter=True,
    )


def test_resume_planning_session_noop_when_already_live(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client
    session_name = _start_manual_session(client, session_id="sid-live")

    fake_tmux.ensure_session.reset_mock()
    fake_tmux.send_keys_literal.reset_mock()
    fake_tmux.has_session.return_value = True  # still live

    r = client.post(
        f"/projects/default/planning-sessions/{session_name}/resume",
        follow_redirects=False,
    )

    assert r.status_code == 303
    fake_tmux.ensure_session.assert_not_called()
    fake_tmux.send_keys_literal.assert_not_called()


def test_resume_planning_session_without_record_404s(app_client):
    client, _, _ = app_client
    r = client.post(
        "/projects/default/planning-sessions/plan-default-20260101000000-000000001/resume",
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_dismiss_planning_session_forgets_record(app_client, tmp_path: Path):
    client, _, _ = app_client
    session_name = _start_manual_session(client, session_id="sid-dismiss")
    assert session_name in _sidecar(tmp_path).read_text(encoding="utf-8")

    r = client.post(
        f"/projects/default/planning-sessions/{session_name}/dismiss",
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert session_name not in _sidecar(tmp_path).read_text(encoding="utf-8")


def test_kill_planning_session_forgets_record(app_client, tmp_path: Path):
    client, _, fake_tmux = app_client
    session_name = _start_manual_session(client, session_id="sid-kill")
    assert session_name in _sidecar(tmp_path).read_text(encoding="utf-8")

    r = client.post(
        f"/projects/default/planning-sessions/{session_name}/kill",
        follow_redirects=False,
    )

    assert r.status_code == 303
    fake_tmux.kill_session.assert_called_once_with(session_name)
    assert session_name not in _sidecar(tmp_path).read_text(encoding="utf-8")


def test_manual_launch_plan_injects_session_id_for_claude():
    with patch.object(server_mod.uuid, "uuid4", return_value="fixed"):
        argv, kind, sid = server_mod._manual_launch_plan(
            ["claude", "--dangerously-skip-permissions"]
        )
    assert kind == "claude"
    assert sid == "fixed"
    assert argv == ["claude", "--session-id", "fixed", "--dangerously-skip-permissions"]


def test_manual_launch_plan_captures_preexisting_session_id():
    argv, kind, sid = server_mod._manual_launch_plan(
        ["claude", "--session-id", "abc", "-x"]
    )
    assert kind == "claude"
    assert sid == "abc"
    assert argv == ["claude", "--session-id", "abc", "-x"]


def test_manual_launch_plan_leaves_non_claude_untouched():
    argv, kind, sid = server_mod._manual_launch_plan(["codex", "--enable", "goals"])
    assert kind == "codex"
    assert sid == ""
    assert argv == ["codex", "--enable", "goals"]


def test_manual_resume_argv_claude_resumes_by_id():
    rec = {
        "agent_kind": "claude",
        "session_id": "sid1",
        "launch_argv": ["claude", "--session-id", "sid1", "--dangerously-skip-permissions"],
    }
    assert server_mod._manual_resume_argv(rec) == [
        "claude", "--resume", "sid1", "--dangerously-skip-permissions",
    ]


def test_manual_resume_argv_codex_resumes_last_with_bypass():
    rec = {
        "agent_kind": "codex",
        "session_id": "",
        "launch_argv": ["codex", "--yolo", "--enable", "goals"],
    }
    assert server_mod._manual_resume_argv(rec) == ["codex", "resume", "--last", "--yolo"]


def test_manual_resume_argv_unknown_agent_cold_relaunches():
    rec = {
        "agent_kind": "gemini",
        "session_id": "",
        "launch_argv": ["gemini", "--foo"],
    }
    assert server_mod._manual_resume_argv(rec) == ["gemini", "--foo"]


def test_task_worker_instructions_save_updates_description(app_client):
    client, memdb, _ = app_client
    description = (
        "Do thing.\n\n"
        "## Acceptance Criteria\n"
        "- done\n\n"
        "## Verification Probes\n"
        "- echo ok\n"
    )
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Detail target", status="backlog", description=description,
    ))

    r = client.post(
        "/actions/task/t1/worker-instructions",
        data={"worker_instructions": "Claim emulator-5554 for this task."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    task = memdb.get_task("t1")
    assert task is not None
    assert "## Worker Instructions" in (task.description or "")
    assert "Claim emulator-5554" in (task.description or "")
    assert (task.description or "").index("## Worker Instructions") < (
        task.description or ""
    ).index("## Acceptance Criteria")


def test_task_codex_goal_checkbox_save_updates_description(app_client):
    client, memdb, _ = app_client
    description = (
        "Do thing.\n\n"
        "## Acceptance Criteria\n"
        "- done\n"
    )
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Detail target", status="backlog", description=description,
    ))

    r = client.post(
        "/actions/task/t1/worker-instructions",
        data={"worker_instructions": "", "codex_goal": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    task = memdb.get_task("t1")
    assert task is not None
    assert "## Codex Goal" in (task.description or "")
    assert "enabled" in (task.description or "")

    r = client.get("/?task=t1")
    assert r.status_code == 200
    assert "Enable Codex <code>/goal</code> for this task" in r.text
    assert "checked" in r.text


# ---- escalation ----------------------------------------------------------


def test_post_escalate_queues_request_with_reason(app_client):
    client, memdb, _ = app_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    r = client.post(
        "/actions/move/t1",
        data={"action": "escalate_to_user", "reason": "needs API key"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].reason == "needs API key"


# ---- global runtime settings ---------------------------------------------


def test_global_runtime_config_used_as_default(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  agent_command: \"codex -m gpt-5.5 --reasoning-effort high\"\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
    )
    cfg = app.state.harbor.transition_config
    assert cfg.agent_command == ("codex", "-m", "gpt-5.5", "--reasoning-effort", "high")


def test_cli_agent_command_overrides_global_runtime_config(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  agent_command: codex\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False,
        runtime_config_path=runtime_yml,
        agent_command=["claude", "--my-cli-override"],
    )
    cfg = app.state.harbor.transition_config
    assert cfg.agent_command == ("claude", "--my-cli-override")


def test_global_runtime_prompt_append_used_by_transition_config(
    tmp_path: Path, memdb: AgtxDb,
):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  prompt_append: Use emulator-5554 for Android checks.\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
    )
    cfg = app.state.harbor.transition_config
    assert cfg.prompt_append == "Use emulator-5554 for Android checks."


def test_settings_page_points_to_task_scoped_worker_instructions(tmp_path: Path, memdb: AgtxDb):
    app = create_app(tmp_path, db=memdb, autostart_worker=False)
    with TestClient(app) as client:
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Shared runtime config" in r.text
        assert "Planning and worker session command" in r.text
        assert "Workflow plugin" in r.text
        assert "Tmux default shell" in r.text
        assert "task-scoped" in r.text
        assert "## Worker Instructions" in r.text


def test_settings_save_updates_global_runtime_config(
    tmp_path: Path, memdb: AgtxDb,
):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  agent_command: codex\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
    )
    with TestClient(app) as client:
        r = client.post(
            "/actions/settings/harbor",
            data={"prompt_append": "Use emulator-5554 for Android checks."},
            follow_redirects=False,
        )
        assert r.status_code == 303

    assert app.state.harbor.transition_config.prompt_append == (
        "Use emulator-5554 for Android checks."
    )
    text = runtime_yml.read_text(encoding="utf-8")
    assert "agent_command:" in text
    assert "- codex" in text
    assert "prompt_append: Use emulator-5554 for Android checks." in text


def test_settings_save_updates_session_command_plugin_and_shell(
    tmp_path: Path, memdb: AgtxDb,
):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  agent_command: claude\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
    )
    with TestClient(app) as client:
        r = client.post(
            "/settings/runtime",
            data={
                "agent_command": "codex -m gpt-5.5",
                "plugin": "harbor-workflow-template",
                "default_shell": "C:/Program Files/Git/bin/bash.exe",
                "prompt_append": "shared",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

    runtime_cfg = app.state.harbor.runtime.cfg
    assert runtime_cfg.harbor_agent_command == ("codex", "-m", "gpt-5.5")
    assert runtime_cfg.default_shell == "C:/Program Files/Git/bin/bash.exe"
    assert runtime_cfg.harbor_plugin == "harbor-workflow-template"
    text = runtime_yml.read_text(encoding="utf-8")
    assert "- codex" in text
    assert "- -m" in text
    assert "- gpt-5.5" in text
    assert "plugin: harbor-workflow-template" in text
    assert "default_shell: C:/Program Files/Git/bin/bash.exe" in text
    assert "prompt_append: shared" in text


def _ctx_and_options(tmp_path: Path, memdb: AgtxDb, *, cli_map=None):
    """Build a minimal ProjectContext + WebuiOptions for _transition_config_for."""
    ctx = server_mod.ProjectContext(
        project=Project(id="p1", name="proj", path=str(tmp_path)),
        path=tmp_path,
        db_path=tmp_path / "x.db",
        db=memdb,
        db_initialized=True,
        config_path=tmp_path / "harbor.yml",
        config_status="ok",
    )
    options = server_mod.WebuiOptions(
        agent_command=None,
        agent_command_by_phase={},
        agent_command_by_agent=cli_map or {},
        base_branch="main",
        worktree_dir=".worktrees",
        init_script=(),
        copy_files=(),
        inject_prompts=True,
        pr_on_review=False,
        plugin=None,
    )
    return ctx, options


def test_transition_config_uses_harbor_yml_agent_map(tmp_path: Path, memdb: AgtxDb):
    """harbor.yml's agent_command_by_agent feeds TransitionConfig: the global
    agent_command targets the manual session, each task agent its own worker."""
    from harbor.agent import Config

    cfg = Config(
        profiles={},
        default_profile="balanced",
        harbor_agent_command=("claude", "--dangerously-skip-permissions"),
        harbor_agent_command_by_agent={
            "codex": ("codex", "--yolo"),
            "claude": ("claude", "--dangerously-skip-permissions"),
        },
    )
    ctx, options = _ctx_and_options(tmp_path, memdb)
    tc = server_mod._transition_config_for(ctx, cfg, options)
    assert tc.agent_command == ("claude", "--dangerously-skip-permissions")
    assert tc.agent_command_by_agent == {
        "codex": ("codex", "--yolo"),
        "claude": ("claude", "--dangerously-skip-permissions"),
    }


def test_transition_config_cli_map_agent_overrides_harbor_yml(
    tmp_path: Path, memdb: AgtxDb,
):
    """A `--map-agent` CLI flag wins over the harbor.yml entry for the same key."""
    from harbor.agent import Config

    cfg = Config(
        profiles={},
        default_profile="balanced",
        harbor_agent_command_by_agent={"codex": ("codex", "--yolo")},
    )
    ctx, options = _ctx_and_options(
        tmp_path, memdb, cli_map={"codex": ("codex", "-m", "gpt-5.5")},
    )
    tc = server_mod._transition_config_for(ctx, cfg, options)
    assert tc.agent_command_by_agent["codex"] == ("codex", "-m", "gpt-5.5")


_AGENT_MAP_YML = (
    "harbor:\n"
    "  agent_command_by_agent:\n"
    "    codex: \"codex --yolo\"\n"
    "    claude: \"claude\"\n"
)


def test_task_agent_dropdown_changes_backlog_task(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(_AGENT_MAP_YML, encoding="utf-8")
    insert_test_task(
        memdb._connect_project(),
        _make_task(id="t1", status="backlog", agent="claude"),
    )
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            r = client.post(
                "/projects/default/actions/task/t1/agent",
                data={"agent": "codex"},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert r.headers["location"] == "/projects/default?task=t1"
    assert memdb.get_task("t1").agent == "codex"


def test_task_agent_change_rejected_when_session_exists(tmp_path: Path, memdb: AgtxDb):
    """Once a task has a tmux session the agent CLI is already running; the
    route refuses the change instead of silently doing nothing."""
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(_AGENT_MAP_YML, encoding="utf-8")
    insert_test_task(
        memdb._connect_project(),
        _make_task(id="t1", status="running", agent="claude", session_name="task-foo"),
    )
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = True
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            r = client.post(
                "/projects/default/actions/task/t1/agent",
                data={"agent": "codex"},
                follow_redirects=False,
            )
            assert r.status_code == 409
    assert memdb.get_task("t1").agent == "claude"


def test_task_agent_change_rejects_unknown_agent(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(_AGENT_MAP_YML, encoding="utf-8")
    insert_test_task(
        memdb._connect_project(),
        _make_task(id="t1", status="backlog", agent="claude"),
    )
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            r = client.post(
                "/projects/default/actions/task/t1/agent",
                data={"agent": "gemini"},  # not configured, not current
                follow_redirects=False,
            )
            assert r.status_code == 400
    assert memdb.get_task("t1").agent == "claude"


def test_task_drawer_renders_agent_dropdown_for_backlog(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(_AGENT_MAP_YML, encoding="utf-8")
    insert_test_task(
        memdb._connect_project(),
        _make_task(id="t1", status="backlog", agent="claude"),
    )
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            r = client.get("/_partials/task/t1")
    assert r.status_code == 200
    assert '<select name="agent"' in r.text
    assert 'value="codex"' in r.text
    assert 'value="claude" selected' in r.text


def test_task_drawer_renders_agent_label_when_session_exists(
    tmp_path: Path, memdb: AgtxDb,
):
    insert_test_task(
        memdb._connect_project(),
        _make_task(id="t1", status="running", agent="codex", session_name="task-foo"),
    )
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = True
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(tmp_path, db=memdb, autostart_worker=False)
        with TestClient(app) as client:
            r = client.get("/_partials/task/t1")
    assert r.status_code == 200
    assert '<select name="agent"' not in r.text
    assert "agent <code>codex</code>" in r.text


def test_settings_saved_agent_command_controls_new_planning_sessions(
    tmp_path: Path, memdb: AgtxDb,
):
    runtime_yml = tmp_path / "runtime.yml"
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            r = client.post(
                "/settings/runtime",
                data={"agent_command": "codex --no-alt-screen"},
                follow_redirects=False,
            )
            assert r.status_code == 303
            r = client.post("/projects/default/planning-sessions", follow_redirects=False)
            assert r.status_code == 303

    session_name = r.headers["location"].split("planning=", 1)[1]
    fake_tmux.send_keys_literal.assert_called_with(
        session_name,
        "",
        server_mod._planning_launcher(
            str(tmp_path.resolve()),
            ["codex", "--no-alt-screen"],
            app.state.harbor.runtime.cfg.default_shell,
        ),
        enter=True,
    )


def test_settings_save_empty_removes_prompt_append(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n"
        "  agent_command: codex\n"
        "  prompt_append: old\n",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path, db=memdb, autostart_worker=False, runtime_config_path=runtime_yml,
    )
    with TestClient(app) as client:
        r = client.post(
            "/actions/settings/harbor",
            data={"prompt_append": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303

    assert app.state.harbor.transition_config.prompt_append == ""
    text = runtime_yml.read_text(encoding="utf-8")
    assert "agent_command:" in text
    assert "- codex" in text
    assert "prompt_append" not in text


def test_project_switching_does_not_mutate_live_config(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n  agent_command: codex\n",
        encoding="utf-8",
    )
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    (project_b / "harbor.yml").write_text(
        "harbor:\n  agent_command: claude\n",
        encoding="utf-8",
    )
    projects = [
        Project(id="pa", name="alpha", path=str(project_a), last_opened="2"),
        Project(id="pb", name="beta", path=str(project_b), last_opened="1"),
    ]
    app = create_app(
        project_a,
        projects=projects,
        project_dbs={"pa": memdb, "pb": memdb},
        autostart_worker=False,
        runtime_config_path=runtime_yml,
    )
    with TestClient(app) as client:
        assert client.get("/projects/pb").status_code == 200
    assert app.state.harbor.transition_config.agent_command == ("codex",)


def test_project_config_load_and_save_are_manual(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "harbor:\n  agent_command: codex\n",
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "harbor.yml").write_text(
        "harbor:\n  agent_command: \"claude --yes\"\n",
        encoding="utf-8",
    )
    projects = [Project(id="p1", name="proj", path=str(project_dir), last_opened="1")]
    app = create_app(
        project_dir,
        projects=projects,
        project_dbs={"p1": memdb},
        autostart_worker=False,
        runtime_config_path=runtime_yml,
    )

    with TestClient(app) as client:
        r = client.post("/projects/p1/config/load", follow_redirects=False)
        assert r.status_code == 303
        assert app.state.harbor.transition_config.agent_command == ("claude", "--yes")

        r = client.post("/settings/runtime", data={"prompt_append": "shared"}, follow_redirects=False)
        assert r.status_code == 303
        r = client.post("/projects/p1/config/save", follow_redirects=False)
        assert r.status_code == 303

    project_text = (project_dir / "harbor.yml").read_text(encoding="utf-8")
    assert "prompt_append: shared" in project_text


def test_project_tree_shows_invalid_project_config(tmp_path: Path, memdb: AgtxDb):
    runtime_yml = tmp_path / "runtime.yml"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "harbor.yml").write_text(
        "default_profile: missing-profile\nprofiles: {}\n",
        encoding="utf-8",
    )
    projects = [Project(id="p1", name="proj", path=str(project_dir), last_opened="1")]
    app = create_app(
        project_dir,
        projects=projects,
        project_dbs={"p1": memdb},
        autostart_worker=False,
        runtime_config_path=runtime_yml,
    )
    with TestClient(app) as client:
        r = client.get("/projects/p1")
    assert r.status_code == 200
    assert "config invalid" in r.text


def test_global_supervisor_processes_multiple_projects_and_skips_uninitialized(
    tmp_path: Path,
):
    def make_db() -> AgtxDb:
        conn = sqlite3.connect(":memory:")
        init_test_db(conn, kind="project")
        return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]

    db_a = make_db()
    db_b = make_db()
    db_missing = AgtxDb(project_db_p=tmp_path / "missing.db")
    insert_test_task(db_a._connect_project(), _make_task(id="a1", status="running", project_id="pa"))
    insert_test_task(db_b._connect_project(), _make_task(id="b1", status="running", project_id="pb"))
    db_a.create_transition_request(task_id="a1", action="move_to_review")
    db_b.create_transition_request(task_id="b1", action="move_to_review")

    projects = [
        Project(id="pa", name="alpha", path=str(tmp_path / "a"), last_opened="3"),
        Project(id="pb", name="beta", path=str(tmp_path / "b"), last_opened="2"),
        Project(id="pc", name="cold", path=str(tmp_path / "c"), last_opened="1"),
    ]
    app = create_app(
        tmp_path / "a",
        projects=projects,
        project_dbs={"pa": db_a, "pb": db_b, "pc": db_missing},
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )

    processed = app.state.harbor.supervisor.process_once()

    assert processed == 2
    assert db_a.get_task("a1").status == "review"
    assert db_b.get_task("b1").status == "review"


def test_setup_route_gone(tmp_path: Path, memdb: AgtxDb):
    """The bead-era /setup page was removed in the Harbor port — no route, no crumb link."""
    app = create_app(tmp_path, db=memdb, autostart_worker=False)
    with TestClient(app) as client:
        assert client.get("/setup").status_code == 404
        assert "/setup" not in client.get("/").text


def test_notifications_render_in_board(app_client):
    client, memdb, _ = app_client
    conn = memdb._connect_project()
    conn.execute(
        "INSERT INTO notifications (id, message, created_at) VALUES (?, ?, ?)",
        ("n1", "hello world", "2026-01-01T00:00:00+00:00"),
    )
    r = client.get("/")
    assert r.status_code == 200
    assert "Notifications" in r.text
    assert "hello world" in r.text


def test_notifications_absent_section_hidden(app_client):
    """No notifications → no Notifications panel rendered (keeps the dashboard tidy)."""
    client, _, _ = app_client
    r = client.get("/")
    assert r.status_code == 200
    # The "Notifications" panel header should not appear when the list is empty.
    assert "<h2>Notifications</h2>" not in r.text


# ---- websocket terminal bridge ------------------------------------------


class FakePtySession:
    def __init__(self) -> None:
        self.output: Queue[str] = Queue()
        self.inputs: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False

    def read(self) -> str:
        return self.output.get(timeout=2.0)

    def write(self, data: str) -> None:
        self.inputs.append(data)

    def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    def close(self) -> None:
        self.closed = True
        self.output.put("")


class FakePtyBackend:
    def __init__(self, session: FakePtySession) -> None:
        self.session = session
        self.spawn_calls: list[tuple[list[str], dict[str, object]]] = []

    def spawn(self, argv, **kwargs):  # noqa: ANN001
        self.spawn_calls.append((list(argv), kwargs))
        return self.session


@pytest.fixture
def ws_client(tmp_path: Path, memdb: AgtxDb):
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = True
    fake_tmux.attach_argv.side_effect = (
        lambda session: ["tmux", "-L", "harbor", "attach", "-t", session]
    )
    pty = FakePtySession()
    backend = FakePtyBackend(pty)
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path,
            db=memdb,
            autostart_worker=False,
            terminal_backend=backend,
        )
        with TestClient(app) as client:
            yield client, memdb, fake_tmux, backend, pty


def _eventually(predicate, timeout: float = 1.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_tmux_websocket_connects_and_streams_output(ws_client):
    client, memdb, fake_tmux, backend, pty = ws_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name="task-live"))
    pty.output.put("hello from tmux")

    with client.websocket_connect("/ws/tmux/t1") as ws:
        assert ws.receive_text() == "hello from tmux"

    assert backend.spawn_calls[0][0] == ["tmux", "-L", "harbor", "attach", "-t", "task-live"]
    fake_tmux.attach_argv.assert_called_once_with("task-live")
    assert pty.closed is True


def test_tmux_websocket_forwards_input_and_resize(ws_client):
    client, memdb, _, _, pty = ws_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name="task-live"))

    with client.websocket_connect("/ws/tmux/t1") as ws:
        ws.send_text(json.dumps({"type": "input", "data": "abc"}))
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 42}))
        assert _eventually(lambda: pty.inputs == ["abc"])
        assert _eventually(lambda: pty.resizes == [(120, 42)])


def test_tmux_websocket_rejects_missing_task(ws_client):
    client, _, _, _, _ = ws_client
    with pytest.raises(WebSocketDisconnect) as ei:
        client.websocket_connect("/ws/tmux/missing").__enter__()
    assert ei.value.code == 1008


def test_tmux_websocket_rejects_task_without_session(ws_client):
    client, memdb, _, _, _ = ws_client
    insert_test_task(memdb._connect_project(), _make_task(id="t1", session_name=None))
    with pytest.raises(WebSocketDisconnect) as ei:
        client.websocket_connect("/ws/tmux/t1").__enter__()
    assert ei.value.code == 1008


def test_planning_websocket_attaches_and_streams_output(ws_client):
    client, _, fake_tmux, backend, pty = ws_client
    session_name = "plan-default-20260516010203-123456789"
    pty.output.put("planning output")

    with client.websocket_connect(f"/ws/planning/{session_name}") as ws:
        assert ws.receive_text() == "planning output"

    assert backend.spawn_calls[0][0] == ["tmux", "-L", "harbor", "attach", "-t", session_name]
    fake_tmux.attach_argv.assert_called_once_with(session_name)
    assert pty.closed is True


def test_planning_websocket_forwards_input_and_resize(ws_client):
    client, _, _, _, pty = ws_client
    session_name = "plan-default-20260516010203-123456789"

    with client.websocket_connect(f"/ws/planning/{session_name}") as ws:
        ws.send_text(json.dumps({"type": "input", "data": "hello"}))
        ws.send_text(json.dumps({"type": "resize", "cols": 111, "rows": 33}))
        assert _eventually(lambda: pty.inputs == ["hello"])
        assert _eventually(lambda: pty.resizes == [(111, 33)])


def test_planning_websocket_rejects_invalid_session_name(ws_client):
    client, _, _, _, _ = ws_client
    with pytest.raises(WebSocketDisconnect) as ei:
        client.websocket_connect("/ws/planning/task-live").__enter__()
    assert ei.value.code == 1008


def test_planning_websocket_rejects_non_live_session(ws_client):
    client, _, fake_tmux, _, _ = ws_client
    fake_tmux.has_session.return_value = False
    session_name = "plan-default-20260516010203-123456789"
    with pytest.raises(WebSocketDisconnect) as ei:
        client.websocket_connect(f"/ws/planning/{session_name}").__enter__()
    assert ei.value.code == 1008
