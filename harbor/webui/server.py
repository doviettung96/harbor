"""FastAPI server for Harbor's Windows-friendly agtx web UI.

The web UI is a single global process. It reads agtx's global project index,
shows every registered project in a left project tree, and processes pending
transition_requests for every initialized project DB. The live Harbor runtime
configuration is shared across the process and persisted at Harbor's global
user config path. Per-project harbor.yml files are presets only: users load or
save them explicitly.
"""
from __future__ import annotations

import html as _stdlib_html
import asyncio
import json
import logging
import os
import shlex
import subprocess
import threading
import time
from queue import Empty, Queue
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..agent import (
    Config,
    global_runtime_config_path,
    load_config,
    load_runtime_config,
    write_config,
    write_runtime_config,
)
from ..bootstrap import BootstrapPlan, apply_bootstrap, build_plan
from ..agtx_client import (
    AgtxDb,
    Project,
    Task,
    VALID_STATUSES,
    global_db_path,
    project_db_path,
    strip_extended_length_prefix,
)
from ..agtx_transitions import (
    CODEX_GOAL_HEADER,
    DEFAULT_AGENT_COMMAND,
    DEFAULT_BASE_BRANCH,
    DEFAULT_WORKTREE_DIR,
    GitOps,
    TransitionConfig,
    TransitionWorker,
    WORKER_INSTRUCTIONS_HEADER,
    replace_markdown_section,
    task_codex_goal_enabled,
    task_worker_instructions,
)
from ..orchestrator import write_tmux_config
from ..plugin_loader import WorkflowPlugin, load_plugin
from ..terminal import PtyBackend, default_backend
from ..tmux import Tmux

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

COLUMNS = ("backlog", "planning", "running", "review", "done")
COLUMN_TITLES = {
    "backlog": "Backlog",
    "planning": "Planning",
    "running": "Running",
    "review": "Review",
    "done": "Done",
}

ALLOWED_MOVE_ACTIONS = frozenset({
    "move_forward",
    "move_backward",
    "move_to_backlog",
    "move_to_planning",
    "move_to_running",
    "move_to_review",
    "move_to_done",
    "resume",
    "escalate_to_user",
})


@dataclass
class RuntimeSettings:
    cfg: Config
    path: Path
    source: str


@dataclass
class ProjectContext:
    project: Project
    path: Path
    db_path: Path
    db: AgtxDb
    db_initialized: bool
    config_path: Path
    config_status: str
    config_error: str = ""
    bootstrap_status: str = "unknown"
    bootstrap_pending_count: int = 0
    bootstrap_plan: BootstrapPlan | None = None
    bootstrap_error: str = ""


@dataclass(frozen=True)
class WebuiOptions:
    agent_command: tuple[str, ...] | None
    agent_command_by_phase: dict[str, tuple[str, ...]]
    agent_command_by_agent: dict[str, tuple[str, ...]]
    base_branch: str
    worktree_dir: str
    init_script: tuple[str, ...]
    copy_files: tuple[str, ...]
    inject_prompts: bool
    cleanup_worktree_on_done: bool
    pr_on_done: bool
    plugin: str | None


@dataclass
class GlobalWebuiState:
    runtime: RuntimeSettings
    tmux: Tmux
    terminal_backend: PtyBackend
    options: WebuiOptions
    selected_project_id: str | None = None
    project_provider: Callable[[], list[Project]] | None = None
    project_dbs: dict[str, AgtxDb] | None = None
    supervisor: "GlobalTransitionSupervisor | None" = None

    def refresh_projects(self) -> list[ProjectContext]:
        projects = self._registered_projects()
        contexts = [self._context_for(project) for project in projects]
        if self.selected_project_id not in {c.project.id for c in contexts}:
            self.selected_project_id = contexts[0].project.id if contexts else None
        return contexts

    def get_project(self, project_id: str) -> ProjectContext:
        for ctx in self.refresh_projects():
            if ctx.project.id == project_id:
                self.selected_project_id = project_id
                return ctx
        raise HTTPException(404, f"project {project_id!r} not found")

    def current_project(self) -> ProjectContext:
        contexts = self.refresh_projects()
        if not contexts:
            raise HTTPException(404, "no agtx projects registered")
        project_id = self.selected_project_id or contexts[0].project.id
        for ctx in contexts:
            if ctx.project.id == project_id:
                return ctx
        return contexts[0]

    def update_runtime(self, cfg: Config) -> None:
        path = write_runtime_config(cfg, self.runtime.path)
        self.runtime = RuntimeSettings(cfg=cfg, path=path, source="global")

    @property
    def transition_config(self) -> TransitionConfig:
        return _transition_config_for(self.current_project(), self.runtime.cfg, self.options)

    def _registered_projects(self) -> list[Project]:
        if self.project_provider is not None:
            return list(self.project_provider())
        db = AgtxDb(project_db_p=Path("__global_only__.db"), global_db_p=global_db_path())
        return db.list_projects()

    def _context_for(self, project: Project) -> ProjectContext:
        # Strip the canonical `\\?\` prefix: the DB hash needs it (passed via
        # `project.path` below), but git/tmux/the pane shell all reject it.
        project_path = strip_extended_length_prefix(project.path)
        db_path = project_db_path(project.path)
        db = self.project_dbs.get(project.id) if self.project_dbs else None
        if db is None:
            db = AgtxDb(project_db_p=db_path, global_db_p=global_db_path())
        try:
            db_initialized = db.is_initialized()
        except Exception:
            db_initialized = False
        config_path = project_path / "harbor.yml"
        config_status, config_error = _project_config_status(config_path)
        bootstrap_status, bootstrap_pending_count, bootstrap_plan, bootstrap_error = (
            _project_bootstrap_status(project_path)
        )
        return ProjectContext(
            project=project,
            path=project_path,
            db_path=db_path,
            db=db,
            db_initialized=db_initialized,
            config_path=config_path,
            config_status=config_status,
            config_error=config_error,
            bootstrap_status=bootstrap_status,
            bootstrap_pending_count=bootstrap_pending_count,
            bootstrap_plan=bootstrap_plan,
            bootstrap_error=bootstrap_error,
        )


