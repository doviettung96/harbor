"""Resource broker: the global runtime-resource pool (acquisition + park-and-wake).

This subsystem is **independent of task orchestration** (see
:mod:`harbor.auto_orchestrator`). Whether a task was started by hand or
auto-admitted, the rule is the same: at its *test* boundary a worker reserves the
runtime it needs (emulator / GPU / app instance) from one global pool, so a
physical resource is never double-booked across the projects run in parallel.

Supply is the typed pool (``harbor.yml`` ``harbor.resources``); demand is
agent-driven via the ``acquire_runtime`` MCP tool. Acquire is one atomic
check-and-hold: free ⇒ the instance's target is written into the worktree and the
agent proceeds; busy ⇒ the task is parked on a FIFO queue (per kind) and ends its
turn. When a permit frees, the supervisor's grant pass (:func:`run_grants`) pops
the head waiter, writes its target, and wakes the parked agent via tmux.

The supervisor runs :func:`reconcile_pool` (materialize permits, reap dead
holders/waiters) and :func:`run_grants` every tick whenever a pool is configured
— regardless of whether the auto-orchestrator (admission) is on.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .agent import Config
from .agtx_transitions import write_target_override

log = logging.getLogger("harbor.resource_broker")

# A task may hold a permit or sit on the wait queue only while it is being
# worked. Any other status (or the task vanishing) frees its permit / drops its
# waiter — the crash-safety net.
_WORKING_STATUSES = frozenset({"planning", "running"})


# Appended to every worker's running-phase prompt when a pool is configured.
RESOURCE_PROTOCOL = """\
## Runtime resource reservation (shared across ALL Harbor projects)

Runtime resources (emulators, GPUs, app instances, game windows) are arbitrated
by one global pool. Do all implementation work freely. But BEFORE anything that
needs an exclusive runtime — your build, `## Verification Probes`, and
`## Related Tests` — reserve it:

1. `mcp__harbor__list_resources` — see the kinds and how many are free.
2. `mcp__harbor__acquire_runtime(project_id=<this task's project>, task_id=<this
   task's id>, kind="<the kind your tests need>", n=1)`. This is one atomic
   check-and-hold; it never busy-loops and never silently fails.
   - `{"status": "granted"}` → its `target` is already written to your worktree
     `.harbor/runtime-target.json`. Run your build/probes/tests now.
   - `{"status": "queued"}` → the resource is busy and you are in line. STOP:
     end your turn and do nothing further. Harbor will message this session when
     the resource is reserved for you; resume your build/tests then.
3. `mcp__harbor__release_runtime(project_id=..., task_id=...)` the INSTANT your
   tests finish, so the next queued task can proceed.

