from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PY = REPO_ROOT / "plugins" / "harbor-workflow-template" / "install.py"


def _run(*args: str, appdata: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if appdata is not None:
        env["APPDATA"] = str(appdata)
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _skill_names() -> list[str]:
    skills_dir = REPO_ROOT / ".claude" / "skills"
    return sorted(
        child.name
        for child in skills_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def test_install_py_default_matches_harbor_bootstrap_apply(tmp_path: Path):
    via_bootstrap = tmp_path / "via-bootstrap"
    via_install_py = tmp_path / "via-install-py"
    via_bootstrap.mkdir()
    via_install_py.mkdir()

    bootstrap = _run(
        "-m", "harbor.bootstrap", "--apply", str(via_bootstrap),
        appdata=tmp_path / "appdata-bootstrap",
    )
    install_py = _run(str(INSTALL_PY), str(via_install_py))

    assert bootstrap.returncode == 0, bootstrap.stderr
    assert install_py.returncode == 0, install_py.stderr
    assert _snapshot_files(via_install_py) == _snapshot_files(via_bootstrap)


def test_install_py_does_not_duplicate_agent_native_paths_mapping():
    assert "AGENT_NATIVE_PATHS" not in INSTALL_PY.read_text(encoding="utf-8")


def test_install_py_is_substantially_smaller_than_the_legacy_installer():
    line_count = len(INSTALL_PY.read_text(encoding="utf-8").splitlines())
    assert line_count <= 120


def test_install_py_skip_skills_keeps_only_plugin_bundled_skills(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    result = _run(str(INSTALL_PY), "--skip-skills", str(project))

    assert result.returncode == 0, result.stderr
    for name in _skill_names():
        assert (
            project
            / ".harbor"
            / "plugins"
            / "harbor-workflow-template"
            / "skills"
            / name
            / "SKILL.md"
        ).is_file()
        assert not (project / ".claude" / "skills" / name / "SKILL.md").exists()
        assert not (project / ".codex" / "skills" / f"{name}.md").exists()
        assert not (project / ".harbor" / "skills" / name / "SKILL.md").exists()
    assert (project / "harbor.yml").is_file()
    assert (project / ".harbor" / "runtime-target.json").is_file()


def test_install_py_skip_harbor_yml_leaves_harbor_yml_absent(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    result = _run(str(INSTALL_PY), "--skip-harbor-yml", str(project))

    assert result.returncode == 0, result.stderr
    assert not (project / "harbor.yml").exists()
    assert (project / ".harbor" / "runtime-target.json").is_file()


def test_install_py_agent_flag_limits_agent_native_skill_deploy(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    result = _run(str(INSTALL_PY), "--agent", "codex", str(project))

    assert result.returncode == 0, result.stderr
    for name in _skill_names():
        assert not (project / ".claude" / "skills" / name / "SKILL.md").exists()
        assert (project / ".codex" / "skills" / f"{name}.md").is_file()
        assert (project / ".harbor" / "skills" / name / "SKILL.md").is_file()


def test_install_py_agent_flag_supports_legacy_agent_names(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    result = _run(str(INSTALL_PY), "--agent", "gemini", str(project))

    assert result.returncode == 0, result.stderr
    for name in _skill_names():
        assert (project / ".gemini" / "commands" / "harbor" / f"{name}.md").is_file()
        assert not (project / ".codex" / "skills" / f"{name}.md").exists()
        assert (project / ".harbor" / "skills" / name / "SKILL.md").is_file()


def test_install_py_force_flag_is_accepted_and_rewrites_changed_files(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / ".harbor" / "plugins" / "harbor-workflow-template" / "plugin.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("stale = true\n", encoding="utf-8")

    result = _run(str(INSTALL_PY), "--force", str(project))

    assert result.returncode == 0, result.stderr
    assert manifest.read_bytes() == (
        REPO_ROOT / "plugins" / "harbor-workflow-template" / "plugin.toml"
    ).read_bytes()
