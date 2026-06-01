from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _not_implemented(name: str) -> int:
    print(f"harbor: '{name}' is not implemented yet (Phase 1 in progress)", file=sys.stderr)
    return 2


def cmd_run_bead(args: argparse.Namespace) -> int:
    from .orchestrator import RunBeadOptions, run_bead

    opts = RunBeadOptions(
        bead_id=args.bead_id,
        profile=args.profile,
        model=args.model,
        effort=args.effort,
        repo_root=Path(args.repo_root or Path.cwd()).resolve(),
        timeout_s=args.timeout,
    )
    result = run_bead(opts)
    print()
    print(result.render_summary())
    return 0 if result.closed else 1


def cmd_run_epic(args: argparse.Namespace) -> int:
    from .epic import RunEpicOptions, run_epic

    opts = RunEpicOptions(
        epic_id=args.epic_id,
        profile=args.profile,
        model=args.model,
        effort=args.effort,
        repo_root=Path(args.repo_root or Path.cwd()).resolve(),
        max_concurrency=args.max_concurrency,
        interval_s=args.interval,
        max_iterations=args.max_iterations,
        bead_timeout_s=args.bead_timeout,
        skip_finalize=args.skip_finalize,
    )
    result = run_epic(opts)
    print()
    print(result.render_summary())
    if result.exit_reason == "lock_held":
        return 2
    return 0 if not result.failed else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    """Legacy `daemon` subcommand — kept as an alias for `webui` so existing scripts
    don't break. Both serve the same Harbor webview now (the bead-coupled webui is gone).
    """
    return cmd_webui(args)


def cmd_webui(args: argparse.Namespace) -> int:
    import shlex
    import uvicorn
    from .webui.server import create_app

    # If the user passed --project-path but not --repo-root, treat the project
    # as the repo root too. That's the typical case — harbor.yml sits at the
    # project root, and forcing the user to type both flags trips up people
    # running the webui from C:\Users\Admin or similar.
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    project_path = Path(args.project_path).resolve() if args.project_path else None
    agent_command: list[str] | None = None
    if args.agent_command:
        agent_command = shlex.split(args.agent_command)
    init_script = tuple(args.init_script or ())
    copy_files = tuple(args.copy_files or ())
    phase_overrides: dict[str, list[str]] = {}
    for phase in ("planning", "running", "review"):
        raw = getattr(args, f"agent_command_{phase}", None)
        if raw:
            phase_overrides[phase] = shlex.split(raw)
    agent_overrides: dict[str, list[str]] = {}
    for raw in (args.map_agent or ()):
        if "=" not in raw:
            print(f"warning: --map-agent {raw!r} ignored (expected KEY=VALUE)", file=sys.stderr)
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            print(f"warning: --map-agent {raw!r} ignored (empty key or value)", file=sys.stderr)
            continue
        agent_overrides[key] = shlex.split(value)

    app = create_app(
        repo_root,
        project_path=project_path,
        agent_command=agent_command,
        agent_command_by_phase=phase_overrides or None,
        agent_command_by_agent=agent_overrides or None,
        base_branch=args.base_branch,
        worktree_dir=args.worktree_dir,
        init_script=init_script,
        copy_files=copy_files,
        inject_prompts=not args.no_inject_prompts,
        pr_on_review=not args.no_pr_on_review,
        plugin=args.plugin,
    )
    msg = f"harbor webui: serving global dashboard at http://{args.host}:{args.port}/"
    if project_path:
        msg += f" (initial project {project_path})"
    print(msg)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    return _not_implemented("status")