class GlobalTransitionSupervisor:
    """One background loop that processes transitions across all projects."""

    def __init__(
        self,
        state: GlobalWebuiState,
        *,
        poll_interval: float = 2.0,
    ) -> None:
        self.state = state
        self.poll_interval = poll_interval
        self.instance_id = "harbor-global-webui"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="harbor-global-transition-supervisor", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def process_once(self) -> int:
        total = 0
        for ctx in self.state.refresh_projects():
            if not ctx.db_initialized:
                continue
            try:
                worker = TransitionWorker(
                    db=ctx.db,
                    config=_transition_config_for(ctx, self.state.runtime.cfg, self.state.options),
                    tmux=self.state.tmux,
                    poll_interval=0.0,
                )
                worker.instance_id = f"{self.instance_id}:{ctx.project.id}"
                total += worker.process_once()
            except Exception:
                log.exception("transition processing failed for project %s", ctx.project.id)
        return total

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:
                log.exception("global transition supervisor crashed")
            self._stop.wait(self.poll_interval)


def create_app(
    repo_root: str | Path | None = None,
    *,
    project_path: str | Path | None = None,
    agent_command: list[str] | tuple[str, ...] | None = None,
    base_branch: str = DEFAULT_BASE_BRANCH,
    worktree_dir: str = DEFAULT_WORKTREE_DIR,
    init_script: tuple[str, ...] = (),
    copy_files: tuple[str, ...] = (),
    inject_prompts: bool = True,
    agent_command_by_phase: dict[str, list[str]] | None = None,
    agent_command_by_agent: dict[str, list[str]] | None = None,
    cleanup_worktree_on_done: bool = False,
    pr_on_done: bool = True,
    plugin: str | None = None,
    autostart_worker: bool = True,
    db: AgtxDb | None = None,
    projects: Sequence[Project] | None = None,
    project_dbs: dict[str, AgtxDb] | None = None,
    runtime_config_path: str | Path | None = None,
    terminal_backend: PtyBackend | None = None,
) -> FastAPI:
    """Build the global FastAPI app.

    `repo_root` and `db` are retained for compatibility with older tests and
    callers. In normal use, projects come from agtx's global index. Passing
    `db` without `projects` creates a single synthetic project context.
    """
    initial_path = Path(project_path or repo_root or Path.cwd()).resolve()
    if db is not None and projects is None:
        projects = [
            Project(
                id="default",
                name=initial_path.name or "project",
                path=str(initial_path),
                last_opened="",
            )
        ]
        project_dbs = {"default": db}

    runtime_path = Path(runtime_config_path) if runtime_config_path else global_runtime_config_path()
    runtime_cfg = load_runtime_config(runtime_path)
    runtime_source = "global" if runtime_path.exists() else "builtins"

    options = WebuiOptions(
        agent_command=tuple(agent_command) if agent_command else None,
        agent_command_by_phase={
            k: tuple(v) for k, v in (agent_command_by_phase or {}).items() if v
        },
        agent_command_by_agent={
            k: tuple(v) for k, v in (agent_command_by_agent or {}).items() if v
        },
        base_branch=base_branch,
        worktree_dir=worktree_dir,
        init_script=tuple(init_script),
        copy_files=tuple(copy_files),
        inject_prompts=inject_prompts,
        cleanup_worktree_on_done=cleanup_worktree_on_done,
        pr_on_done=pr_on_done,
        plugin=plugin,
    )

    workflow_dir = (Path(repo_root).resolve() if repo_root else Path.cwd().resolve()) / ".harbor"
    conf_path = write_tmux_config(workflow_dir, runtime_cfg.default_shell)
    tmux = Tmux(config_path=str(conf_path) if conf_path else None)

    provider = (lambda: list(projects)) if projects is not None else None
    state = GlobalWebuiState(
        runtime=RuntimeSettings(cfg=runtime_cfg, path=runtime_path, source=runtime_source),
        tmux=tmux,
        terminal_backend=terminal_backend or default_backend(),
        options=options,
        project_provider=provider,
        project_dbs=project_dbs,
    )
    state.selected_project_id = _select_initial_project(state.refresh_projects(), initial_path)
    state.supervisor = GlobalTransitionSupervisor(state)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI(title="harbor (agtx webview)", docs_url=None, redoc_url=None)
    app.state.harbor = state

    if autostart_worker:
        @app.on_event("startup")
        async def _startup() -> None:
            assert state.supervisor is not None
            state.supervisor.start()

        @app.on_event("shutdown")
        async def _shutdown() -> None:
            assert state.supervisor is not None
            state.supervisor.stop()

    # ----- template helpers ----------------------------------------------

    def _template_context(
        request: Request,
        *,
        selected: ProjectContext | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        projects_ctx = state.refresh_projects()
        if selected is None and state.selected_project_id:
            selected = next(
                (c for c in projects_ctx if c.project.id == state.selected_project_id),
                None,
            )
        return {
            "request": request,
            "projects": projects_ctx,
            "selected_project": selected,
            "selected_project_id": selected.project.id if selected else state.selected_project_id,
            "runtime": state.runtime,
            **extra,
        }

    def _board_columns(ctx: ProjectContext) -> list[dict[str, Any]]:
        all_tasks = ctx.db.list_tasks()
        by_status: dict[str, list[Task]] = {s: [] for s in COLUMNS}
        for t in all_tasks:
            if t.status in by_status:
                by_status[t.status].append(t)
        return [
            {"key": s, "title": COLUMN_TITLES[s], "tasks": by_status[s]}
            for s in COLUMNS
        ]

    def _notifications(ctx: ProjectContext) -> list:
        try:
            return ctx.db.list_notifications(limit=10)
        except Exception:
            return []

    def _capture_pane(session: str | None) -> str:
        if not session:
            return ""
        try:
            if not state.tmux.has_session(session):
                return ""
            return state.tmux.capture_pane(session, "", lines=300)
        except Exception:
            return ""

    def _attach_command(session: str | None) -> str:
        return state.tmux.attach_command(session) if session else ""

    def _blockers_for(ctx: ProjectContext, task: Task) -> list[Any]:
        return task.blocking_dependencies

    def _is_session_live(session: str | None) -> bool:
        if not session:
            return False
        try:
            return state.tmux.has_session(session)
        except Exception:
            return False

    def _agent_options_for(ctx: ProjectContext, task: Task) -> list[str]:
        """Agent names offered in the per-task agent dropdown.

        The configured `agent_command_by_agent` keys (harbor.yml + any
        `--map-agent` CLI overrides) plus the task's current agent, so the
        existing value is always selectable even if it was never mapped.
        """
        tc = _transition_config_for(ctx, state.runtime.cfg, state.options)
        opts = set(tc.agent_command_by_agent)
        if task.agent:
            opts.add(task.agent)
        return sorted(opts)

    def _task_detail_context(
        request: Request,
        ctx: ProjectContext,
        task_id: str,
        *,
        drawer: bool = False,
    ) -> dict[str, Any]:
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        live_session = _is_session_live(task.session_name)
        return _template_context(
            request,
            selected=ctx,
            project=ctx,
            task=task,
            drawer=drawer,
            live_session=live_session,
            pane_capture="" if live_session else _capture_pane(task.session_name),
            attach_command=_attach_command(task.session_name),
            dependencies=task.dependencies,
            blockers=_blockers_for(ctx, task),
            recent_requests=ctx.db.recent_transition_requests(task_id, limit=10),
            valid_statuses=VALID_STATUSES,
            codex_goal_enabled=task_codex_goal_enabled(task),
            worker_instructions=task_worker_instructions(task),
            agent_options=_agent_options_for(ctx, task),
            # The agent CLI is chosen at spawn time; once a tmux session
            # exists the running agent is fixed, so editing is Backlog-only.
            agent_editable=not task.session_name,
        )

    def _planning_session_prefix(ctx: ProjectContext) -> str:
        project_slug = _safe_session_chunk(ctx.project.id or ctx.project.name) or "project"
        return f"plan-{project_slug[:32]}-"

    def _new_planning_session_name(ctx: ProjectContext) -> str:
        prefix = _planning_session_prefix(ctx)
        for _ in range(10):
            stamp = time.strftime("%Y%m%d%H%M%S")
            suffix = f"{time.time_ns() % 1_000_000_000:09d}"
            session_name = f"{prefix}{stamp}-{suffix}"
            if not _is_session_live(session_name):
                return session_name
            time.sleep(0.001)
        return f"{prefix}{time.time_ns()}"

    def _is_valid_planning_session(ctx: ProjectContext, session_name: str | None) -> bool:
        if not session_name:
            return False
        prefix = _planning_session_prefix(ctx)
        if not session_name.startswith(prefix):
            return False
        return session_name == _safe_session_name(session_name)

    def _planning_sessions(ctx: ProjectContext) -> list[dict[str, str]]:
        try:
            names = state.tmux.list_sessions()
        except Exception:
            return []
        if not isinstance(names, (list, tuple, set)):
            return []
        sessions: list[dict[str, str]] = []
        for raw_name in names:
            name = str(raw_name).strip()
            if not _is_valid_planning_session(ctx, name):
                continue
            sessions.append({
                "session_name": name,
                "attach_command": _attach_command(name),
            })
        sessions.sort(key=lambda s: s["session_name"], reverse=True)
        return sessions

    def _planning_agent_argv(ctx: ProjectContext) -> tuple[str, ...]:
        cfg = _transition_config_for(ctx, state.runtime.cfg, state.options)
        return tuple(cfg.agent_command or DEFAULT_AGENT_COMMAND)

    def _planning_detail_context(
        request: Request,
        ctx: ProjectContext,
        session_name: str,
        *,
        drawer: bool = False,
    ) -> dict[str, Any]:
        if not _is_valid_planning_session(ctx, session_name):
            raise HTTPException(400, "invalid planning session")
        live_session = _is_session_live(session_name)
        return _template_context(
            request,
            selected=ctx,
            project=ctx,
            drawer=drawer,
            session_name=session_name,
            live_session=live_session,
            pane_capture="" if live_session else _capture_pane(session_name),
            attach_command=_attach_command(session_name),
        )

    # ----- read pages -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        task: str | None = None,
        planning: str | None = None,
    ) -> HTMLResponse:
        contexts = state.refresh_projects()
        if state.selected_project_id:
            ctx = state.current_project()
            open_planning = (
                _planning_detail_context(request, ctx, planning, drawer=True)
                if planning and not task else None
            )
            if not ctx.db_initialized:
                return templates.TemplateResponse(
                    "board.html",
                    _template_context(
                        request,
                        selected=ctx,
                        columns=[],
                        notifications=[],
                        project=ctx,
                        db_uninitialized=True,
                        open_task=None,
                        open_planning=open_planning,
                        planning_sessions=_planning_sessions(ctx),
                    ),
                )
            open_task = (
                _task_detail_context(request, ctx, task, drawer=True)
                if task else None
            )
            return templates.TemplateResponse(
                "board.html",
                _template_context(
                    request,
                    selected=ctx,
                    columns=_board_columns(ctx),
                    notifications=_notifications(ctx),
                    project=ctx,
                    db_uninitialized=False,
                    open_task=open_task,
                    open_planning=open_planning,
                    planning_sessions=_planning_sessions(ctx),
                ),
            )
        return templates.TemplateResponse(
            "dashboard.html",
            _template_context(request, projects=contexts),
        )

    def _register_project_from_path(project_path: str, project_name: str = "") -> Project:
        raw_path = project_path.strip()
        if not raw_path:
            raise HTTPException(400, "project path is required")
        path = Path(raw_path)
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise HTTPException(400, f"project path does not exist: {raw_path}") from exc
        if not resolved.is_dir():
            raise HTTPException(400, f"project path is not a directory: {raw_path}")

        db = AgtxDb(project_db_p=Path("__global_only__.db"), global_db_p=global_db_path())
        project = db.register_project(
            resolved,
            name=project_name.strip() or None,
        )
        state.selected_project_id = project.id
        return project

    @app.post("/projects/init")
    async def action_project_init(
        project_path: str = Form(...),
        project_name: str = Form(""),
    ) -> RedirectResponse:
        project = _register_project_from_path(project_path, project_name)
        return RedirectResponse(f"/projects/{project.id}?tracked=1", status_code=303)

    @app.get("/projects/init/browse", response_class=HTMLResponse)
    async def project_folder_browser(
        request: Request,
        path: str | None = None,
    ) -> HTMLResponse:
        view = _folder_browser_view(path)
        return templates.TemplateResponse(
            "folder_picker.html",
            _template_context(request, **view),
        )

    @app.post("/projects/init/browse/register")
    async def action_project_init_from_browser(
        project_path: str = Form(...),
        project_name: str = Form(""),
    ) -> RedirectResponse:
        project = _register_project_from_path(project_path, project_name)
        return RedirectResponse(f"/projects/{project.id}?tracked=1", status_code=303)

    async def _pick_and_register_project() -> RedirectResponse:
        try:
            picked = await asyncio.to_thread(_pick_folder_with_native_dialog)
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        if not picked:
            return RedirectResponse("/", status_code=303)

        project = _register_project_from_path(picked)
        return RedirectResponse(f"/projects/{project.id}?tracked=1", status_code=303)

    @app.get("/projects/init/pick-folder")
    async def action_project_init_pick_folder_get() -> RedirectResponse:
        return await _pick_and_register_project()

    @app.post("/projects/init/pick-folder")
    async def action_project_init_pick_folder() -> RedirectResponse:
        return await _pick_and_register_project()

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    async def board(
        request: Request,
        project_id: str,
        task: str | None = None,
        planning: str | None = None,
        bootstrap: str | None = None,
        tracked: str | None = None,
    ) -> HTMLResponse:
        ctx = state.get_project(project_id)
        bootstrap_preview = bootstrap == "preview"
        post_track_prompt = bool(tracked) and ctx.bootstrap_status != "bootstrapped"
        open_planning = (
            _planning_detail_context(request, ctx, planning, drawer=True)
            if planning and not task else None
        )
        if not ctx.db_initialized:
            return templates.TemplateResponse(
                "board.html",
                _template_context(
                    request,
                    selected=ctx,
                    columns=[],
                    notifications=[],
                    project=ctx,
                    db_uninitialized=True,
                    open_task=None,
                    open_planning=open_planning,
                    planning_sessions=_planning_sessions(ctx),
                    bootstrap_preview=bootstrap_preview,
                    post_track_prompt=post_track_prompt,
                )
            )
        open_task = (
            _task_detail_context(request, ctx, task, drawer=True)
            if task else None
        )
        return templates.TemplateResponse(
            "board.html",
            _template_context(
                request,
                selected=ctx,
                columns=_board_columns(ctx),
                notifications=_notifications(ctx),
                project=ctx,
                db_uninitialized=False,
                open_task=open_task,
                open_planning=open_planning,
                planning_sessions=_planning_sessions(ctx),
                bootstrap_preview=bootstrap_preview,
                post_track_prompt=post_track_prompt,
            ),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> HTMLResponse:
        try:
            ctx: ProjectContext | None = state.current_project()
        except HTTPException:
            ctx = None
        cfg = state.runtime.cfg
        effective_agent = (
            _planning_agent_argv(ctx)
            if ctx is not None
            else tuple(cfg.agtx_agent_command or DEFAULT_AGENT_COMMAND)
        )
        return templates.TemplateResponse(
            "settings.html",
            _template_context(
                request,
                selected=ctx,
                cfg=cfg,
                agent_command_text=_shell_join(cfg.agtx_agent_command),
                effective_agent_command_text=_shell_join(effective_agent),
                cli_agent_override=state.options.agent_command is not None,
                default_shell_text=cfg.default_shell or "",
                plugin_text=cfg.agtx_plugin or "",
            ),
        )

    @app.get("/projects/{project_id}/_partials/board", response_class=HTMLResponse)
    async def board_partial(request: Request, project_id: str) -> HTMLResponse:
        ctx = state.get_project(project_id)
        if not ctx.db_initialized:
            raise HTTPException(409, "project DB is not initialized")
        return templates.TemplateResponse(
            "_board_partial.html",
            _template_context(
                request,
                selected=ctx,
                columns=_board_columns(ctx),
                notifications=_notifications(ctx),
                project=ctx,
                planning_sessions=_planning_sessions(ctx),
            ),
        )

    @app.get("/_partials/board", response_class=HTMLResponse)
    async def compat_board_partial(request: Request) -> HTMLResponse:
        ctx = state.current_project()
        return await board_partial(request, ctx.project.id)

    @app.get("/projects/{project_id}/_partials/task/{task_id}", response_class=HTMLResponse)
    async def task_partial(request: Request, project_id: str, task_id: str) -> HTMLResponse:
        ctx = state.get_project(project_id)
        return templates.TemplateResponse(
            "_task_detail.html",
            _task_detail_context(request, ctx, task_id, drawer=True),
        )

    @app.get("/_partials/task/{task_id}", response_class=HTMLResponse)
    async def compat_task_partial(request: Request, task_id: str) -> HTMLResponse:
        ctx = state.current_project()
        return await task_partial(request, ctx.project.id, task_id)

    @app.get("/projects/{project_id}/_partials/planning/{session_name}", response_class=HTMLResponse)
    async def planning_partial(
        request: Request,
        project_id: str,
        session_name: str,
    ) -> HTMLResponse:
        ctx = state.get_project(project_id)
        return templates.TemplateResponse(
            "_planning_session.html",
            _planning_detail_context(request, ctx, session_name, drawer=True),
        )

    @app.get("/_partials/planning/{session_name}", response_class=HTMLResponse)
    async def compat_planning_partial(request: Request, session_name: str) -> HTMLResponse:
        ctx = state.current_project()
        return await planning_partial(request, ctx.project.id, session_name)

    @app.get("/projects/{project_id}/_partials/pane/{task_id}", response_class=HTMLResponse)
    async def pane_partial(project_id: str, task_id: str) -> HTMLResponse:
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        body = _capture_pane(task.session_name)
        return HTMLResponse(f"<pre class='pane-capture'>{_stdlib_html.escape(body)}</pre>")

    @app.get("/_partials/pane/{task_id}", response_class=HTMLResponse)
    async def compat_pane_partial(task_id: str) -> HTMLResponse:
        ctx = state.current_project()
        return await pane_partial(ctx.project.id, task_id)

    @app.websocket("/projects/{project_id}/ws/tmux/{task_id}")
    async def tmux_ws(websocket: WebSocket, project_id: str, task_id: str) -> None:
        ctx = state.get_project(project_id)
        await _serve_tmux_ws(websocket, ctx, task_id)

    @app.websocket("/ws/tmux/{task_id}")
    async def compat_tmux_ws(websocket: WebSocket, task_id: str) -> None:
        ctx = state.current_project()
        await _serve_tmux_ws(websocket, ctx, task_id)

    @app.websocket("/projects/{project_id}/ws/planning/{session_name}")
    async def planning_ws(websocket: WebSocket, project_id: str, session_name: str) -> None:
        ctx = state.get_project(project_id)
        await _serve_planning_ws(websocket, ctx, session_name)

    @app.websocket("/ws/planning/{session_name}")
    async def compat_planning_ws(websocket: WebSocket, session_name: str) -> None:
        ctx = state.current_project()
        await _serve_planning_ws(websocket, ctx, session_name)

    async def _serve_tmux_ws(websocket: WebSocket, ctx: ProjectContext, task_id: str) -> None:
        task = ctx.db.get_task(task_id)
        if task is None:
            await websocket.close(code=1008, reason="task not found")
            return
        if not task.session_name:
            await websocket.close(code=1008, reason="task has no tmux session")
            return
        if not _is_session_live(task.session_name):
            await websocket.close(code=1008, reason="tmux session is not live")
            return
        await _serve_attached_tmux_session(
            websocket,
            ctx,
            task.session_name,
            thread_name=f"harbor-ws-tmux-{task_id}",
        )

    async def _serve_planning_ws(
        websocket: WebSocket,
        ctx: ProjectContext,
        session_name: str,
    ) -> None:
        if not _is_valid_planning_session(ctx, session_name):
            await websocket.close(code=1008, reason="invalid planning session")
            return
        if not _is_session_live(session_name):
            await websocket.close(code=1008, reason="planning session is not live")
            return
        await _serve_attached_tmux_session(
            websocket,
            ctx,
            session_name,
            thread_name=f"harbor-ws-planning-{session_name}",
        )

    async def _serve_attached_tmux_session(
        websocket: WebSocket,
        ctx: ProjectContext,
        session_name: str,
        *,
        thread_name: str,
    ) -> None:
        await websocket.accept()
        try:
            pty_session = state.terminal_backend.spawn(
                state.tmux.attach_argv(session_name),
                cwd=ctx.path,
                cols=100,
                rows=30,
            )
        except Exception as exc:
            await websocket.send_text(f"\r\n[harbor] terminal attach failed: {exc}\r\n")
            await websocket.close(code=1011)
            return

        output: Queue[str | None] = Queue()
        stop = threading.Event()

        def _reader() -> None:
            while not stop.is_set():
                try:
                    chunk = pty_session.read()
                except Exception as exc:
                    output.put(f"\r\n[harbor] terminal read failed: {exc}\r\n")
                    break
                if not chunk:
                    break
                output.put(chunk)
            output.put(None)

        thread = threading.Thread(target=_reader, name=thread_name, daemon=True)
        thread.start()

        async def _send_output() -> None:
            while True:
                try:
                    chunk = output.get_nowait()
                except Empty:
                    await asyncio.sleep(0.02)
                    continue
                if chunk is None:
                    await websocket.close()
                    return
                await websocket.send_text(chunk)

        async def _receive_input() -> None:
            while True:
                message = await websocket.receive_text()
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    pty_session.write(message)
                    continue
                if not isinstance(payload, dict):
                    continue
                msg_type = payload.get("type")
                if msg_type == "input":
                    pty_session.write(str(payload.get("data", "")))
                elif msg_type == "resize":
                    cols = _coerce_terminal_size(payload.get("cols"), default=100)
                    rows = _coerce_terminal_size(payload.get("rows"), default=30)
                    pty_session.resize(cols, rows)

        sender = asyncio.create_task(_send_output())
        receiver = asyncio.create_task(_receive_input())
        try:
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task_obj in done:
                exc = task_obj.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
            for task_obj in pending:
                task_obj.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            pty_session.close()

    # ----- planning session actions --------------------------------------

    @app.post("/projects/{project_id}/planning-sessions")
    async def action_start_planning_session(project_id: str) -> RedirectResponse:
        ctx = state.get_project(project_id)
        session_name = _new_planning_session_name(ctx)
        argv = _planning_agent_argv(ctx)
        cmd = " ".join(shlex.quote(p) for p in argv)
        try:
            state.tmux.ensure_session(
                session_name,
                str(ctx.path),
                default_shell=state.runtime.cfg.default_shell,
            )
            state.tmux.send_keys(session_name, "", cmd)
        except Exception as exc:
            raise HTTPException(500, f"planning session launch failed: {exc}") from exc
        return RedirectResponse(
            f"/projects/{project_id}?planning={session_name}",
            status_code=303,
        )

    @app.post("/projects/{project_id}/planning-sessions/{session_name}/kill")
    async def action_kill_planning_session(
        project_id: str,
        session_name: str,
    ) -> RedirectResponse:
        ctx = state.get_project(project_id)
        if not _is_valid_planning_session(ctx, session_name):
            raise HTTPException(400, "invalid planning session")
        state.tmux.kill_session(session_name)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    # ----- config actions -------------------------------------------------

    @app.post("/projects/{project_id}/config/load")
    async def action_config_load(project_id: str) -> RedirectResponse:
        ctx = state.get_project(project_id)
        if not ctx.config_path.exists():
            raise HTTPException(404, f"{ctx.config_path} does not exist")
        try:
            cfg = load_config(ctx.config_path)
        except Exception as exc:
            raise HTTPException(400, f"invalid project config: {exc}") from exc
        state.update_runtime(cfg)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/config/save")
    async def action_config_save(project_id: str) -> RedirectResponse:
        ctx = state.get_project(project_id)
        write_config(ctx.config_path, state.runtime.cfg, backup=True)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.post("/projects/{project_id}/bootstrap")
    async def action_project_bootstrap(project_id: str) -> RedirectResponse:
        ctx = state.get_project(project_id)
        try:
            apply_bootstrap(ctx.path)
        except Exception as exc:
            raise HTTPException(500, f"bootstrap failed: {exc}") from exc
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    @app.post("/settings/runtime")
    async def action_settings_runtime(
        agent_command: str | None = Form(None),
        default_shell: str | None = Form(None),
        plugin: str | None = Form(None),
        prompt_append: str | None = Form(None),
    ) -> RedirectResponse:
        cfg = state.runtime.cfg
        updates: dict[str, Any] = {}
        if agent_command is not None:
            try:
                updates["agtx_agent_command"] = shell_split(agent_command)
            except ValueError as exc:
                raise HTTPException(400, f"invalid agent command: {exc}") from exc
        if default_shell is not None:
            updates["default_shell"] = default_shell.strip() or None
        if plugin is not None:
            updates["agtx_plugin"] = plugin.strip() or None
        if prompt_append is not None:
            updates["agtx_prompt_append"] = prompt_append.rstrip()
        cfg = replace(cfg, **updates)
        state.update_runtime(cfg)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/actions/settings/agtx")
    async def compat_action_settings_agtx(prompt_append: str = Form("")) -> RedirectResponse:
        return await action_settings_runtime(
            agent_command=None,
            default_shell=None,
            plugin=None,
            prompt_append=prompt_append,
        )

    # ----- task actions ---------------------------------------------------

    def _queue_transition(
        ctx: ProjectContext,
        task_id: str,
        action: str,
        reason: str,
    ) -> None:
        if action not in ALLOWED_MOVE_ACTIONS:
            raise HTTPException(400, f"unknown action {action!r}")
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        if task.status == "backlog" and action in {
            "move_forward", "move_to_planning", "research",
        } and not task.deps_satisfied:
            blockers = ", ".join(
                f"{dep.short_id} {dep.title} [{dep.status}]"
                for dep in task.blocking_dependencies
            )
            raise HTTPException(409, f"task is blocked by dependencies: {blockers}")
        ctx.db.create_transition_request(
            task_id=task_id,
            action=action,
            reason=reason or None,
        )

    @app.post("/projects/{project_id}/actions/move/{task_id}")
    async def action_move(
        project_id: str,
        task_id: str,
        action: str = Form(...),
        reason: str = Form(""),
    ) -> RedirectResponse:
        ctx = state.get_project(project_id)
        _queue_transition(ctx, task_id, action, reason)
        return RedirectResponse(f"/projects/{project_id}?task={task_id}", status_code=303)

    @app.post("/actions/move/{task_id}")
    async def compat_action_move(
        task_id: str,
        action: str = Form(...),
        reason: str = Form(""),
    ) -> RedirectResponse:
        ctx = state.current_project()
        _queue_transition(ctx, task_id, action, reason)
        return RedirectResponse(f"/?task={task_id}", status_code=303)

    @app.post("/projects/{project_id}/actions/send-keys/{task_id}")
    async def action_send_keys(
        project_id: str,
        task_id: str,
        text: str = Form(...),
    ) -> RedirectResponse:
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        if not task.session_name:
            raise HTTPException(409, "task has no tmux session yet")
        try:
            state.tmux.send_keys(task.session_name, "", text)
        except Exception as exc:
            raise HTTPException(500, f"send-keys failed: {exc}") from exc
        return RedirectResponse(f"/projects/{project_id}?task={task_id}", status_code=303)

    @app.post("/actions/send-keys/{task_id}")
    async def compat_action_send_keys(
        task_id: str,
        text: str = Form(...),
    ) -> RedirectResponse:
        ctx = state.current_project()
        return await action_send_keys(ctx.project.id, task_id, text)

    @app.post("/projects/{project_id}/actions/kill/{task_id}")
    async def action_kill(project_id: str, task_id: str) -> RedirectResponse:
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        if task.session_name:
            state.tmux.kill_session(task.session_name)
        return RedirectResponse(f"/projects/{project_id}?task={task_id}", status_code=303)

    @app.post("/actions/kill/{task_id}")
    async def compat_action_kill(task_id: str) -> RedirectResponse:
        ctx = state.current_project()
        return await action_kill(ctx.project.id, task_id)

    @app.post("/projects/{project_id}/actions/task/{task_id}/worker-instructions")
    async def action_task_worker_instructions(
        project_id: str,
        task_id: str,
        worker_instructions: str = Form(""),
        codex_goal: str = Form(""),
    ) -> RedirectResponse:
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        current = task.description if task.description is not None else task.title
        updated = replace_markdown_section(
            current,
            WORKER_INSTRUCTIONS_HEADER,
            worker_instructions,
        )
        updated = replace_markdown_section(
            updated,
            CODEX_GOAL_HEADER,
            "enabled" if codex_goal else "",
        )
        ctx.db.update_task(task_id, description=updated)

        refreshed = ctx.db.get_task(task_id)
        if refreshed is not None and refreshed.worktree_path:
            shared = Path(refreshed.worktree_path) / ".agtx" / "shared-instructions.md"
            instructions = task_worker_instructions(refreshed)
            if instructions:
                shared.parent.mkdir(parents=True, exist_ok=True)
                shared.write_text(
                    "# Shared Worker Instructions\n\n"
                    f"{instructions}\n",
                    encoding="utf-8",
                    newline="\n",
                )
            elif shared.exists():
                shared.unlink()
        return RedirectResponse(f"/projects/{project_id}?task={task_id}", status_code=303)

    @app.post("/actions/task/{task_id}/worker-instructions")
    async def compat_action_task_worker_instructions(
        task_id: str,
        worker_instructions: str = Form(""),
        codex_goal: str = Form(""),
    ) -> RedirectResponse:
        ctx = state.current_project()
        return await action_task_worker_instructions(
            ctx.project.id,
            task_id,
            worker_instructions,
            codex_goal,
        )

    @app.post("/projects/{project_id}/actions/task/{task_id}/agent")
    async def action_task_agent(
        project_id: str,
        task_id: str,
        agent: str = Form(...),
    ) -> RedirectResponse:
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        # The agent CLI is launched when the task's tmux session spawns;
        # changing the agent afterwards would not affect the running session.
        if task.session_name:
            raise HTTPException(
                409,
                f"cannot change agent: task {task_id!r} already has a tmux "
                f"session ({task.session_name}); move it back to Backlog first",
            )
        agent = agent.strip()
        allowed = set(_agent_options_for(ctx, task))
        if agent not in allowed:
            raise HTTPException(
                400,
                f"unknown agent {agent!r}; expected one of {sorted(allowed)}",
            )
        ctx.db.update_task(task_id, agent=agent)
        return RedirectResponse(f"/projects/{project_id}?task={task_id}", status_code=303)

    @app.post("/actions/task/{task_id}/agent")
    async def compat_action_task_agent(
        task_id: str,
        agent: str = Form(...),
    ) -> RedirectResponse:
        ctx = state.current_project()
        return await action_task_agent(ctx.project.id, task_id, agent)

    @app.post("/projects/{project_id}/actions/task/{task_id}/cleanup-worktree")
    async def action_task_cleanup_worktree(
        project_id: str,
        task_id: str,
    ) -> RedirectResponse:
        """Remove the task's worktree and force-delete its branch.

        Refuses unless the task is Done — pre-Done tasks still need the
        worktree alive. After cleanup, clears worktree_path/branch_name on
        the row so the Done view stops offering the button.
        """
        ctx = state.get_project(project_id)
        task = ctx.db.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"task {task_id!r} not found")
        if task.status != "done":
            raise HTTPException(
                409,
                f"cleanup-worktree requires status=done (got {task.status!r})",
            )
        if not task.worktree_path and not task.branch_name:
            raise HTTPException(409, "task has no worktree or branch to clean up")
        git = GitOps()
        if task.worktree_path:
            wt = Path(task.worktree_path)
            try:
                git.remove_worktree(ctx.path, wt)
            except Exception as exc:  # noqa: BLE001 — surface via 500
                raise HTTPException(
                    500, f"git worktree remove failed: {exc}",
                ) from exc
        if task.branch_name:
            try:
                git.delete_branch(ctx.path, task.branch_name)
            except Exception as exc:  # noqa: BLE001
                # Worktree removed but branch delete failed (e.g. checked out
                # elsewhere): leave the row alone so the user can retry.
                raise HTTPException(
                    500, f"git branch -D failed: {exc}",
                ) from exc
        ctx.db.update_task(task_id, worktree_path=None, branch_name=None)
        return RedirectResponse(
            f"/projects/{project_id}?task={task_id}", status_code=303,
        )

    @app.post("/actions/task/{task_id}/cleanup-worktree")
    async def compat_action_task_cleanup_worktree(task_id: str) -> RedirectResponse:
        ctx = state.current_project()
        return await action_task_cleanup_worktree(ctx.project.id, task_id)

    return app


