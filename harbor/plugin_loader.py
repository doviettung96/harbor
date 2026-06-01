"""Read Harbor workflow plugins.

A plugin is a directory containing `plugin.toml` (the workflow config: per-phase
commands, artifacts, prompts, agent overrides, auto-dismiss patterns) and
optionally a bundled `skills/` subdirectory. Harbor's transition worker uses
the plugin's `commands`/`prompts` instead of the hardcoded
`DEFAULT_PHASE_PROMPTS` when a plugin is configured.

This module is a faithful (if minimal) Python port of upstream agtx's
`WorkflowPlugin` (D:/Projects/agtx/src/config/mod.rs:427-615). We omit
runtime-only concerns upstream agtx wires up elsewhere (e.g., `copy_back` ↔ TUI's
per-phase reuse, `cyclic` ↔ Review→Planning loop). When the user asks for a
feature we don't yet support, harbor falls back to its hardcoded defaults
and prints a warning.

Plugin search order:
  1. `<repo>/plugins/<name>/plugin.toml`            (harbor convention)
  2. `<repo>/.harbor/plugins/<name>/plugin.toml`     (project-local)
  3. `~/.config/harbor/plugins/<name>/plugin.toml`   (global)

If `<name>` is itself a path (contains a separator), we just resolve it and
read the file directly — useful for ad-hoc testing.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ImportError:  # Python <3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


# Mirror of agtx's `agent_native_skill_dir()` — D:/Projects/agtx/src/skills.rs:35-44.
# Map: agent kind → (base_dir relative to worktree, namespace subdir).
# When namespace is empty (codex, opencode, cursor), skills land directly in base_dir.
AGENT_NATIVE_PATHS: dict[str, tuple[str, str]] = {
    "claude":   (".claude/commands", "harbor"),
    "gemini":   (".gemini/commands", "harbor"),
    "opencode": (".opencode/command", ""),
    "codex":    (".codex/skills", ""),
    "cursor":   (".cursor/skills", ""),
    "copilot":  (".github/agents", "harbor"),
}


# ---- Dataclasses (mirror agtx's WorkflowPlugin nested structs) ------------


@dataclass(frozen=True)
class PluginArtifacts:
    """Files that signal phase completion. The `running` artifact uses
    `{phase}` for the cycle number (zero-pad supported via `{phase:02}`)."""
    preresearch: tuple[str, ...] = ()
    research: str | None = None
    planning: str | None = None
    running: str | None = None
    review: str | None = None


@dataclass(frozen=True)
class PluginCommands:
    """Slash commands the orchestrator types into the pane per phase.
    Supports `{task}`, `{task_id}`, `{phase}` placeholders. `preresearch`
    falls back to `research` if not set."""
    preresearch: str | None = None
    research: str | None = None
    planning: str | None = None
    running: str | None = None
    review: str | None = None


@dataclass(frozen=True)
class PluginPrompts:
    """Free-text prompts sent AFTER the slash command. The `_with_research`
    variants are used when the prior phase's artifact already exists.
    Same placeholders as commands."""
    research: str | None = None
    planning: str | None = None
    planning_with_research: str | None = None
    running: str | None = None
    running_with_research_or_planning: str | None = None
    review: str | None = None


@dataclass(frozen=True)
class PluginPromptTriggers:
    """Text patterns we poll the pane for, indicating the agent is ready
    to receive the phase prompt (e.g., claude's '│ >' prompt marker)."""
    research: str | None = None
    planning: str | None = None
    running: str | None = None
    review: str | None = None


@dataclass(frozen=True)
class AutoDismiss:
    """One confirmation dialog the orchestrator auto-answers.

    `detect` is an AND-list: all substrings must be present in the pane
    content for the entry to fire. `response` is the keystrokes to send,
    newline-separated (e.g. `"2\\nEnter"` means: type "2", then press
    Enter as a tmux key name).
    """
    detect: tuple[str, ...]
    response: str


# Map: agent kind -> (base dir relative to worktree, namespace subdir).
# Mirrors agtx's skills::agent_native_skill_dir() at
# D:/Projects/agtx/src/skills.rs:35-44. When namespace is "", skills land
# directly in the base dir; otherwise under `<base>/<namespace>/`.
AGENT_NATIVE_PATHS: dict[str, tuple[str, str]] = {
    "claude":   (".claude/commands", "harbor"),
    "gemini":   (".gemini/commands", "harbor"),
    "opencode": (".opencode/command", ""),
    "codex":    (".codex/skills", ""),
    "cursor":   (".cursor/skills", ""),
    "copilot":  (".github/agents", "harbor"),
}


def detect_agent_kind(agent_argv: Sequence[str]) -> str | None:
    """Identify which agent CLI this argv launches, for skill-deployment routing.

    Looks at the executable name (first argv element), stripped of path and
    .exe extension, lowercased. Returns the matching key in AGENT_NATIVE_PATHS
    or None if unknown.
    """
    if not agent_argv:
        return None
    head = agent_argv[0]
    # Strip directory portion + .exe / .cmd extensions
    name = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name if name in AGENT_NATIVE_PATHS else None


@dataclass(frozen=True)
class WorkflowPlugin:
    name: str
    description: str | None = None
    init_script: str | None = None
    supported_agents: tuple[str, ...] = ()
    copy_dirs: tuple[str, ...] = ()
    copy_files: tuple[str, ...] = ()
    cyclic: bool = False
    clear_context_on_advance: bool = False
    artifacts: PluginArtifacts = field(default_factory=PluginArtifacts)
    commands: PluginCommands = field(default_factory=PluginCommands)
    prompts: PluginPrompts = field(default_factory=PluginPrompts)
    prompt_triggers: PluginPromptTriggers = field(default_factory=PluginPromptTriggers)
    auto_dismiss: tuple[AutoDismiss, ...] = ()
    # Source file path for error messages.
    source_path: Path | None = None


# ---- Loader ---------------------------------------------------------------


def _search_paths(name: str, repo_root: Path) -> list[Path]:
    """Return candidate paths for plugin <name>'s plugin.toml in priority order."""
    return [
        repo_root / "plugins" / name / "plugin.toml",
        repo_root / ".harbor" / "plugins" / name / "plugin.toml",
        _global_plugin_root() / name / "plugin.toml",
    ]


def _global_plugin_root() -> Path:
    """Return Harbor's global plugin location across platforms."""
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "harbor" / "plugins"


def load_plugin(name_or_path: str, *, repo_root: Path) -> WorkflowPlugin:
    """Load a plugin by name (searches) or by direct path to plugin.toml or dir.

    `name_or_path` may be:
      - A plain name like `harbor-workflow-template` → searched per `_search_paths`
      - A directory path → reads `<dir>/plugin.toml`
      - A file path → reads that file directly
    """
    p = Path(name_or_path)
    # Treat as path if it contains a separator, OR if it exists as-is.
    is_path_like = (
        os.sep in name_or_path
        or "/" in name_or_path
        or name_or_path.endswith(".toml")
        or p.exists()
    )
    if is_path_like:
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        if p.is_dir():
            p = p / "plugin.toml"
        if not p.exists():
            raise FileNotFoundError(f"plugin file not found: {p}")
        return _parse_plugin_file(p)

    for candidate in _search_paths(name_or_path, repo_root):
        if candidate.exists():
            return _parse_plugin_file(candidate)
    raise FileNotFoundError(
        f"plugin {name_or_path!r} not found. Searched:\n  "
        + "\n  ".join(str(p) for p in _search_paths(name_or_path, repo_root))
    )


def _parse_plugin_file(path: Path) -> WorkflowPlugin:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{path}: 'name' is required and must be a string")

    artifacts_raw = data.get("artifacts", {}) or {}
    commands_raw = data.get("commands", {}) or {}
    prompts_raw = data.get("prompts", {}) or {}
    prompt_triggers_raw = data.get("prompt_triggers", {}) or {}
    auto_dismiss_raw = data.get("auto_dismiss", []) or []

    preresearch_artifact = artifacts_raw.get("preresearch")
    if isinstance(preresearch_artifact, str):
        preresearch_tuple: tuple[str, ...] = (preresearch_artifact,)
    elif isinstance(preresearch_artifact, list):
        preresearch_tuple = tuple(str(x) for x in preresearch_artifact)
    else:
        preresearch_tuple = ()

    return WorkflowPlugin(
        name=name,
        description=data.get("description"),
        init_script=data.get("init_script"),
        supported_agents=tuple(str(a) for a in data.get("supported_agents", []) or []),
        copy_dirs=tuple(str(d) for d in data.get("copy_dirs", []) or []),
        copy_files=tuple(str(f) for f in data.get("copy_files", []) or []),
        cyclic=bool(data.get("cyclic", False)),
        clear_context_on_advance=bool(data.get("clear_context_on_advance", False)),
        artifacts=PluginArtifacts(
            preresearch=preresearch_tuple,
            research=artifacts_raw.get("research"),
            planning=artifacts_raw.get("planning"),
            running=artifacts_raw.get("running"),
            review=artifacts_raw.get("review"),
        ),
        commands=PluginCommands(
            preresearch=commands_raw.get("preresearch"),
            research=commands_raw.get("research"),
            planning=commands_raw.get("planning"),
            running=commands_raw.get("running"),
            review=commands_raw.get("review"),
        ),
        prompts=PluginPrompts(
            research=prompts_raw.get("research"),
            planning=prompts_raw.get("planning"),
            planning_with_research=prompts_raw.get("planning_with_research"),
            running=prompts_raw.get("running"),
            running_with_research_or_planning=prompts_raw.get("running_with_research_or_planning"),
            review=prompts_raw.get("review"),
        ),
        prompt_triggers=PluginPromptTriggers(
            research=prompt_triggers_raw.get("research"),
            planning=prompt_triggers_raw.get("planning"),
            running=prompt_triggers_raw.get("running"),
            review=prompt_triggers_raw.get("review"),
        ),
        auto_dismiss=tuple(
            AutoDismiss(
                detect=tuple(str(s) for s in (entry.get("detect") or [])),
                response=str(entry.get("response", "")),
            )
            for entry in auto_dismiss_raw
            if isinstance(entry, dict)
        ),
        source_path=path,
    )


# ---- Skill directory resolution -------------------------------------------


def resolve_skills_dir(plugin: WorkflowPlugin) -> Path | None:
    """Return the directory holding the plugin's per-skill subdirectories.

    Two layouts are supported:

      1. **Bundled** — `<plugin-dir>/skills/`. This is how a distributed
         plugin ships: `install.py` copies the skills into the plugin dir so
         it is self-contained at its install location (`.harbor/plugins/...`
         or `~/.config/harbor/plugins/...`).

      2. **In-repo** — harbor's own `harbor-workflow-template` plugin does NOT
         bundle a `skills/` dir. The single canonical copy lives at the repo
         root in `.claude/skills/`, so harbor's own Claude Code sessions
         auto-discover the skills. For a plugin at
         `<repo>/plugins/<name>/plugin.toml`, that resolves to
         `<repo>/.claude/skills/`.

    Bundled wins when both exist. Returns None when neither is present (the
    caller treats that as "plugin has no skills" and no-ops).
    """
    if plugin.source_path is None:
        return None
    bundled = plugin.source_path.parent / "skills"
    if bundled.is_dir():
        return bundled
    # In-repo fallback: plugin sits at <repo>/plugins/<name>/plugin.toml, so
    # the repo root is two levels up from the plugin dir.
    repo_root = plugin.source_path.parent.parent.parent
    in_repo = repo_root / ".claude" / "skills"
    if in_repo.is_dir():
        return in_repo
    return None


# ---- Placeholder substitution (matches agtx's resolve_prompt / resolve_skill_command) ----


def _substitute(template: str, *, task_content: str, task_id: str, cycle: int) -> str:
    return (
        template.replace("{task}", task_content)
        .replace("{task_id}", task_id)
        .replace("{phase}", str(cycle))
    )


def resolve_prompt(
    plugin: WorkflowPlugin | None,
    phase: str,
    *,
    task_content: str,
    task_id: str,
    cycle: int = 1,
) -> str:
    """Return the prompt text for `phase`, with placeholders substituted.

    Returns empty string if no prompt is configured for the phase. Mirrors
    `resolve_prompt` in D:/Projects/agtx/src/tui/app.rs:7737-7780.
    """
    if plugin is None:
        return ""
    p = plugin.prompts
    template = {
        "preresearch": p.research,
        "research": p.research,
        "planning": p.planning,
        "planning_with_research": p.planning_with_research,
        "running": p.running,
        "running_with_research_or_planning": p.running_with_research_or_planning,
        "review": p.review,
    }.get(phase)
    if not template:
        return ""
    return _substitute(template, task_content=task_content, task_id=task_id, cycle=cycle)


def resolve_skill_command(
    plugin: WorkflowPlugin | None,
    phase: str,
    *,
    task_content: str,
    task_id: str,
    cycle: int = 1,
) -> str | None:
    """Return the slash command for `phase`, with placeholders substituted.

    Returns None when no command is configured (or when the configured command
    is the empty string — that's an intentional "no-op phase" signal like the
    `void` plugin uses). Mirrors `resolve_skill_command` in D:/Projects/agtx/src/tui/app.rs:7784-7831.
    """
    if plugin is None:
        return None
    c = plugin.commands
    cmd = {
        "preresearch": c.preresearch or c.research,
        "research": c.research,
        "planning": c.planning,
        "planning_with_research": c.planning,
        "running": c.running,
        "running_with_research_or_planning": c.running,
        "review": c.review,
    }.get(phase)
    if not cmd:
        return None
    # For "with_research" variants, agtx strips {task} (the agent already has
    # context from the prior phase) but still substitutes {task_id} and {phase}.
    if phase in ("planning_with_research", "running_with_research_or_planning"):
        return (
            cmd.replace("{task}", "")
            .replace("{task_id}", task_id)
            .replace("{phase}", str(cycle))
            .strip()
        )
    # Otherwise: substitute. For commands, agtx collapses task content to one
    # line (newlines → spaces) since slash commands are line-oriented.
    task_oneline = " ".join(line.strip() for line in task_content.splitlines() if line.strip())
    return _substitute(cmd, task_content=task_oneline, task_id=task_id, cycle=cycle)


# ---- Phase variants + artifact existence ----------------------------------


_GLOB_CHARS = re.compile(r"[*?\[]")


def phase_artifact_exists(
    plugin: WorkflowPlugin | None,
    phase: str,
    *,
    worktree_path: Path,
    cycle: int = 1,
) -> bool:
    """True iff the configured artifact for `phase` exists under `worktree_path`.

    Supports `{phase}` placeholder (cycle number, with optional zero-padding
    via `{phase:02}`) and basic glob wildcards.
    """
    if plugin is None:
        return False
    a = plugin.artifacts
    template = {
        "research": a.research,
        "planning": a.planning,
        "running": a.running,
        "review": a.review,
    }.get(phase)
    if not template:
        return False
    candidates = _expand_artifact_template(template, cycle=cycle)
    for rel in candidates:
        absolute = (worktree_path / rel).resolve()
        if absolute.exists():
            return True
        # Glob support
        if _GLOB_CHARS.search(rel):
            matches = list(worktree_path.glob(rel))
            if matches:
                return True
    return False


def _expand_artifact_template(template: str, *, cycle: int) -> list[str]:
    """Return all reasonable string expansions of an artifact template.

    agtx supports `{phase}` → cycle number and a zero-padded variant
    `{phase:02}`. We try both to match either layout.
    """
    out = [template.replace("{phase:02}", f"{cycle:02d}").replace("{phase}", str(cycle))]
    # Also try un-padded {phase:02} in case the file is named "1" not "01"
    alt = template.replace("{phase:02}", str(cycle)).replace("{phase}", str(cycle))
    if alt != out[0]:
        out.append(alt)
    return out


def determine_phase_variant(
    plugin: WorkflowPlugin | None,
    base_phase: str,
    *,
    worktree_path: Path,
    cycle: int = 1,
) -> str:
    """Pick the appropriate variant of `base_phase` based on which prior-phase
    artifacts exist. Mirrors `determine_phase_variant` in agtx's tui/app.rs.

    - For `planning`: returns `planning_with_research` if research artifact exists.
    - For `running`: returns `running_with_research_or_planning` if research OR
      planning artifact exists.
    - Otherwise returns `base_phase` unchanged.
    """
    if plugin is None:
        return base_phase
    if base_phase == "planning":
        if phase_artifact_exists(plugin, "research", worktree_path=worktree_path, cycle=cycle):
            return "planning_with_research"
        return "planning"
    if base_phase == "running":
        if phase_artifact_exists(plugin, "research", worktree_path=worktree_path, cycle=cycle):
            return "running_with_research_or_planning"
        if phase_artifact_exists(plugin, "planning", worktree_path=worktree_path, cycle=cycle):
            return "running_with_research_or_planning"
        return "running"
    return base_phase
