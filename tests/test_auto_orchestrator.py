"""Tests for the auto-orchestrator: admission + agent-driven resource pool.

These exercise, against an in-memory project DB and a real (temp-file) global
pool DB:
- the typed permit pool (`permit_specs`, `reconcile_resources`, atomic
  `acquire_permits`, release),
- the FIFO waiter queue,
- `run_admission` (resource-blind, max_live_agents cap, in-flight guard,
  dead-waiter/permit reconcile),
- `run_grants` (FIFO grant + worktree override + tmux wake, strict per-kind
  head-of-line), and
- the MCP `acquire_runtime` / `release_runtime` tools.

No emulators required: instance permits carry plain `target` dicts.
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harbor import agtx_client as ac
from harbor.agent import Config, ResourceInstance, ResourceSpec, load_config
from harbor.agtx_client import AgtxDb, Task, init_test_db, insert_test_task
from harbor.auto_orchestrator import count_live_agents, run_admission
from harbor.resource_broker import (
    RESOURCE_PROTOCOL,
    permit_specs,
    reconcile_pool,
    run_grants,
)


# ---- fixtures / helpers ---------------------------------------------------


@pytest.fixture
def global_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: tmp_path / "harbor-data")
    return tmp_path


@pytest.fixture
def lease_db(global_data):
    return AgtxDb(project_db_p=None, global_db_p=ac.global_db_path())  # type: ignore[arg-type]


def _project_ctx(project_id="proj-1", *, path="/tmp/proj-1"):
    conn = sqlite3.connect(":memory:")
    init_test_db(conn, kind="project")
    db = AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]
    project = types.SimpleNamespace(id=project_id)
    return types.SimpleNamespace(
        project=project, db=db, db_initialized=True, path=path,
    )


def _add_task(ctx, task_id, *, status="backlog", referenced=None, escalation=None,
              session_name=None, worktree_path=None, branch=None, description=None):
    insert_test_task(
        ctx.db._connect_project(),
        Task(
            id=task_id,
            title=task_id,
            description=description,
            status=status,
            agent="codex",
            project_id=ctx.project.id,
            referenced_tasks=referenced,
            escalation_note=escalation,
            session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch,
        ),
    )


def _cfg(*, enabled=True, max_live=0, resources=()):
    base = load_config(None)
    return Config(
        profiles=base.profiles,
        default_profile=base.default_profile,
        auto_orchestrator_enabled=enabled,
        auto_orchestrator_max_live_agents=max_live,
        resources=tuple(resources),
    )


def _inst(name, **target):
    return ResourceInstance(name=name, target=target or {"kind": "local"})


def _queued_actions(ctx):
    rows = ctx.db._connect_project().execute(
        "SELECT task_id, action FROM transition_requests"
    ).fetchall()
    return [(r["task_id"], r["action"]) for r in rows]


# ---- permit expansion -----------------------------------------------------


def test_permit_specs_expands_instances_and_capacity():
    cfg = _cfg(resources=[
        ResourceSpec(kind="emulator", instances=(_inst("a", kind="emulator", emulator={"adb_port": 5555}),
                                                 _inst("b", kind="emulator"))),
        ResourceSpec(kind="gpu_gb", capacity=3),
    ])
    specs = permit_specs(cfg)
    ids = [pid for pid, *_ in specs]
    assert ids == ["emulator/a", "emulator/b", "gpu_gb#0", "gpu_gb#1", "gpu_gb#2"]
    # instance carries target_json; counted does not
    emu_a = next(s for s in specs if s[0] == "emulator/a")
    assert json.loads(emu_a[3])["emulator"]["adb_port"] == 5555
    gpu0 = next(s for s in specs if s[0] == "gpu_gb#0")
    assert gpu0[2] is None and gpu0[3] is None


# ---- pool DB: reconcile / acquire / release -------------------------------


def test_reconcile_creates_free_permits_and_prunes_stale(lease_db):
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    assert {p.permit_id for p in lease_db.list_permits()} == {"emulator/a"}
    # add one, remove the first
    lease_db.reconcile_resources([("emulator/b", "emulator", "b", None)])
    assert {p.permit_id for p in lease_db.list_permits()} == {"emulator/b"}


def test_reconcile_keeps_held_permit_across_config_edit(lease_db):
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    lease_db.acquire_permits(kind="emulator", n=1, task_id="t1", project_id="p", label="t1")
    # 'a' no longer configured, but it's held → must survive the prune.
    lease_db.reconcile_resources([("emulator/b", "emulator", "b", None)])
    ids = {p.permit_id for p in lease_db.list_permits()}
    assert ids == {"emulator/a", "emulator/b"}


def test_acquire_is_all_or_nothing(lease_db):
    lease_db.reconcile_resources([("gpu_gb#0", "gpu_gb", None, None),
                                  ("gpu_gb#1", "gpu_gb", None, None)])
    # ask for more than free → None and nothing held
    assert lease_db.acquire_permits(kind="gpu_gb", n=3, task_id="t1", project_id="p", label="t1") is None
    assert lease_db.count_free_permits("gpu_gb") == 2
    got = lease_db.acquire_permits(kind="gpu_gb", n=2, task_id="t1", project_id="p", label="t1")
    assert got is not None and len(got) == 2
    assert lease_db.count_free_permits("gpu_gb") == 0


def test_release_permits_for_task(lease_db):
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    lease_db.acquire_permits(kind="emulator", n=1, task_id="t1", project_id="p", label="t1")
    assert lease_db.count_free_permits("emulator") == 0
    assert lease_db.release_permits_for_task("t1") == 1
    assert lease_db.count_free_permits("emulator") == 1


# ---- waiter queue ---------------------------------------------------------


def test_enqueue_is_fifo_and_idempotent_per_task_kind(lease_db):
    lease_db.enqueue_waiter(task_id="t1", project_id="p", kind="emulator", n=1, session_name="s1")
    lease_db.enqueue_waiter(task_id="t2", project_id="p", kind="emulator", n=1, session_name="s2")
    assert [w.task_id for w in lease_db.list_waiters()] == ["t1", "t2"]
    assert lease_db.waiter_position("t1", "emulator") == 1
    assert lease_db.waiter_position("t2", "emulator") == 2
    # re-enqueue t1 keeps its place (idempotent), only refreshes n/session
    first_enqueued = lease_db.list_waiters()[0].enqueued_at
    lease_db.enqueue_waiter(task_id="t1", project_id="p", kind="emulator", n=2, session_name="s1b")
    waiters = lease_db.list_waiters()
    assert [w.task_id for w in waiters] == ["t1", "t2"]
    t1 = next(w for w in waiters if w.task_id == "t1")
    assert t1.n == 2 and t1.session_name == "s1b" and t1.enqueued_at == first_enqueued


# ---- admission ------------------------------------------------------------


def test_admits_ready_skips_blocked_and_escalated():
    ctx = _project_ctx()
    _add_task(ctx, "aaaaaaaa")
    _add_task(ctx, "bbbbbbbb", referenced="zzzzzzzz")   # blocked
    _add_task(ctx, "cccccccc", escalation="held")        # escalated
    _add_task(ctx, "dddddddd")

    admitted = run_admission([ctx], _cfg())

    assert admitted == 2
    queued = {tid for tid, _ in _queued_actions(ctx)}
    assert queued == {"aaaaaaaa", "dddddddd"}


def test_admission_skips_opted_out_task():
    ctx = _project_ctx()
    _add_task(ctx, "aaaaaaaa")  # eligible (default)
    _add_task(ctx, "bbbbbbbb", description="Do the thing\n\n## Auto Orchestrator\nskip\n")

    admitted = run_admission([ctx], _cfg())

    assert admitted == 1
    assert {tid for tid, _ in _queued_actions(ctx)} == {"aaaaaaaa"}


def test_admission_takes_no_lease_db():
    """Admission is resource-blind — its signature must not require a lease_db."""
    import inspect
    assert "lease_db" not in inspect.signature(run_admission).parameters


def test_max_live_agents_caps_admission():
    ctx = _project_ctx()
    for tid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        _add_task(ctx, tid)
    admitted = run_admission([ctx], _cfg(max_live=2))
    assert admitted == 2


def test_running_tasks_count_against_cap():
    ctx = _project_ctx()
    _add_task(ctx, "running01", status="running")
    _add_task(ctx, "backlog01")
    # cap of 1, one already running → no admission
    admitted = run_admission([ctx], _cfg(max_live=1))
    assert admitted == 0
    assert _queued_actions(ctx) == []


def test_no_duplicate_admission_while_move_forward_in_flight():
    ctx = _project_ctx()
    _add_task(ctx, "aaaaaaaa")
    run_admission([ctx], _cfg())
    assert ctx.db.count_unprocessed_transitions("aaaaaaaa", "move_forward") == 1
    # second tick: task still backlog with in-flight move → not re-queued
    run_admission([ctx], _cfg())
    assert len(_queued_actions(ctx)) == 1


# ---- resource broker: reconcile (independent of admission) -----------------


def test_reconcile_pool_reaps_dead_waiter_and_held_permit(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "gone-task", status="review")  # left the working window
    cfg = _cfg(resources=[ResourceSpec(kind="emulator", instances=(_inst("a", kind="emulator"),))])
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    lease_db.acquire_permits(kind="emulator", n=1, task_id="gone-task", project_id="proj-1", label="x")
    lease_db.enqueue_waiter(task_id="gone-task", project_id="proj-1", kind="gpu_gb", n=1, session_name="s")

    reconcile_pool([ctx], cfg, lease_db=lease_db)

    assert lease_db.count_free_permits("emulator") == 1   # permit reclaimed
    assert lease_db.list_waiters() == []                  # waiter dropped


# ---- manual mode (admission off, pool configured) -------------------------


def test_reconcile_pool_creates_permits_without_admission(lease_db):
    """With the auto-orchestrator OFF, the pool still materializes so manually
    started workers can acquire — and dead holders are still reaped."""
    ctx = _project_ctx()
    _add_task(ctx, "gone-task", status="done")
    cfg = _cfg(enabled=False, resources=[
        ResourceSpec(kind="emulator", instances=(_inst("a", kind="emulator"),)),
    ])
    # a stale held permit bound to a finished task
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    lease_db.acquire_permits(kind="emulator", n=1, task_id="gone-task", project_id="proj-1", label="x")

    reconcile_pool([ctx], cfg, lease_db=lease_db)

    # permit exists and was reclaimed (task is done) — no admission queued
    assert lease_db.count_free_permits("emulator") == 1
    assert _queued_actions(ctx) == []


# ---- grants (park-and-wake) -----------------------------------------------


def test_grant_assigns_writes_override_and_wakes(lease_db, tmp_path):
    proj = tmp_path / "repo"
    (proj / ".harbor").mkdir(parents=True)
    (proj / ".harbor" / "runtime-target.json").write_text(
        json.dumps({"version": 1, "mode": "ssh", "ssh_host": "box", "target": {"kind": "local"}}),
        encoding="utf-8",
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()

    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running", session_name="sess-1",
              worktree_path=str(worktree))
    lease_db.reconcile_resources(
        [("emulator/a", "emulator", "a", json.dumps({"kind": "emulator", "emulator": {"adb_port": 5557}}))]
    )
    lease_db.enqueue_waiter(task_id="task0001", project_id="proj-1", kind="emulator",
                            n=1, session_name="sess-1")

    tmux = MagicMock()
    granted = run_grants([ctx], lease_db=lease_db, tmux=tmux)

    assert granted == 1
    assert lease_db.list_waiters() == []                 # dequeued
    assert lease_db.count_free_permits("emulator") == 0  # held now
    # override written, preserving mode/ssh and swapping target
    written = json.loads((worktree / ".harbor" / "runtime-target.json").read_text(encoding="utf-8"))
    assert written["mode"] == "ssh" and written["ssh_host"] == "box"
    assert written["target"]["emulator"]["adb_port"] == 5557
    # agent woken via tmux
    tmux.send_keys_literal.assert_called_once()
    assert tmux.send_keys_literal.call_args.args[0] == "sess-1"


def test_grant_strict_fifo_head_of_line_per_kind(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "task-head", status="running", session_name="s1")
    _add_task(ctx, "task-next", status="running", session_name="s2")
    # one free gpu unit; head wants 2 (can't fit), next wants 1 (would fit)
    lease_db.reconcile_resources([("gpu_gb#0", "gpu_gb", None, None)])
    lease_db.enqueue_waiter(task_id="task-head", project_id="proj-1", kind="gpu_gb", n=2, session_name="s1")
    lease_db.enqueue_waiter(task_id="task-next", project_id="proj-1", kind="gpu_gb", n=1, session_name="s2")

    granted = run_grants([ctx], lease_db=lease_db, tmux=MagicMock())

    # strict FIFO: head blocks the kind, next does NOT jump ahead
    assert granted == 0
    assert {w.task_id for w in lease_db.list_waiters()} == {"task-head", "task-next"}


def test_grant_drops_dead_waiter(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "deadtask", status="review")  # not in working window
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    lease_db.enqueue_waiter(task_id="deadtask", project_id="proj-1", kind="emulator", n=1, session_name="s")

    granted = run_grants([ctx], lease_db=lease_db, tmux=MagicMock())

    assert granted == 0
    assert lease_db.list_waiters() == []
    assert lease_db.count_free_permits("emulator") == 1


# ---- live count -----------------------------------------------------------


def test_count_live_agents_includes_inflight_backlog(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "running01", status="running")
    _add_task(ctx, "planned01", status="planning")
    _add_task(ctx, "admitted1")  # backlog, will get an in-flight move
    ctx.db.create_transition_request(task_id="admitted1", action="move_forward", reason="x")
    _add_task(ctx, "idle-back")  # backlog, no move → not counted
    assert count_live_agents([ctx]) == 3


# ---- protocol -------------------------------------------------------------


def test_resource_protocol_mentions_acquire_and_park():
    assert "acquire_runtime" in RESOURCE_PROTOCOL
    assert "release_runtime" in RESOURCE_PROTOCOL
    assert "end your turn" in RESOURCE_PROTOCOL.lower()


# ---- MCP tools ------------------------------------------------------------


def _mcp_service(lease_db, project, db):
    from harbor.mcp_server import HarborMcpService

    svc = HarborMcpService(tmux=MagicMock())
    svc._global_db = lambda: lease_db          # type: ignore[method-assign]
    svc._project_db = lambda pid: (project, db)  # type: ignore[method-assign]
    return svc


def test_acquire_runtime_granted_writes_override(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running", worktree_path=str(worktree))
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    lease_db.reconcile_resources(
        [("emulator/a", "emulator", "a", json.dumps({"kind": "emulator", "emulator": {"adb_port": 5555}}))]
    )
    svc = _mcp_service(lease_db, project, ctx.db)

    res = svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator")

    assert res["status"] == "granted"
    assert res["target"]["emulator"]["adb_port"] == 5555
    written = json.loads((worktree / ".harbor" / "runtime-target.json").read_text(encoding="utf-8"))
    assert written["target"]["emulator"]["adb_port"] == 5555


def test_acquire_runtime_queues_when_busy(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running", session_name="s1")
    _add_task(ctx, "task0002", status="running", session_name="s2")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    svc = _mcp_service(lease_db, project, ctx.db)

    first = svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator")
    second = svc.acquire_runtime(project_id="proj-1", task_id="task0002", kind="emulator")

    assert first["status"] == "granted"
    assert second["status"] == "queued"
    assert second["position"] == 1
    assert {w.task_id for w in lease_db.list_waiters()} == {"task0002"}


def test_acquire_runtime_idempotent_when_already_held(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    svc = _mcp_service(lease_db, project, ctx.db)

    svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator")
    again = svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator")

    assert again["status"] == "granted" and again["already_held"] is True
    assert lease_db.count_free_permits("emulator") == 0  # not double-held


def test_release_runtime_frees_and_clears_waiter(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    lease_db.reconcile_resources([("emulator/a", "emulator", "a", None)])
    svc = _mcp_service(lease_db, project, ctx.db)
    svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator")

    res = svc.release_runtime(project_id="proj-1", task_id="task0001")

    assert res["released"] == 1
    assert lease_db.count_free_permits("emulator") == 1
