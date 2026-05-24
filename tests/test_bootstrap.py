from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from harbor import agtx_client
from harbor.bootstrap import PLUGIN_NAME, apply_bootstrap, build_plan, main


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_agtx_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(agtx_client, "agtx_config_dir", lambda: tmp_path / "agtx-config")


def _skill_names() -> list[str]:
    skills_dir = REPO_ROOT / ".claude" / "skills"
    return sorted(
        child.name
        for child in skills_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_apply_bootstrap_to_blank_project_creates_required_files(tmp_path: Path):
    project = tmp_path / "blank"
    project.mkdir()

    plan, applied = apply_bootstrap(project)

    assert applied
    assert not build_plan(project).pending_operations
    assert (project / ".agtx" / "plugins" / PLUGIN_NAME / "plugin.toml").is_file()

    for name in _skill_names():
        assert (
            project / ".agtx" / "plugins" / PLUGIN_NAME / "skills" / name / "SKILL.md"
        ).is_file()
        assert (project / ".claude" / "skills" / name / "SKILL.md").is_file()
        assert (project / ".codex" / "skills" / f"{name}.md").is_file()
        assert (project / ".agtx" / "skills" / name / "SKILL.md").is_file()

    harbor_yml = yaml.safe_load((project / "harbor.yml").read_text(encoding="utf-8"))
    assert harbor_yml["agtx"]["plugin"] == PLUGIN_NAME

    runtime = json.loads((project / ".agtx" / "runtime-target.json").read_text(encoding="utf-8"))
    assert runtime["target"]["kind"] == "local"
    assert plan.project == project.resolve()


def test_apply_bootstrap_is_idempotent(tmp_path: Path):
    project = tmp_path / "blank"
    project.mkdir()

    apply_bootstrap(project)
    before = _snapshot_files(project)

    second_plan, second_applied = apply_bootstrap(project)

    assert second_applied == ()
    assert second_plan.pending_operations == ()
    assert _snapshot_files(project) == before


def test_plan_mode_prints_operations_without_applying(tmp_path: Path, capsys):
    project = tmp_path / "blank"
    project.mkdir()

    assert main(["--plan", str(project)]) == 0

    out = capsys.readouterr().out
    assert "Bootstrap plan for" in out
    assert ".agtx/plugins/agtx-workflow-template/plugin.toml" in out
    assert "Pending operations:" in out
    assert not (project / ".agtx").exists()
    assert not (project / "harbor.yml").exists()


def test_existing_harbor_yml_is_merged_non_destructively(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "harbor.yml").write_text(
        "default_profile: fast\n"
        "custom:\n"
        "  enabled: true\n"
        "agtx:\n"
        "  agent_command: codex\n",
        encoding="utf-8",
    )

    apply_bootstrap(project)

    data = yaml.safe_load((project / "harbor.yml").read_text(encoding="utf-8"))
    assert data["default_profile"] == "fast"
    assert data["custom"] == {"enabled": True}
    assert data["agtx"]["agent_command"] == "codex"
    assert data["agtx"]["plugin"] == PLUGIN_NAME


def test_existing_runtime_target_is_preserved(tmp_path: Path):
    project = tmp_path / "project"
    runtime = project / ".agtx" / "runtime-target.json"
    runtime.parent.mkdir(parents=True)
    original = '{"version": 1, "target": {"kind": "device", "device": {"id": "abc"}}}\n'
    runtime.write_text(original, encoding="utf-8")

    apply_bootstrap(project)

    assert runtime.read_text(encoding="utf-8") == original
