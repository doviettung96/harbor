from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harbor import agtx_client
from harbor.bootstrap import (
    CONFIGURE_BUILD_TITLE,
    CONFIGURE_RUNTIME_TITLE,
    WORKER_SMOKE_TITLE,
    apply_bootstrap,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HEADERS = (
    "## Acceptance Criteria",
    "## Verification Probes",
    "## Related Tests",
    "## Worker Instructions",
)


@pytest.fixture
def isolated_harbor_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "harbor-config"
    monkeypatch.setattr(agtx_client, "harbor_data_dir", lambda: config)
    return config


def _project_db(project: Path) -> agtx_client.AgtxDb:
    db_path, canonical = agtx_client.resolve_project_db_path(project)
    assert canonical is not None
    return agtx_client.AgtxDb(project_db_p=db_path)


def _section(description: str, header: str) -> str:
    marker = f"{header}\n"
    assert marker in description
    start = description.index(marker) + len(marker)
    next_header = description.find("\n## ", start)
    if next_header == -1:
        return description[start:].strip()
    return description[start:next_header].strip()


def test_apply_bootstrap_seeds_two_init_tasks(
    tmp_path: Path,
    isolated_harbor_data: Path,
):
    project = tmp_path / "blank"
    project.mkdir()

    apply_bootstrap(project)

    tasks = _project_db(project).list_tasks()
    titles = {task.title for task in tasks}
    assert titles == {CONFIGURE_RUNTIME_TITLE, WORKER_SMOKE_TITLE, CONFIGURE_BUILD_TITLE}


def test_seeded_task_descriptions_have_required_headers(
    tmp_path: Path,
    isolated_harbor_data: Path,
):
    project = tmp_path / "blank"
    project.mkdir()

    apply_bootstrap(project)

    for task in _project_db(project).list_tasks():
        description = task.description or ""
        for header in REQUIRED_HEADERS:
            assert _section(description, header)


def test_bootstrap_seed_is_idempotent_by_title(
    tmp_path: Path,
    isolated_harbor_data: Path,
):
    project = tmp_path / "blank"
    project.mkdir()

    apply_bootstrap(project)
    apply_bootstrap(project)

    tasks = _project_db(project).list_tasks()
    assert [task.title for task in tasks].count(CONFIGURE_RUNTIME_TITLE) == 1
    assert [task.title for task in tasks].count(WORKER_SMOKE_TITLE) == 1
    assert [task.title for task in tasks].count(CONFIGURE_BUILD_TITLE) == 1
    assert len(tasks) == 3


def test_configure_runtime_seeded_probe_command_exists():
    result = subprocess.run(
        [sys.executable, "scripts/shared/target_runtime.py", "target", "show"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_configure_build_seeded_probe_runs():
    from harbor.bootstrap import CONFIGURE_BUILD_DESCRIPTION

    probe = _section(CONFIGURE_BUILD_DESCRIPTION, "## Verification Probes")
    command = probe.strip().removeprefix("- ").strip()
    # Run with this interpreter (which has PyYAML) instead of bare `python`.
    if command.startswith("python "):
        command = f'"{sys.executable}"' + command[len("python") :]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )
    # This repo's harbor.yml leaves harbor.build unset, so the probe should pass.
    assert result.returncode == 0, result.stderr
    assert "harbor.build:" in result.stdout
