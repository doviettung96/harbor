"""Tests for harbor.plugin_loader — the Python port of agtx's WorkflowPlugin.

The schema is a faithful mirror of D:/Projects/agtx/src/config/mod.rs:427-468
so plugin.toml files written for agtx work in harbor without modification.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from harbor.plugin_loader import (
    AGENT_NATIVE_PATHS,
    AutoDismiss,
    PluginArtifacts,
    PluginCommands,
    PluginPrompts,
    WorkflowPlugin,
    detect_agent_kind,
    determine_phase_variant,
    load_plugin,
    phase_artifact_exists,
    resolve_prompt,
    resolve_skill_command,
    resolve_skills_dir,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- file parsing ---------------------------------------------------------


def _write_plugin(tmp_path: Path, name: str, body: str) -> Path:
    """Write a plugins/<name>/plugin.toml and return the repo_root."""
    plugin_dir = tmp_path / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(dedent(body), encoding="utf-8")
    return tmp_path


def test_load_minimal_plugin(tmp_path: Path):
    repo_root = _write_plugin(tmp_path, "mini", """\
        name = "mini"
        description = "the smallest possible plugin"
    """)
    plugin = load_plugin("mini", repo_root=repo_root)
    assert plugin.name == "mini"
    assert plugin.description == "the smallest possible plugin"
    assert plugin.commands.planning is None
    assert plugin.prompts.planning is None
    assert plugin.auto_dismiss == ()


def test_load_full_plugin(tmp_path: Path):
    repo_root = _write_plugin(tmp_path, "full", """\
        name = "full"
        description = "everything"
        clear_context_on_advance = true
        cyclic = true
        supported_agents = ["claude", "codex"]
        copy_files = [".env", "config.json"]
        copy_dirs = [".secrets"]

        [artifacts]
        research = ".harbor/research.md"
        planning = ".harbor/plan.md"
        running = ".harbor/execute.md"
        review = ".harbor/review.md"

        [commands]
        planning = "/plan {task_id}"
        running = "/exec {task_id}"
        review = "/review"

        [prompts]
        planning = "Plan task {task} with id {task_id} for cycle {phase}"
        running = "Implement {task_id}"
        review = "Verify."

        [prompt_triggers]
        planning = "│ >"

        [[auto_dismiss]]
        detect = ["trust this folder?"]
        response = "1\\nEnter"

        [[auto_dismiss]]
        detect = ["Yes, I accept", "the risk"]
        response = "2\\nEnter"
    """)
    plugin = load_plugin("full", repo_root=repo_root)
    assert plugin.name == "full"
    assert plugin.clear_context_on_advance is True
    assert plugin.cyclic is True
    assert plugin.supported_agents == ("claude", "codex")
    assert plugin.copy_files == (".env", "config.json")
    assert plugin.copy_dirs == (".secrets",)
    assert plugin.artifacts.planning == ".harbor/plan.md"
    assert plugin.commands.planning == "/plan {task_id}"
    assert plugin.prompts.running == "Implement {task_id}"
    assert plugin.prompt_triggers.planning == "│ >"
    assert len(plugin.auto_dismiss) == 2
    assert plugin.auto_dismiss[0].detect == ("trust this folder?",)
    assert plugin.auto_dismiss[0].response == "1\nEnter"
    assert plugin.auto_dismiss[1].detect == ("Yes, I accept", "the risk")


def test_load_missing_name_raises(tmp_path: Path):
    plugin_dir = tmp_path / "plugins" / "x"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text('description = "no name"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="'name' is required"):
        load_plugin("x", repo_root=tmp_path)


def test_load_by_direct_path(tmp_path: Path):
    repo_root = _write_plugin(tmp_path, "byname", 'name = "byname"\n')
    direct = repo_root / "plugins" / "byname" / "plugin.toml"
    plugin = load_plugin(str(direct), repo_root=tmp_path)
    assert plugin.name == "byname"


def test_load_by_directory(tmp_path: Path):
    repo_root = _write_plugin(tmp_path, "bydir", 'name = "bydir"\n')
    direct_dir = repo_root / "plugins" / "bydir"
    plugin = load_plugin(str(direct_dir), repo_root=tmp_path)
    assert plugin.name == "bydir"


def test_load_not_found_lists_search_paths(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="not found"):
        load_plugin("doesnotexist", repo_root=tmp_path)


def test_load_search_order(tmp_path: Path):
    """plugins/<name>/ wins over .harbor/plugins/<name>/."""
    # Create both locations with different name strings to confirm which wins.
    (tmp_path / "plugins" / "dup").mkdir(parents=True)
    (tmp_path / "plugins" / "dup" / "plugin.toml").write_text(
        'name = "from-plugins-dir"\n', encoding="utf-8",
    )
    (tmp_path / ".harbor" / "plugins" / "dup").mkdir(parents=True)
    (tmp_path / ".harbor" / "plugins" / "dup" / "plugin.toml").write_text(
        'name = "from-harbor-dir"\n', encoding="utf-8",
    )
    plugin = load_plugin("dup", repo_root=tmp_path)
    assert plugin.name == "from-plugins-dir"


# ---- placeholder substitution ---------------------------------------------


def test_resolve_prompt_substitutes_placeholders():
    plugin = WorkflowPlugin(
        name="p",
        prompts=PluginPrompts(planning="Plan {task} as task {task_id} cycle {phase}"),
    )
    out = resolve_prompt(plugin, "planning", task_content="do thing", task_id="abc12345", cycle=2)
    assert out == "Plan do thing as task abc12345 cycle 2"


def test_resolve_prompt_returns_empty_when_not_configured():
    plugin = WorkflowPlugin(name="p")
    assert resolve_prompt(plugin, "planning", task_content="x", task_id="x") == ""


def test_resolve_prompt_with_no_plugin():
    assert resolve_prompt(None, "planning", task_content="x", task_id="x") == ""


def test_resolve_prompt_planning_with_research_uses_variant_field():
    plugin = WorkflowPlugin(
        name="p",
        prompts=PluginPrompts(
            planning="plain planning {task}",
            planning_with_research="research-aware planning",
        ),
    )
    out = resolve_prompt(
        plugin, "planning_with_research", task_content="thing", task_id="x",
    )
    assert out == "research-aware planning"


# ---- skill command resolution ---------------------------------------------


def test_resolve_skill_command_collapses_task_content_to_one_line():
    plugin = WorkflowPlugin(
        name="p",
        commands=PluginCommands(planning="/plan {task}"),
    )
    out = resolve_skill_command(
        plugin, "planning",
        task_content="line one\nline two\n\nline three",
        task_id="abc",
    )
    # Commands are line-oriented so newlines collapse to spaces.
    assert out == "/plan line one line two line three"


def test_resolve_skill_command_planning_with_research_strips_task():
    plugin = WorkflowPlugin(
        name="p",
        commands=PluginCommands(planning="/plan {task_id} {task}"),
    )
    out = resolve_skill_command(
        plugin, "planning_with_research", task_content="anything", task_id="abc",
    )
    assert out == "/plan abc"  # {task} stripped


def test_resolve_skill_command_returns_none_for_unconfigured_phase():
    plugin = WorkflowPlugin(name="p")
    assert resolve_skill_command(plugin, "planning", task_content="x", task_id="x") is None


def test_resolve_skill_command_preresearch_falls_back_to_research():
    plugin = WorkflowPlugin(
        name="p",
        commands=PluginCommands(research="/research {task_id}"),
    )
    out = resolve_skill_command(plugin, "preresearch", task_content="x", task_id="abc")
    assert out == "/research abc"


def test_resolve_skill_command_with_no_plugin():
    assert resolve_skill_command(None, "planning", task_content="x", task_id="x") is None


# ---- artifact existence + phase variants ----------------------------------


def test_phase_artifact_exists_simple(tmp_path: Path):
    plugin = WorkflowPlugin(
        name="p",
        artifacts=PluginArtifacts(planning=".harbor/plan.md"),
    )
    (tmp_path / ".harbor").mkdir()
    assert phase_artifact_exists(plugin, "planning", worktree_path=tmp_path) is False
    (tmp_path / ".harbor" / "plan.md").write_text("plan")
    assert phase_artifact_exists(plugin, "planning", worktree_path=tmp_path) is True


def test_phase_artifact_supports_glob(tmp_path: Path):
    plugin = WorkflowPlugin(
        name="p",
        artifacts=PluginArtifacts(planning=".planning/phases/*/PLAN.md"),
    )
    assert phase_artifact_exists(plugin, "planning", worktree_path=tmp_path) is False
    (tmp_path / ".planning" / "phases" / "01-design").mkdir(parents=True)
    (tmp_path / ".planning" / "phases" / "01-design" / "PLAN.md").write_text("plan")
    assert phase_artifact_exists(plugin, "planning", worktree_path=tmp_path) is True


def test_determine_phase_variant_planning(tmp_path: Path):
    plugin = WorkflowPlugin(
        name="p",
        artifacts=PluginArtifacts(
            research=".harbor/research.md",
            planning=".harbor/plan.md",
        ),
    )
    # Without research artifact → plain planning
    assert determine_phase_variant(plugin, "planning", worktree_path=tmp_path) == "planning"
    # With research artifact → planning_with_research
    (tmp_path / ".harbor").mkdir()
    (tmp_path / ".harbor" / "research.md").write_text("r")
    assert (
        determine_phase_variant(plugin, "planning", worktree_path=tmp_path)
        == "planning_with_research"
    )


def test_determine_phase_variant_running(tmp_path: Path):
    plugin = WorkflowPlugin(
        name="p",
        artifacts=PluginArtifacts(
            research=".harbor/research.md",
            planning=".harbor/plan.md",
        ),
    )
    assert determine_phase_variant(plugin, "running", worktree_path=tmp_path) == "running"
    (tmp_path / ".harbor").mkdir()
    (tmp_path / ".harbor" / "plan.md").write_text("p")
    assert (
        determine_phase_variant(plugin, "running", worktree_path=tmp_path)
        == "running_with_research_or_planning"
    )


def test_determine_phase_variant_passthrough_for_unsupported():
    plugin = WorkflowPlugin(name="p")
    # research/review/other don't have variants
    assert determine_phase_variant(plugin, "review", worktree_path=Path(".")) == "review"


# ---- agent-kind detection -------------------------------------------------


def test_agent_native_paths_covers_known_agents():
    """Sanity: every key in AGENT_NATIVE_PATHS has a (base, namespace) tuple."""
    for agent, mapping in AGENT_NATIVE_PATHS.items():
        assert isinstance(mapping, tuple) and len(mapping) == 2, agent


def test_detect_agent_kind_simple():
    assert detect_agent_kind(["claude"]) == "claude"
    assert detect_agent_kind(["codex"]) == "codex"
    assert detect_agent_kind(["gemini"]) == "gemini"
    assert detect_agent_kind(["cursor"]) == "cursor"


def test_detect_agent_kind_strips_path_and_extension():
    assert detect_agent_kind(["C:/bin/claude.exe"]) == "claude"
    assert detect_agent_kind(["/usr/local/bin/codex"]) == "codex"
    assert detect_agent_kind(["claude.CMD"]) == "claude"


def test_detect_agent_kind_unknown_returns_none():
    assert detect_agent_kind(["my-custom-cli"]) is None
    assert detect_agent_kind([]) is None


# ---- skill directory resolution -------------------------------------------


def test_resolve_skills_dir_prefers_bundled(tmp_path: Path):
    """A plugin that bundles its own skills/ resolves to that dir."""
    repo_root = _write_plugin(tmp_path, "bundled", 'name = "bundled"\n')
    plugin = load_plugin("bundled", repo_root=repo_root)
    bundled = repo_root / "plugins" / "bundled" / "skills"
    bundled.mkdir()
    # Also create an in-repo .claude/skills/ to confirm bundled WINS.
    (repo_root / ".claude" / "skills").mkdir(parents=True)
    assert resolve_skills_dir(plugin) == bundled


def test_resolve_skills_dir_in_repo_fallback(tmp_path: Path):
    """No bundled skills/ → fall back to <repo>/.claude/skills/."""
    repo_root = _write_plugin(tmp_path, "inrepo", 'name = "inrepo"\n')
    plugin = load_plugin("inrepo", repo_root=repo_root)
    claude_skills = repo_root / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    assert resolve_skills_dir(plugin) == claude_skills


def test_resolve_skills_dir_none_when_neither_exists(tmp_path: Path):
    repo_root = _write_plugin(tmp_path, "empty", 'name = "empty"\n')
    plugin = load_plugin("empty", repo_root=repo_root)
    assert resolve_skills_dir(plugin) is None


def test_resolve_skills_dir_none_when_no_source_path():
    assert resolve_skills_dir(WorkflowPlugin(name="p")) is None


# ---- our actual template plugin -------------------------------------------


def test_workflow_template_plugin_loads():
    """Sanity check: the plugin we ship in plugins/harbor-workflow-template/
    parses cleanly and has the expected commands."""
    plugin = load_plugin("harbor-workflow-template", repo_root=REPO_ROOT)
    assert plugin.name == "harbor-workflow-template"
    assert plugin.commands.planning == "/harbor-task-worker {task_id}"
    assert plugin.commands.running == "/harbor-task-worker {task_id}"
    assert plugin.commands.review == "/harbor-task-verify"
    assert plugin.artifacts.planning == ".harbor/plan.md"
    assert "Yes, I accept" in plugin.auto_dismiss[0].detect


def test_workflow_template_skills_resolve_to_claude_skills():
    """The in-repo harbor-workflow-template plugin has no bundled skills/;
    resolve_skills_dir must fall back to harbor's .claude/skills/."""
    repo = REPO_ROOT
    plugin = load_plugin("harbor-workflow-template", repo_root=repo)
    skills_dir = resolve_skills_dir(plugin)
    assert skills_dir == repo / ".claude" / "skills"
    assert (skills_dir / "harbor-task-worker" / "SKILL.md").exists()
