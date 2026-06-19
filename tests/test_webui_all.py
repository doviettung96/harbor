from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import harbor.webui.server as server_mod
from harbor.agtx_client import AgtxDb, Project, Task, init_test_db, insert_test_task
from harbor.webui.server import create_app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")


def _make_db() -> AgtxDb:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_test_db(conn, kind="project")
    return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]


def _make_task(
    *,
    id: str,
    title: str,
    status: str,
    project_id: str,
    session_name: str | None = None,
    escalation_note: str | None = None,
) -> Task:
    n = _now()
    return Task(
        id=id,
        title=title,
        description=f"description for {title}",
        status=status,
        agent="codex",
        project_id=project_id,
        session_name=session_name,
        escalation_note=escalation_note,
        created_at=n,
        updated_at=n,
    )


@pytest.fixture
def all_client(tmp_path: Path):
    db_a = _make_db()
    db_b = _make_db()
    projects = [
        Project(id="pa", name="alpha", path=str(tmp_path / "alpha"), last_opened="2"),
        Project(id="pb", name="beta", path=str(tmp_path / "beta"), last_opened="1"),
    ]
    fake_tmux = MagicMock()
    fake_tmux.list_sessions.return_value = []
    fake_tmux.attach_command.side_effect = lambda session: f"tmux attach -t {session}"
    fake_tmux.capture_pane.return_value = ""
    fake_tmux.has_session.side_effect = lambda session: session in {"live-session", "sentinel-session"}
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path / "alpha",
            projects=projects,
            project_dbs={"pa": db_a, "pb": db_b},
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
        )
        with TestClient(app) as client:
            yield client, db_a, db_b, fake_tmux


def test_all_board_renders_columns_project_groups_placeholders_and_done_density(all_client):
    client, db_a, db_b, _ = all_client
    insert_test_task(db_a._connect_project(), _make_task(
        id="a-back", title="Alpha backlog", status="backlog", project_id="pa",
    ))
    insert_test_task(db_b._connect_project(), _make_task(
        id="b-plan", title="Beta planning", status="planning", project_id="pb",
    ))
    insert_test_task(db_b._connect_project(), _make_task(
        id="b-done", title="Beta done", status="done", project_id="pb",
    ))

    r = client.get("/all")

    assert r.status_code == 200
    assert 'href="/all"' in r.text
    assert "All tasks" in r.text
    assert 'hx-get="/all/_partials/board"' in r.text
    assert 'hx-trigger="every 4s"' in r.text
    for title in ("Backlog", "Planning", "Running", "Review", "Done"):
        assert title in r.text
    assert 'data-column="backlog"' in r.text
    assert r.text.index('data-project-id="pa"') < r.text.index('data-project-id="pb"')
    assert "Alpha backlog" in r.text
    assert "Beta planning" in r.text
    assert "Beta done" in r.text
    assert '<div class="all-project-empty">-</div>' in r.text

    done_start = r.text.index('data-column="done"')
    done_body = r.text[done_start:]
    assert 'data-project-id="pb"' in done_body
    assert 'data-project-id="pa"' not in done_body


def test_all_board_signals_apply_only_to_planning_running_review(all_client):
    client, db_a, _, fake_tmux = all_client
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-back", title="Backlog no signal", status="backlog", project_id="pa",
        session_name="live-session",
    ))
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-live", title="Live planning", status="planning", project_id="pa",
        session_name="live-session",
    ))
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-dead", title="Dead running", status="running", project_id="pa",
        session_name="dead-session",
    ))
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-sentinel", title="Sentinel review", status="review", project_id="pa",
        session_name="sentinel-session",
    ))
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-escalated", title="Escalated review", status="review", project_id="pa",
        session_name="live-session", escalation_note="needs human",
    ))
    insert_test_task(db_a._connect_project(), _make_task(
        id="t-done", title="Done no signal", status="done", project_id="pa",
        session_name="live-session", escalation_note="old note",
    ))
    fake_tmux.capture_pane.side_effect = lambda session, *_args, **_kwargs: (
        "harbor-verify task=t-sentinel probes=1 passed" if session == "sentinel-session" else ""
    )

    r = client.get("/all")

    assert r.status_code == 200
    assert 'data-task-id="t-live"' in r.text
    assert 'data-signal="live"' in r.text
    assert 'data-task-id="t-dead"' in r.text
    assert 'data-signal="attention"' in r.text
    assert 'data-task-id="t-sentinel"' in r.text
    assert 'title="sentinel reached"' in r.text
    assert 'data-task-id="t-escalated"' in r.text
    assert 'data-signal="escalated"' in r.text

    backlog_card = r.text[r.text.index('data-task-id="t-back"'):r.text.index('data-task-id="t-live"')]
    assert 'data-signal=' not in backlog_card
    done_card = r.text[r.text.index('data-task-id="t-done"'):]
    assert 'data-signal=' not in done_card


