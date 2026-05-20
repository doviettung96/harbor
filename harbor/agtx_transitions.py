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

from .agtx_client import AgtxDb, Task, TransitionRequest
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
        "plan and ## Acceptance Criteria. When implementation is complete, run "
        "the agtx-task-verify skill (which executes ## Verification Probes via "
        "target-runtime-exec). Stop after verify and wait for me to move the "
        "task to Review."
    ),
    "review": (
        "Now in the Review phase. Run the agtx-task-verify skill once more and "
        "summarize: what changed, what probes ran, what passed, any follow-ups. "
        "Do not auto-merge — wait for me to move to Done."
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
    # Substring → response. When the substring appears in the readiness-loop
    # pane capture, the response is typed and Enter is pressed. Pass an empty
    # tuple to disable; otherwise extends the defaults.
    auto_dismiss: tuple[tuple[str, str], ...] = DEFAULT_AUTO_DISMISS
    # On Review→Done, remove the worktree (and the per-task tmux session is
    # killed regardless). Default off so a user can inspect the branch after
    # marking the task Done.
    cleanup_worktree_on_done: bool = False
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
        argv = [
            "git", "worktree", "add",
            "-b", branch,
            str(worktree_path),
            base_branch,
        ]
        self._run_git(argv, cwd=repo_root)

    def remove_worktree(self, repo_root: Path, worktree_path: Path) -> None:
        if not worktree_path.exists():
            return
        argv = ["git", "worktree", "remove", "--force", str(worktree_path)]
        self._run_git(argv, cwd=repo_root)

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


# Status transitions that don't require any agent/worktree work.
# Used for actions that just flip a column in the DB.
_STATUS_AFTER_NOOP_TRANSITION = {
    "move_to_review": "review",
    "move_to_done": "done",
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

        if action in _STATUS_AFTER_NOOP_TRANSITION:
            target = _STATUS_AFTER_NOOP_TRANSITION[action]
            if action == "move_to_review" and task.status != "running":
                raise RuntimeError(
                    f"move_to_review requires status=running (got {task.status!r})"
                )
            if action == "move_to_done" and task.status != "review":
                raise RuntimeError(
                    f"move_to_done requires status=review (got {task.status!r})"
                )
            if action == "move_to_done":
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
            self.db.update_task(task.id, status="review")
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
            # Push the review prompt before flipping; agent should run
            # agtx-task-verify and summarize before user marks Done.
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

        # 6. Persist task state BEFORE the (potentially slow) prompt injection
        #    so the UI sees the new session/worktree/branch even if step 7 is
        #    still running.
        self.db.update_task(
            task.id,
            status=target_status,
            session_name=session,
            worktree_path=str(worktree_path),
            branch_name=branch,
        )

        # 7. Wait for the agent to settle, then inject the planning-phase prompt.
        self._inject_phase_prompt_for_session(
            session=session, phase=target_status, wait_for_ready=True, task=task,
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

    def _wait_for_agent_ready(self, session: str) -> None:
        """Poll capture-pane until output stabilizes, marker appears, or timeout.

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
                return

            if content != last_content:
                change_count += 1
                stable_ticks = 0
                last_content = content
            elif change_count >= 2:
                stable_ticks += 1
                if stable_ticks >= STABLE_THRESHOLD:
                    return
        # Timed out — fall through; the prompt may still be queued by the agent.
        log.info("agent in %s did not signal readiness within %.1fs; injecting anyway",
                 session, timeout)

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

        try:
            # Type the slash command first (one keystroke that triggers the
            # agent's skill loader), then the prompt (additional context).
            if skill_command:
                self.tmux.send_keys_literal(session, "", skill_command, enter=True)
            if prompt:
                self.tmux.send_keys_literal(session, "", prompt, enter=True)
        except TmuxError as exc:
            log.warning("failed to inject %s prompt into %s: %s", phase, session, exc)

    def _teardown_session(self, task: Task) -> None:
        if task.session_name and self.tmux.has_session(task.session_name):
            self.tmux.kill_session(task.session_name)
        if self.config.cleanup_worktree_on_done and task.worktree_path:
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
            else:
                self._emit("worktree_removed", {
                    "task_id": task.id,
                    "worktree_path": str(wt),
                })
        else:
            self._emit("worktree_kept", {
                "task_id": task.id,
                "worktree_path": task.worktree_path,
            })

    def _default_worktree_path(self, task: Task, *, branch: str | None = None) -> Path:
        if task.worktree_path:
            return Path(task.worktree_path)
        b = branch or task.branch_name or f"task/{task.id[:8]}"
        # Replace path separators inside the branch name for the directory leaf.
        leaf = b.replace("/", "-").replace("\\", "-")
        return self.config.project_path / self.config.worktree_dir / leaf

    def _emit(self, event: str, payload: dict) -> None:
        cb = self.on_event
        if cb is not None:
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001
                log.exception("on_event callback raised for %s", event)


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
