"""Tests for harbor.agtx_transitions.TransitionWorker.

We exercise the dispatcher with a real in-memory AgtxDb and mocks for the
two side-effecting boundaries: `Tmux` and `GitOps`. This way we can assert
both the DB writes (status flip, session_name set) and the side-effect
choreography (worktree add → tmux session → send-keys agent launch).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harbor.agtx_client import AgtxDb, Task, init_test_db, insert_test_task
from harbor.agtx_transitions import (
    GitOps,
    TransitionConfig,
    TransitionWorker,
    _generate_session_name,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")


def _make_task(
    *, id: str = "task-aaaa", title: str = "do thing", status: str = "backlog",
    project_id: str = "proj-1", agent: str = "claude",
    session_name: str | None = None, worktree_path: str | None = None,
    branch_name: str | None = None, description: str | None = None,
    referenced_tasks: str | None = None,
) -> Task:
    n = _now()
    return Task(
        id=id, title=title, description=description, status=status, agent=agent,
        project_id=project_id, session_name=session_name,
        worktree_path=worktree_path, branch_name=branch_name,
        referenced_tasks=referenced_tasks,
        created_at=n, updated_at=n,
    )


@pytest.fixture
def memdb() -> AgtxDb:
    conn = sqlite3.connect(":memory:")
    init_test_db(conn, kind="project")
    return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]


@pytest.fixture
def fake_tmux() -> MagicMock:
    m = MagicMock()
    m.has_session.return_value = False
    return m


@pytest.fixture
def fake_git() -> MagicMock:
    return MagicMock(spec=GitOps)


@pytest.fixture
def worker_factory(memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock):
    def _make(
        project_path: Path | None = None,
        *,
        inject_prompts: bool = False,  # tests default to disabled to avoid 20s waits
        init_script: tuple[str, ...] = (),
        copy_files: tuple[str, ...] = (),
        agent_ready_timeout_s: float = 0.0,
        # 0 = no real sleeps / no capture-pane polling, so the prompt-submit
        # choreography stays instant under MagicMock tmux.
        prompt_submit_delay_s: float = 0.0,
        prompt_render_timeout_s: float = 0.0,
    ) -> TransitionWorker:
        cfg = TransitionConfig(
            project_path=project_path or Path("/test/project"),
            agent_command=("claude", "--yes"),
            init_script=init_script,
            copy_files=copy_files,
            inject_prompts=inject_prompts,
            agent_ready_timeout_s=agent_ready_timeout_s,
            prompt_submit_delay_s=prompt_submit_delay_s,
            prompt_render_timeout_s=prompt_render_timeout_s,
            # Tests opt in to PR-on-Done explicitly so move-to-done in
            # unrelated tests doesn't try to push a fake branch.
            pr_on_done=False,
        )
        return TransitionWorker(
            db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
        )
    return _make


# ---- session-name parity --------------------------------------------------


def test_session_name_format():
    """task-<id8>--<project_safe>--<title_slug>, alphanumerics-only and trimmed."""
    t = _make_task(id="abcdef1234567890", title="Hello, World!", project_id="my proj")
    name = _generate_session_name(t)
    assert name == "task-abcdef12--my-proj--hello--world"


def test_session_name_truncates_slug():
    t = _make_task(id="abcdef12", title="this is a very very long task title to truncate")
    name = _generate_session_name(t)
    head, _, slug = name.rpartition("--")
    assert len(slug) <= 20


# ---- single-shot dispatch -------------------------------------------------


def test_move_forward_from_backlog_creates_worktree_and_session(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    insert_test_task(memdb._connect_project(), _make_task(id="task-aaaaaaaa", title="A task"))
    memdb.create_transition_request(task_id="task-aaaaaaaa", action="move_forward")

    worker = worker_factory(project_path=Path("/repo"))
    n = worker.process_once()

    assert n == 1
    fake_git.add_worktree.assert_called_once()
    args, kwargs = fake_git.add_worktree.call_args
    assert args[0] == Path("/repo")
    assert "task-aaaaaaaa"[:8] in str(args[1])
    assert kwargs["base_branch"] == "main"

    # tmux: ensure_session + 1x send_keys_literal (the bash-wrapper launcher
    # combines cd + export + agent into a single command).
    fake_tmux.ensure_session.assert_called_once()
    fake_tmux.send_keys_literal.assert_called()

    t = memdb.get_task("task-aaaaaaaa")
    assert t.status == "planning"
    assert t.session_name is not None
    assert t.worktree_path is not None
    assert t.branch_name == "task/task-aaa"


def test_move_forward_from_blocked_backlog_records_error_without_spawn(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="planning",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Blocked Task", status="backlog", referenced_tasks=dep_id,
    ))
    memdb.create_transition_request(task_id=task_id, action="move_forward")

    worker = worker_factory(project_path=Path("/repo"))
    worker.process_once()

    fake_git.add_worktree.assert_not_called()
    fake_tmux.ensure_session.assert_not_called()
    task = memdb.get_task(task_id)
    assert task.status == "backlog"
    recent = memdb.recent_transition_requests(task_id)
    assert "blocked by dependencies" in (recent[0].error or "")
    assert "aaaaaaaa Dependency Task [planning]" in (recent[0].error or "")


def test_research_from_blocked_backlog_records_error_without_spawn(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory,
):
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="review",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Blocked Task", status="backlog", referenced_tasks=dep_id,
    ))
    memdb.create_transition_request(task_id=task_id, action="research")

    worker = worker_factory()
    worker.process_once()

    fake_git.add_worktree.assert_not_called()
    task = memdb.get_task(task_id)
    assert task.status == "backlog"
    recent = memdb.recent_transition_requests(task_id)
    assert "aaaaaaaa Dependency Task [review]" in (recent[0].error or "")


def test_move_forward_from_backlog_allows_done_dependencies(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory,
):
    dep_id = "aaaaaaaa-1111-2222-3333-444444444444"
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    insert_test_task(memdb._connect_project(), _make_task(
        id=dep_id, title="Dependency Task", status="done",
    ))
    insert_test_task(memdb._connect_project(), _make_task(
        id=task_id, title="Unblocked Task", status="backlog", referenced_tasks=dep_id,
    ))
    memdb.create_transition_request(task_id=task_id, action="move_forward")

    worker = worker_factory()
    worker.process_once()

    fake_git.add_worktree.assert_called_once()
    assert memdb.get_task(task_id).status == "planning"


def test_move_forward_planning_to_running_no_side_effects(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    """Planning → Running is a pure status flip — same session, same agent."""
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
        branch_name="task/t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory()
    worker.process_once()

    fake_git.add_worktree.assert_not_called()
    fake_tmux.ensure_session.assert_not_called()

    t = memdb.get_task("t1")
    assert t.status == "running"


def test_move_to_review_from_running(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="running"))
    memdb.create_transition_request(task_id="t1", action="move_to_review")

    worker = worker_factory()
    worker.process_once()
    assert memdb.get_task("t1").status == "review"


def test_move_to_review_rejects_wrong_status(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="backlog"))
    req_id = memdb.create_transition_request(task_id="t1", action="move_to_review")

    worker = worker_factory()
    worker.process_once()

    # Status unchanged, request marked processed with an error.
    assert memdb.get_task("t1").status == "backlog"
    recent = memdb.recent_transition_requests("t1")
    assert recent[0].id == req_id
    assert recent[0].error is not None
    assert "running" in recent[0].error


def test_move_to_done_kills_session_and_keeps_worktree(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    worker = worker_factory()
    worker.process_once()

    fake_tmux.kill_session.assert_called_once_with("task-t1--p--do")
    t = memdb.get_task("t1")
    assert t.status == "done"
    # Worktree path remains in the row (not cleared) — v1 keeps it on disk.
    assert t.worktree_path == "/repo/.worktrees/task-t1"


def test_escalate_to_user_writes_note_only(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="planning"))
    memdb.create_transition_request(
        task_id="t1", action="escalate_to_user", reason="needs API key",
    )

    worker = worker_factory()
    worker.process_once()

    t = memdb.get_task("t1")
    assert t.status == "planning"  # status unchanged
    assert t.escalation_note == "needs API key"


def test_unknown_action_records_error(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    req_id = memdb.create_transition_request(task_id="t1", action="fly_to_moon")

    worker = worker_factory()
    worker.process_once()

    recent = memdb.recent_transition_requests("t1")
    assert recent[0].id == req_id
    assert recent[0].error is not None
    assert "unknown action" in recent[0].error


def test_failing_git_does_not_advance_task(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory,
):
    fake_git.add_worktree.side_effect = RuntimeError("git: detached")
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    req_id = memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory()
    worker.process_once()

    assert memdb.get_task("t1").status == "backlog"
    recent = memdb.recent_transition_requests("t1")
    assert recent[0].error is not None
    assert "git: detached" in recent[0].error


# ---- claim contention -----------------------------------------------------


def test_only_one_worker_claims_a_request(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    a = worker_factory()
    a.instance_id = "harbor-A"
    b = worker_factory()
    b.instance_id = "harbor-B"

    # A claims and processes.
    assert a.process_once() == 1
    # B sees nothing pending (already claimed and processed).
    assert b.process_once() == 0


# ---- init_script + copy_files ---------------------------------------------


def test_init_script_runs_in_worktree(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory, monkeypatch,
):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "cwd": kwargs.get("cwd")})
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr("harbor.agtx_transitions.subprocess.run", fake_run)

    worker = worker_factory(
        project_path=Path("/repo"),
        init_script=("echo hello", "pip install -e ."),
    )
    worker.process_once()

    # Both init_script commands ran, in order, in the worktree.
    assert len(calls) == 2
    assert calls[0]["argv"] == ["echo", "hello"]
    assert calls[1]["argv"] == ["pip", "install", "-e", "."]
    # cwd is the worktree path
    assert "task-t1"[:7] in str(calls[0]["cwd"])


def test_init_script_failure_aborts_transition(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory, monkeypatch,
):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    def failing_run(argv, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "boom"
        return result

    monkeypatch.setattr("harbor.agtx_transitions.subprocess.run", failing_run)

    worker = worker_factory(init_script=("false-command",))
    worker.process_once()

    assert memdb.get_task("t1").status == "backlog"  # aborted
    recent = memdb.recent_transition_requests("t1")
    assert "init_script failed" in (recent[0].error or "")


def test_copy_files_into_worktree(
    memdb: AgtxDb, fake_git: MagicMock, worker_factory, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SECRET=42")
    (project / "config.json").write_text("{}")

    # GitOps no-ops if worktree exists — pre-create it so the test doesn't
    # actually shell out to git.
    worktree_root = project / ".worktrees"
    worktree_root.mkdir()
    (worktree_root / "task-t1").mkdir()  # branch leaf will land here
    fake_git.add_worktree.side_effect = None  # success no-op

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(
        project_path=project, copy_files=(".env", "config.json"),
    )
    worker.process_once()

    # Find whichever worktree leaf was created and check the copied files.
    leaves = list(worktree_root.iterdir())
    assert len(leaves) == 1
    assert (leaves[0] / ".env").read_text() == "SECRET=42"
    assert (leaves[0] / "config.json").read_text() == "{}"


# ---- prompt injection -----------------------------------------------------


def test_planning_prompt_injected_after_spawn(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Backlog → Planning should send-keys-literal the planning prompt."""
    # Make _wait_for_agent_ready return immediately by making capture_pane
    # return content with a known ready marker.
    fake_tmux.capture_pane.return_value = "Try \"hello\"\n>"

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True, agent_ready_timeout_s=2.0)
    worker.process_once()

    # send_keys_literal should have been called with the planning prompt.
    assert fake_tmux.send_keys_literal.called
    args, kwargs = fake_tmux.send_keys_literal.call_args
    prompt_text = args[2] if len(args) > 2 else kwargs.get("text", "")
    assert "Planning" in prompt_text or "agtx-task-worker" in prompt_text


