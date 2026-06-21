"""Resource broker: dynamic claim-by-identity arbitration of runtime resources.

Independent of task orchestration (:mod:`harbor.auto_orchestrator`). There is NO
declared pool — agents DISCOVER what exists at their test boundary and reserve
one. Whether a task was started by hand or auto-admitted, the rule is the same:
before anything that needs an exclusive runtime (emulator / app instance / game
window), the worker enumerates candidate identities (e.g. ``adb devices``) and
calls the ``acquire_runtime`` MCP tool with them. The broker atomically locks the
first candidate not already held by another task — so two tasks never get the
same device — and writes its target into the worktree. If every candidate is
busy, the task is parked on a per-kind FIFO queue and ends its turn; when an
identity frees, the supervisor grant pass re-claims for the head waiter, writes
its target, and wakes the parked agent via tmux (park-and-wake).

The lock registry (held permits) is global so an identity can't be double-booked
across the projects run in parallel. The supervisor runs :func:`reconcile`
(reap crashed holders/waiters) and :func:`run_grants` every tick while the broker
is enabled — regardless of whether the auto-orchestrator (admission) is on.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .agtx_transitions import write_target_override

log = logging.getLogger("harbor.resource_broker")

# A task may hold an identity or sit on the wait queue only while it is being
# worked. Any other status (or the task vanishing) frees its permit / drops its
# waiter — the crash-safety net.
_WORKING_STATUSES = frozenset({"planning", "running"})


# Appended to every worker's running-phase prompt while the broker is enabled.
RESOURCE_PROTOCOL = """\
## Runtime resource reservation (shared across ALL Harbor projects)

Runtime resources (emulators, app instances, game windows) are shared across
every Harbor project on this machine. There is no pre-declared pool — you
discover what exists and reserve one. Do all implementation freely. But BEFORE
anything that needs an exclusive runtime — your build, `## Verification Probes`,
`## Related Tests` — reserve one:

1. DISCOVER the candidates for the kind your task needs. For an Android emulator:
   run `adb devices` and collect every running serial (e.g. emulator-5554,
   emulator-5556). Use the serial as the canonical identity (`key`).
2. RESERVE one — pass ALL discovered candidates in one call:
   `mcp__harbor__acquire_runtime(project_id=<this task's project>,
   task_id=<this task's id>, kind="emulator", candidates=[
     {"key": "emulator-5554", "target": {"kind": "emulator",
        "emulator": {"name": "emulator-5554", "adb_port": 5554},
        "probe_command": "adb -s emulator-5554 get-state"}},
     {"key": "emulator-5556", "target": {"kind": "emulator",
        "emulator": {"name": "emulator-5556", "adb_port": 5556},
        "probe_command": "adb -s emulator-5556 get-state"}}])`.
   The broker atomically locks the first candidate not already held by another
   task and returns it — so two tasks never get the same device.
   - `{"status": "granted", "key": ..., "target": ...}` → the target is written
     to your worktree `.harbor/runtime-target.json`. Run your build/probes/tests
     (via target-runtime-exec, or use the granted serial directly).
   - `{"status": "queued"}` → every candidate is busy and you are in line. STOP:
     end your turn and do nothing further. Harbor will message this pane when one
     frees; resume your build/tests then.
3. `mcp__harbor__release_runtime(project_id=..., task_id=...)` the INSTANT your
   tests finish, so the next queued task can proceed.

If you crash or the task leaves Running, Harbor reclaims your reservation
automatically.
"""


def derive_label(task: Any) -> str:
    """A short, human-readable tag for a held permit (branch / short id)."""
    return task.branch_name or f"task/{task.id[:8]}"


def candidate_pairs(candidates: Any) -> list[tuple[str, str | None]]:
    """Normalize an acquire/​waiter candidate list into (key, target_json) pairs.

    Accepts a list of ``{"key": str, "target": dict|None}`` dicts. Skips entries
    without a non-empty key; serializes each target to JSON (None ⇒ no override).
    """
    pairs: list[tuple[str, str | None]] = []
    if not isinstance(candidates, (list, tuple)):
        return pairs
    for item in candidates:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        target = item.get("target")
        target_json = json.dumps(target) if target else None
        pairs.append((key, target_json))
    return pairs


def reconcile(contexts: Iterable[Any], *, lease_db: Any) -> None:
    """Reap held identities and waiters whose task finished, failed, or vanished.

    The crash-safety net: an agent that died holding a lock (held permit) or
    parked (waiter) without releasing is reclaimed here so the identity isn't
    stranded. There is no supply to materialize — permits exist only while held.
    """
    ctx_by_id = {ctx.project.id: ctx for ctx in contexts}
    for waiter in lease_db.list_waiters():
        task = _safe_get_task(ctx_by_id.get(waiter.project_id), waiter.task_id)
        if task is None or task.status not in _WORKING_STATUSES:
            lease_db.delete_waiter(waiter.waiter_id)
    for permit in lease_db.list_permits():
        if not permit.task_id:
            continue
        task = _safe_get_task(ctx_by_id.get(permit.project_id), permit.task_id)
        if task is None or task.status not in _WORKING_STATUSES:
            lease_db.release_permit(permit.permit_id)


def run_grants(contexts: Iterable[Any], *, lease_db: Any, tmux: Any) -> int:
    """Grant a freed identity to parked waiters (FIFO per kind) and wake them.

    For each waiter in FIFO order: drop it if its task is gone or no longer being
    worked; otherwise re-attempt ``claim_first_free`` against its stored
    candidates. On success, dequeue it, write the granted target into its
    worktree, and ``tmux send-keys`` a resume prompt into its session. Strict
    FIFO: if the head waiter for a kind still can't claim any candidate, later
    waiters of that *same* kind are skipped this pass (no line-jumping). Returns
    the number granted.
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
        try:
            candidates = candidate_pairs(json.loads(waiter.candidates_json))
        except (ValueError, TypeError):
            candidates = []
        permit = lease_db.claim_first_free(
            kind=waiter.kind,
            candidates=candidates,
            task_id=waiter.task_id,
            project_id=waiter.project_id,
            label=derive_label(task),
        )
        if permit is None:
            blocked_kinds.add(waiter.kind)  # head-of-line: hold the queue for this kind
            continue
        lease_db.delete_waiter(waiter.waiter_id)
        _apply_grant(ctx, task, permit, tmux, waiter=waiter)
        granted += 1
    return granted


def _apply_grant(ctx: Any, task: Any, permit: Any, tmux: Any, *, waiter: Any) -> None:
    """Write the granted target into the worktree, then wake the parked agent."""
    target = _permit_target(permit)
    if target is not None and ctx is not None and task.worktree_path:
        try:
            write_target_override(Path(ctx.path), Path(task.worktree_path), target)
        except Exception:  # noqa: BLE001 — a write failure shouldn't strand the wake
            log.exception("grant: writing runtime-target override failed for task %s", task.id)
    session = waiter.session_name or task.session_name
    if session:
        try:
            tmux.send_keys_literal(session, "", _wake_message(permit), enter=True)
        except Exception:  # noqa: BLE001 — wake is best-effort; reconcile is the backstop
            log.exception("grant: tmux wake failed for session %s", session)


def _wake_message(permit: Any) -> str:
    name = permit.instance_name or permit.permit_id
    return (
        f"Harbor: your runtime resource is now reserved for you ({name}). "
        "Its target is written to .harbor/runtime-target.json. Resume now: run "
        "your build + probes + related tests, then call "
        "mcp__harbor__release_runtime the instant they finish."
    )


def _permit_target(permit: Any) -> dict | None:
    if not permit.target_json:
        return None
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