def cmd_webui_diagnose(args: argparse.Namespace) -> int:
    """Print the resolved agtx DB path for --project-path and list registered projects.

    Useful when the webui can't find a project's DB. Tells you what hash we
    computed, whether the file exists, and what paths agtx itself has on file.
    """
    from .agtx_client import (
        agtx_config_dir,
        global_db_path,
        hash_project_path,
        harbor_data_dir,
        list_registered_projects,
        project_db_path,
        resolve_project_db_path,
    )

    project_path = Path(args.project_path or Path.cwd()).resolve()
    print(f"harbor data dir:    {harbor_data_dir()}")
    print(f"legacy agtx dir:    {agtx_config_dir()}")
    print(f"global index.db:    {global_db_path()} {'(exists)' if global_db_path().exists() else '(missing)'}")
    print()
    print(f"Project path:       {project_path}")
    literal_hash = hash_project_path(str(project_path))
    literal_db = project_db_path(project_path)
    print(f"Literal-hash DB:    {literal_db}")
    print(f"  hash:             {literal_hash}")
    print(f"  exists:           {literal_db.exists()}")
    print()
    resolved_db, canonical = resolve_project_db_path(project_path)
    print(f"Resolved DB path:   {resolved_db}")
    print(f"  via index.db:     {canonical or '(not found in index — used literal hash as fallback)'}")
    print(f"  exists:           {resolved_db.exists()}")
    print()
    registered = list_registered_projects()
    if registered:
        print(f"Projects in Harbor index.db ({len(registered)}):")
        for name, path in registered:
            mark = "<-- this project" if path == canonical else ""
            print(f"  - {name:30} {path}  {mark}")
    else:
        print("No projects registered in Harbor's index.db.")
    return 0


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from .mcp_server import run_stdio

    run_stdio()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harbor",
        description="Tmux-pane-per-bead runner. Drives a beads epic to completion without "
        "consuming the chat session's context window.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    rb = sub.add_parser("run-bead", help="Run a single bead in a tmux pane.")
    rb.add_argument("bead_id")
    rb.add_argument("--profile", default=None, help="Agent profile from harbor.yml.")
    rb.add_argument("--model", default=None, help="Override model (else profile default).")
    rb.add_argument("--effort", default=None, help="Override reasoning effort.")
    rb.add_argument("--repo-root", default=None, help="Repo root (default: cwd).")
    rb.add_argument("--timeout", type=int, default=3600, help="Seconds to wait for runner (default 3600).")
    rb.set_defaults(func=cmd_run_bead)

    re = sub.add_parser("run-epic", help="Run all ready descendants of an epic, polling for new ones.")
    re.add_argument("epic_id")
    re.add_argument("--profile", default=None, help="Agent profile from harbor.yml.")
    re.add_argument("--model", default=None, help="Override model (else profile default).")
    re.add_argument("--effort", default=None, help="Override reasoning effort.")
    re.add_argument("--repo-root", default=None, help="Repo root (default: cwd).")
    re.add_argument("--max-concurrency", type=int, default=3, help="Max concurrent run_bead workers (default 3, 1 = sequential).")
    re.add_argument("--interval", type=float, default=30.0, help="Poll interval seconds (timeout per outer loop tick).")
    re.add_argument("--max-iterations", type=int, default=None, help="Stop after N tick iterations (default: unlimited).")
    re.add_argument("--bead-timeout", type=int, default=60 * 60 * 6, help="Per-bead timeout seconds (default 6h).")
    re.add_argument("--skip-finalize", action="store_true", help="Skip the build-and-test + review-epic finalize pipeline.")
    re.set_defaults(func=cmd_run_epic)

    def _add_webui_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument(
            "--repo-root",
            default=None,
            help="Compatibility option for tmux config workspace (default: cwd). "
            "The live runtime config comes from Harbor's global config path.",
        )
        parser.add_argument(
            "--project-path",
            default=None,
            help="Initial project to select. The project tree comes from Harbor's global index.",
        )
        parser.add_argument(
            "--agent-command",
            default=None,
            help="Shell-quoted command to type into each spawned tmux pane. "
            "Default: 'claude --dangerously-skip-permissions'.",
        )
        parser.add_argument(
            "--agent-command-planning", default=None,
            help="Override --agent-command for the planning phase only "
            "(used when spawning the session on Backlog->Planning).",
        )
        parser.add_argument(
            "--agent-command-running", default=None,
            help="Override --agent-command for the running phase (currently "
            "unused — running reuses the planning session — but reserved for "
            "future 'respawn per phase' mode).",
        )
        parser.add_argument(
            "--agent-command-review", default=None,
            help="Override --agent-command for the review phase (currently "
            "unused — see --agent-command-running).",
        )
        parser.add_argument(
            "--no-pr-on-review", action="store_true",
            help="Disable PR-on-Review. By default, when a task is moved to "
            "Review, harbor pushes the task branch and runs `gh pr create "
            "--base <base-branch>`; the PR url is stored on the task and "
            "re-entering Review (after a Running bounce) reuses the same "
            "PR. Move to Done once the PR has merged; Done removes the "
            "worktree. Requires the `gh` CLI on PATH.",
        )
        parser.add_argument(
            "--map-agent", action="append", default=[], metavar="AGENT=CMD",
            help="Map an agtx task.agent value to a CLI invocation. Example: "
            '`--map-agent "claude=claude --dangerously-skip-permissions"` '
            '`--map-agent "codex=codex -m gpt-5.3-codex"`. The task.agent column '
            "is what agtx stored when the task was created (claude/codex/gemini/etc). "
            "This mapping is checked before --agent-command-<phase> and --agent-command. "
            "Built-in defaults cover claude, codex, gemini, copilot.",
        )
        parser.add_argument(
            "--plugin", default=None,
            help="Workflow plugin to use for phase commands/prompts/auto-dismiss. "
            "Pass a plugin NAME (searched in <repo>/plugins/, .harbor/plugins/, "
            "~/.config/harbor/plugins/) or a direct PATH to plugin.toml. Overrides "
            "harbor.yml's `harbor.plugin` key.",
        )
        parser.add_argument(
            "--base-branch", default="main",
            help="Base branch for `git worktree add -b <task-branch> <base>` (default: main).",
        )
        parser.add_argument(
            "--worktree-dir", default=".worktrees",
            help="Subdirectory under --project-path where worktrees are created (default: .worktrees).",
        )
        parser.add_argument(
            "--init-script", action="append", default=[], metavar="CMD",
            help="Shell command to run in each new worktree before launching the agent. "
            "Repeat the flag for multiple commands; they run sequentially. Example: "
            '`--init-script "pip install -e ."`',
        )
        parser.add_argument(
            "--copy-files", action="append", default=[], metavar="PATH",
            help="File or directory to copy from --project-path into each new worktree "
            "(useful for .env etc.). Repeat the flag for multiple paths.",
        )
        parser.add_argument(
            "--no-inject-prompts", action="store_true",
            help="Disable automatic phase-prompt injection (planning/running/review). "
            "By default, harbor types a starter prompt into the agent pane after each "
            "forward transition.",
        )

    w = sub.add_parser("webui", help="Run the Harbor kanban webview at http://127.0.0.1:8765/.")
    _add_webui_args(w)
    w.set_defaults(func=cmd_webui)

    d = sub.add_parser("daemon", help="Alias for `webui` (kept for backwards compatibility).")
    _add_webui_args(d)
    d.set_defaults(func=cmd_daemon)

    st = sub.add_parser("status", help="Print current runner state.")
    st.set_defaults(func=cmd_status)

    diag = sub.add_parser(
        "webui-diagnose",
        help="Print the resolved Harbor DB path and registered projects (for debugging "
        "'no such table: transition_requests' errors).",
    )
    diag.add_argument("--project-path", default=None, help="Project path (default: cwd).")
    diag.set_defaults(func=cmd_webui_diagnose)

    mcp = sub.add_parser("mcp-serve", help="Run Harbor's MCP server over stdio.")
    mcp.set_defaults(func=cmd_mcp_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