def _project_config_status(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "missing", ""
    try:
        load_config(path)
    except Exception as exc:
        return "invalid", str(exc)
    return "valid", ""


def _project_bootstrap_status(
    project_path: Path,
) -> tuple[str, int, BootstrapPlan | None, str]:
    try:
        plan = build_plan(project_path)
    except Exception as exc:
        return "error", 0, None, str(exc)
    pending = plan.pending_operations
    if not pending:
        status = "bootstrapped"
    elif any(op.status == "update" for op in pending):
        status = "stale"
    else:
        status = "not bootstrapped"
    return status, len(pending), plan, ""


def _select_initial_project(
    contexts: Sequence[ProjectContext],
    initial_path: Path,
) -> str | None:
    if not contexts:
        return None
    target = str(initial_path)
    for ctx in contexts:
        if str(ctx.path) == target:
            return ctx.project.id
        try:
            if ctx.path.resolve() == initial_path:
                return ctx.project.id
        except Exception:
            pass
    return contexts[0].project.id


def _coerce_terminal_size(value: object, *, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, 500))


def _transition_config_for(
    ctx: ProjectContext,
    cfg: Config,
    options: WebuiOptions,
) -> TransitionConfig:
    if options.agent_command:
        resolved_agent_command: tuple[str, ...] | None = options.agent_command
    elif cfg.agtx_agent_command:
        resolved_agent_command = cfg.agtx_agent_command
    else:
        resolved_agent_command = None

    plugin_name = options.plugin or cfg.agtx_plugin
    resolved_plugin: WorkflowPlugin | None = None
    if plugin_name:
        resolved_plugin = load_plugin(plugin_name, repo_root=ctx.path)

    # harbor.yml's `agtx.agent_command_by_agent` is the base map; the webui's
    # `--map-agent` CLI flags overlay it per-key so a one-off override wins.
    resolved_agent_command_by_agent: dict[str, tuple[str, ...]] = dict(
        cfg.agtx_agent_command_by_agent
    )
    resolved_agent_command_by_agent.update(options.agent_command_by_agent)

    return TransitionConfig(
        project_path=ctx.path,
        agent_command=resolved_agent_command,
        agent_command_by_phase=options.agent_command_by_phase,
        agent_command_by_agent=resolved_agent_command_by_agent,
        base_branch=options.base_branch,
        worktree_dir=options.worktree_dir,
        init_script=options.init_script,
        copy_files=options.copy_files,
        prompt_append=cfg.agtx_prompt_append,
        inject_prompts=options.inject_prompts,
        cleanup_worktree_on_done=options.cleanup_worktree_on_done,
        pr_on_done=options.pr_on_done,
        default_shell=cfg.default_shell,
        plugin=resolved_plugin,
    )


