"""Tests for harbor.agtx_client.

The two interesting things to verify:

1. **Path-hash parity with agtx.** agtx derives the per-project DB filename by
   SHA-256-hashing the project path string and taking the first 8 bytes as 16
   hex chars (D:/Projects/agtx/src/db/schema.rs:61). If our hash diverges by a
   single byte, we open the wrong file and silently lose all task state. So we
   pin both a hand-computed expected hex value AND the algorithm.

2. **CRUD against an in-memory SQLite using Harbor's schema.**
   `init_test_db` builds a DB with Harbor's column layout so we can exercise
   list/get/update/claim/mark without touching disk.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pytest

from harbor.agtx_client import (
    AgtxDb,
    Task,
    TransitionRequest,
    harbor_data_dir,
    hash_project_path,
    init_test_db,
    insert_test_task,
    project_db_path,
)


# ---- path-hash parity -----------------------------------------------------


def test_hash_project_path_matches_sha256_first_8_bytes():
    """Algorithmic invariant: the agtx Rust implementation truncates SHA-256 to 8 bytes."""
    paths = [
        "D:/Projects/harbor",
        "/home/user/work/repo",
        "C:\\Users\\Admin\\code\\app",
        "",  # edge case
        "a" * 1024,  # long path
    ]
    for p in paths:
        expected = hashlib.sha256(p.encode()).digest()[:8].hex()
        assert hash_project_path(p) == expected, f"divergence for {p!r}"


def test_hash_project_path_known_vectors():
    """Pin a couple of values so a future refactor can't silently break parity."""
    # sha256("test").digest()[:8].hex() == "9f86d081884c7d65"
    assert hash_project_path("test") == "9f86d081884c7d65"
    # sha256("").digest()[:8].hex() == "e3b0c44298fc1c14"
    assert hash_project_path("") == "e3b0c44298fc1c14"


def test_project_db_path_under_config_dir():
    """The DB path should always be `<config_dir>/projects/<hash>.db`."""
    p = project_db_path("/some/path")
    assert p.parent.name == "projects"
    assert p.name == f"{hash_project_path('/some/path')}.db"
    assert p.parent.parent == harbor_data_dir()


# ---- resolution via global index.db ---------------------------------------


