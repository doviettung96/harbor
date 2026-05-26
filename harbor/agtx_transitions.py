"""Background processor for agtx transition_requests.

agtx's MCP server (`mcp__agtx__move_task`) only INSERTs a row into
`transition_requests`. The actual side effects — `git worktree add`, tmux
session creation, agent CLI launch — are normally executed by the agtx ratatui
TUI's polling loop (`D:/Projects/agtx/src/tui/app.rs:5464`
`process_transition_requests`).

On Windows the TUI is unusable, so harbor's webview takes over the executor
role. This module implements the minimal-viable subset described in the plan:
no plugin system, no agent registry, no skill resolution. Just create a
worktree, spawn a tmux session, launch one configurable agent CLI, and let the
agent itself walk the task forward via the `agtx-task-worker` skill.
"""
from __future__ import annotations

import logging
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .agtx_client import AgtxDb, Task, TransitionRequest, strip_extended_length_prefix
from .plugin_loader import (
    AGENT_NATIVE_PATHS,
    AutoDismiss,
    WorkflowPlugin,
    determine_phase_variant,
    resolve_prompt as plugin_resolve_prompt,
    resolve_skill_command as plugin_resolve_skill_command,
    resolve_skills_dir,
)
from .tmux import Tmux, TmuxError

log = logging.getLogger(__name__)

# Default agent command if `harbor.yml` doesn't override it. Claude on
# Windows is the most-tested path; users can pass --agent-command on the CLI.
DEFAULT_AGENT_COMMAND: tuple[str, ...] = ("claude", "--dangerously-skip-permissions")
DEFAULT_BASE_BRANCH = "main"
DEFAULT_WORKTREE_DIR = ".worktrees"
SHARED_INSTRUCTIONS_REL = Path(".agtx") / "shared-instructions.md"
WORKER_INSTRUCTIONS_HEADER = "## Worker Instructions"
CODEX_GOAL_HEADER = "## Codex Goal"
_SECTION_HEADER_RE = re.compile(r"(?m)^##\s+.+$")

# A pane whose last non-empty line matches this is sitting at a PowerShell or
# cmd.exe prompt (`PS C:\...>` / `C:\...>`). Used — alongside a bare `$`/`#`
# check — to detect that a spawned agent CLI exited on startup and left the
# pane at an interactive shell, so harbor never types a phase prompt into it.
_CMD_PROMPT_RE = re.compile(r"^(PS )?[A-Za-z]:[\\/].*>$")

# Built-in mapping from agtx's `tasks.agent` column to the CLI to launch.
# agtx writes a generic agent kind (claude/codex/gemini/copilot) when the
# task is created; we expand it to a sensible default invocation. Users
# override individual entries via `agent_command_by_agent` / `--map-agent`.
DEFAULT_AGENT_COMMAND_BY_AGENT: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "--dangerously-skip-permissions"),
    "codex": ("codex",),
    "gemini": ("gemini",),
    "copilot": ("gh", "copilot", "suggest"),  # best-effort; user likely overrides
}

# Default per-phase prompts pushed into the agent pane after each forward
# transition. They reference the agtx-task-worker / agtx-task-verify skills
# and rely on `$AGTX_TASK_ID` being set in the pane env.
DEFAULT_PHASE_PROMPTS: dict[str, str] = {
    "planning": (
        "You are the worker for an agtx task. $AGTX_TASK_ID is set in this "
        "pane's environment. Invoke the agtx-task-worker skill — read the task "
        "description, parse the three headers (Acceptance Criteria, Verification "
        "Probes, Runtime Target), and PLAN the work. Do not implement yet. When "
        "the plan is ready, stop and wait for me to move the task to Running."
    ),
    "running": (
        "Now in the Running phase. Implement the work for $AGTX_TASK_ID per the "
        "plan and ## Acceptance Criteria. When implementation is complete, "
        "before calling verify: re-read your own diff "
        "(`git diff $(git merge-base HEAD main)...HEAD` in the worktree) and "
        "look for obvious issues — leftover debug prints, TODOs, dead or "
        "commented-out code, unrelated changes, wrong files committed. Fix "
        "what you find. Then run the agtx-task-verify skill (which executes "
        "## Verification Probes via target-runtime-exec). If verify reports "
        "passed, call mcp__agtx__move_task(task_id=\"$AGTX_TASK_ID\", "
        "action=\"move_forward\") yourself to advance the task to Review. If "
        "verify reports failed, fix the failure and re-run verify — never "
        "advance with failing probes."
    ),
    "review": (
        "Now in the Review phase. FIRST action: verify the PR exists by "
        "running `gh pr list --head $(git rev-parse --abbrev-ref HEAD) "
        "--json url,number,state` in the worktree. If a PR is found, that "
        "is THE pull request for this task — report the URL, then run the "
        "agtx-task-verify skill once more and summarize what changed, what "
        "probes ran, what passed, any follow-ups. DO NOT open another PR "
        "yourself (never run `gh pr create`); harbor handles PR creation, "
        "and Running-bounce re-entries reuse the same PR — if I move this "
        "task back to Running for revisions, commit your fixes and push to "
        "the SAME branch and the existing PR will pick up the new commits. "
        "If NO PR is found, harbor's PR-on-Review step failed silently — "
        "stop, report this to me, and do NOT open a PR yourself; I will "
        "diagnose (usually `gh auth status` or a duplicate-branch issue) "
        "and retry. Wait for me to mark Done after the PR is merged."
    ),
}

# Markers a freshly-launched agent is "ready" for input. We look for any of
# these in the pane content; if none appear within the timeout we still proceed
# (the prompt may sit in the agent's input buffer until it's ready).
AGENT_READY_MARKERS: tuple[str, ...] = (
    # Claude REPL
    "Try \"",
    "/help for help",
    # Codex
    "to send",
    # Generic prompts: just look for content stabilization
)

# Auto-dismiss: when one of these substrings appears in the pane, send the
# paired response (plus Enter). Useful for "trust this folder?" style
# confirmation dialogs at agent startup that block the agent from reaching
# its prompt. The default set covers Gemini's trust dialog and Claude's
# bypass-permissions dialog (in case the user runs claude WITHOUT
# --dangerously-skip-permissions).
DEFAULT_AUTO_DISMISS: tuple[tuple[str, str], ...] = (
    # Claude bypass prompt — "1" = "No", "2" = "Yes, I accept"
    ("Yes, I accept", "2"),
    ("I accept the risk", "2"),
    # Gemini trust dialog — "1" trusts the folder
    ("Do you trust the files in this folder?", "1"),
)