def shell_split(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    argv = tuple(shlex.split(raw))
    return argv or None


def _shell_join(argv: Sequence[str] | None) -> str:
    if not argv:
        return ""
    return shlex.join(str(part) for part in argv)


def _pick_folder_with_native_dialog() -> str | None:
    """Open a host-native folder picker and return the selected path.

    This runs in the Harbor server process, not in the browser. That is the
    only way a local web UI can get a real Windows filesystem path without
    asking the user to type it.
    """
    if os.name == "nt":
        return _pick_folder_with_windows_dialog()

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - host dependent
        raise RuntimeError("native folder picker is not available") from exc

    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        picked = filedialog.askdirectory(
            parent=root,
            title="Track project folder",
            mustexist=True,
        )
    except Exception as exc:  # pragma: no cover - host dependent
        raise RuntimeError(f"native folder picker failed: {exc}") from exc
    finally:
        root.destroy()

    return picked or None


def _pick_folder_with_windows_dialog() -> str | None:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.TopMost = $true
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(1, 1)
$form.ShowInTaskbar = $false
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Track project folder'
$dialog.ShowNewFolderButton = $false
$result = $dialog.ShowDialog($form)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $dialog.SelectedPath
}
$form.Close()
"""
    cp = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or "PowerShell folder picker failed").strip()
        raise RuntimeError(msg)
    picked = (cp.stdout or "").strip()
    return picked or None


def _folder_browser_view(raw_path: str | None) -> dict[str, Any]:
    error = ""
    if raw_path:
        candidate = Path(raw_path)
    else:
        candidate = _default_folder_browser_path()

    try:
        current = candidate.resolve(strict=True)
        if not current.is_dir():
            error = f"not a directory: {candidate}"
            current = _default_folder_browser_path().resolve(strict=True)
    except Exception as exc:
        error = f"cannot open {candidate}: {exc}"
        current = _default_folder_browser_path().resolve(strict=True)

    entries: list[dict[str, str]] = []
    try:
        for child in current.iterdir():
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except OSError as exc:
        error = f"cannot list {current}: {exc}"

    entries.sort(key=lambda e: e["name"].lower())
    parent: str | None = None
    try:
        if current.parent != current:
            parent = str(current.parent)
    except Exception:
        parent = None

    return {
        "current_path": str(current),
        "parent_path": parent,
        "entries": entries,
        "drives": _folder_browser_drives(),
        "error": error,
    }


def _default_folder_browser_path() -> Path:
    for raw in ("D:/Projects", "D:/", str(Path.cwd())):
        path = Path(raw)
        if path.exists() and path.is_dir():
            return path
    return Path.cwd()


def _folder_browser_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if Path(root).exists():
                drives.append(root)
        except OSError:
            pass
    return drives


def _safe_session_chunk(value: str) -> str:
    out = []
    for ch in value.lower():
        out.append(ch if ch.isalnum() else "-")
    return "-".join(part for part in "".join(out).split("-") if part)


def _safe_session_name(value: str) -> str:
    return _safe_session_chunk(value)