def test_resolve_via_global_index_uses_canonical_path(tmp_path, monkeypatch):
    r"""When the global index.db has the project under a `\\?\` prefixed path,
    we should hash THAT string — otherwise the hash misses agtx's DB."""
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    (fake_config / "projects").mkdir(parents=True)
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    canonical = "\\\\?\\D:\\Projects\\harbor"
    user_input = "D:\\Projects\\harbor"

    # Seed a global index.db with the canonical path
    gdb = fake_config / "index.db"
    conn = sqlite3.connect(str(gdb))
    ac.init_test_db(conn, kind="global")
    conn.execute(
        "INSERT INTO projects (id, name, path, last_opened) VALUES (?, ?, ?, ?)",
        ("p1", "harbor", canonical, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Resolution should pick the canonical path's hash, not the user's literal
    db_path, found_canonical = ac.resolve_project_db_path(user_input)
    assert found_canonical == canonical
    expected_hash = ac.hash_project_path(canonical)
    assert db_path.name == f"{expected_hash}.db"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path casing behavior")
def test_resolve_via_global_index_matches_windows_paths_case_insensitively(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    (fake_config / "projects").mkdir(parents=True)
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    stored = "\\\\?\\C:\\Users\\Admin\\Repo"
    user_input = "c:\\users\\admin\\repo"

    gdb = fake_config / "index.db"
    conn = sqlite3.connect(str(gdb))
    ac.init_test_db(conn, kind="global")
    conn.execute(
        "INSERT INTO projects (id, name, path, last_opened) VALUES (?, ?, ?, ?)",
        ("p1", "repo", stored, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    db_path, found_canonical = ac.resolve_project_db_path(user_input)
    assert found_canonical == stored
    assert db_path.name == f"{ac.hash_project_path(stored)}.db"


def test_resolve_falls_back_to_literal_when_not_in_index(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    (fake_config / "projects").mkdir(parents=True)
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)
    # No index.db — fallback path
    db_path, found = ac.resolve_project_db_path("/orphan")
    assert found is None
    assert db_path.name == f"{ac.hash_project_path('/orphan')}.db"


def test_list_registered_projects_returns_rows(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    fake_config.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    gdb = fake_config / "index.db"
    conn = sqlite3.connect(str(gdb))
    ac.init_test_db(conn, kind="global")
    conn.execute(
        "INSERT INTO projects (id, name, path, last_opened) VALUES (?, ?, ?, ?)",
        ("p1", "alpha", "/a", "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, path, last_opened) VALUES (?, ?, ?, ?)",
        ("p2", "beta", "/b", "2026-01-02T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    rows = ac.list_registered_projects()
    assert rows == [("alpha", "/a"), ("beta", "/b")]


def test_list_registered_projects_empty_when_no_index(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    monkeypatch.setattr(ac, "harbor_data_dir", lambda: tmp_path / "missing")
    assert ac.list_registered_projects() == []


def test_register_project_creates_global_row_and_project_db(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    db = ac.AgtxDb(project_db_p=None, global_db_p=ac.global_db_path())  # type: ignore[arg-type]
    project = db.register_project(project_dir, name="Repo")

    assert project.name == "Repo"
    rows = db.list_projects()
    assert len(rows) == 1
    assert rows[0].id == project.id
    assert rows[0].path == project.path

    project_db = fake_config / "projects" / f"{ac.hash_project_path(project.path)}.db"
    assert project_db.exists()
    assert ac.AgtxDb(project_db_p=project_db).is_initialized() is True


def test_register_project_is_idempotent_for_same_path(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    db = ac.AgtxDb(project_db_p=None, global_db_p=ac.global_db_path())  # type: ignore[arg-type]
    first = db.register_project(project_dir, name="First")
    second = db.register_project(project_dir, name="Second")

    rows = db.list_projects()
    assert len(rows) == 1
    assert first.id == second.id
    assert rows[0].name == "Second"


def test_delete_project_removes_row_and_project_db(tmp_path, monkeypatch):
    from harbor import agtx_client as ac

    fake_config = tmp_path / "agtx-config"
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.setattr(ac, "harbor_data_dir", lambda: fake_config)

    db = ac.AgtxDb(project_db_p=None, global_db_p=ac.global_db_path())  # type: ignore[arg-type]
    project = db.register_project(project_dir, name="Repo")
    project_db = fake_config / "projects" / f"{ac.hash_project_path(project.path)}.db"
    assert project_db.exists()

    assert db.delete_project(project.id) is True
    assert db.list_projects() == []
    assert not project_db.exists()

    # Idempotent / unknown id -> False, no error.
    assert db.delete_project(project.id) is False
    assert db.delete_project("no-such-id") is False


# ---- AgtxDb.is_initialized + missing-file safety --------------------------


def test_agtxdb_refuses_to_open_missing_file(tmp_path):
    db_path = tmp_path / "does-not-exist.db"
    db = AgtxDb(project_db_p=db_path)
    with pytest.raises(FileNotFoundError, match="Harbor project DB does not exist"):
        db.list_tasks()
    # And the file should NOT have been silently created
    assert not db_path.exists()


def test_agtxdb_is_initialized_false_when_missing(tmp_path):
    db_path = tmp_path / "does-not-exist.db"
    db = AgtxDb(project_db_p=db_path)
    assert db.is_initialized() is False


def test_agtxdb_is_initialized_false_for_empty_db(tmp_path):
    """Edge case: file exists but no tables — sqlite3.connect-created stub."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()
    db = AgtxDb(project_db_p=db_path)
    assert db.is_initialized() is False


def test_agtxdb_is_initialized_true_after_schema(memdb: AgtxDb):
    assert memdb.is_initialized() is True


# ---- notifications --------------------------------------------------------


def test_list_notifications_empty(memdb: AgtxDb):
    assert memdb.list_notifications() == []


def test_list_notifications_newest_first(memdb: AgtxDb):
    conn = memdb._connect_project()
    base = datetime.now(timezone.utc)
    rows = [
        ("n1", "first", (base.replace(microsecond=100000)).isoformat().replace("+00:00", "+00:00")),
        ("n2", "second", (base.replace(microsecond=200000)).isoformat().replace("+00:00", "+00:00")),
        ("n3", "third", (base.replace(microsecond=300000)).isoformat().replace("+00:00", "+00:00")),
    ]
    for row in rows:
        conn.execute(
            "INSERT INTO notifications (id, message, created_at) VALUES (?, ?, ?)", row,
        )
    out = memdb.list_notifications()
    assert [n.message for n in out] == ["third", "second", "first"]


def test_list_notifications_respects_limit(memdb: AgtxDb):
    conn = memdb._connect_project()
    for i in range(5):
        conn.execute(
            "INSERT INTO notifications (id, message, created_at) VALUES (?, ?, ?)",
            (f"n{i}", f"msg{i}", f"2026-01-0{i+1}T00:00:00+00:00"),
        )
    assert len(memdb.list_notifications(limit=2)) == 2


# ---- in-memory CRUD -------------------------------------------------------


@pytest.fixture
def memdb() -> AgtxDb:
    conn = sqlite3.connect(":memory:")
    init_test_db(conn, kind="project")
    return AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]


def _make_task(
    *, id: str = "t1", title: str = "do thing", status: str = "backlog",
    project_id: str = "p1", agent: str = "claude",
) -> Task:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")
    return Task(
        id=id, title=title, description=None, status=status, agent=agent,
        project_id=project_id, created_at=now, updated_at=now,
    )


def test_list_and_get_task(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1", title="A"))
    insert_test_task(memdb._connect_project(), _make_task(id="t2", title="B", status="planning"))

    tasks = memdb.list_tasks()
    assert {t.id for t in tasks} == {"t1", "t2"}

    only_planning = memdb.list_tasks(status="planning")
    assert len(only_planning) == 1
    assert only_planning[0].id == "t2"

    one = memdb.get_task("t1")
    assert one is not None
    assert one.title == "A"

    none = memdb.get_task("missing")
    assert none is None


def test_update_task_whitelist(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    memdb.update_task("t1", status="planning", session_name="task-aaaa--p1--do")
    t = memdb.get_task("t1")
    assert t.status == "planning"
    assert t.session_name == "task-aaaa--p1--do"


def test_update_task_rejects_non_whitelisted_columns(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    with pytest.raises(ValueError, match="non-whitelisted"):
        memdb.update_task("t1", id="other", project_id="hijack")


def test_update_task_rejects_invalid_status(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    with pytest.raises(ValueError, match="invalid status"):
        memdb.update_task("t1", status="frobbed")


# ---- transition_requests --------------------------------------------------


def test_create_pending_claim_mark_round_trip(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))

    req_id = memdb.create_transition_request(task_id="t1", action="move_forward")
    pending = memdb.pending_transition_requests()
    assert len(pending) == 1
    assert pending[0].id == req_id
    assert pending[0].action == "move_forward"
    assert pending[0].claimed_by is None
    assert pending[0].processed_at is None

    # First claim wins.
    assert memdb.claim_transition_request(req_id, "harbor-A") is True
    # Second claim by anyone else loses.
    assert memdb.claim_transition_request(req_id, "harbor-B") is False
    # Pending list now empty (claimed but not processed).
    assert memdb.pending_transition_requests() == []

    memdb.mark_transition_processed(req_id, error=None)
    recent = memdb.recent_transition_requests("t1")
    assert len(recent) == 1
    assert recent[0].processed_at is not None
    assert recent[0].error is None


def test_mark_transition_processed_with_error(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    req_id = memdb.create_transition_request(task_id="t1", action="move_forward")
    memdb.claim_transition_request(req_id, "harbor")
    memdb.mark_transition_processed(req_id, error="boom")
    recent = memdb.recent_transition_requests("t1")
    assert recent[0].error == "boom"


def test_recent_transition_requests_orders_newest_first(memdb: AgtxDb):
    insert_test_task(memdb._connect_project(), _make_task(id="t1"))
    ids = []
    for i in range(3):
        # Force distinct timestamps so the ORDER BY is deterministic — agtx writes
        # microsecond-precision RFC3339 strings, but on Windows clock resolution can
        # round multiple sub-microsecond inserts to the same value.
        time.sleep(0.001)
        ids.append(memdb.create_transition_request(task_id="t1", action="move_forward"))
    recent = memdb.recent_transition_requests("t1")
    assert [r.id for r in recent] == list(reversed(ids))


# ---- referenced_tasks parsing ---------------------------------------------


def test_task_referenced_task_ids_parses_csv():
    t = _make_task()
    t.referenced_tasks = "abc, def , , ghi"
    assert t.referenced_task_ids == ["abc", "def", "ghi"]


def test_task_referenced_task_ids_empty():
    t = _make_task()
    assert t.referenced_task_ids == []
    t.referenced_tasks = ""
    assert t.referenced_task_ids == []