def test_all_card_click_targets_owning_project_drawer_and_query_preloads_detail(all_client):
    client, _, db_b, _ = all_client
    insert_test_task(db_b._connect_project(), _make_task(
        id="shared-task-id", title="Beta owns this", status="planning", project_id="pb",
    ))

    board = client.get("/all")
    detail = client.get("/all", params={"task": "shared-task-id"})

    assert board.status_code == 200
    assert 'href="/all?task=shared-task-id"' in board.text
    assert 'data-partial-url="/projects/pb/_partials/task/shared-task-id"' in board.text
    assert detail.status_code == 200
    assert 'task-drawer open' in detail.text
    assert "Beta owns this" in detail.text
    assert 'action="/projects/pb/actions/task/shared-task-id/agent"' in detail.text


def test_all_partial_renders_read_only_board(all_client):
    client, db_a, _, _ = all_client
    insert_test_task(db_a._connect_project(), _make_task(
        id="a-review", title="Alpha review", status="review", project_id="pa",
    ))

    r = client.get("/all/_partials/board")

    assert r.status_code == 200
    assert "Alpha review" in r.text
    assert "New Manual Session" not in r.text
    assert 'action="/projects/pa/actions/move/a-review"' not in r.text


def test_all_task_partial_uses_owning_project_websocket(all_client):
    client, _, db_b, _ = all_client
    insert_test_task(db_b._connect_project(), _make_task(
        id="b-live", title="Beta live", status="running", project_id="pb",
        session_name="live-session",
    ))

    r = client.get("/all/_partials/task/b-live")

    assert r.status_code == 200
    assert 'data-ws-url="/projects/pb/ws/tmux/b-live"' in r.text


# ---- auto-orchestrator UI -------------------------------------------------


@pytest.fixture
def orch_client(tmp_path: Path, monkeypatch):
    """A client with an isolated global data dir so resource leases don't touch
    the real index.db, plus an enabled orchestrator + a 2-slot pool in config."""
    import harbor.agtx_client as ac
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: tmp_path / "gdata")

    runtime_yml = tmp_path / "runtime.yml"
    runtime_yml.write_text(
        "default_profile: balanced\n"
        "harbor:\n"
        "  auto_orchestrator:\n"
        "    enabled: true\n"
        "    max_live_agents: 2\n"
        "  resources:\n"
        "    - kind: emulator\n"
        "      instances:\n"
        "        - name: emu-a\n"
        "          target: {kind: emulator, emulator: {adb_port: 5555}}\n"
        "        - name: emu-b\n"
        "          target: {kind: emulator, emulator: {adb_port: 5557}}\n"
        "    - kind: gpu_gb\n"
        "      capacity: 4\n",
        encoding="utf-8",
    )

    db_a = _make_db()
    projects = [Project(id="pa", name="alpha", path=str(tmp_path / "alpha"), last_opened="2")]
    fake_tmux = MagicMock()
    fake_tmux.list_sessions.return_value = []
    fake_tmux.capture_pane.return_value = ""
    fake_tmux.has_session.return_value = False
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            tmp_path / "alpha",
            projects=projects,
            project_dbs={"pa": db_a},
            autostart_worker=False,
            runtime_config_path=runtime_yml,
        )
        with TestClient(app) as client:
            yield client, db_a


def test_settings_renders_orchestrator_panel_and_pool(orch_client):
    client, _ = orch_client
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Auto-orchestrator" in r.text
    assert "Runtime resource pool" in r.text
    assert "emu-a" in r.text          # pool serialized into the textarea
    assert "gpu_gb" in r.text         # counted spec too
    assert 'name="enabled"' in r.text


def test_board_shows_orchestrator_toggle(orch_client):
    client, _ = orch_client
    r = client.get("/projects/pa")
    assert r.status_code == 200
    assert "Auto-orchestrator" in r.text
    assert 'action="/settings/orchestrator/toggle"' in r.text
    assert "Turn off" in r.text       # currently ON


def test_orchestrator_toggle_flips_enabled(orch_client):
    client, _ = orch_client
    r = client.post("/settings/orchestrator/toggle", data={"next": "/projects/pa"},
                    follow_redirects=False)
    assert r.status_code == 303
    # After flipping, the board shows the OFF state / Turn on button.
    board = client.get("/projects/pa")
    assert "Turn on" in board.text


def test_backlog_card_shows_candidate_checkbox_checked_by_default(orch_client):
    client, db = orch_client
    insert_test_task(db._connect_project(), _make_task(
        id="cand0001", title="Wire up login", status="backlog", project_id="pa",
    ))
    r = client.get("/projects/pa")
    assert r.status_code == 200
    assert 'name="eligible"' in r.text          # checkbox rendered on the card
    assert "orchestrator-eligibility" in r.text  # toggle form action
    assert "checked" in r.text                   # eligible by default


def test_toggle_opt_out_then_back_in(orch_client):
    from harbor.agtx_transitions import task_orchestrator_optout

    client, db = orch_client
    insert_test_task(db._connect_project(), _make_task(
        id="cand0002", title="Template task", status="backlog", project_id="pa",
    ))
    # Uncheck → form omits `eligible` → task opts out.
    r = client.post("/projects/pa/tasks/cand0002/orchestrator-eligibility",
                    data={}, follow_redirects=False)
    assert r.status_code == 303
    assert task_orchestrator_optout(db.get_task("cand0002")) is True
    # Re-check → `eligible=1` → eligible again.
    r = client.post("/projects/pa/tasks/cand0002/orchestrator-eligibility",
                    data={"eligible": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert task_orchestrator_optout(db.get_task("cand0002")) is False
