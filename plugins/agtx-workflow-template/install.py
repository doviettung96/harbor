"""Install this plugin into a target project, agtx-style.

Mirrors agtx's plugin convention (D:/Projects/agtx/src/config/mod.rs:588-615
for search order, src/tui/app.rs:8880-8950 for skill deployment):

  1. Install the plugin DIR (plugin.toml + skills/) to a well-known search
     path so harbor / agtx can find it by name. Default: project-local at
     `<target>/.agtx/plugins/<name>/`. Pass `--global` for
     `~/.config/agtx/plugins/<name>/`.

  2. Deploy skills to the agent's native discovery path so slash commands
     (e.g. `/agtx-task-worker`) actually resolve. agtx maps:
       claude   -> .claude/commands/agtx/
       codex    -> .codex/skills/
       gemini   -> .gemini/commands/agtx/
       copilot  -> .github/agents/agtx/
       opencode -> .opencode/command/
       cursor   -> .cursor/skills/
     Defaults to `claude` and `codex` (the two harbor commonly drives).

  3. Optionally write a starter `harbor.yml` referencing this plugin so the
     target project Just Works with `python -m harbor webui --project-path <target>`.

Usage:
    python plugins/agtx-workflow-template/install.py <target-project-dir>

Common flags:
    --global              Install plugin globally instead of project-local
    --agent claude        Deploy skills for this agent (repeat for multiple).
                          Default: --agent claude --agent codex
    --skip-skills         Don't deploy skills; just install the plugin
    --skip-harbor-yml     Don't write a starter harbor.yml
    --force               Overwrite existing files at the destinations
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Mirror of agtx's skills::agent_native_skill_dir() — D:/Projects/agtx/src/skills.rs:35-44.
# Map: agent kind -> (base_dir relative to target, namespace subdir under base_dir).
# Kept in sync with harbor.plugin_loader.AGENT_NATIVE_PATHS. (install.py is
# standalone for distribution -- it can't import from harbor.)
AGENT_NATIVE_PATHS: dict[str, tuple[str, str]] = {
    "claude":   (".claude/commands", "agtx"),
    "gemini":   (".gemini/commands", "agtx"),
    "opencode": (".opencode/command", ""),
    "codex":    (".codex/skills", ""),
    "cursor":   (".cursor/skills", ""),
    "copilot":  (".github/agents", "agtx"),
}

HARBOR_YML_DEFAULT = """\
agtx:
  # Workflow plugin. Harbor will find it at .agtx/plugins/agtx-workflow-template/
  # (project-local) or ~/.config/agtx/plugins/agtx-workflow-template/ (global).
  plugin: agtx-workflow-template

  # Default agent CLI to launch in each task's tmux pane.
  # agent_command: "codex"