@dataclass(frozen=True)
class TransitionConfig:
    """Configurable knobs for the transition executor.

    `agent_command` — argv typed into the spawned tmux pane to launch the agent.
    `init_script`  — shell commands run in the new worktree BEFORE the agent
        launches. One command per element; each runs sequentially via
        `subprocess.run(... shell=False, cwd=<worktree>)` after splitting with
        shlex. Failures abort the transition.
    `copy_files`   — file paths (relative to project_path) copied into the same
        relative path under the worktree. Useful for `.env` or any gitignored
        config the agent needs.
    `phase_prompts` — text injected into the pane after the agent CLI launches
        and stabilizes. Empty string disables for that phase.
    `inject_prompts` — global kill switch; False to disable all prompt pushing.
    `agent_ready_timeout_s` — max seconds to wait for the agent CLI to settle
        before injecting the prompt. We proceed regardless after the timeout.
    """
    project_path: Path
    # User's explicit global default agent command. When None, falls back to
    # `DEFAULT_AGENT_COMMAND_BY_AGENT[task.agent]` (if matched) or
    # `DEFAULT_AGENT_COMMAND`. When set, it beats the built-in agent map but
    # NOT user-supplied `agent_command_by_agent` / `agent_command_by_phase` —
    # those are explicit per-task or per-phase choices.
    agent_command: Sequence[str] | None = None
    # Per-phase override. If a phase key is present, that command replaces
    # `agent_command` for the session spawn that produces that phase. Only
    # the *spawning* phase matters today (currently `planning`), since
    # later phases reuse the same session — but the dict is keyed by phase
    # so a future "respawn on each phase" mode can plug in without changes.
    agent_command_by_phase: dict[str, Sequence[str]] = field(default_factory=dict)
    # Mapping from agtx task.agent value (claude/codex/gemini/etc.) to argv.
    # Resolved BEFORE agent_command_by_phase, so a per-task agent kind beats a
    # per-phase override. Falls back to DEFAULT_AGENT_COMMAND_BY_AGENT for any
    # key the user didn't supply.
    agent_command_by_agent: dict[str, Sequence[str]] = field(default_factory=dict)
    base_branch: str = DEFAULT_BASE_BRANCH
    worktree_dir: str = DEFAULT_WORKTREE_DIR
    init_script: tuple[str, ...] = ()
    copy_files: tuple[str, ...] = ()
    prompt_append: str = ""
    phase_prompts: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PHASE_PROMPTS))
    inject_prompts: bool = True
    agent_ready_timeout_s: float = 20.0
    # Prompt-submit choreography. After the prompt body is typed into the agent
    # pane, harbor waits for the pane to render it, pauses `prompt_submit_delay_s`,
    # THEN sends a standalone Enter. Folding the Enter into the same send-keys
    # burst makes agent TUIs (codex especially — Ink input boxes with paste-burst
    # detection) treat it as a literal newline: the message stays in the draft
    # box, unsent. `prompt_render_timeout_s` caps the render/picker poll; 0
    # disables polling (used by tests to avoid real sleeps). Mirrors agtx's
    # `send_skill_and_prompt` timing (D:/Projects/agtx/src/tui/app.rs:8045-8101).
    prompt_submit_delay_s: float = 0.3
    prompt_render_timeout_s: float = 4.0
    # Substring → response. When the substring appears in the readiness-loop
    # pane capture, the response is typed and Enter is pressed. Pass an empty
    # tuple to disable; otherwise extends the defaults.
    auto_dismiss: tuple[tuple[str, str], ...] = DEFAULT_AUTO_DISMISS
    # On Running→Review, push the task branch and open a PR via `gh pr create`.
    # On by default — disable with `--no-pr-on-review` (CLI) or by passing
    # `pr_on_review=False` to `create_app`. Failures don't block the
    # transition — the error is recorded on the task and the user retries
    # from the UI. Skips automatically if the task already has a `pr_url`
    # (e.g. re-entering Review after a Running bounce — the existing PR
    # picks up new commits automatically). Requires `gh` on PATH.
    pr_on_review: bool = True
    # Path to the shell tmux should use for new sessions on Windows. Mirrors
    # `agent.Config.default_shell` — Git Bash so `cd` and `export` work.
    # None = inherit tmux's default (cmd.exe on Windows, which breaks our POSIX
    # send-keys). Passed to `Tmux.ensure_session` for the post-create
    # `set-option default-shell` fallback when the server is already running.
    default_shell: str | None = None
    # Optional workflow plugin (parsed from a `plugin.toml`). When set, its
    # `commands` and `prompts` override `phase_prompts` and provide
    # auto-dismiss patterns. Skill command is typed first (e.g.
    # `/agtx-task-worker abc12345`), then the free-text prompt — same flow
    # agtx's TUI uses.
    plugin: WorkflowPlugin | None = None


# Thin abstractions so tests can mock without monkeypatching subprocess/Tmux globally.

