from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from harbor import agtx_client as ac
import harbor.webui.server as server_mod
from harbor.agtx_client import AgtxDb, Project, init_test_db
from harbor.bootstrap import PLUGIN_NAME, build_plan
from harbor.webui.server import create_app


@pytest.fixture(autouse=True)
def isolated_harbor_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: tmp_path / "harbor-config")
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: tmp_path / "missing-agtx-config")


def _make_memdb() -> AgtxDb:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_test_db(conn, kind="project")
    return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]


def _skill_names() -> list[str]:
    skills_dir = Path(__file__).resolve().parents[1] / ".claude" / "skills"
    return sorted(
        child.name
        for child in skills_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


@contextmanager
def _client_for_project(tmp_path: Path, project_dir: Path) -> Iterator[TestClient]:
    fake_tmux = MagicMock()
    fake_tmux.has_session.return_value = False
    fake_tmux.list_sessions.return_value = []
    fake_tmux.attach_command.return_value = "tmux -L harbor attach -t fake"
    project = Project(id="p1", name="Blank Project", path=str(project_dir), last_opened="1")
    with patch.object(server_mod, "Tmux", return_value=fake_tmux):
        app = create_app(
            project_dir,
            projects=[project],
            project_dbs={"p1": _make_memdb()},
            autostart_worker=False,
            runtime_config_path=tmp_path / "runtime.yml",
        )
        with TestClient(app) as client:
            yield client


def test_blank_project_page_shows_not_bootstrapped_and_setup_button(tmp_path: Path):
    project_dir = tmp_path / "blank"
    project_dir.mkdir()
    with _client_for_project(tmp_path, project_dir) as client:
        r = client.get("/projects/p1")

    assert r.status_code == 200
    assert "not bootstrapped" in r.text
    assert "Set up workflow" in r.text


def test_setup_button_preview_matches_bootstrap_plan_output(tmp_path: Path):
    project_dir = tmp_path / "blank"
    project_dir.mkdir()
    expected = build_plan(project_dir).render()
    with _client_for_project(tmp_path, project_dir) as client:
        r = client.get("/projects/p1", params={"bootstrap": "preview"})

    assert r.status_code == 200
    assert "Bootstrap plan" in r.text
    assert expected in r.text


def test_post_bootstrap_applies_plan_and_page_reloads_bootstrapped(tmp_path: Path):
    project_dir = tmp_path / "blank"
    project_dir.mkdir()
    with _client_for_project(tmp_path, project_dir) as client:
        r = client.post("/projects/p1/bootstrap", follow_redirects=True)

    assert r.status_code == 200
    assert "bootstrapped" in r.text
    assert not build_plan(project_dir).pending_operations
    assert (project_dir / ".harbor" / "plugins" / PLUGIN_NAME / "plugin.toml").is_file()
    assert (project_dir / "harbor.yml").is_file()
    assert (project_dir / ".harbor" / "runtime-target.json").is_file()
    for name in _skill_names():
        assert (project_dir / ".claude" / "skills" / name / "SKILL.md").is_file()
        assert (project_dir / ".codex" / "skills" / f"{name}.md").is_file()
        assert (project_dir / ".harbor" / "skills" / name / "SKILL.md").is_file()


def test_second_visit_shows_bootstrapped_or_stale_when_project_files_change(tmp_path: Path):
    project_dir = tmp_path / "blank"
    project_dir.mkdir()
    with _client_for_project(tmp_path, project_dir) as client:
        first = client.post("/projects/p1/bootstrap", follow_redirects=True)
        changed = project_dir / ".claude" / "skills" / _skill_names()[0] / "SKILL.md"
        changed.write_text("local override\n", encoding="utf-8")
        second = client.get("/projects/p1")

    assert first.status_code == 200
    assert "bootstrapped" in first.text
    assert second.status_code == 200
    assert "stale" in second.text


def test_post_track_prompt_appears_for_new_unbootstrapped_project(
    tmp_path: Path,
    monkeypatch,
):
    fake_config = tmp_path / "harbor-config"
    project_dir = tmp_path / "new-project"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )
    with TestClient(app) as client:
        r = client.post(
            "/projects/init",
            data={"project_path": str(project_dir), "project_name": "New Project"},
            follow_redirects=True,
        )

    assert r.status_code == 200
    assert "Set this project up now?" in r.text
    assert "not bootstrapped" in r.text