"""

RUNTIME_TARGET_STARTER = """\
{
  "version": 1,
  "mode": "local",
  "target": {
    "kind": "local"
  }
}
"""


def _global_plugin_root() -> Path:
    """Mirror agtx's global plugin path on each platform.

    agtx (Rust) uses $HOME/.config/agtx/plugins/ on all platforms (it reads
    HOME directly — see config/mod.rs:603). We follow the same convention
    even though Windows would typically use %APPDATA%."""
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "agtx" / "plugins"


def _install_plugin_dir(
    plugin_src: Path, dest: Path, skills_src: Path, *, force: bool,
) -> str:
    """Copy plugin.toml + README.md + skills/ to `dest`. Returns "installed"
    or "skipped" for logging.

    The installed plugin is made self-contained: skills always land at
    `<dest>/skills/`, copied from `skills_src` (which may be the plugin's own
    bundled `skills/` or harbor's in-repo `.claude/skills/`)."""
    if dest.exists():
        if not force:
            return "skipped"
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy the plugin dir except install.py itself (no point bundling the
    # installer) and any `skills/` — skills are materialized explicitly below
    # so the source can be either the bundled dir or the in-repo location.
    shutil.copytree(
        plugin_src, dest,
        ignore=shutil.ignore_patterns("install.py", "__pycache__", "skills"),
    )
    # Materialize skills into the installed plugin so it is self-contained at
    # its install location regardless of where skills_src lived.
    shutil.copytree(skills_src, dest / "skills")
    return "installed"


def _deploy_skills_to_agent_native_path(
    skills_src: Path, target: Path, agent: str, *, force: bool,
) -> tuple[int, int, int]:
    """Copy each `<skill>/SKILL.md` to the agent's native discovery path.

    Returns (copied, overwritten, skipped). For claude, files land at
    `<target>/.claude/commands/agtx/<skill-name>.md`. Namespace subdir is
    omitted when empty (codex, opencode, cursor)."""
    mapping = AGENT_NATIVE_PATHS.get(agent)
    if mapping is None:
        print(f"  warning: unknown agent {agent!r}, no native skill path configured")
        return (0, 0, 0)
    base, namespace = mapping
    native_dir = target / base / namespace if namespace else target / base
    native_dir.mkdir(parents=True, exist_ok=True)

    copied = overwritten = skipped = 0
    for skill_src in sorted(skills_src.iterdir()):
        if not skill_src.is_dir():
            continue
        skill_md = skill_src / "SKILL.md"
        if not skill_md.exists():
            continue
        # File name = skill dir name + .md (no "SKILL" prefix). Matches
        # claude/gemini/codex conventions per agtx's skills.rs:57-67.
        dst = native_dir / f"{skill_src.name}.md"
        if dst.exists():
            if not force:
                skipped += 1
                continue
            dst.unlink()
            shutil.copy2(skill_md, dst)
            overwritten += 1
        else:
            shutil.copy2(skill_md, dst)
            copied += 1
    return (copied, overwritten, skipped)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the agtx-workflow-template plugin into a target project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="Path to the target project directory.")
    parser.add_argument(
        "--global", dest="global_install", action="store_true",
        help="Install plugin globally at ~/.config/agtx/plugins/<name>/ "
        "instead of project-local at <target>/.agtx/plugins/<name>/.",
    )
    parser.add_argument(
        "--agent", action="append", default=None, metavar="NAME",
        help="Deploy skills to this agent's native discovery path. Repeat "
        "for multiple agents. Default: claude + codex. Known agents: "
        + ", ".join(AGENT_NATIVE_PATHS.keys()),
    )
    parser.add_argument(
        "--skip-skills", action="store_true",
        help="Don't deploy skills to agent-native paths. (The plugin's "
        "bundled skills/ dir is still installed alongside plugin.toml.)",
    )
    parser.add_argument(
        "--skip-harbor-yml", action="store_true",
        help="Don't write a starter harbor.yml.",
    )
    parser.add_argument(
        "--skip-runtime-target", action="store_true",
        help="Don't write .agtx/runtime-target.example.json.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing files at all destinations.",
    )
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not target.exists() or not target.is_dir():
        print(f"error: target is not an existing directory: {target}", file=sys.stderr)
        return 2

    plugin_src = Path(__file__).resolve().parent
    plugin_name = plugin_src.name
    # Skills source: a distributed plugin bundles `<plugin>/skills/`; harbor's
    # in-repo plugin keeps the single canonical copy at `<repo>/.claude/skills/`
    # (the plugin dir sits at `<repo>/plugins/<name>/`).
    skills_src = plugin_src / "skills"
    if not skills_src.is_dir():
        skills_src = plugin_src.parent.parent / ".claude" / "skills"
    if not skills_src.is_dir():
        print(
            "error: skills not found (looked in <plugin>/skills and "
            "<repo>/.claude/skills)",
            file=sys.stderr,
        )
        return 2

    # --- 1. Install the plugin DIR ---
    if args.global_install:
        plugin_dst = _global_plugin_root() / plugin_name
    else:
        plugin_dst = target / ".agtx" / "plugins" / plugin_name
    print(f"Installing plugin to: {plugin_dst}")
    result = _install_plugin_dir(plugin_src, plugin_dst, skills_src, force=args.force)
    if result == "skipped":
        print(f"  skipped (already exists; use --force to overwrite)")
    else:
        print(f"  installed {sum(1 for _ in plugin_dst.rglob('*') if _.is_file())} files")

    # --- 2. Deploy skills to agent-native paths ---
    if not args.skip_skills:
        agents = args.agent or ["claude", "codex"]
        print(f"Deploying skills for agents: {', '.join(agents)}")
        for agent in agents:
            mapping = AGENT_NATIVE_PATHS.get(agent)
            if mapping is None:
                print(f"  {agent}: unknown agent (skipping)")
                continue
            base, namespace = mapping
            native_dir = target / base / namespace if namespace else target / base
            copied, overwritten, skipped = _deploy_skills_to_agent_native_path(
                skills_src, target, agent, force=args.force,
            )
            print(
                f"  {agent} -> {native_dir.relative_to(target)}: "
                f"{copied} copied, {overwritten} overwritten, {skipped} skipped"
                + ("" if not skipped else " (use --force to overwrite)")
            )

    # --- 3. Starter harbor.yml ---
    if not args.skip_harbor_yml:
        harbor_yml = target / "harbor.yml"
        if harbor_yml.exists() and not args.force:
            print(f"harbor.yml: already exists at {harbor_yml}, leaving as-is")
        else:
            harbor_yml.write_text(HARBOR_YML_DEFAULT, encoding="utf-8")
            print(f"harbor.yml: wrote starter at {harbor_yml}")

    # --- 4. Starter runtime-target ---
    if not args.skip_runtime_target:
        agtx_dir = target / ".agtx"
        runtime_target = agtx_dir / "runtime-target.example.json"
        if runtime_target.exists() and not args.force:
            print(f"runtime-target.example.json: already exists, leaving as-is")
        else:
            agtx_dir.mkdir(parents=True, exist_ok=True)
            runtime_target.write_text(RUNTIME_TARGET_STARTER, encoding="utf-8")
            print(f"runtime-target.example.json: wrote starter at {runtime_target}")

    print()
    print("Done. Next steps:")
    print(f"  1. (optional) edit harbor.yml to set agent_command, etc.")
    print(f"  2. agtx trust && agtx              # in {target}")
    print(f"  3. python -m harbor webui --project-path {target}")
    print(f"     -> opens at http://127.0.0.1:8765/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
