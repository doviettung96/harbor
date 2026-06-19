"""Auto-orchestrator: the task-admission controller (orchestration only).

This subsystem decides *which tasks run*, not *what resources they use* — runtime
resources are a separate concern owned by :mod:`harbor.resource_broker`.

When enabled, the webui's background supervisor calls :func:`run_admission` every
poll: it auto-pulls *ready* Backlog tasks into Planning by queuing a
``move_forward`` transition — exactly what a human does by clicking "Move
forward". Admission is **resource-blind** (coding needs no runtime); the only
bound is a soft ``max_live_agents`` cap on concurrent worker spawns. It
deliberately stops at Review — Done stays the user's manual acceptance gate.

Turning the auto-orchestrator off reverts to fully manual task driving; resource
arbitration still applies (the broker runs whenever a pool is configured).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from .agent import Config
from .agtx_transitions import task_orchestrator_optout

log = logging.getLogger("harbor.auto_orchestrator")


def count_live_agents(contexts: Iterable[Any]) -> int:
    """How many worker agents are committed: planning + running, plus Backlog
    tasks with an admission (``move_forward``) still in flight (admitted but not
    yet spawned). The in-flight term keeps the cap stable across ticks."""
    n = 0
    for ctx in contexts:
        if not getattr(ctx, "db_initialized", False):
            continue
        try:
            for status in ("planning", "running"):
                n += len(ctx.db.list_tasks(status=status))
            for task in ctx.db.list_tasks(status="backlog"):
                if ctx.db.count_unprocessed_transitions(task.id, "move_forward") > 0:
                    n += 1
        except Exception:  # noqa: BLE001 — one bad project shouldn't skew the rest
            log.exception("auto-orchestrator: live-agent count failed for %s", _pid(ctx))
    return n


def run_admission(contexts: Iterable[Any], cfg: Config) -> int:
    """One admission pass across all projects. Returns the number admitted.

    `contexts` is an iterable of objects exposing ``.project.id``, ``.db`` (an
    ``AgtxDb``) and ``.db_initialized``. Admits ready Backlog tasks (deps
    satisfied, not escalated, no admission already in flight) until the soft
    ``max_live_agents`` cap is hit. Touches only the task DB — never the resource
    pool.
    """
    contexts = list(contexts)
    max_live = cfg.auto_orchestrator_max_live_agents
    live = count_live_agents(contexts)
    admitted = 0
    for ctx in contexts:
        if max_live and live >= max_live:
            break
        if not ctx.db_initialized:
            continue
        try:
            backlog = ctx.db.list_tasks(status="backlog")
        except Exception:  # noqa: BLE001
            log.exception("auto-orchestrator: listing backlog failed for %s", _pid(ctx))
            continue
        for task in backlog:
            if max_live and live >= max_live:
                break
            if not task.deps_satisfied:
                continue
            if task.escalation_note:
                continue
            # User (or a template) opted this task out of auto-admission.
            if task_orchestrator_optout(task):
                continue
            # Already admitted on a prior tick and still spawning — don't queue a
            # duplicate move_forward.
            try:
                if ctx.db.count_unprocessed_transitions(task.id, "move_forward") > 0:
                    continue
            except Exception:  # noqa: BLE001
                pass
            ctx.db.create_transition_request(
                task_id=task.id,
                action="move_forward",
                reason="auto-orchestrator admission",
            )
            live += 1
            admitted += 1
    return admitted


def _pid(ctx: Any) -> Any:
    try:
        return ctx.project.id
    except Exception:  # noqa: BLE001
        return "<unknown>"