class GitOps:
    """Minimal git wrapper for worktree create/remove. Subclassable for tests."""

    def add_worktree(
        self, repo_root: Path, worktree_path: Path, branch: str, base_branch: str
    ) -> None:
        if worktree_path.exists():
            return
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        # git rejects extended-length `\\?\` paths for both cwd and the
        # worktree argument; normalize before shelling out.
        repo_root = strip_extended_length_prefix(repo_root)
        worktree_path = strip_extended_length_prefix(worktree_path)
        if self._branch_exists(branch, cwd=repo_root):
            # The branch is left over from an earlier run whose `worktree add`
            # failed *after* `-b` created the branch. Attach a worktree to the
            # existing branch instead of trying (and failing) to recreate it.
            argv = ["git", "worktree", "add", str(worktree_path), branch]
        else:
            argv = [
                "git", "worktree", "add",
                "-b", branch,
                str(worktree_path),
                base_branch,
            ]
        self._run_git(argv, cwd=repo_root)

    def _branch_exists(self, branch: str, *, cwd: Path) -> bool:
        cp = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(cwd), check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return cp.returncode == 0

    def remove_worktree(self, repo_root: Path, worktree_path: Path) -> None:
        if not worktree_path.exists():
            return
        repo_root = strip_extended_length_prefix(repo_root)
        worktree_path = strip_extended_length_prefix(worktree_path)
        argv = ["git", "worktree", "remove", "--force", str(worktree_path)]
        self._run_git(argv, cwd=repo_root)

    def delete_branch(self, repo_root: Path, branch: str) -> None:
        repo_root = strip_extended_length_prefix(repo_root)
        argv = ["git", "branch", "-D", branch]
        self._run_git(argv, cwd=repo_root)

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        """`git push -u origin <branch>` from inside the worktree.

        Raises RuntimeError on non-zero exit so the caller (PR opener) can
        record the failure on the task without aborting the Done transition.
        """
        wt = strip_extended_length_prefix(worktree_path)
        argv = ["git", "push", "-u", "origin", branch]
        cp = subprocess.run(
            argv, cwd=str(wt),
            check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"git push failed: argv={argv!r} stderr={cp.stderr.strip()!r}"
            )

    def open_pull_request(
        self, worktree_path: Path, *, base: str, title: str, body: str,
    ) -> str:
        """`gh pr create` from the worktree; returns the PR URL.

        Body goes through a temp file so newlines/quotes survive the argv hop
        on Windows. gh prints the PR URL as the last `https://` line of stdout.
        """
        import tempfile
        wt = strip_extended_length_prefix(worktree_path)
        with tempfile.NamedTemporaryFile(
            "w", delete=False, suffix=".md", encoding="utf-8",
        ) as fh:
            fh.write(body or "Opened by harbor on Done.")
            body_path = fh.name
        try:
            argv = [
                "gh", "pr", "create",
                "--base", base,
                "--title", title,
                "--body-file", body_path,
            ]
            cp = subprocess.run(
                argv, cwd=str(wt),
                check=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        finally:
            try:
                Path(body_path).unlink()
            except OSError:
                pass
        if cp.returncode != 0:
            raise RuntimeError(
                f"gh pr create failed: argv={argv[:5]!r}... "
                f"stderr={(cp.stderr or cp.stdout).strip()!r}"
            )
        for line in reversed(cp.stdout.splitlines()):
            ln = line.strip()
            if ln.startswith("https://"):
                return ln
        raise RuntimeError(
            f"gh pr create produced no URL; stdout={cp.stdout!r}"
        )

    def _run_git(self, argv: list[str], *, cwd: Path) -> None:
        cp = subprocess.run(
            argv, cwd=str(cwd),
            check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"git failed: argv={argv!r} stderr={cp.stderr.strip()!r}"
            )


# Source-status preconditions for the two explicit "move to a specific
# column" actions. The actions themselves carry side effects (opening a PR
# on entry to Review; tearing down the session + worktree on entry to Done)
# so they aren't pure noops — but they share the validate-source-then-
# transition shape, hence this lookup.
_EXPLICIT_TARGET_SOURCE = {
    "move_to_review": ("running", "review"),
    "move_to_done": ("review", "done"),
}


def _generate_session_name(task: Task) -> str:
    """Mirror agtx's `Task::generate_session_name` (D:/Projects/agtx/src/db/models.rs:120).

    Format: `task-<id[:8]>--<project_id_safe>--<title_slug>`. We don't have the
    project name handy at the DB layer, so we use project_id (which agtx
    interpolates the same way). tmux-safe characters only.
    """
    head = task.id[:8]
    proj = _safe_session_chunk(task.project_id)
    slug = _safe_session_chunk(task.title)[:20]
    return f"task-{head}--{proj}--{slug}".strip("-")


def _safe_session_chunk(s: str) -> str:
    """tmux session names can't contain `:` or `.`; we also drop spaces.

    Matches agtx's `safe_session_name` (alphanumeric -> kept; everything else
    -> dash; trim leading/trailing dashes)."""
    out = []
    for ch in s.lower():
        out.append(ch if ch.isalnum() else "-")
    return "".join(out).strip("-")


@dataclass
class TransitionWorker:
    """Polls agtx's transition_requests table and executes side effects.

    Lifecycle:
      worker = TransitionWorker(db=db, config=cfg)
      worker.start()        # spawns daemon thread
      ...
      worker.stop()         # cooperative shutdown
    """

    db: AgtxDb
    config: TransitionConfig
    tmux: Tmux = field(default_factory=Tmux)
    git: GitOps = field(default_factory=GitOps)
    poll_interval: float = 2.0
    instance_id: str = field(default_factory=lambda: f"harbor-webui@{socket.gethostname()}")
    on_event: Callable[[str, dict], None] | None = None  # (event_name, payload)

    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _last_unhealthy_log: float = 0.0  # rate-limit "DB not initialized" warnings

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="agtx-transition-worker", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def _loop(self) -> None:
        last_cleanup = 0.0
        while not self._stop.is_set():
            # Skip work entirely if the DB isn't initialized — otherwise we'd
            # spam OperationalError 30 times a minute. The webui startup
            # already surfaced this state as a user-facing error; here we
            # just idle quietly with a once-per-minute reminder log.
            if not self.db.is_initialized():
                now = time.time()
                if now - self._last_unhealthy_log > 60:
                    log.warning(
                        "agtx transition_requests table missing — worker idling. "
                        "Run `python -m harbor webui-diagnose` for resolution."
                    )
                    self._last_unhealthy_log = now
                self._stop.wait(self.poll_interval)
                continue

            try:
                self.process_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("agtx transition loop iteration crashed")
            now = time.time()
            if now - last_cleanup > 600:
                try:
                    self.db.cleanup_old_transition_requests()
                except Exception:  # noqa: BLE001
                    log.exception("transition_requests cleanup failed")
                last_cleanup = now
            self._stop.wait(self.poll_interval)

    # ---- single-tick interface (also used by tests) -----------------------

    def process_once(self) -> int:
        """Process all currently-pending transitions. Returns count claimed."""
        pending = self.db.pending_transition_requests()
        claimed = 0
        for req in pending:
            if not self.db.claim_transition_request(req.id, self.instance_id):
                continue
            claimed += 1
            try:
                self._execute(req)
            except Exception as exc:  # noqa: BLE001 — record per-request
                err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                self.db.mark_transition_processed(req.id, error=err[:4000])
                self._emit("transition_failed", {
                    "request_id": req.id,
                    "task_id": req.task_id,
                    "action": req.action,
                    "error": str(exc),
                })
                continue
            self.db.mark_transition_processed(req.id, error=None)
            self._emit("transition_succeeded", {
                "request_id": req.id,
                "task_id": req.task_id,
                "action": req.action,
            })
        return claimed

    # ---- dispatch ---------------------------------------------------------

    def _execute(self, req: TransitionRequest) -> None:
        task = self.db.get_task(req.task_id)
        if task is None:
            raise RuntimeError(f"task {req.task_id!r} not found")

        action = req.action
        if action == "escalate_to_user":
            self.db.update_task(task.id, escalation_note=req.reason or "")
            return

        if action in _EXPLICIT_TARGET_SOURCE:
            required_source, target = _EXPLICIT_TARGET_SOURCE[action]
            if task.status != required_source:
                raise RuntimeError(
                    f"{action} requires status={required_source} "
                    f"(got {task.status!r})"
                )
            if action == "move_to_review":
                self._open_pr_for_task(task)
                self._inject_phase_prompt(task, phase="review")
            elif action == "move_to_done":
                self._teardown_session(task)
            self.db.update_task(task.id, status=target)
            return

        if action == "move_forward":
            self._move_forward(task)
            return

        if action == "move_backward":
            self._move_backward(task)
            return

        if action == "move_to_backlog":
            self._move_to_backlog(task)
            return

        if action in ("move_to_planning", "research"):
            if task.status != "backlog":
                raise RuntimeError(
                    f"{action} requires status=backlog (got {task.status!r})"
                )
            self._ensure_dependencies_satisfied(task)
            self._spawn_session(task, target_status="planning")
            return

        if action == "move_to_running":
            if task.status not in ("backlog", "planning"):
                raise RuntimeError(
                    f"move_to_running requires status=backlog or planning (got {task.status!r})"
                )
            if task.session_name and self.tmux.has_session(task.session_name):
                self.db.update_task(task.id, status="running")
            else:
                self._spawn_session(task, target_status="running")
            return

        if action == "resume":
            if not task.session_name:
                raise RuntimeError("resume: task has no session_name")
            if not self.tmux.has_session(task.session_name):
                # Re-create the session (worktree should still exist)
                wt = task.worktree_path or self._default_worktree_path(task)
                self.tmux.ensure_session(str(task.session_name), str(wt))
                self._launch_agent(task.session_name)
            return

        raise RuntimeError(f"unknown action {action!r}")

    def _move_backward(self, task: Task) -> None:
        if task.status == "backlog":
            raise RuntimeError("task is already in Backlog; cannot move backward")
        if task.status == "planning":
            self._move_to_backlog(task)
        elif task.status == "running":
            self.db.update_task(task.id, status="planning")
        elif task.status == "review":
            self.db.update_task(task.id, status="running")
        elif task.status == "done":
            # Done is terminal: the session is gone, the worktree is removed.
            # There is no coherent way to rewind Done → Review.
            raise RuntimeError("task is Done; Done is terminal, cannot move back")
        else:
            raise RuntimeError(f"unknown status {task.status!r}")

    def _move_to_backlog(self, task: Task) -> None:
        """Reset a task to Backlog. Kills the tmux session and clears the
        per-task session/worktree/branch fields so the next forward push spawns
        cleanly. The worktree on disk is left in place — the user can inspect
        it or remove it manually."""
        if task.session_name and self.tmux.has_session(task.session_name):
            self.tmux.kill_session(task.session_name)
        # Clear the per-run fields so _spawn_session picks fresh names next time.
        # SQLite expects None to write NULL.
        self.db.update_task(
            task.id,
            status="backlog",
            session_name=None,
            worktree_path=None,
            branch_name=None,
            escalation_note=None,
        )
        self._emit("task_reset", {
            "task_id": task.id,
            "previous_session": task.session_name,
            "previous_worktree": task.worktree_path,
        })

    def _move_forward(self, task: Task) -> None:
        if task.status == "backlog":
            self._ensure_dependencies_satisfied(task)
            self._spawn_session(task, target_status="planning")
        elif task.status == "planning":
            # Same session; flip status and push the running-phase prompt so
            # the agent knows it's now in implementation mode.
            self.db.update_task(task.id, status="running")
            self._inject_phase_prompt(task, phase="running")
        elif task.status == "running":
            # Open PR first (idempotent on existing pr_url so re-entering
            # Review after a Running bounce reuses the same PR), then inject
            # the review-phase prompt, then flip status.
            self._open_pr_for_task(task)
            self._inject_phase_prompt(task, phase="review")
            self.db.update_task(task.id, status="review")
        elif task.status == "review":
            self._teardown_session(task)
            self.db.update_task(task.id, status="done")
        elif task.status == "done":
            raise RuntimeError("task is already done")
        else:
            raise RuntimeError(f"unknown status {task.status!r}")

    @staticmethod
    def _ensure_dependencies_satisfied(task: Task) -> None:
        if task.deps_satisfied:
            return
        blockers = ", ".join(
            f"{dep.short_id} {dep.title} [{dep.status}]"
            for dep in task.blocking_dependencies
        )
        raise RuntimeError(f"task is blocked by dependencies: {blockers}")

    # ---- side-effect primitives -------------------------------------------

    def _spawn_session(self, task: Task, *, target_status: str) -> None:
        """Create worktree + tmux session, launch agent CLI, push initial prompt, update task row."""
        branch = task.branch_name or f"task/{task.id[:8]}"
        worktree_path = self._default_worktree_path(task, branch=branch)
        session = task.session_name or _generate_session_name(task)

        # 1. git worktree add (idempotent — GitOps no-ops if path exists).
        self.git.add_worktree(
            self.config.project_path, worktree_path,
            branch=branch, base_branch=self.config.base_branch,
        )

        # 2. Copy gitignored files (e.g. .env) from the main repo into the worktree.
        self._copy_files_into_worktree(worktree_path)

        # 3. Run init_script in the worktree (e.g. `pip install -e .`).
        self._run_init_script(worktree_path)

        # 3a. Persist per-task worker instructions for later skill invocations.
        self._write_shared_instructions(worktree_path, task)

        # 3b. Deploy plugin's skills into the worktree so the agent CLI can
        #     find them when it starts up. Mirrors agtx's `write_skills_to_worktree`
        #     (tui/app.rs:8880-8950) — writes to .agtx/skills/ AND the agent's
        #     native discovery path (.claude/commands/agtx/, .codex/skills/, etc.).
        self._deploy_plugin_skills_to_worktree(worktree_path, task)

        # 4. tmux session in the worktree.
        self.tmux.ensure_session(
            session, str(worktree_path), default_shell=self.config.default_shell,
        )

        # 5. Type a SINGLE line into the pane that works regardless of shell:
        #    `"<bash>" -c "cd <worktree> && export AGTX_TASK_ID=<id> && exec <agent>"`.
        #    cmd.exe / PowerShell / bash all forward this to bash.exe, which
        #    then runs cd + export + exec in one go. Avoids the "pane shell is
        #    cmd.exe so `export` fails" class of bug entirely.
        agent_argv = self._resolve_agent_argv(task=task, phase=target_status)
        agent_argv = self._maybe_enable_codex_goals(agent_argv, task)
        launcher = self._build_pane_launcher(worktree_path, task.id, agent_argv)
        try:
            self.tmux.send_keys_literal(session, "", launcher, enter=True)
        except TmuxError as exc:
            raise RuntimeError(f"tmux send-keys (agent launch) failed: {exc}") from exc

        # 6. Wait for the agent CLI to come up. If the pane fell back to a
        #    shell prompt — the agent exited on startup, most often codex
        #    performing a self-update ("Please restart Codex.") and quitting —
        #    relaunch it once. If it still isn't an agent, this raises and the
        #    transition is marked failed WITHOUT typing the prompt into a live
        #    shell (prompt text has backticks/parens — running it is unsafe).
        self._await_agent_or_relaunch(session, launcher)

        # 7. Persist task state now that the agent is confirmed up. Doing this
        #    AFTER step 6 means a launch failure leaves the task in Backlog so
        #    the user can simply retry the move.
        self.db.update_task(
            task.id,
            status=target_status,
            session_name=session,
            worktree_path=str(worktree_path),
            branch_name=branch,
        )
        task.status = target_status
        task.session_name = session
        task.worktree_path = str(worktree_path)
        task.branch_name = branch

        # 8. Inject the phase prompt — step 6 already settled the agent, so no
        #    extra readiness wait here.
        self._inject_phase_prompt_for_session(
            session=session, phase=target_status, wait_for_ready=False, task=task,
        )

    def _build_pane_launcher(
        self, worktree: Path, task_id: str, agent_argv: Sequence[str],
    ) -> str:
        """Build a single pane command that works in any shell.

        When `default_shell` is configured (typically Git Bash on Windows), we
        wrap with `"<bash>" -c "..."`. cmd.exe and PowerShell will both
        forward this correctly to bash.exe; bash will execute the inner
        script. Inner script uses single-quoted values so cmd.exe's outer
        double-quote parsing doesn't conflict.

        When `default_shell` is None, fall back to typing the agent argv
        directly — assumes the pane shell is POSIX-y enough to handle `cd`
        and `export` via the separate send-keys path (legacy behavior).
        """
        bash = self.config.default_shell
        if not bash:
            return " ".join(shlex.quote(p) for p in agent_argv)

        worktree_for_bash = str(worktree).replace("\\", "/")
        cd_cmd = f"cd '{worktree_for_bash}'"
        export_cmd = f"export AGTX_TASK_ID='{task_id}'"
        agent_quoted = " ".join(shlex.quote(p) for p in agent_argv)
        inner = f"{cd_cmd} && {export_cmd} && exec {agent_quoted}"
        return f'"{bash}" -c "{inner}"'

    def _deploy_plugin_skills_to_worktree(
        self, worktree_path: Path, task: Task,
    ) -> None:
        """Copy the plugin's SKILL.md files into the worktree.

        Two destinations, matching agtx's `write_skills_to_worktree`:
          1. Canonical: `<worktree>/.agtx/skills/<skill-name>/SKILL.md` — what
             agtx-task-worker / agtx-task-verify expect when they look for
             helper skills via `<task-cwd>/.agtx/skills/`.
          2. Agent-native: `<worktree>/<agent-native-path>/<skill-name>.md` —
             so the agent's slash-command auto-discovery picks them up
             (e.g. claude looks at `.claude/commands/agtx/<name>.md` for
             commands like `/agtx-task-worker`).

        Per-task agent (`task.agent`) determines which native path is used.
        Unknown agents skip the native deployment but still get the canonical.
        No-op when no plugin is configured or the plugin exposes no skills.

        Skill source is resolved via `resolve_skills_dir`: a distributed
        plugin bundles `<plugin>/skills/`, while harbor's own in-repo plugin
        reads the canonical `<repo>/.claude/skills/`.
        """
        plugin = self.config.plugin
        if plugin is None:
            return
        skills_src = resolve_skills_dir(plugin)
        if skills_src is None:
            return

        # Canonical destination
        canonical_dir = worktree_path / ".agtx" / "skills"
        canonical_dir.mkdir(parents=True, exist_ok=True)

        # Agent-native destination (optional)
        native_dir: Path | None = None
        agent = (task.agent or "").strip().lower()
        native_mapping = AGENT_NATIVE_PATHS.get(agent)
        if native_mapping is not None:
            base, namespace = native_mapping
            native_dir = worktree_path / base
            if namespace:
                native_dir = native_dir / namespace
            native_dir.mkdir(parents=True, exist_ok=True)

        deployed = 0
        for skill_dir in sorted(skills_src.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            # 1. canonical: keep the directory layout so supporting files
            #    (scripts/, examples) are discoverable too. Copy the WHOLE
            #    skill dir, not just SKILL.md.
            canonical_dst = canonical_dir / skill_dir.name
            try:
                if canonical_dst.exists():
                    shutil.rmtree(canonical_dst)
                shutil.copytree(skill_dir, canonical_dst)
            except Exception:  # noqa: BLE001 — non-fatal
                log.warning("skill canonical copy failed: %s", skill_dir.name, exc_info=True)
                continue
            # 2. agent-native: just SKILL.md, renamed to <name>.md (matches
            #    claude/codex/gemini conventions).
            if native_dir is not None:
                native_dst = native_dir / f"{skill_dir.name}.md"
                try:
                    shutil.copy2(skill_md, native_dst)
                except Exception:  # noqa: BLE001
                    log.warning("skill native copy failed: %s -> %s",
                                skill_dir.name, native_dst, exc_info=True)
            deployed += 1
        if deployed:
            log.info(
                "deployed %d skill(s) to worktree %s (agent=%s, native=%s)",
                deployed, worktree_path, agent or "?",
                native_dir.relative_to(worktree_path) if native_dir else "n/a",
            )

    def _copy_files_into_worktree(self, worktree_path: Path) -> None:
        if not self.config.copy_files:
            return
        for rel in self.config.copy_files:
            src = self.config.project_path / rel
            dst = worktree_path / rel
            if not src.exists():
                log.warning("copy_files: %s does not exist; skipping", src)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            except Exception as exc:  # noqa: BLE001 — surface to user via err
                raise RuntimeError(f"copy_files: {src} → {dst} failed: {exc}") from exc

    def _run_init_script(self, worktree_path: Path) -> None:
        if not self.config.init_script:
            return
        for cmd_str in self.config.init_script:
            argv = shlex.split(cmd_str)
            if not argv:
                continue
            cp = subprocess.run(
                argv, cwd=str(worktree_path),
                check=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if cp.returncode != 0:
                raise RuntimeError(
                    f"init_script failed: argv={argv!r} returncode={cp.returncode} "
                    f"stderr={cp.stderr.strip()[:500]!r}"
                )

    def _write_shared_instructions(self, worktree_path: Path, task: Task) -> None:
        path = worktree_path / SHARED_INSTRUCTIONS_REL
        instructions = task_worker_instructions(task)
        if not instructions:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Shared Worker Instructions\n\n"
            f"{instructions}\n",
            encoding="utf-8",
            newline="\n",
        )

    def _launch_agent(
        self, session: str, *, task: Task | None = None, phase: str | None = None,
    ) -> None:
        argv = self._resolve_agent_argv(task=task, phase=phase)
        cmd = " ".join(shlex.quote(p) for p in argv)
        try:
            self.tmux.send_keys(session, "", cmd)
        except TmuxError as exc:
            raise RuntimeError(f"tmux send-keys (agent launch) failed: {exc}") from exc

    def _resolve_agent_argv(
        self, *, task: Task | None, phase: str | None,
    ) -> Sequence[str]:
        """Pick the agent invocation. Precedence (most specific first):

          1. user `--map-agent <task.agent>=...` — explicit per-agent
          2. user `--agent-command-<phase>` — explicit per-phase
          3. user `--agent-command` — explicit global
          4. built-in `DEFAULT_AGENT_COMMAND_BY_AGENT[task.agent]` — convenience
          5. `DEFAULT_AGENT_COMMAND` — final fallback

        Explicit user flags always beat built-in conveniences. That way
        `--agent-command codex` does what you'd expect even when tasks have
        `agent='claude'` written by agtx.
        """
        # 1. Explicit per-agent override
        if task is not None and task.agent:
            user_map = self.config.agent_command_by_agent.get(task.agent)
            if user_map:
                return user_map
        # 2. Explicit per-phase override
        by_phase = self.config.agent_command_by_phase.get(phase or "")
        if by_phase:
            return by_phase
        # 3. Explicit global override
        if self.config.agent_command is not None:
            return self.config.agent_command
        # 4. Built-in map (only when the user gave no explicit command)
        if task is not None and task.agent:
            builtin = DEFAULT_AGENT_COMMAND_BY_AGENT.get(task.agent)
            if builtin:
                return builtin
        # 5. Final fallback
        return DEFAULT_AGENT_COMMAND

    def _maybe_enable_codex_goals(
        self, argv: Sequence[str], task: Task | None,
    ) -> tuple[str, ...]:
        """Enable Codex's experimental /goal command for opted-in tasks only."""
        out = tuple(argv)
        if task is None or not task_codex_goal_enabled(task):
            return out
        if not _is_codex_argv(out) or _codex_goals_already_configured(out):
            return out
        return (out[0], "--enable", "goals", *out[1:])

    def _set_pane_env(self, session: str, name: str, value: str) -> None:
        """Type an export/set into the pane so the agent's child process inherits it."""
        # Works in bash, zsh, and Git Bash (the windows webview's expected default).
        # On native PowerShell the user can override agent_command to wrap with
        # `$env:AGTX_TASK_ID=...; <cmd>` — out of scope for v1.
        line = f'export {name}={shlex.quote(value)}'
        try:
            self.tmux.send_keys(session, "", line)
        except TmuxError as exc:
            log.warning("failed to set %s in pane: %s", name, exc)

    # ---- prompt injection -------------------------------------------------

    def _wait_for_agent_ready(self, session: str) -> bool:
        """Poll capture-pane until a ready marker appears, output stabilizes,
        or the timeout elapses.

        Returns True ONLY when a real agent ready marker was seen. A pane that
        merely stabilized — or that timed out — returns False, because a dead
        shell prompt is just as "stable" as a live agent; the spawn path uses
        the False result to check whether the agent actually came up.

        Simplified version of agtx's `wait_for_agent_ready`. We don't probe
        `pane_current_command` (not portable across tmux variants on Windows);
        instead we use content-stabilization plus marker-string matching.
        Also auto-dismisses known confirmation dialogs (claude bypass, gemini
        trust) by typing their configured response. Plugin-defined
        auto_dismiss entries (in plugin.toml's `[[auto_dismiss]]` table)
        EXTEND the built-in tuples; both fire if applicable.
        """
        timeout = self.config.agent_ready_timeout_s
        deadline = time.monotonic() + timeout
        last_content = ""
        stable_ticks = 0
        change_count = 0
        dismissed: set[str] = set()  # don't re-dismiss the same dialog
        STABLE_THRESHOLD = 3  # ticks of unchanged content (~3s) after change

        # Build the effective dismissal list: config.auto_dismiss (substring →
        # response tuples) PLUS plugin's AND-pattern auto_dismiss entries.
        plugin_dismissals: list[AutoDismiss] = []
        if self.config.plugin is not None:
            plugin_dismissals = list(self.config.plugin.auto_dismiss)

        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                content = self.tmux.capture_pane(session, "", lines=80)
            except Exception:  # noqa: BLE001
                continue

            handled_dismiss = False
            # Built-in single-substring auto-dismiss
            for substring, response in self.config.auto_dismiss:
                if substring in content and substring not in dismissed:
                    dismissed.add(substring)
                    try:
                        self.tmux.send_keys(session, "", response)
                    except TmuxError as exc:
                        log.warning("auto-dismiss send-keys failed: %s", exc)
                    handled_dismiss = True
            # Plugin AND-pattern auto-dismiss
            for entry in plugin_dismissals:
                if not entry.detect:
                    continue
                key = " && ".join(entry.detect)
                if key in dismissed:
                    continue
                if all(s in content for s in entry.detect):
                    dismissed.add(key)
                    # Response is newline-separated keystrokes per agtx
                    # convention (e.g. "2\nEnter" → type "2", press Enter).
                    for line in entry.response.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self.tmux.send_keys(session, "", line)
                        except TmuxError as exc:
                            log.warning("plugin auto-dismiss send-keys failed: %s", exc)
                    handled_dismiss = True
            if handled_dismiss:
                last_content = ""
                stable_ticks = 0
                change_count = 0
                continue

            if any(marker in content for marker in AGENT_READY_MARKERS):
                return True

            if content != last_content:
                change_count += 1
                stable_ticks = 0
                last_content = content
            elif change_count >= 2:
                stable_ticks += 1
                if stable_ticks >= STABLE_THRESHOLD:
                    return False
        # No ready marker within the timeout. The False return lets the spawn
        # path detect this and check whether the pane fell back to a shell.
        log.info("agent in %s did not signal readiness within %.1fs",
                 session, timeout)
        return False

    def _pane_looks_like_shell(self, session: str) -> bool:
        """True iff the pane capture ends at an interactive shell prompt.

        Only consulted after `_wait_for_agent_ready` returned False, so a bare
        `$`/`#`/`>` line is overwhelmingly a dead shell rather than an agent
        input box."""
        try:
            content = self.tmux.capture_pane(session, "", lines=40)
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(content, str):
            return False
        return _looks_like_shell_prompt(content)

    def _await_agent_or_relaunch(self, session: str, launcher: str) -> None:
        """Block until the spawned agent CLI is up; relaunch it once if not.

        `_wait_for_agent_ready` returns True only when a real agent ready
        marker appears. When it doesn't AND the pane has fallen back to a
        shell prompt, the agent process exited before harbor could drive it —
        the usual cause is codex performing a self-update on startup
        ("Update ran successfully! Please restart Codex.") and quitting. We
        relaunch the agent once (a second codex launch no longer needs to
        update) and wait again. If it STILL isn't an agent, raise: the
        transition is marked failed and — critically — no phase prompt is
        typed into the live shell."""
        if self._wait_for_agent_ready(session):
            return
        if not self._pane_looks_like_shell(session):
            # No marker, but not a shell either — possibly an agent we have no
            # ready-marker for. Proceed best-effort (legacy behavior).
            return
        log.warning(
            "agent pane %s fell back to a shell prompt; relaunching agent once",
            session,
        )
        self._emit("agent_relaunched", {"session": session})
        try:
            self.tmux.send_keys_literal(session, "", launcher, enter=True)
        except TmuxError as exc:
            raise RuntimeError(
                f"agent relaunch send-keys failed for session {session!r}: {exc}"
            ) from exc
        if self._wait_for_agent_ready(session):
            return
        if self._pane_looks_like_shell(session):
            raise RuntimeError(
                f"agent CLI did not start in tmux session {session!r}: the pane "
                f"is at a shell prompt after two launch attempts. The agent "
                f"likely exited on startup (e.g. codex self-update — 'Please "
                f"restart Codex'). The phase prompt was NOT injected and the "
                f"task stays in Backlog — retry the move once the agent "
                f"launches cleanly in that worktree."
            )

    def _inject_phase_prompt(self, task: Task, *, phase: str) -> None:
        """Push a phase-specific prompt into the task's existing tmux session."""
        if not task.session_name:
            log.warning("can't inject %s prompt for task %s: no session_name", phase, task.id)
            return
        if not self.tmux.has_session(task.session_name):
            log.warning("can't inject %s prompt for task %s: session %s missing",
                        phase, task.id, task.session_name)
            return
        self._inject_phase_prompt_for_session(
            session=task.session_name, phase=phase, wait_for_ready=False, task=task,
        )

    def _inject_phase_prompt_for_session(
        self, *, session: str, phase: str, wait_for_ready: bool,
        task: Task | None = None,
    ) -> None:
        if not self.config.inject_prompts:
            return

        # Pick the effective phase (resolves "planning" → "planning_with_research"
        # etc. when prior artifacts exist).
        effective_phase = phase
        if self.config.plugin is not None and task is not None and task.worktree_path:
            try:
                effective_phase = determine_phase_variant(
                    self.config.plugin, phase,
                    worktree_path=Path(task.worktree_path),
                )
            except Exception:  # noqa: BLE001 — never let phase variant detection break injection
                log.warning("phase variant detection failed for %s", phase, exc_info=True)

        # Resolve skill_command + prompt: plugin first, then hardcoded fallback.
        skill_command = None
        prompt = ""
        if self.config.plugin is not None and task is not None:
            skill_command = plugin_resolve_skill_command(
                self.config.plugin, effective_phase,
                task_content=task.content_text(),
                task_id=task.id,
                cycle=task.cycle or 1,
            )
            prompt = plugin_resolve_prompt(
                self.config.plugin, effective_phase,
                task_content=task.content_text(),
                task_id=task.id,
                cycle=task.cycle or 1,
            )
        if not prompt:
            # Fall back to harbor's hardcoded DEFAULT_PHASE_PROMPTS (keyed on
            # the unresolved phase name so the dict lookup matches).
            prompt = self.config.phase_prompts.get(phase, "")

        prompt = prompt.strip()
        prompt_append = task_worker_instructions(task) if task is not None else ""
        if prompt_append:
            shared = (
                "Task-specific worker instructions "
                f"(also saved at {SHARED_INSTRUCTIONS_REL.as_posix()}):\n"
                f"{prompt_append}"
            )
            prompt = f"{prompt}\n\n{shared}" if prompt else shared
        if not skill_command and not prompt:
            return

        if wait_for_ready:
            self._wait_for_agent_ready(session)

        agent_kind = (task.agent or "").strip().lower() if task is not None else ""
        try:
            # codex/gemini/cursor parse a skill mention as an INLINE reference
            # that has to live inside a message — sending the skill command on
            # its own does nothing. Combine skill + prompt into one message for
            # those agents (mirrors agtx send_skill_and_prompt, app.rs:8040-8048).
            if agent_kind in ("codex", "gemini", "cursor"):
                combined = "\n\n".join(p for p in (skill_command, prompt) if p)
                if combined:
                    self._send_prompt_message(session, combined, agent=agent_kind)
            else:
                # Other agents (claude): skill command first — one keystroke
                # that triggers the skill loader — then the prompt as context.
                if skill_command:
                    self._send_prompt_message(session, skill_command, agent=agent_kind)
                if prompt:
                    self._send_prompt_message(session, prompt, agent=agent_kind)
        except TmuxError as exc:
            log.warning("failed to inject %s prompt into %s: %s", phase, session, exc)

    def _send_prompt_message(self, session: str, text: str, *, agent: str) -> None:
        """Type a message into the agent pane and submit it.

        The submit Enter is sent as a SEPARATE keystroke — after the typed
        text has rendered and a short pause — not bundled into the same
        send-keys burst as the body. Agent TUIs (codex, gemini: Ink input
        boxes with paste-burst detection) treat an Enter that arrives inside
        the keystroke burst as a literal newline, so the message just sits
        unsent in the draft box. Codex additionally pops a slash-command
        picker whose first Enter only dismisses the popup, so codex needs a
        second Enter to actually submit. Mirrors agtx's send_skill_and_prompt
        (D:/Projects/agtx/src/tui/app.rs:8045-8101)."""
        text = text.strip()
        if not text:
            return
        try:
            baseline = self.tmux.capture_pane(session, "", lines=80)
        except Exception:  # noqa: BLE001
            baseline = ""
        # 1. Type the body — but DON'T let send_keys_literal append the submit
        #    Enter; we send it separately below once the burst has settled.
        self.tmux.send_keys_literal(session, "", text, enter=False)
        # 2. Wait for the pane to render the typed text, then pause so the
        #    keystroke burst is definitely over before the submit Enter.
        self._wait_for_pane_change(session, baseline)
        time.sleep(self.config.prompt_submit_delay_s)
        # 3. Submit with a standalone Enter keystroke.
        self.tmux.send_keys(session, "", "Enter", enter=False)
        # 4. Codex: the first Enter only dismissed the slash-command picker —
        #    wait for it to clear, then send a second Enter to submit.
        if agent == "codex":
            self._wait_for_codex_picker_clear(session)
            time.sleep(self.config.prompt_submit_delay_s)
            self.tmux.send_keys(session, "", "Enter", enter=False)

    def _wait_for_pane_change(self, session: str, baseline: str) -> None:
        """Poll capture-pane until content differs from `baseline`.

        Confirms the agent received and rendered the typed text. Bounded by
        `prompt_render_timeout_s` (0 disables the poll entirely); on timeout
        we proceed anyway — the caller's pause still separates the Enter from
        the type burst."""
        timeout = self.config.prompt_render_timeout_s
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                content = self.tmux.capture_pane(session, "", lines=80)
            except Exception:  # noqa: BLE001
                continue
            if content != baseline:
                return

    def _wait_for_codex_picker_clear(self, session: str) -> None:
        """Poll until codex's slash-command picker popup is gone.

        Codex shows "Press enter to insert" while the picker is open; the
        Enter that closes it does NOT submit the message. Mirrors agtx
        (app.rs:8087-8098)."""
        timeout = self.config.prompt_render_timeout_s
        if timeout <= 0:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                content = self.tmux.capture_pane(session, "", lines=80)
            except Exception:  # noqa: BLE001
                continue
            if "Press enter to insert" not in content:
                return

    def _open_pr_for_task(self, task: Task) -> None:
        """Push the task branch and open a PR. Failures don't abort the
        Running→Review transition — they're recorded on the task's
        escalation_note so the user can retry from the UI.

        Idempotent: if `task.pr_url` is already set, this is a no-op. That
        path is what makes Review→Running→Review safe — the second entry
        into Review just lets the agent push more commits to the same
        branch and GitHub updates the existing PR automatically.
        """
        if not self.config.pr_on_review:
            return
        if task.pr_url:
            log.info("PR already exists for task %s: %s", task.id, task.pr_url)
            return
        if not (task.branch_name and task.worktree_path):
            err = "pr_on_review: task has no branch_name or worktree_path"
            log.warning("%s (task=%s)", err, task.id)
            self.db.update_task(task.id, escalation_note=err)
            self._emit("pr_failed", {"task_id": task.id, "error": err})
            return

        wt = Path(task.worktree_path)
        body = (task.description or "").strip()
        if not body:
            body = f"Opened by harbor for task {task.id[:8]}."
        try:
            self.git.push_branch(wt, task.branch_name)
            url = self.git.open_pull_request(
                wt, base=self.config.base_branch,
                title=task.title, body=body,
            )
        except Exception as exc:  # noqa: BLE001 — record but don't fail the move
            log.warning("pr_on_review failed for task %s: %s", task.id, exc)
            self.db.update_task(task.id, escalation_note=f"pr_on_review: {exc}")
            self._emit("pr_failed", {"task_id": task.id, "error": str(exc)})
            return

        pr_number: int | None = None
        m = re.search(r"/pull/(\d+)", url)
        if m:
            try:
                pr_number = int(m.group(1))
            except ValueError:
                pr_number = None
        self.db.update_task(task.id, pr_url=url, pr_number=pr_number)
        self._emit("pr_opened", {
            "task_id": task.id, "pr_url": url, "pr_number": pr_number,
        })

    def _teardown_session(self, task: Task) -> None:
        """Tear the per-task session and worktree down on Review→Done.

        Done is terminal: the tmux session is killed and the git worktree is
        removed. Worktree removal failures (locked, dirty, etc.) are logged
        and emitted but do NOT block the transition — the row still flips to
        Done. The local branch is left in place; the user can remove it via
        the webui's Cleanup button once the PR has merged.
        """
        if task.session_name and self.tmux.has_session(task.session_name):
            self.tmux.kill_session(task.session_name)
        if not task.worktree_path:
            return
        wt = Path(task.worktree_path)
        try:
            self.git.remove_worktree(self.config.project_path, wt)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            log.warning("worktree cleanup failed for %s: %s", wt, exc)
            self._emit("worktree_cleanup_failed", {
                "task_id": task.id,
                "worktree_path": str(wt),
                "error": str(exc),
            })
            return
        self.db.update_task(task.id, worktree_path=None)
        self._emit("worktree_removed", {
            "task_id": task.id,
            "worktree_path": str(wt),
        })

    def _default_worktree_path(self, task: Task, *, branch: str | None = None) -> Path:
        # Strip any extended-length `\\?\` prefix — a stored worktree_path may
        # carry it from a project registered before that prefix was stripped,
        # and git/tmux reject it.
        if task.worktree_path:
            return strip_extended_length_prefix(task.worktree_path)
        b = branch or task.branch_name or f"task/{task.id[:8]}"
        # Replace path separators inside the branch name for the directory leaf.
        leaf = b.replace("/", "-").replace("\\", "-")
        root = strip_extended_length_prefix(self.config.project_path)
        return root / self.config.worktree_dir / leaf

    def _emit(self, event: str, payload: dict) -> None:
        cb = self.on_event
        if cb is not None:
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001
                log.exception("on_event callback raised for %s", event)


def _looks_like_shell_prompt(content: str) -> bool:
    r"""Heuristic: does this pane capture end at an interactive shell prompt?

    True for Git Bash / POSIX sh / bash (last line a bare `$` or `#`) and for
    PowerShell / cmd.exe (last line `PS C:\...>` / `C:\...>`). Only consulted
    when no agent ready-marker was seen — see `_await_agent_or_relaunch`."""
    lines = [ln.rstrip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].strip()
    if last in {"$", "#"}:
        return True
    return bool(_CMD_PROMPT_RE.match(last))


def task_worker_instructions(task: Task) -> str:
    """Return the task's optional `## Worker Instructions` section."""
    instructions = extract_markdown_section(task.content_text(), WORKER_INSTRUCTIONS_HEADER)
    return "" if instructions.strip().lower() == "none" else instructions


def task_codex_goal_enabled(task: Task) -> bool:
    """Return whether this task opts into Codex's experimental /goal command."""
    raw = extract_markdown_section(task.content_text(), CODEX_GOAL_HEADER).strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled", "enable"}


def _is_codex_argv(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    exe = Path(str(argv[0]).replace("\\", "/")).name.lower()
    return exe in {"codex", "codex.exe", "codex.cmd", "codex.ps1"}


def _codex_goals_already_configured(argv: Sequence[str]) -> bool:
    for idx, token in enumerate(argv):
        if token == "--enable" and idx + 1 < len(argv) and argv[idx + 1] == "goals":
            return True
        if token == "--disable" and idx + 1 < len(argv) and argv[idx + 1] == "goals":
            return True
        if token in {"--enable=goals", "--disable=goals"}:
            return True
        if token in {"-c", "--config"} and idx + 1 < len(argv):
            if str(argv[idx + 1]).replace(" ", "") in {
                "features.goals=true",
                "features.goals=false",
            }:
                return True
        if str(token).replace(" ", "") in {
            "-cfeatures.goals=true",
            "-cfeatures.goals=false",
            "--configfeatures.goals=true",
            "--configfeatures.goals=false",
        }:
            return True
    return False


def extract_markdown_section(text: str, header: str) -> str:
    lines = text.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == header.lower():
            start = idx + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        if _SECTION_HEADER_RE.match(lines[idx]):
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def replace_markdown_section(text: str, header: str, body: str) -> str:
    """Replace or remove a level-2 markdown section.

    New worker instructions are inserted before the acceptance/probe/runtime
    contract sections so the task's verification contract stays grouped.
    """
    body = body.strip()
    lines = text.splitlines()

    start: int | None = None
    end: int | None = None
    for idx, line in enumerate(lines):
        if line.strip().lower() == header.lower():
            start = idx
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                if _SECTION_HEADER_RE.match(lines[j]):
                    end = j
                    break
            break

    new_section = [header, *body.splitlines()] if body else []
    if start is not None and end is not None:
        replacement = new_section
        if replacement and start > 0 and lines[start - 1].strip():
            replacement = ["", *replacement]
        if replacement and end < len(lines) and lines[end].strip():
            replacement = [*replacement, ""]
        return "\n".join([*lines[:start], *replacement, *lines[end:]]).strip() + "\n"

    if not new_section:
        return text

    insert_at = len(lines)
    for idx, line in enumerate(lines):
        if line.strip() in {
            "## Acceptance Criteria",
            "## Verification Probes",
            "## Runtime Target",
        }:
            insert_at = idx
            break

    insertion = ["", *new_section, ""]
    return "\n".join([*lines[:insert_at], *insertion, *lines[insert_at:]]).strip() + "\n"