If you crash or the task leaves planning/running, Harbor reclaims your
reservation automatically.
"""


def permit_specs(cfg: Config) -> list[tuple[str, str, str | None, str | None]]:
    """Expand the configured pool into permit rows.

    Returns (permit_id, kind, instance_name, target_json) tuples:
    - instance resources → one permit per named instance, carrying its target.
    - counted resources  → ``capacity`` anonymous permits (no name, no target).
    """
    out: list[tuple[str, str, str | None, str | None]] = []
    for spec in cfg.resources:
        if spec.instances:
            for inst in spec.instances:
                target_json = json.dumps(inst.target) if inst.target else None
                out.append((f"{spec.kind}/{inst.name}", spec.kind, inst.name, target_json))
        else:
            for i in range(spec.capacity):
                out.append((f"{spec.kind}#{i}", spec.kind, None, None))
    return out


def derive_label(task: Any) -> str:
    """A short, human-readable tag for a held permit (branch / short id)."""
    return task.branch_name or f"task/{task.id[:8]}"


def reconcile_pool(contexts: Iterable[Any], cfg: Config, *, lease_db: Any) -> None:
    """Sync the permit table to config and reap dead waiters/permits.

    Run every tick whenever a pool is configured — independent of admission — so
    permits exist for manually-started workers and crashed holders are reclaimed.
    """
    lease_db.reconcile_resources(permit_specs(cfg))
    _reconcile_dead(lease_db, {ctx.project.id: ctx for ctx in contexts})


def run_grants(contexts: Iterable[Any], *, lease_db: Any, tmux: Any) -> int:
    """Grant freed permits to parked waiters (FIFO per kind) and wake them.

    For each waiter in FIFO order: if its task is gone or no longer being worked,
    drop it; otherwise try to atomically acquire ``n`` permits of its kind. On
    success, dequeue it, write the granted instance target into its worktree, and
    ``tmux send-keys`` a resume prompt into its session. Strict FIFO: if the head
    waiter for a kind can't be satisfied this pass, later waiters of that *same*
    kind are skipped (no line-jumping). Returns the number granted.
    """
    ctx_by_id = {ctx.project.id: ctx for ctx in contexts}
    blocked_kinds: set[str] = set()
    granted = 0
    for waiter in lease_db.list_waiters():
        if waiter.kind in blocked_kinds:
            continue
        ctx = ctx_by_id.get(waiter.project_id)
        task = _safe_get_task(ctx, waiter.task_id)
        if task is None or task.status not in _WORKING_STATUSES:
            lease_db.delete_waiter(waiter.waiter_id)
            continue
        permits = lease_db.acquire_permits(
            kind=waiter.kind,
            n=waiter.n,
            task_id=waiter.task_id,
            project_id=waiter.project_id,
            label=derive_label(task),
        )
        if permits is None:
            blocked_kinds.add(waiter.kind)  # head-of-line: hold the queue for this kind
            continue
        lease_db.delete_waiter(waiter.waiter_id)
        _apply_grant(ctx, task, permits, tmux, waiter=waiter)
        granted += 1
    return granted


def _apply_grant(ctx: Any, task: Any, permits: list[Any], tmux: Any, *, waiter: Any) -> None:
    """Write the granted target into the worktree, then wake the parked agent."""
    target = _first_target(permits)
    if target is not None and ctx is not None and task.worktree_path:
        try:
            write_target_override(Path(ctx.path), Path(task.worktree_path), target)
        except Exception:  # noqa: BLE001 — a write failure shouldn't strand the wake
            log.exception("grant: writing runtime-target override failed for task %s", task.id)
    session = waiter.session_name or task.session_name
    if session:
        try:
            tmux.send_keys_literal(session, "", _wake_message(permits), enter=True)
        except Exception:  # noqa: BLE001 — wake is best-effort; reconcile is the backstop
            log.exception("grant: tmux wake failed for session %s", session)


def _wake_message(permits: list[Any]) -> str:
    names = ", ".join(p.instance_name or p.permit_id for p in permits)
    return (
        f"Harbor: your runtime resource is now reserved for you ({names}). "
        "Its target is written to .harbor/runtime-target.json. Resume now: run "
        "your build + probes + related tests, then call "
        "mcp__harbor__release_runtime the instant they finish."
    )


def _reconcile_dead(lease_db: Any, ctx_by_id: dict[str, Any]) -> None:
    """Drop waiters and free permits whose task finished, failed, or vanished."""
    for waiter in lease_db.list_waiters():
        task = _safe_get_task(ctx_by_id.get(waiter.project_id), waiter.task_id)
        if task is None or task.status not in _WORKING_STATUSES:
            lease_db.delete_waiter(waiter.waiter_id)
    for permit in lease_db.list_permits():
        if permit.state != "held" or not permit.task_id:
            continue
        task = _safe_get_task(ctx_by_id.get(permit.project_id), permit.task_id)
        if task is None or task.status not in _WORKING_STATUSES:
            lease_db.release_permit(permit.permit_id)


def _first_target(permits: list[Any]) -> dict | None:
    """The first granted permit's runtime-target, if any (counted permits have none)."""
    for permit in permits:
        if permit.target_json:
            try:
                return json.loads(permit.target_json)
            except (ValueError, TypeError):
                log.warning("grant: bad target_json on permit %s", permit.permit_id)
    return None


def _safe_get_task(ctx: Any, task_id: str) -> Any | None:
    if ctx is None or not getattr(ctx, "db_initialized", False):
        return None
    try:
        return ctx.db.get_task(task_id)
    except Exception:  # noqa: BLE001
        return None