def test_running_prompt_injected_on_planning_to_running(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Planning → Running flips status AND injects the running prompt."""
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-t1--p--do",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    assert memdb.get_task("t1").status == "running"
    assert fake_tmux.send_keys_literal.called
    args, kwargs = fake_tmux.send_keys_literal.call_args
    prompt_text = args[2] if len(args) > 2 else kwargs.get("text", "")
    assert "Running" in prompt_text or "Implement" in prompt_text


def test_review_prompt_injected_on_running_to_review(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Running → Review injects the review prompt before flipping status."""
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="running", session_name="task-t1--p--do",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    assert memdb.get_task("t1").status == "review"
    assert fake_tmux.send_keys_literal.called
    args, kwargs = fake_tmux.send_keys_literal.call_args
    prompt_text = args[2] if len(args) > 2 else kwargs.get("text", "")
    assert "Review" in prompt_text or "agtx-task-verify" in prompt_text


def test_inject_prompts_disabled_skips_send_keys_literal(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-t1--p--do",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=False)
    worker.process_once()

    fake_tmux.send_keys_literal.assert_not_called()


def test_inject_prompt_skipped_when_session_missing(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Planning → Running with a stale session_name shouldn't crash."""
    fake_tmux.has_session.return_value = False
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-stale",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    # Status still flips (the agent process is gone, but the user can resume)
    assert memdb.get_task("t1").status == "running"
    fake_tmux.send_keys_literal.assert_not_called()


# ---- prompt-submit choreography (Enter must not ride the type burst) ------


def _enter_keystrokes(fake_tmux: MagicMock) -> list:
    """send_keys(...) calls whose keys argument is a bare 'Enter'."""
    return [
        c for c in fake_tmux.send_keys.call_args_list
        if len(c.args) >= 3 and c.args[2] == "Enter"
    ]


def test_prompt_body_typed_without_submit_enter(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """The prompt body must be typed with enter=False — the submit Enter is a
    separate keystroke so the agent's paste-burst detection doesn't swallow it."""
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-t1--p--do", agent="claude",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    # The prompt body went through send_keys_literal with enter=False.
    body_calls = [c for c in fake_tmux.send_keys_literal.call_args_list
                  if c.kwargs.get("enter") is False]
    assert body_calls, "prompt body should be typed with enter=False"
    # ...and the submit Enter arrived as its own standalone send_keys keystroke.
    assert len(_enter_keystrokes(fake_tmux)) == 1


def test_non_codex_prompt_submitted_with_single_enter(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-t1--p--do", agent="claude",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    assert len(_enter_keystrokes(fake_tmux)) == 1


def test_codex_prompt_submitted_with_double_enter(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Codex's first Enter only dismisses the slash-command picker; the message
    needs a second Enter to actually submit."""
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning", session_name="task-t1--p--do", agent="codex",
    ))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True)
    worker.process_once()

    assert memdb.get_task("t1").status == "running"
    assert len(_enter_keystrokes(fake_tmux)) == 2


def test_spawn_aborts_when_agent_falls_back_to_shell(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    """If the agent exits on startup (e.g. codex self-update) and leaves the
    pane at a shell prompt, the spawn relaunches once, then fails the
    transition WITHOUT typing the prompt into the shell."""
    # capture_pane always shows a Git Bash prompt — the agent never comes up.
    fake_tmux.capture_pane.return_value = (
        "Update ran successfully! Please restart Codex.\n"
        "Admin@HOST MINGW64 /d/Projects/harbor/.worktrees/task-t1 (task/t1)\n"
        "$"
    )
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="codex"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(project_path=Path("/repo"), inject_prompts=True)
    worker.process_once()

    # Task stays in Backlog so the user can retry the move.
    assert memdb.get_task("t1").status == "backlog"
    # The transition request is recorded as failed with an explanatory error.
    reqs = memdb.recent_transition_requests("t1")
    assert reqs and reqs[0].error and "shell prompt" in reqs[0].error
    # The launcher was sent twice (initial + one relaunch); the phase prompt
    # was never typed into the pane.
    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list
             if len(c.args) >= 3]
    assert len(typed) == 2 and typed[0] == typed[1]
    assert not any("agtx-task-worker" in t for t in typed)


def test_spawn_proceeds_when_agent_marker_appears(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    """A pane showing a real agent ready-marker is NOT relaunched: launcher
    sent once, prompt injected."""
    fake_tmux.capture_pane.return_value = 'Try "fix the bug"\n/help for help\n>'
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="codex"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(
        project_path=Path("/repo"), inject_prompts=True, agent_ready_timeout_s=2.0,
    )
    worker.process_once()

    assert memdb.get_task("t1").status == "planning"
    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list
             if len(c.args) >= 3]
    # Exactly one launcher (no relaunch) + the prompt body.
    assert any("agtx-task-worker" in t for t in typed)


# ---- per-phase agent command ---------------------------------------------


# ---- plugin skill deployment to worktree ----------------------------------


def _make_fake_plugin_with_skills(tmp_path: Path, skill_names: list[str]) -> "WorkflowPlugin":
    """Create a fake plugin dir on disk with bundled skills, then load it."""
    from harbor.plugin_loader import WorkflowPlugin, PluginCommands
    plugin_root = tmp_path / "plugins" / "fake-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "plugin.toml").write_text('name = "fake-plugin"\n', encoding="utf-8")
    skills_dir = plugin_root / "skills"
    skills_dir.mkdir()
    for skill_name in skill_names:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n---\n# {skill_name}\nhello",
            encoding="utf-8",
        )
        # Add a supporting file to verify whole-directory copy
        (skill_dir / "helper.txt").write_text("support", encoding="utf-8")
    return WorkflowPlugin(
        name="fake-plugin",
        commands=PluginCommands(planning="/agtx-task-worker {task_id}"),
        source_path=plugin_root / "plugin.toml",
    )


def test_plugin_skills_deployed_to_canonical_and_claude_native(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, tmp_path: Path,
):
    """When task.agent is 'claude', skills land at both
    <worktree>/.agtx/skills/<name>/SKILL.md AND
    <worktree>/.claude/commands/agtx/<name>.md."""
    project = tmp_path / "project"
    project.mkdir()
    worktree_root = project / ".worktrees"
    worktree_root.mkdir()
    # Pre-create the worktree so GitOps mock no-ops
    (worktree_root / "task-aaa").mkdir()

    plugin = _make_fake_plugin_with_skills(
        tmp_path, ["agtx-task-worker", "agtx-task-verify"],
    )

    insert_test_task(memdb._connect_project(), _make_task(
        id="aaaaaaaa-task", agent="claude", title="Claude task",
    ))
    memdb.create_transition_request(task_id="aaaaaaaa-task", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=plugin,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    # Find the worktree that has our skills (filter out the pre-created
    # placeholder that simulates "already-exists GitOps no-op").
    worktree = next(
        d for d in worktree_root.iterdir()
        if d.is_dir() and (d / ".agtx" / "skills").exists()
    )

    # Canonical: keeps directory layout including supporting files
    assert (worktree / ".agtx" / "skills" / "agtx-task-worker" / "SKILL.md").exists()
    assert (worktree / ".agtx" / "skills" / "agtx-task-worker" / "helper.txt").exists()
    assert (worktree / ".agtx" / "skills" / "agtx-task-verify" / "SKILL.md").exists()

    # Agent-native (claude → .claude/commands/agtx/<name>.md)
    assert (worktree / ".claude" / "commands" / "agtx" / "agtx-task-worker.md").exists()
    assert (worktree / ".claude" / "commands" / "agtx" / "agtx-task-verify.md").exists()


def test_plugin_skills_deployed_to_codex_native(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, tmp_path: Path,
):
    """task.agent='codex' lands skills at .codex/skills/<name>.md (no agtx/ namespace)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".worktrees" / "task-bbb").mkdir(parents=True)

    plugin = _make_fake_plugin_with_skills(tmp_path, ["my-skill"])

    insert_test_task(memdb._connect_project(), _make_task(id="bbbbbbbb-task", agent="codex"))
    memdb.create_transition_request(task_id="bbbbbbbb-task", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=plugin,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    worktree = next(
        d for d in (project / ".worktrees").iterdir()
        if d.is_dir() and (d / ".agtx" / "skills").exists()
    )
    assert (worktree / ".agtx" / "skills" / "my-skill" / "SKILL.md").exists()
    # codex's mapping is (".codex/skills", "") — namespace empty, so no agtx/ subdir
    assert (worktree / ".codex" / "skills" / "my-skill.md").exists()
    assert not (worktree / ".codex" / "skills" / "agtx" / "my-skill.md").exists()


def test_plugin_skills_unknown_agent_canonical_only(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, tmp_path: Path,
):
    """task.agent='unknown' → still deploy to .agtx/skills/ but skip agent-native."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".worktrees" / "task-ccc").mkdir(parents=True)

    plugin = _make_fake_plugin_with_skills(tmp_path, ["only-skill"])

    insert_test_task(memdb._connect_project(), _make_task(id="cccccccc-task", agent="some-novel-agent"))
    memdb.create_transition_request(task_id="cccccccc-task", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=plugin,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    ).process_once()

    worktree = next(
        d for d in (project / ".worktrees").iterdir()
        if d.is_dir() and (d / ".agtx" / "skills").exists()
    )
    assert (worktree / ".agtx" / "skills" / "only-skill" / "SKILL.md").exists()
    # No agent-native path should have been touched
    assert not (worktree / ".claude").exists()
    assert not (worktree / ".codex").exists()


def test_no_plugin_means_no_skill_deployment(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, tmp_path: Path,
):
    """Sanity: when config.plugin is None, no skill files are written at all."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".worktrees" / "task-ddd").mkdir(parents=True)

    insert_test_task(memdb._connect_project(), _make_task(id="dddddddd-task", agent="claude"))
    memdb.create_transition_request(task_id="dddddddd-task", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=None,  # no plugin
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    ).process_once()

    # In the no-plugin case neither .agtx/skills nor .claude should be written
    # in ANY of the .worktrees children — including the placeholder we
    # pre-created (it has no .agtx/ subdir, which is what we're asserting).
    for worktree in (project / ".worktrees").iterdir():
        assert not (worktree / ".agtx" / "skills").exists()
        assert not (worktree / ".claude").exists()


# ---- plugin-driven prompts/commands ---------------------------------------


def test_plugin_skill_command_and_prompt_both_injected(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """When a plugin is configured, the worker types BOTH the slash command
    (from plugin.commands) AND the prompt text (from plugin.prompts) after
    agent readiness."""
    from harbor.plugin_loader import (
        PluginCommands, PluginPrompts, WorkflowPlugin,
    )
    fake_tmux.capture_pane.return_value = "Try \"hello\""  # marks agent ready

    insert_test_task(memdb._connect_project(), _make_task(id="abc12345", title="thing"))
    memdb.create_transition_request(task_id="abc12345", action="move_forward")

    plugin = WorkflowPlugin(
        name="test",
        commands=PluginCommands(planning="/my-skill {task_id}"),
        prompts=PluginPrompts(planning="Plan task {task_id}."),
    )

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex",),
        inject_prompts=True,
        agent_ready_timeout_s=2.0,
        plugin=plugin,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    # The launcher line (bash wrapper) is first, then the skill command, then the prompt.
    skill_calls = [t for t in typed if t.startswith("/my-skill")]
    prompt_calls = [t for t in typed if t.startswith("Plan task")]
    assert any("/my-skill abc12345" in t for t in skill_calls), \
        f"expected '/my-skill abc12345', got: {typed}"
    assert any("Plan task abc12345." in t for t in prompt_calls), \
        f"expected prompt, got: {typed}"


def test_task_worker_instructions_are_injected_and_written_to_worktree(
    tmp_path: Path, memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    from harbor.plugin_loader import (
        PluginCommands, PluginPrompts, WorkflowPlugin,
    )
    fake_tmux.capture_pane.return_value = "Try \"hello\""

    description = (
        "Implement thing.\n\n"
        "## Worker Instructions\n"
        "Android verification policy:\n"
        "- Use emulator-5554 unless the task says otherwise.\n\n"
        "## Acceptance Criteria\n"
        "- done\n\n"
        "## Verification Probes\n"
        "- echo ok\n\n"
        "## Runtime Target\n"
        "default\n"
    )
    insert_test_task(memdb._connect_project(), _make_task(
        id="abc12345", title="thing", description=description,
    ))
    memdb.create_transition_request(task_id="abc12345", action="move_forward")

    plugin = WorkflowPlugin(
        name="test",
        commands=PluginCommands(planning="/my-skill {task_id}"),
        prompts=PluginPrompts(planning="Plan task {task_id}."),
    )
    cfg = TransitionConfig(
        project_path=tmp_path,
        agent_command=("codex",),
        inject_prompts=True,
        agent_ready_timeout_s=2.0,
        plugin=plugin,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any("Task-specific worker instructions" in t for t in typed), typed
    assert any("Use emulator-5554" in t for t in typed), typed

    shared = tmp_path / ".worktrees" / "task-abc12345" / ".agtx" / "shared-instructions.md"
    assert "Use emulator-5554" in shared.read_text(encoding="utf-8")


def test_codex_goal_marker_enables_goals_feature_for_that_task(
    tmp_path: Path, memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    description = (
        "Implement thing.\n\n"
        "## Codex Goal\n"
        "enabled\n\n"
        "## Acceptance Criteria\n"
        "- done\n"
    )
    insert_test_task(memdb._connect_project(), _make_task(
        id="abc12345", title="thing", agent="codex", description=description,
    ))
    memdb.create_transition_request(task_id="abc12345", action="move_forward")

    cfg = TransitionConfig(
        project_path=tmp_path,
        agent_command=("codex", "-m", "gpt-5.5"),
        inject_prompts=False,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    launcher = fake_tmux.send_keys_literal.call_args_list[0].args[2]
    assert "codex --enable goals -m gpt-5.5" in launcher


def test_codex_goal_marker_does_not_change_non_codex_agent(
    tmp_path: Path, memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    description = "## Codex Goal\nenabled\n"
    insert_test_task(memdb._connect_project(), _make_task(
        id="abc12345", title="thing", agent="claude", description=description,
    ))
    memdb.create_transition_request(task_id="abc12345", action="move_forward")

    cfg = TransitionConfig(
        project_path=tmp_path,
        agent_command=("claude", "--dangerously-skip-permissions"),
        inject_prompts=False,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    launcher = fake_tmux.send_keys_literal.call_args_list[0].args[2]
    assert "--enable goals" not in launcher


def test_task_without_worker_instructions_does_not_write_shared_file(
    tmp_path: Path, memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    fake_tmux.capture_pane.return_value = "ready"
    insert_test_task(memdb._connect_project(), _make_task(id="abc12345", title="thing"))
    memdb.create_transition_request(task_id="abc12345", action="move_forward")

    cfg = TransitionConfig(
        project_path=tmp_path,
        agent_command=("codex",),
        inject_prompts=True,
        agent_ready_timeout_s=2.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    shared = tmp_path / ".worktrees" / "task-abc12345" / ".agtx" / "shared-instructions.md"
    assert not shared.exists()


def test_plugin_falls_back_to_default_prompts_when_unconfigured(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """A plugin with no `prompts.planning` set leaves the default
    DEFAULT_PHASE_PROMPTS['planning'] in effect (so users can adopt a
    minimal plugin that only defines commands without losing prompts)."""
    from harbor.plugin_loader import PluginCommands, WorkflowPlugin
    fake_tmux.capture_pane.return_value = "ready"

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    plugin = WorkflowPlugin(
        name="minimal",
        commands=PluginCommands(planning="/skill {task_id}"),
        # No prompts defined
    )

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        inject_prompts=True,
        agent_ready_timeout_s=2.0,
        plugin=plugin,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    # Hardcoded "You are the worker for an agtx task" should still appear
    assert any("agtx-task-worker skill" in t for t in typed), \
        f"expected hardcoded fallback prompt, got: {typed}"


def test_plugin_auto_dismiss_and_pattern(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """Plugin's [[auto_dismiss]] entries (AND-list of substrings) fire when
    all detect strings are present in pane content."""
    from harbor.plugin_loader import AutoDismiss, WorkflowPlugin

    # Pane content must contain BOTH "Map codebase" and "Enter to select" for
    # the entry to fire.
    fake_tmux.capture_pane.return_value = "Map codebase\nSkip mapping\nEnter to select"

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    plugin = WorkflowPlugin(
        name="gsd-like",
        auto_dismiss=(
            AutoDismiss(
                detect=("Map codebase", "Enter to select"),
                response="2\nEnter",
            ),
        ),
    )

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex",),
        inject_prompts=True,
        agent_ready_timeout_s=3.0,
        plugin=plugin,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys.call_args_list if len(c.args) >= 3]
    assert "2" in typed and "Enter" in typed, \
        f"expected '2' and 'Enter' from plugin auto_dismiss, got: {typed}"


def test_plugin_auto_dismiss_skips_when_partial_match(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """Plugin's auto_dismiss requires ALL substrings; partial match shouldn't fire."""
    from harbor.plugin_loader import AutoDismiss, WorkflowPlugin

    # Only "Map codebase" present — "Enter to select" missing → should NOT dismiss
    fake_tmux.capture_pane.return_value = "Map codebase\nDifferent prompt here"

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    plugin = WorkflowPlugin(
        name="strict",
        auto_dismiss=(
            AutoDismiss(detect=("Map codebase", "Enter to select"), response="2\nEnter"),
        ),
    )

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex",),
        inject_prompts=True,
        agent_ready_timeout_s=2.0,
        plugin=plugin,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys.call_args_list if len(c.args) >= 3]
    # We check that this specific plugin entry did NOT fire. Its response is
    # "2\nEnter"; the "2" keystroke uniquely identifies it (a bare "Enter" is
    # also emitted by the normal prompt-submit choreography, so "Enter" can't
    # be used as the proxy).
    assert "2" not in typed, \
        f"plugin auto_dismiss fired with only partial match: {typed}"


# ---- per-worktree skill deployment ---------------------------------------


def test_deploy_skills_to_worktree_canonical_and_agent_native(
    memdb: AgtxDb, fake_tmux: MagicMock, tmp_path: Path,
):
    """When plugin has skills/, _spawn_session writes them to BOTH:
       - <worktree>/.agtx/skills/<skill>/SKILL.md (canonical)
       - <worktree>/<agent-native-path>/<skill>.md (agent-native)
    """
    from harbor.plugin_loader import (
        PluginCommands, WorkflowPlugin, AutoDismiss,
    )

    # Build a fake plugin dir on disk with two skills
    plugin_dir = tmp_path / "plugins" / "fake-plugin"
    skills_dir = plugin_dir / "skills"
    (skills_dir / "skill-alpha").mkdir(parents=True)
    (skills_dir / "skill-alpha" / "SKILL.md").write_text("---\nname: skill-alpha\n---\nA")
    (skills_dir / "skill-alpha" / "helper.txt").write_text("supporting file")
    (skills_dir / "skill-beta").mkdir()
    (skills_dir / "skill-beta" / "SKILL.md").write_text("---\nname: skill-beta\n---\nB")
    (plugin_dir / "plugin.toml").write_text('name = "fake-plugin"\n')

    plugin = WorkflowPlugin(
        name="fake-plugin",
        source_path=plugin_dir / "plugin.toml",
    )

    # Project + worktree on disk so the worker can actually copy files.
    project = tmp_path / "project"
    project.mkdir()
    worktree_root = project / ".worktrees"
    worktree_root.mkdir()
    # Pre-create the worktree dir so fake_git's no-op add_worktree doesn't matter.
    worktree_dir_name = "task-t1aaaaaa"
    worktree_path = worktree_root / worktree_dir_name
    worktree_path.mkdir()

    insert_test_task(memdb._connect_project(), _make_task(
        id="t1aaaaaa", agent="claude", title="x",
    ))
    memdb.create_transition_request(task_id="t1aaaaaa", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    fake_git = MagicMock()
    fake_git.add_worktree.return_value = None  # worktree already exists
    cfg = TransitionConfig(
        project_path=project,
        plugin=plugin,
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    # Canonical layout preserved (whole dir copied, including helper.txt)
    canonical = worktree_path / ".agtx" / "skills"
    assert (canonical / "skill-alpha" / "SKILL.md").read_text() == "---\nname: skill-alpha\n---\nA"
    assert (canonical / "skill-alpha" / "helper.txt").read_text() == "supporting file"
    assert (canonical / "skill-beta" / "SKILL.md").read_text() == "---\nname: skill-beta\n---\nB"

    # Agent-native (claude → .claude/commands/agtx/<name>.md, flattened)
    claude_dir = worktree_path / ".claude" / "commands" / "agtx"
    assert (claude_dir / "skill-alpha.md").read_text() == "---\nname: skill-alpha\n---\nA"
    assert (claude_dir / "skill-beta.md").read_text() == "---\nname: skill-beta\n---\nB"
    # No helper.txt in the agent-native dir — only SKILL.md flattens.
    assert not (claude_dir / "helper.txt").exists()


def test_deploy_skills_codex_uses_codex_skill_dir(
    memdb: AgtxDb, fake_tmux: MagicMock, tmp_path: Path,
):
    """Codex has namespace='' — skills land directly in .codex/skills/."""
    from harbor.plugin_loader import WorkflowPlugin

    plugin_dir = tmp_path / "plugins" / "fp"
    (plugin_dir / "skills" / "s1").mkdir(parents=True)
    (plugin_dir / "skills" / "s1" / "SKILL.md").write_text("S1")
    (plugin_dir / "plugin.toml").write_text('name = "fp"\n')

    project = tmp_path / "project"
    project.mkdir()
    worktree_path = project / ".worktrees" / "task-aa11abcd"
    worktree_path.mkdir(parents=True)

    plugin = WorkflowPlugin(name="fp", source_path=plugin_dir / "plugin.toml")
    insert_test_task(memdb._connect_project(), _make_task(id="aa11abcd", agent="codex"))
    memdb.create_transition_request(task_id="aa11abcd", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    fake_git = MagicMock()
    cfg = TransitionConfig(
        project_path=project, plugin=plugin,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    ).process_once()

    codex_dir = worktree_path / ".codex" / "skills"
    assert (codex_dir / "s1.md").read_text() == "S1"


def test_deploy_skills_unknown_agent_still_writes_canonical(
    memdb: AgtxDb, fake_tmux: MagicMock, tmp_path: Path,
):
    """task.agent='something-novel' → canonical .agtx/skills/ still written;
    agent-native deployment skipped silently."""
    from harbor.plugin_loader import WorkflowPlugin

    plugin_dir = tmp_path / "plugins" / "fp"
    (plugin_dir / "skills" / "x").mkdir(parents=True)
    (plugin_dir / "skills" / "x" / "SKILL.md").write_text("X")
    (plugin_dir / "plugin.toml").write_text('name = "fp"\n')

    project = tmp_path / "project"
    project.mkdir()
    worktree_path = project / ".worktrees" / "task-bb22abcd"
    worktree_path.mkdir(parents=True)

    plugin = WorkflowPlugin(name="fp", source_path=plugin_dir / "plugin.toml")
    insert_test_task(memdb._connect_project(), _make_task(id="bb22abcd", agent="unknown-agent"))
    memdb.create_transition_request(task_id="bb22abcd", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=plugin,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=MagicMock(), poll_interval=0.0,
    ).process_once()

    # Canonical present
    assert (worktree_path / ".agtx" / "skills" / "x" / "SKILL.md").read_text() == "X"
    # No agent-native subtree for an unknown agent
    assert not (worktree_path / ".claude").exists()
    assert not (worktree_path / ".codex").exists()


def test_deploy_skills_skipped_when_no_plugin(
    memdb: AgtxDb, fake_tmux: MagicMock, tmp_path: Path,
):
    """No plugin → no skill deployment, even if worktree is created."""
    project = tmp_path / "project"
    project.mkdir()
    worktree_path = project / ".worktrees" / "task-cc33abcd"
    worktree_path.mkdir(parents=True)

    insert_test_task(memdb._connect_project(), _make_task(id="cc33abcd", agent="claude"))
    memdb.create_transition_request(task_id="cc33abcd", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=project, plugin=None,
        inject_prompts=False, agent_ready_timeout_s=0.0,
    )
    TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=MagicMock(), poll_interval=0.0,
    ).process_once()

    assert not (worktree_path / ".agtx" / "skills").exists()
    assert not (worktree_path / ".claude").exists()


# ---- bash-wrapper launcher ------------------------------------------------


def test_pane_launcher_wraps_in_bash_when_default_shell_configured(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """When default_shell is set, the agent launch line is a single
    `"<bash>" -c "cd ... && export ... && exec <agent>"` typed into the pane.
    This works regardless of whether the pane shell is cmd.exe or bash."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1abcd23", agent="claude"))
    memdb.create_transition_request(task_id="t1abcd23", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex", "-m", "gpt-5.5"),
        default_shell="C:/Program Files/Git/bin/bash.exe",
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert len(typed) == 1, f"expected exactly one launcher call, got: {typed}"
    line = typed[0]
    # Bash invocation
    assert line.startswith('"C:/Program Files/Git/bin/bash.exe" -c "')
    # cd to worktree (forward slashes)
    assert "cd '" in line and "/.worktrees/" in line
    # export AGTX_TASK_ID
    assert "export AGTX_TASK_ID='t1abcd23'" in line
    # exec the agent
    assert "exec codex -m gpt-5.5" in line


def test_pane_launcher_falls_back_to_raw_agent_when_no_default_shell(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """When default_shell is None, fall back to typing the agent argv directly.
    (Used by tests; in real usage default_shell is always set on Windows.)"""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="claude"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex",),
        default_shell=None,  # explicit
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert "codex" in typed, f"expected raw codex command, got: {typed}"


# ---- agent-command-by-task-agent resolution -------------------------------


def test_builtin_agent_map_applies_when_no_user_flags(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """With NO user flags at all, task.agent='claude' uses the built-in
    `claude --dangerously-skip-permissions` mapping."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="claude"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        # agent_command intentionally None — testing the no-explicit-flag path
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any("claude --dangerously-skip-permissions" in t for t in typed), \
        f"expected built-in claude mapping, got: {typed}"


def test_explicit_agent_command_beats_builtin_map(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """User explicit --agent-command must win over the built-in map for
    task.agent. This is the 'I want codex everywhere even though my tasks
    were created with agent=claude' case."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="claude"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("codex",),  # explicit user choice
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any(t.strip() == "codex" for t in typed), \
        f"expected explicit --agent-command to win, got: {typed}"
    assert not any("dangerously-skip-permissions" in t for t in typed)


def test_user_map_agent_overrides_builtin(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """User-supplied --map-agent value wins over the built-in default."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="claude"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("default",),
        agent_command_by_agent={"claude": ("claude", "--my-flag")},
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any("claude --my-flag" in t for t in typed), \
        f"expected user mapping to win, got: {typed}"


def test_unknown_task_agent_falls_through_to_phase_then_global(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """task.agent='something-novel' with no per-agent mapping → phase override wins."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="something-novel"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("global-default",),
        agent_command_by_phase={"planning": ("phase-default",)},
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any("phase-default" in t for t in typed), \
        f"expected phase override to apply when task.agent has no per-agent mapping, got: {typed}"


def test_global_default_used_when_no_per_agent_or_per_phase(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """task.agent='novel' (unmapped) + no phase override + explicit --agent-command → global wins."""
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="something-novel"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("global-default",),
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    typed = [c.args[2] for c in fake_tmux.send_keys_literal.call_args_list if len(c.args) >= 3]
    assert any("global-default" in t for t in typed)


def test_per_phase_agent_command_used_when_spawning(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """If agent_command_by_phase["planning"] is set AND task.agent has no
    mapping, the phase override wins over the global default."""
    # task.agent='unmapped' so the task-agent resolution falls through to phase
    insert_test_task(memdb._connect_project(), _make_task(id="t1", agent="unmapped"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    from harbor.agtx_transitions import TransitionConfig
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        agent_command=("global", "--default"),
        agent_command_by_phase={"planning": ("codex", "--planning-mode")},
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    from harbor.agtx_transitions import TransitionWorker
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux,
        git=MagicMock(spec=__import__("harbor.agtx_transitions", fromlist=["GitOps"]).GitOps),
        poll_interval=0.0,
    )
    worker.process_once()

    calls = [c.args for c in fake_tmux.send_keys_literal.call_args_list]
    assert any("codex --planning-mode" in c[2] for c in calls if len(c) >= 3), \
        f"expected phase override to win, got: {calls}"
    assert not any("global --default" in c[2] for c in calls if len(c) >= 3)


# ---- auto-dismiss --------------------------------------------------------


def test_auto_dismiss_responds_to_trust_dialog(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """When capture-pane shows a known confirmation dialog, send the configured response."""
    # Sequence: first poll shows the trust dialog; subsequent polls show
    # ready content. Use side_effect to script the sequence.
    fake_tmux.capture_pane.side_effect = [
        "Do you trust the files in this folder?\n[1] Trust\n[2] No",
        "Try \"hello\"\n>",  # ready marker
    ]

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True, agent_ready_timeout_s=5.0)
    worker.process_once()

    # send_keys was called with "1" at some point (the trust response).
    typed = [c.args[2] for c in fake_tmux.send_keys.call_args_list if len(c.args) >= 3]
    assert "1" in typed, f"expected '1' (trust response) in send_keys calls, got: {typed}"


def test_auto_dismiss_only_runs_once_per_dialog(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """If a dialog substring persists in pane content, we shouldn't keep
    sending the response to that *same* dialog tick after tick."""
    # Use a string that matches exactly one of the default dismissal substrings.
    fake_tmux.capture_pane.return_value = "Do you trust the files in this folder?\n> "

    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.create_transition_request(task_id="t1", action="move_forward")

    worker = worker_factory(inject_prompts=True, agent_ready_timeout_s=3.0)
    worker.process_once()

    ones = [c for c in fake_tmux.send_keys.call_args_list if len(c.args) >= 3 and c.args[2] == "1"]
    assert len(ones) == 1, f"expected exactly one '1' send-keys, got {len(ones)}"


# ---- worktree cleanup on Done --------------------------------------------


def test_cleanup_worktree_on_done_calls_git_remove(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    # worker_factory doesn't accept cleanup_worktree_on_done, so build inline
    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        cleanup_worktree_on_done=True,
        pr_on_done=False,  # this test exercises the cleanup path, not the PR path
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    fake_tmux.kill_session.assert_called_once()
    fake_git.remove_worktree.assert_called_once_with(
        Path("/repo"), Path("/repo/.worktrees/task-t1"),
    )
    assert memdb.get_task("t1").status == "done"


def test_no_cleanup_when_flag_off(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    worker = worker_factory()
    worker.process_once()

    fake_git.remove_worktree.assert_not_called()


# ---- move_backward / move_to_backlog -------------------------------------


def test_move_backward_planning_to_backlog_kills_session_and_clears_state(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
        branch_name="task/t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_backward")

    worker = worker_factory()
    worker.process_once()

    fake_tmux.kill_session.assert_called_once_with("task-t1--p--do")
    t = memdb.get_task("t1")
    assert t.status == "backlog"
    assert t.session_name is None
    assert t.worktree_path is None
    assert t.branch_name is None


def test_move_backward_running_to_planning_status_only(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Running → Planning is just a status flip; the session keeps running."""
    fake_tmux.has_session.return_value = True
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="running",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_backward")

    worker = worker_factory()
    worker.process_once()

    fake_tmux.kill_session.assert_not_called()
    t = memdb.get_task("t1")
    assert t.status == "planning"
    assert t.session_name == "task-t1--p--do"  # preserved


def test_move_backward_review_to_running(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="review"))
    memdb.create_transition_request(task_id="t1", action="move_backward")

    worker_factory().process_once()
    assert memdb.get_task("t1").status == "running"


def test_move_backward_done_to_review(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="done"))
    memdb.create_transition_request(task_id="t1", action="move_backward")

    worker_factory().process_once()
    assert memdb.get_task("t1").status == "review"


def test_move_backward_from_backlog_records_error(memdb: AgtxDb, worker_factory):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", status="backlog"))
    req_id = memdb.create_transition_request(task_id="t1", action="move_backward")

    worker_factory().process_once()

    assert memdb.get_task("t1").status == "backlog"
    recent = memdb.recent_transition_requests("t1")
    assert "already in Backlog" in (recent[0].error or "")


def test_move_to_backlog_clears_escalation_note(
    memdb: AgtxDb, fake_tmux: MagicMock, worker_factory,
):
    """Resetting a task to Backlog should also clear any escalation_note."""
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="planning",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    # Manually set an escalation note via a prior request
    memdb.update_task("t1", escalation_note="I got stuck")
    memdb.create_transition_request(task_id="t1", action="move_to_backlog")

    worker_factory().process_once()
    t = memdb.get_task("t1")
    assert t.status == "backlog"
    assert t.escalation_note is None


def test_cleanup_failure_is_non_fatal(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    fake_tmux.has_session.return_value = True
    fake_git.remove_worktree.side_effect = RuntimeError("git: worktree locked")
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review",
        session_name="task-t1--p--do", worktree_path="/repo/.worktrees/task-t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        cleanup_worktree_on_done=True,
        pr_on_done=False,  # this test exercises the cleanup path, not the PR path
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    worker = TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )
    worker.process_once()

    # Task still moves to Done even if cleanup failed.
    assert memdb.get_task("t1").status == "done"


# ---- PR-on-done -----------------------------------------------------------


def _pr_on_done_worker(memdb, fake_tmux, fake_git):
    from harbor.agtx_transitions import TransitionConfig, TransitionWorker
    cfg = TransitionConfig(
        project_path=Path("/repo"),
        base_branch="main",
        pr_on_done=True,
        inject_prompts=False,
        agent_ready_timeout_s=0.0,
    )
    return TransitionWorker(
        db=memdb, config=cfg, tmux=fake_tmux, git=fake_git, poll_interval=0.0,
    )


def test_pr_on_done_pushes_and_opens_pr(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    fake_tmux.has_session.return_value = True
    fake_git.open_pull_request.return_value = "https://github.com/owner/repo/pull/42"
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="Wire up X", status="review",
        session_name="task-t1--p--do",
        worktree_path="/repo/.worktrees/task-t1",
        branch_name="task/t1",
        description="Body of the task.",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    events: list[tuple[str, dict]] = []
    worker = _pr_on_done_worker(memdb, fake_tmux, fake_git)
    worker.on_event = lambda name, payload: events.append((name, payload))
    worker.process_once()

    fake_git.push_branch.assert_called_once_with(
        Path("/repo/.worktrees/task-t1"), "task/t1",
    )
    fake_git.open_pull_request.assert_called_once_with(
        Path("/repo/.worktrees/task-t1"),
        base="main", title="Wire up X", body="Body of the task.",
    )
    # Worktree is NOT removed under pr_on_done — user has to click Cleanup.
    fake_git.remove_worktree.assert_not_called()

    t = memdb.get_task("t1")
    assert t.status == "done"
    assert t.pr_url == "https://github.com/owner/repo/pull/42"
    assert t.pr_number == 42

    pr_events = [e for e in events if e[0] == "pr_opened"]
    assert pr_events and pr_events[0][1]["pr_url"].endswith("/pull/42")


def test_pr_on_done_failure_still_marks_done(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    fake_tmux.has_session.return_value = True
    fake_git.push_branch.side_effect = RuntimeError("gh not authenticated")
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", title="T", status="review",
        session_name="task-t1--p--do",
        worktree_path="/repo/.worktrees/task-t1",
        branch_name="task/t1",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    events: list[tuple[str, dict]] = []
    worker = _pr_on_done_worker(memdb, fake_tmux, fake_git)
    worker.on_event = lambda name, payload: events.append((name, payload))
    worker.process_once()

    fake_git.open_pull_request.assert_not_called()
    t = memdb.get_task("t1")
    assert t.status == "done"  # Done still wins
    assert t.pr_url is None
    assert t.escalation_note and "pr_on_done" in t.escalation_note
    assert any(e[0] == "pr_failed" for e in events)


def test_pr_on_done_skips_when_already_opened(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """Re-Done after Review bounce: don't open a second PR."""
    fake_tmux.has_session.return_value = True
    conn = memdb._connect_project()
    insert_test_task(conn, _make_task(
        id="t1", status="review",
        session_name="task-t1--p--do",
        worktree_path="/repo/.worktrees/task-t1",
        branch_name="task/t1",
    ))
    # Stash an existing PR url directly so the early-return kicks in.
    memdb.update_task("t1", pr_url="https://github.com/owner/repo/pull/7", pr_number=7)
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    worker = _pr_on_done_worker(memdb, fake_tmux, fake_git)
    worker.process_once()

    fake_git.push_branch.assert_not_called()
    fake_git.open_pull_request.assert_not_called()
    assert memdb.get_task("t1").status == "done"


def test_pr_on_done_skipped_when_no_branch(
    memdb: AgtxDb, fake_tmux: MagicMock, fake_git: MagicMock,
):
    """A task with no branch_name (e.g. dropped Backlog→Done by hand) records
    the failure but still flips to Done."""
    fake_tmux.has_session.return_value = False
    insert_test_task(memdb._connect_project(), _make_task(
        id="t1", status="review",
    ))
    memdb.create_transition_request(task_id="t1", action="move_to_done")

    worker = _pr_on_done_worker(memdb, fake_tmux, fake_git)
    worker.process_once()

    fake_git.push_branch.assert_not_called()
    t = memdb.get_task("t1")
    assert t.status == "done"
    assert t.escalation_note and "no branch_name" in t.escalation_note
