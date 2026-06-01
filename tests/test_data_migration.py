from __future__ import annotations

import sqlite3
from pathlib import Path

from harbor import agtx_client as ac
from harbor.agtx_client import AgtxDb, Task, init_test_db, insert_test_task
from harbor.webui.server import create_app


def _now() -> str:
    return "2026-01-01T00:00:00+00:00"


def _task(project_id: str, task_id: str) -> Task:
    return Task(
        id=task_id,
        title=f"task {task_id}",
        description=None,
        status="backlog",
        agent="codex",
        project_id=project_id,
        created_at=_now(),
        updated_at=_now(),
    )


def _seed_fake_agtx_config(root: Path) -> tuple[Path, Path, list[Path]]:
    agtx_config = root / "agtx-config-copy"
    projects_dir = agtx_config / "projects"
    projects_dir.mkdir(parents=True)

    project_a = root / "alpha"
    project_b = root / "beta"
    for project in (project_a, project_b):
        (project / ".agtx").mkdir(parents=True)
        (project / ".agtx" / "runtime-target.json").write_text("{}", encoding="utf-8")

    project_rows = [
        ("pa", "alpha", str(project_a), "2026-01-02T00:00:00+00:00"),
        ("pb", "beta", str(project_b), "2026-01-01T00:00:00+00:00"),
    ]

    index = agtx_config / "index.db"
    conn = sqlite3.connect(str(index))
    init_test_db(conn, kind="global")
    for project_id, name, path, last_opened in project_rows:
        conn.execute(
            "INSERT INTO projects (id, name, path, last_opened) VALUES (?, ?, ?, ?)",
            (project_id, name, path, last_opened),
        )
    conn.commit()
    conn.close()

    for project_id, _name, path, _last_opened in project_rows:
        db_path = projects_dir / f"{ac.hash_project_path(path)}.db"
        conn = sqlite3.connect(str(db_path))
        init_test_db(conn, kind="project")
        insert_test_task(conn, _task(project_id, f"{project_id}-task"))
        conn.commit()
        conn.close()

    return agtx_config, root / "harbor-config", [project_a, project_b]


def test_launch_migrates_agtx_copy_to_harbor_owned_data_dir(tmp_path, monkeypatch):
    agtx_config, harbor_config, project_dirs = _seed_fake_agtx_config(tmp_path)
    source_snapshot = {
        p.relative_to(agtx_config).as_posix(): p.read_bytes()
        for p in agtx_config.rglob("*.db")
    }
    monkeypatch.setattr(ac, "agtx_config_dir", lambda: agtx_config)
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: harbor_config)

    app = create_app(
        tmp_path,
        autostart_worker=False,
        runtime_config_path=tmp_path / "runtime.yml",
    )
    report = app.state.harbor_migration_report

    assert report is not None
    assert report.copied_global_db is True
    assert report.hash_stable is True
    assert report.hash_notes
    assert all("stable" in note for note in report.hash_notes)
    assert {Path(p).parent for p in report.renamed_project_dirs} == set(project_dirs)

    harbor_index = harbor_config / "index.db"
    assert harbor_index.is_file()
    projects = AgtxDb(
        project_db_p=harbor_config / "unused.db",
        global_db_p=harbor_index,
    ).list_projects()
    assert {project.name for project in projects} == {"alpha", "beta"}

    for project in projects:
        db_path = harbor_config / "projects" / f"{ac.hash_project_path(project.path)}.db"
        assert db_path.is_file()
        db = AgtxDb(project_db_p=db_path, global_db_p=harbor_index)
        assert db.is_initialized() is True
        assert db.list_tasks()[0].project_id == project.id

    for project_dir in project_dirs:
        assert not (project_dir / ".agtx").exists()
        assert (project_dir / ".harbor").is_dir()

    second = ac.ensure_harbor_data_migrated()
    assert second.empty is True
    assert second.operations == ()

    for rel, content in source_snapshot.items():
        source_db = agtx_config / rel
        assert source_db.is_file()
        assert source_db.read_bytes() == content
