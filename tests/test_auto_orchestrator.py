"""Tests for the two subsystems: admission (auto_orchestrator) + the dynamic
claim-by-identity resource broker.

Against an in-memory project DB and a real (temp-file) global lock DB:
- `claim_first_free` (atomic first-free identity lock) + release,
- the FIFO waiter queue (candidates stored per waiter),
- `run_admission` (resource-blind, max_live cap, in-flight guard, opt-out),
- `reconcile` (reap dead holders/waiters) and `run_grants` (FIFO grant +
  worktree override + tmux wake, strict per-kind head-of-line),
- the MCP `acquire_runtime(candidates)` / `release_runtime` tools.
"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harbor import agtx_client as ac
from harbor.agent import Config, load_config
from harbor.agtx_client import AgtxDb, Task, init_test_db, insert_test_task
from harbor.auto_orchestrator import count_live_agents, run_admission
from harbor.resource_broker import (
    RESOURCE_PROTOCOL,
    candidate_pairs,
    reconcile,
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


def _cfg(*, enabled=True, max_live=0):
    base = load_config(None)
    return Config(
        profiles=base.profiles,
        default_profile=base.default_profile,
        auto_orchestrator_enabled=enabled,
        auto_orchestrator_max_live_agents=max_live,
    )


def _cand(key, **target):
    """A candidate dict {key, target} as an agent would pass to acquire_runtime."""
    return {"key": key, "target": target or None}


def _claim(lease_db, kind, candidates, task_id, project_id="proj-1", label="x"):
    return lease_db.claim_first_free(
        kind=kind,
        candidates=candidate_pairs(candidates),
        task_id=task_id,
        project_id=project_id,
        label=label,
    )


def _queued_actions(ctx):
    rows = ctx.db._connect_project().execute(
        "SELECT task_id, action FROM transition_requests"
    ).fetchall()
    return [(r["task_id"], r["action"]) for r in rows]


# ---- candidate_pairs ------------------------------------------------------


def test_candidate_pairs_normalizes_and_skips_keyless():
    pairs = candidate_pairs([
        {"key": "emulator-5554", "target": {"kind": "emulator"}},
        {"key": "", "target": {}},          # skipped: no key
        {"target": {"kind": "emulator"}},   # skipped: no key
        {"key": "emulator-5556"},           # no target ⇒ None
    ])
    assert [p[0] for p in pairs] == ["emulator-5554", "emulator-5556"]
    assert json.loads(pairs[0][1])["kind"] == "emulator"
    assert pairs[1][1] is None


# ---- claim_first_free (dynamic lock registry) -----------------------------


def test_claim_locks_first_free_identity(lease_db):
    got = _claim(lease_db, "emulator",
                 [_cand("emulator-5554"), _cand("emulator-5556")], "t1")
    assert got is not None and got.instance_name == "emulator-5554"
    assert got.permit_id == "emulator/emulator-5554"
    assert got.state == "held"


def test_claim_skips_held_and_takes_next(lease_db):
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "t1")
    # t2 passes both; 5554 is held → it gets 5556
    got = _claim(lease_db, "emulator",
                 [_cand("emulator-5554"), _cand("emulator-5556")], "t2")
    assert got is not None and got.instance_name == "emulator-5556"


def test_claim_returns_none_when_all_held(lease_db):
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "t1")
    _claim(lease_db, "emulator", [_cand("emulator-5556")], "t2")
    got = _claim(lease_db, "emulator",
                 [_cand("emulator-5554"), _cand("emulator-5556")], "t3")
    assert got is None


def test_claim_writes_target_json(lease_db):
    got = _claim(lease_db, "emulator",
                 [_cand("emulator-5554", kind="emulator", emulator={"adb_port": 5554})], "t1")
    assert json.loads(got.target_json)["emulator"]["adb_port"] == 5554


def test_release_frees_identity_for_reclaim(lease_db):
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "t1")
    assert lease_db.release_permits_for_task("t1") == 1
    assert lease_db.list_permits() == []
    # now another task can take it
    got = _claim(lease_db, "emulator", [_cand("emulator-5554")], "t2")
    assert got is not None and got.task_id == "t2"


def test_held_permits_for_task(lease_db):
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "t1")
    held = lease_db.held_permits_for_task("t1")
    assert [p.instance_name for p in held] == ["emulator-5554"]


# ---- waiter queue ---------------------------------------------------------


def test_enqueue_fifo_idempotent_keeps_place(lease_db):
    lease_db.enqueue_waiter(task_id="t1", project_id="p", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5554")]), session_name="s1")
    lease_db.enqueue_waiter(task_id="t2", project_id="p", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5556")]), session_name="s2")
    assert [w.task_id for w in lease_db.list_waiters()] == ["t1", "t2"]
    assert lease_db.waiter_position("t1", "emulator") == 1
    first_enq = lease_db.list_waiters()[0].enqueued_at
    # re-enqueue t1 with new candidates → keeps place + enqueued_at, refreshes candidates
    lease_db.enqueue_waiter(task_id="t1", project_id="p", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-9999")]), session_name="s1b")
    waiters = lease_db.list_waiters()
    assert [w.task_id for w in waiters] == ["t1", "t2"]
    t1 = next(w for w in waiters if w.task_id == "t1")
    assert t1.enqueued_at == first_enq
    assert json.loads(t1.candidates_json)[0]["key"] == "emulator-9999"


# ---- admission ------------------------------------------------------------


def test_admits_ready_skips_blocked_escalated_optedout():
    ctx = _project_ctx()
    _add_task(ctx, "aaaaaaaa")
    _add_task(ctx, "bbbbbbbb", referenced="zzzzzzzz")   # blocked
    _add_task(ctx, "cccccccc", escalation="held")        # escalated
    _add_task(ctx, "dddddddd", description="x\n\n## Auto Orchestrator\nskip\n")  # opted out
    _add_task(ctx, "eeeeeeee")

    admitted = run_admission([ctx], _cfg())

    assert admitted == 2
    assert {tid for tid, _ in _queued_actions(ctx)} == {"aaaaaaaa", "eeeeeeee"}


def test_admission_takes_no_lease_db():
    import inspect
    assert "lease_db" not in inspect.signature(run_admission).parameters


def test_max_live_agents_caps_admission():
    ctx = _project_ctx()
    for tid in ("aaaaaaaa", "bbbbbbbb", "cccccccc"):
        _add_task(ctx, tid)
    assert run_admission([ctx], _cfg(max_live=2)) == 2


def test_running_counts_against_cap():
    ctx = _project_ctx()
    _add_task(ctx, "running01", status="running")
    _add_task(ctx, "backlog01")
    assert run_admission([ctx], _cfg(max_live=1)) == 0


def test_no_duplicate_admission_in_flight():
    ctx = _project_ctx()
    _add_task(ctx, "aaaaaaaa")
    run_admission([ctx], _cfg())
    run_admission([ctx], _cfg())
    assert len(_queued_actions(ctx)) == 1


def test_count_live_agents_includes_inflight_backlog():
    ctx = _project_ctx()
    _add_task(ctx, "running01", status="running")
    _add_task(ctx, "planned01", status="planning")
    _add_task(ctx, "admitted1")
    ctx.db.create_transition_request(task_id="admitted1", action="move_forward", reason="x")
    _add_task(ctx, "idle-back")
    assert count_live_agents([ctx]) == 3


# ---- broker reconcile (reap) ----------------------------------------------


def test_reconcile_reaps_dead_holder_and_waiter(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "gone-task", status="review")  # left the working window
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "gone-task")
    lease_db.enqueue_waiter(task_id="gone-task", project_id="proj-1", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5556")]), session_name="s")

    reconcile([ctx], lease_db=lease_db)

    assert lease_db.list_permits() == []   # held identity reclaimed
    assert lease_db.list_waiters() == []   # waiter dropped


# ---- grants (park-and-wake) -----------------------------------------------


def test_grant_claims_writes_override_and_wakes(lease_db, tmp_path):
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
    lease_db.enqueue_waiter(
        task_id="task0001", project_id="proj-1", kind="emulator",
        candidates_json=json.dumps([
            _cand("emulator-5556", kind="emulator", emulator={"adb_port": 5556})
        ]),
        session_name="sess-1",
    )

    tmux = MagicMock()
    assert run_grants([ctx], lease_db=lease_db, tmux=tmux) == 1

    assert lease_db.list_waiters() == []
    assert [p.instance_name for p in lease_db.list_permits()] == ["emulator-5556"]
    written = json.loads((worktree / ".harbor" / "runtime-target.json").read_text(encoding="utf-8"))
    assert written["mode"] == "ssh" and written["ssh_host"] == "box"
    assert written["target"]["emulator"]["adb_port"] == 5556
    tmux.send_keys_literal.assert_called_once()
    assert tmux.send_keys_literal.call_args.args[0] == "sess-1"


def test_grant_strict_fifo_head_of_line_per_kind(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "task-head", status="running", session_name="s1")
    _add_task(ctx, "task-next", status="running", session_name="s2")
    # 5554 already held by someone else; head wants only 5554 (blocked),
    # next wants 5556 (free) but must NOT jump ahead.
    _claim(lease_db, "emulator", [_cand("emulator-5554")], "holder")
    lease_db.enqueue_waiter(task_id="task-head", project_id="proj-1", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5554")]), session_name="s1")
    lease_db.enqueue_waiter(task_id="task-next", project_id="proj-1", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5556")]), session_name="s2")

    assert run_grants([ctx], lease_db=lease_db, tmux=MagicMock()) == 0
    assert {w.task_id for w in lease_db.list_waiters()} == {"task-head", "task-next"}


def test_grant_drops_dead_waiter(lease_db):
    ctx = _project_ctx()
    _add_task(ctx, "deadtask", status="review")
    lease_db.enqueue_waiter(task_id="deadtask", project_id="proj-1", kind="emulator",
                            candidates_json=json.dumps([_cand("emulator-5554")]), session_name="s")
    assert run_grants([ctx], lease_db=lease_db, tmux=MagicMock()) == 0
    assert lease_db.list_waiters() == []


# ---- protocol -------------------------------------------------------------


def test_resource_protocol_mentions_discover_acquire_park():
    assert "adb devices" in RESOURCE_PROTOCOL
    assert "acquire_runtime" in RESOURCE_PROTOCOL
    assert "release_runtime" in RESOURCE_PROTOCOL
    assert "candidates" in RESOURCE_PROTOCOL
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
    svc = _mcp_service(lease_db, project, ctx.db)

    res = svc.acquire_runtime(
        project_id="proj-1", task_id="task0001", kind="emulator",
        candidates=[{"key": "emulator-5554",
                     "target": {"kind": "emulator", "emulator": {"adb_port": 5554}}}],
    )

    assert res["status"] == "granted" and res["key"] == "emulator-5554"
    assert res["target"]["emulator"]["adb_port"] == 5554
    written = json.loads((worktree / ".harbor" / "runtime-target.json").read_text(encoding="utf-8"))
    assert written["target"]["emulator"]["adb_port"] == 5554


def test_acquire_runtime_queues_when_all_busy(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running", session_name="s1")
    _add_task(ctx, "task0002", status="running", session_name="s2")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    svc = _mcp_service(lease_db, project, ctx.db)

    cands = [{"key": "emulator-5554", "target": {"kind": "emulator"}}]
    first = svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator", candidates=cands)
    second = svc.acquire_runtime(project_id="proj-1", task_id="task0002", kind="emulator", candidates=cands)

    assert first["status"] == "granted"
    assert second["status"] == "queued" and second["position"] == 1
    assert {w.task_id for w in lease_db.list_waiters()} == {"task0002"}


def test_acquire_runtime_idempotent_when_already_held(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    svc = _mcp_service(lease_db, project, ctx.db)

    cands = [{"key": "emulator-5554", "target": {"kind": "emulator"}}]
    svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator", candidates=cands)
    again = svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator", candidates=cands)

    assert again["status"] == "granted" and again["already_held"] is True
    assert len(lease_db.held_permits_for_task("task0001")) == 1  # not double-held


def test_acquire_runtime_rejects_empty_candidates(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    svc = _mcp_service(lease_db, project, ctx.db)

    with pytest.raises(ValueError):
        svc.acquire_runtime(project_id="proj-1", task_id="task0001", kind="emulator", candidates=[])


def test_release_runtime_frees_and_clears_waiter(lease_db, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    ctx = _project_ctx(path=str(proj))
    _add_task(ctx, "task0001", status="running")
    project = types.SimpleNamespace(id="proj-1", path=str(proj))
    svc = _mcp_service(lease_db, project, ctx.db)
    svc.acquire_runtime(
        project_id="proj-1", task_id="task0001", kind="emulator",
        candidates=[{"key": "emulator-5554", "target": {"kind": "emulator"}}],
    )

    res = svc.release_runtime(project_id="proj-1", task_id="task0001")
    assert res["released"] == 1
    assert lease_db.list_permits() == []
