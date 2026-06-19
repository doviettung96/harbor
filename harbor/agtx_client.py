"""SQLite client for Harbor's per-project task database.

Harbor stores each project's kanban state in a SQLite file under its own config
directory. Older installs borrowed agtx's config directory; startup migration
copies that legacy data into Harbor's data dir and leaves the agtx source files
untouched.

The path layout:
- Config dir: `%APPDATA%\\harbor\\config` on Windows,
  `~/Library/Application Support/harbor` on macOS, and
  `$XDG_CONFIG_HOME/harbor` or `~/.config/harbor` on Linux.
- Per-project DB: `<config_dir>/projects/<sha256_8>.db`, where `<sha256_8>` is
  the first 8 bytes of SHA-256(project_path_str) rendered as 16 hex chars.
- Global index DB: `<config_dir>/index.db` holds the `projects` and
  `running_agents` tables.

Harbor owns the schema in this module. We only update task columns listed in
`ALLOWED_TASK_UPDATE_COLUMNS`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Whitelist for `update_task` — anything else is rejected to keep us from
# corrupting fields agtx considers immutable (created_at, project_id, id).
ALLOWED_TASK_UPDATE_COLUMNS = frozenset({
    "status",
    "agent",
    "session_name",
    "worktree_path",
    "branch_name",
    "pr_number",
    "pr_url",
    "plugin",
    "cycle",
    "referenced_tasks",
    "escalation_note",
    "base_branch",
    "title",
    "description",
})

VALID_STATUSES = ("backlog", "planning", "running", "review", "done")


# ---- Path resolution ------------------------------------------------------


def agtx_config_dir() -> Path:
    """Return the legacy agtx config directory for migration source reads.

    Mirrors `directories::ProjectDirs::from("","","agtx").config_dir()`:
      - Windows: `%APPDATA%\\agtx\\config`
      - macOS: `~/Library/Application Support/agtx`
      - Linux: `$XDG_CONFIG_HOME/agtx` or `~/.config/agtx`
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "agtx" / "config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agtx"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "agtx"


def harbor_data_dir() -> Path:
    """Return Harbor's owned data directory."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "harbor" / "config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "harbor"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "harbor"


def hash_project_path(project_path_str: str) -> str:
    """Re-implement agtx's `hash_path`: SHA-256 truncated to first 8 bytes, hex.

    Source: D:/Projects/agtx/src/db/schema.rs:61
        let mut hasher = Sha256::new();
        hasher.update(path.as_bytes());
        let result = hasher.finalize();
        format!("{:016x}", u64::from_be_bytes(result[..8].try_into().unwrap()))
    """
    digest = hashlib.sha256(project_path_str.encode()).digest()
    return digest[:8].hex()


def project_db_path(project_path: str | Path) -> Path:
    """Path to Harbor's SQLite for `project_path` using the literal string supplied.

    Note: this does NOT consult Harbor's global index.db. On Windows the
    original agtx path canonicalization used Rust's `std::fs::canonicalize`,
    which returns
    a `\\\\?\\` extended-length form — so the literal hash often won't match
    what the stored project row uses. Use `resolve_project_db_path()` instead for the
    end-to-end lookup; this function is kept for tests and as a fallback.
    """
    s = str(project_path) if isinstance(project_path, str) else str(project_path)
    return harbor_data_dir() / "projects" / f"{hash_project_path(s)}.db"


def _windows_path_variants(p: Path) -> list[str]:
    """Return likely path strings agtx may have hashed on Windows.

    agtx (Rust) on Windows canonicalizes paths through `std::fs::canonicalize`,
    which returns a UNC `\\\\?\\` extended-length path. The bare path the user
    typed at the shell typically doesn't have this prefix. We try both forms,
    plus a couple of casing/separator variants, so the lookup survives the
    most common ways a project ends up registered.
    """
    s = str(p)
    out: list[str] = [s]
    # Backslash <-> forward-slash flip
    if "/" in s:
        out.append(s.replace("/", "\\"))
    if "\\" in s:
        out.append(s.replace("\\", "/"))
    # Add `\\?\` extended-length prefix (and remove it) for each variant
    extra: list[str] = []
    for v in out:
        if v.startswith("\\\\?\\"):
            extra.append(v[4:])
        else:
            extra.append("\\\\?\\" + v.lstrip("\\/"))
    out.extend(extra)
    # De-dupe while preserving order (first match wins)
    seen: set[str] = set()
    unique: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def resolve_project_db_path(project_path: str | Path) -> tuple[Path, str | None]:
    """Find the per-project SQLite Harbor uses for `project_path`.

    Strategy:
      1. Look up Harbor's global `index.db` `projects` table for any row whose
         `path` matches `project_path` (or one of its common Windows variants).
         If found, use the *stored* path string for hashing.
      2. Otherwise fall back to hashing the input path verbatim.

    Returns (db_path, canonical_path_str_or_None). If `canonical_path_str` is
    None, the project is NOT registered in Harbor's global index — caller may
    want to surface that to the user.
    """
    # Keep the literal input string for the fallback hash so behavior is
    # predictable when the project isn't in Harbor's index. We still construct
    # a Path for the variant generator.
    input_str = str(project_path)
    input_path_obj = project_path if isinstance(project_path, Path) else Path(input_str)
    candidates = _windows_path_variants(input_path_obj)
    candidate_set = {c for c in candidates}
    # Always include the raw input string as a possible match.
    candidate_set.add(input_str)
    candidate_folded = {c.casefold() for c in candidate_set}

    gdb = global_db_path()
    if gdb.exists():
        try:
            conn = sqlite3.connect(str(gdb), timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT path FROM projects").fetchall()
            finally:
                conn.close()
            for row in rows:
                stored = row["path"]
                if stored in candidate_set or (
                    sys.platform == "win32" and stored.casefold() in candidate_folded
                ):
                    return harbor_data_dir() / "projects" / f"{hash_project_path(stored)}.db", stored
        except sqlite3.Error:
            pass

    # Fallback — caller will probably get a "schema not found" error and we
    # surface that with a helpful message. Hash the literal input string for
    # consistency with how `project_db_path()` behaves on its own.
    return project_db_path(input_str), None


def list_registered_projects() -> list[tuple[str, str]]:
    """Return [(name, path), ...] from Harbor's global index.db. Empty if absent."""
    gdb = global_db_path()
    if not gdb.exists():
        return []
    try:
        conn = sqlite3.connect(str(gdb), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT name, path FROM projects ORDER BY name").fetchall()
            return [(r["name"], r["path"]) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def global_db_path() -> Path:
    """Path to Harbor's global index.db (projects + running_agents)."""
    return harbor_data_dir() / "index.db"


def canonical_project_path_str(project_path: str | Path) -> str:
    r"""Return the path string agtx would normally store for a project.

    agtx canonicalizes the path before hashing/registering. On Windows, Rust's
    canonicalize commonly yields an extended-length `\\?\` path, so we store
    that form for new Harbor-registered projects to keep DB filenames aligned.
    """
    resolved = Path(project_path).resolve(strict=True)
    raw = str(resolved)
    if sys.platform == "win32" and not raw.startswith("\\\\?\\"):
        return "\\\\?\\" + raw.lstrip("\\/")
    return raw


def strip_extended_length_prefix(path: str | Path) -> Path:
    r"""Return `path` with any Windows extended-length `\\?\` prefix removed.

    Harbor stores project paths in agtx's canonical `\\?\`-prefixed form (see
    `canonical_project_path_str`) so the per-project DB hash lines up with
    agtx's Rust canonicalization. That form is fine for hashing but poisons
    anything that shells out: `git worktree add`, tmux's session cwd, and the
    `cd` inside the pane launcher all reject it. Use this whenever a stored
    project/worktree path is about to be handed to git, tmux, or a shell.
    """
    s = str(path)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return Path(s)


# ---- Migration ------------------------------------------------------------


@dataclass(frozen=True)
class MigrationReport:
    """Summary of a legacy agtx -> Harbor data migration run."""

    copied_global_db: bool = False
    copied_project_dbs: tuple[str, ...] = ()
    rehashed_project_dbs: tuple[str, ...] = ()
    renamed_project_dirs: tuple[str, ...] = ()
    hash_stable: bool | None = None
    hash_notes: tuple[str, ...] = ()

    @property
    def operations(self) -> tuple[str, ...]:
        ops: list[str] = []
        if self.copied_global_db:
            ops.append("copied index.db")
        ops.extend(f"copied projects/{name}" for name in self.copied_project_dbs)
        ops.extend(f"rehashed projects/{name}" for name in self.rehashed_project_dbs)
        ops.extend(f"renamed {path}" for path in self.renamed_project_dirs)
        return tuple(ops)

    @property
    def empty(self) -> bool:
        return not self.operations


def ensure_harbor_data_migrated() -> MigrationReport:
    """Copy legacy agtx data into Harbor's data dir once.

    The agtx config directory is treated as read-only. Existing Harbor data wins:
    once Harbor has an `index.db`, later launches are no-ops.
    """
    source = agtx_config_dir()
    dest = harbor_data_dir()
    source_index = source / "index.db"
    dest_index = dest / "index.db"
    if not source_index.exists():
        return MigrationReport()
    # Treat an existing-but-empty Harbor index (0 projects) as not-yet-migrated.
    # An empty index.db can be created by an earlier launch before any legacy
    # agtx data was importable; guarding on mere existence would then wedge
    # migration off permanently and Harbor would show zero projects.
    if dest_index.exists() and _read_project_paths(dest_index):
        return MigrationReport()

    dest_projects = dest / "projects"
    dest_projects.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_index, dest_index)
    copied_global_db = True

    copied_project_dbs: list[str] = []
    source_projects = source / "projects"
    if source_projects.is_dir():
        for source_db in sorted(source_projects.glob("*.db"), key=lambda p: p.name):
            dest_db = dest_projects / source_db.name
            if not dest_db.exists():
                shutil.copy2(source_db, dest_db)
                copied_project_dbs.append(source_db.name)

    project_paths = _read_project_paths(dest_index)
    rehashed_project_dbs, hash_notes = _ensure_project_db_hashes(
        project_paths,
        source_projects=source_projects,
        dest_projects=dest_projects,
    )
    renamed_project_dirs = _rename_project_metadata_dirs(project_paths)

    return MigrationReport(
        copied_global_db=copied_global_db,
        copied_project_dbs=tuple(copied_project_dbs),
        rehashed_project_dbs=tuple(rehashed_project_dbs),
        renamed_project_dirs=tuple(renamed_project_dirs),
        hash_stable=(
            all(": stable " in note for note in hash_notes)
            if hash_notes else None
        ),
        hash_notes=tuple(hash_notes),
    )


def _read_project_paths(index_db: Path) -> tuple[str, ...]:
    if not index_db.exists():
        return ()
    try:
        conn = sqlite3.connect(str(index_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT path FROM projects ORDER BY name").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return ()
    return tuple(str(row["path"]) for row in rows)


def _ensure_project_db_hashes(
    project_paths: Iterable[str],
    *,
    source_projects: Path,
    dest_projects: Path,
) -> tuple[list[str], list[str]]:
    rehashed: list[str] = []
    notes: list[str] = []
    for project_path in project_paths:
        expected_name = f"{hash_project_path(project_path)}.db"
        expected_dest = dest_projects / expected_name
        if expected_dest.exists():
            notes.append(f"{project_path}: stable {expected_name}")
            continue

        candidate_names = _legacy_metadata_hash_candidates(project_path)
        source_candidate = next(
            (
                source_projects / name
                for name in candidate_names
                if (source_projects / name).exists()
            ),
            None,
        )
        if source_candidate is None:
            notes.append(f"{project_path}: missing expected {expected_name}")
            continue

        shutil.copy2(source_candidate, expected_dest)
        rehashed.append(expected_name)
        notes.append(
            f"{project_path}: rehashed {source_candidate.name} to {expected_name}"
        )
    return rehashed, notes


def _legacy_metadata_hash_candidates(project_path: str) -> tuple[str, ...]:
    base = strip_extended_length_prefix(project_path)
    candidates = [
        str(base / ".agtx"),
        str(base / ".harbor"),
        str(Path(project_path) / ".agtx"),
        str(Path(project_path) / ".harbor"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = f"{hash_project_path(candidate)}.db"
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _rename_project_metadata_dirs(project_paths: Iterable[str]) -> list[str]:
    renamed: list[str] = []
    for project_path in project_paths:
        root = strip_extended_length_prefix(project_path)
        old = root / ".agtx"
        new = root / ".harbor"
        if not old.is_dir() or new.exists():
            continue
        old.rename(new)
        renamed.append(str(new))
    return renamed


# ---- Dataclasses ----------------------------------------------------------


@dataclass
class TaskDependency:
    id: str
    title: str
    status: str

    @property
    def short_id(self) -> str:
        return self.id[:8]


@dataclass
class Task:
    id: str
    title: str
    description: str | None
    status: str  # one of VALID_STATUSES
    agent: str
    project_id: str
    session_name: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    plugin: str | None = None
    cycle: int = 1
    referenced_tasks: str | None = None
    escalation_note: str | None = None
    base_branch: str | None = None
    created_at: str = ""  # RFC 3339
    updated_at: str = ""
    dependencies: list[TaskDependency] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def referenced_task_ids(self) -> list[str]:
        if not self.referenced_tasks:
            return []
        return [s for s in (p.strip() for p in self.referenced_tasks.split(",")) if s]

    @property
    def blocking_dependencies(self) -> list[TaskDependency]:
        return [dep for dep in self.dependencies if dep.status != "done"]

    @property
    def deps_satisfied(self) -> bool:
        return not self.blocking_dependencies

    def content_text(self) -> str:
        return self.description if self.description else self.title


@dataclass
class Project:
    id: str
    name: str
    path: str
    github_url: str | None = None
    default_agent: str | None = None
    last_opened: str = ""


@dataclass
class Notification:
    """One row from agtx's `notifications` table — short status messages
    agtx writes for the user/orchestrator. We surface them in the webui's
    dashboard panel so users see the same flash messages the TUI would."""
    id: str
    message: str
    created_at: str


@dataclass
class TransitionRequest:
    id: str
    task_id: str
    action: str
    reason: str | None
    requested_at: str
    processed_at: str | None
    error: str | None
    claimed_by: str | None = None


@dataclass
class ResourcePermit:
    """One leasable unit of the global runtime-resource pool (supply).

    A permit is `free` until an agent acquires it for a task (`held`). For an
    `instance` resource each instance is one permit carrying its own
    `target_json` (the runtime-target subobject written into the task worktree
    on acquire). For a `counted` resource the capacity is expanded into N
    anonymous permits (no `instance_name`, no `target_json`) — acquiring `n`
    units holds `n` of them.

    The table is GLOBAL (`index.db`) so a physical resource cannot be
    double-booked across the projects running in parallel.
    """
    permit_id: str
    kind: str
    instance_name: str | None
    target_json: str | None
    task_id: str | None
    project_id: str | None
    state: str
    label: str | None
    leased_at: str | None
    released_at: str | None


@dataclass
class ResourceWaiter:
    """One parked task waiting for `n` free permits of `kind` (demand queue).

    Ordered by `enqueued_at` (FIFO). The supervisor's grant pass pops the head
    waiter for a kind when permits free up, acquires for it, writes the worktree
    override, and wakes the parked agent via tmux. `session_name` is the wake
    target.
    """
    waiter_id: str
    task_id: str
    project_id: str
    kind: str
    n: int
    session_name: str | None
    enqueued_at: str


# ---- DB wrapper -----------------------------------------------------------


def _now_rfc3339() -> str:
    """RFC 3339 UTC timestamp, matching agtx's `chrono::Utc::now().to_rfc3339()`."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "+00:00")


@dataclass
class AgtxDb:
    """Connection pair: one against the per-project DB, one against the global index.

    `project_db_p` is opened lazily and re-opened per call to keep the connection
    short-lived (SQLite + WAL handles concurrent readers/writers fine; agtx may
    also be reading/writing). For tests we accept a pre-built sqlite3.Connection
    via `connection` (in which case `project_db_p` is ignored).
    """

    project_db_p: Path
    global_db_p: Path | None = None
    connection: sqlite3.Connection | None = None  # for tests

    def _connect_project(self) -> sqlite3.Connection:
        if self.connection is not None:
            return self.connection
        # CRITICAL: refuse to open a non-existent file. sqlite3.connect
        # silently creates an empty DB file, and we'd then run queries against
        # missing tables forever. Better to fail loudly so the caller can
        # explain the situation to the user.
        if not self.project_db_p.exists():
            raise FileNotFoundError(
                f"Harbor project DB does not exist: {self.project_db_p}. "
                "Harbor has not initialized this project yet, or the project path "
                "doesn't match what Harbor registered. Run `python -m harbor webui-diagnose` "
                "for available projects."
            )
        conn = sqlite3.connect(str(self.project_db_p), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Do not create missing DBs here. Callers that initialize projects go
        # through _open_project_create(); read paths should fail if the file is absent.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def is_initialized(self) -> bool:
        """True iff the per-project DB exists AND has Harbor's required tables.

        Returns False instead of raising so the webui startup can render a
        helpful error page rather than crashing on import.
        """
        if self.connection is None and not self.project_db_p.exists():
            return False
        try:
            conn = self._connect_project()
        except FileNotFoundError:
            return False
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('tasks', 'transition_requests')"
            ).fetchall()
            names = {r["name"] for r in rows}
            return {"tasks", "transition_requests"}.issubset(names)
        except sqlite3.Error:
            return False

    def _connect_global(self) -> sqlite3.Connection | None:
        path = self.global_db_p
        if path is None or not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _open_global_create(self) -> sqlite3.Connection:
        path = self.global_db_p or global_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_GLOBAL_SCHEMA_SQL)
        return conn

    @staticmethod
    def _open_project_create(db_path: Path) -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_PROJECT_SCHEMA_SQL)
        return conn

    # ---- Tasks ----

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[Task]:
        sql = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(f"unknown status {status!r}; want one of {VALID_STATUSES}")
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        conn = self._connect_project()
        rows = conn.execute(sql, params).fetchall()
        tasks = [_task_from_row(r) for r in rows]
        self._resolve_task_dependencies(conn, tasks)
        return tasks

    def get_task(self, task_id: str) -> Task | None:
        conn = self._connect_project()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        task = _task_from_row(row)
        self._resolve_task_dependencies(conn, [task])
        return task

    def find_task_by_title(
        self,
        title: str,
        *,
        project_id: str | None = None,
    ) -> Task | None:
        sql = "SELECT * FROM tasks WHERE title = ?"
        params: list[Any] = [title]
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at LIMIT 1"
        conn = self._connect_project()
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        task = _task_from_row(row)
        self._resolve_task_dependencies(conn, [task])
        return task

    def create_task(
        self,
        *,
        title: str,
        description: str,
        project_id: str,
        agent: str = "codex",
        status: str = "backlog",
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> Task:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        task_id = str(uuid.uuid4())
        now = _now_rfc3339()
        conn = self._connect_project()
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, agent, project_id, "
            "session_name, worktree_path, branch_name, pr_number, pr_url, plugin, "
            "cycle, referenced_tasks, escalation_note, base_branch, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, "
            "1, ?, NULL, ?, ?, ?)",
            (
                task_id,
                title,
                description,
                status,
                agent,
                project_id,
                plugin,
                referenced_tasks,
                base_branch,
                now,
                now,
            ),
        )
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"failed to create task {task_id}")
        return task

    def create_task_if_title_missing(
        self,
        *,
        title: str,
        description: str,
        project_id: str,
        agent: str = "codex",
        status: str = "backlog",
        plugin: str | None = None,
        referenced_tasks: str | None = None,
        base_branch: str | None = None,
    ) -> tuple[Task, bool]:
        existing = self.find_task_by_title(title, project_id=project_id)
        if existing is not None:
            return existing, False
        created = self.create_task(
            title=title,
            description=description,
            project_id=project_id,
            agent=agent,
            status=status,
            plugin=plugin,
            referenced_tasks=referenced_tasks,
            base_branch=base_branch,
        )
        return created, True

    @staticmethod
    def _resolve_task_dependencies(conn: sqlite3.Connection, tasks: list[Task]) -> None:
        dep_ids: list[str] = []
        seen: set[str] = set()
        for task in tasks:
            for dep_id in task.referenced_task_ids:
                if dep_id not in seen:
                    dep_ids.append(dep_id)
                    seen.add(dep_id)
        if not dep_ids:
            return

        placeholders = ", ".join("?" for _ in dep_ids)
        rows = conn.execute(
            f"SELECT id, title, status FROM tasks WHERE id IN ({placeholders})",
            dep_ids,
        ).fetchall()
        resolved = {
            row["id"]: TaskDependency(
                id=row["id"],
                title=row["title"],
                status=row["status"],
            )
            for row in rows
        }
        for task in tasks:
            task.dependencies = [
                resolved.get(
                    dep_id,
                    TaskDependency(id=dep_id, title="missing task", status="missing"),
                )
                for dep_id in task.referenced_task_ids
            ]

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - ALLOWED_TASK_UPDATE_COLUMNS
        if bad:
            raise ValueError(f"refusing to update non-whitelisted task columns: {sorted(bad)}")
        if "status" in fields and fields["status"] not in VALID_STATUSES:
            raise ValueError(f"invalid status: {fields['status']!r}")
        cols = sorted(fields.keys())
        set_clause = ", ".join(f"{c} = ?" for c in cols) + ", updated_at = ?"
        values = [fields[c] for c in cols] + [_now_rfc3339(), task_id]
        conn = self._connect_project()
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)

    # ---- Transition requests ----

    def pending_transition_requests(self) -> list[TransitionRequest]:
        conn = self._connect_project()
        rows = conn.execute(
            "SELECT * FROM transition_requests "
            "WHERE processed_at IS NULL AND claimed_by IS NULL "
            "ORDER BY requested_at ASC"
        ).fetchall()
        return [_tr_from_row(r) for r in rows]

    def count_unprocessed_transitions(self, task_id: str, action: str) -> int:
        """How many `action` requests for `task_id` are still in flight.

        Counts rows whose `processed_at IS NULL` (queued, or claimed but not yet
        finished). The orchestrator uses this so it won't reclaim a just-admitted
        slot while its `move_forward` is still being executed by the worker.
        """
        conn = self._connect_project()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM transition_requests "
            "WHERE task_id = ? AND action = ? AND processed_at IS NULL",
            (task_id, action),
        ).fetchone()
        return int(row["n"]) if row else 0

    def recent_transition_requests(
        self, task_id: str, *, limit: int = 10
    ) -> list[TransitionRequest]:
        conn = self._connect_project()
        rows = conn.execute(
            "SELECT * FROM transition_requests "
            "WHERE task_id = ? "
            "ORDER BY requested_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [_tr_from_row(r) for r in rows]

    def get_transition_request(self, req_id: str) -> TransitionRequest | None:
        conn = self._connect_project()
        row = conn.execute(
            "SELECT * FROM transition_requests WHERE id = ?",
            (req_id,),
        ).fetchone()
        return _tr_from_row(row) if row is not None else None

    def create_transition_request(
        self, *, task_id: str, action: str, reason: str | None = None
    ) -> str:
        """Queue a transition. Returns the new request id (UUID4)."""
        import uuid

        req_id = str(uuid.uuid4())
        conn = self._connect_project()
        conn.execute(
            "INSERT INTO transition_requests "
            "(id, task_id, action, reason, requested_at, processed_at, error) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            (req_id, task_id, action, reason, _now_rfc3339()),
        )
        return req_id

    def claim_transition_request(self, req_id: str, claimant: str) -> bool:
        """Atomic claim. Returns True if this caller won the race."""
        conn = self._connect_project()
        cur = conn.execute(
            "UPDATE transition_requests "
            "SET claimed_by = ? "
            "WHERE id = ? AND claimed_by IS NULL AND processed_at IS NULL",
            (claimant, req_id),
        )
        return cur.rowcount == 1

    def mark_transition_processed(self, req_id: str, error: str | None) -> None:
        conn = self._connect_project()
        conn.execute(
            "UPDATE transition_requests SET processed_at = ?, error = ? WHERE id = ?",
            (_now_rfc3339(), error, req_id),
        )

    def cleanup_old_transition_requests(self) -> None:
        """Match agtx's `cleanup_old_transition_requests` — 1h cutoff."""
        cutoff = (datetime.now(timezone.utc).timestamp() - 3600)
        cutoff_str = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "+00:00")
        conn = self._connect_project()
        conn.execute(
            "DELETE FROM transition_requests "
            "WHERE (processed_at IS NOT NULL AND processed_at < ?) "
            "OR (processed_at IS NULL AND claimed_by IS NOT NULL AND requested_at < ?)",
            (cutoff_str, cutoff_str),
        )

    # ---- Notifications ----

    def list_notifications(self, *, limit: int = 20) -> list[Notification]:
        """Most-recent-first notifications (matches what the agtx TUI shows)."""
        conn = self._connect_project()
        rows = conn.execute(
            "SELECT id, message, created_at FROM notifications "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Notification(id=r["id"], message=r["message"], created_at=r["created_at"])
            for r in rows
        ]

    def consume_notifications(self, *, limit: int = 20) -> list[Notification]:
        """Return and remove pending notifications.

        agtx's MCP endpoint is polling-only: callers ask for notifications and
        consumed rows disappear from the queue.
        """
        conn = self._connect_project()
        notifications = self.list_notifications(limit=limit)
        if notifications:
            conn.execute(
                f"DELETE FROM notifications WHERE id IN ({','.join('?' for _ in notifications)})",
                [n.id for n in notifications],
            )
        return notifications

    def delete_task(self, task_id: str) -> None:
        conn = self._connect_project()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    # ---- Projects (global index.db) ----

    def list_projects(self) -> list[Project]:
        conn = self._connect_global()
        if conn is None:
            return []
        rows = conn.execute("SELECT * FROM projects ORDER BY last_opened DESC").fetchall()
        return [_project_from_row(r) for r in rows]

    def find_project_id_by_path(self, project_path: str | Path) -> str | None:
        """Look up the agtx project_id for a path. Returns None if not registered."""
        conn = self._connect_global()
        if conn is None:
            return None
        target = str(project_path)
        row = conn.execute(
            "SELECT id FROM projects WHERE path = ?", (target,)
        ).fetchone()
        return row["id"] if row else None

    def register_project(
        self,
        project_path: str | Path,
        *,
        name: str | None = None,
        github_url: str | None = None,
        default_agent: str | None = None,
    ) -> Project:
        """Register a project in Harbor's global index and initialize its DB.

        Mirrors the TUI startup path: canonicalize the project path, upsert the
        global project row, then create/migrate the central per-project DB.
        """
        canonical = canonical_project_path_str(project_path)
        project_name = (name or Path(canonical[4:] if canonical.startswith("\\\\?\\") else canonical).name or "project").strip()
        now = _now_rfc3339()
        candidates = set(_windows_path_variants(Path(canonical[4:] if canonical.startswith("\\\\?\\") else canonical)))
        candidates.add(str(project_path))
        candidates.add(canonical)

        conn = self._open_global_create()
        try:
            existing = conn.execute(
                f"SELECT * FROM projects WHERE path IN ({','.join('?' for _ in candidates)})",
                tuple(candidates),
            ).fetchone()
            if existing is not None:
                project_id = existing["id"]
                stored_path = existing["path"]
                conn.execute(
                    "UPDATE projects SET name = ?, github_url = ?, default_agent = ?, "
                    "last_opened = ? WHERE id = ?",
                    (project_name, github_url, default_agent, now, project_id),
                )
            else:
                project_id = str(uuid.uuid4())
                stored_path = canonical
                conn.execute(
                    "INSERT INTO projects (id, name, path, github_url, default_agent, last_opened) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, project_name, stored_path, github_url, default_agent, now),
                )
        finally:
            conn.close()

        db_path = harbor_data_dir() / "projects" / f"{hash_project_path(stored_path)}.db"
        project_conn = self._open_project_create(db_path)
        project_conn.close()
        return Project(
            id=project_id,
            name=project_name,
            path=stored_path,
            github_url=github_url,
            default_agent=default_agent,
            last_opened=now,
        )

    def delete_project(self, project_id: str) -> bool:
        """Remove a project from Harbor's global index and drop its per-project DB.

        Untracks the project: deletes its `projects` row and unlinks the
        `projects/<hash>.db` file keyed off the row's stored path. Does NOT
        touch the project's files on disk. Returns True if a row was removed.
        """
        conn = self._open_global_create()
        try:
            row = conn.execute(
                "SELECT path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return False
            stored_path = row["path"]
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        finally:
            conn.close()
        db_path = harbor_data_dir() / "projects" / f"{hash_project_path(stored_path)}.db"
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass  # leaving an orphan db file is harmless; the row is what matters
        return True

    # ---- Resource pool: permits (supply) + waiters (demand) — global index.db
    #
    # GLOBAL because a permit maps to a physical resource (an emulator on a given
    # adb port, an app instance on a port, a GPU unit) that must not be
    # double-booked across the projects running in parallel. Every method opens
    # the global DB via `_open_global_create`, which runs `_GLOBAL_SCHEMA_SQL`
    # (CREATE TABLE IF NOT EXISTS), so the tables self-create on first use.

    def reconcile_resources(
        self, permits: list[tuple[str, str, str | None, str | None]]
    ) -> None:
        """Sync the permit table to the configured pool.

        `permits` is a list of (permit_id, kind, instance_name, target_json).
        Free rows are inserted for new permits and their kind/instance/target
        refreshed (config may have changed). Free rows whose permit is no longer
        configured are deleted. `held` rows are never touched — an in-use permit
        outlives a config edit until its task releases it.
        """
        conn = self._open_global_create()
        try:
            ids = [pid for pid, _, _, _ in permits]
            for pid, kind, instance_name, target_json in permits:
                conn.execute(
                    "INSERT OR IGNORE INTO resource_permits "
                    "(permit_id, kind, instance_name, target_json, task_id, project_id, "
                    " state, label, leased_at, released_at) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, 'free', NULL, NULL, NULL)",
                    (pid, kind, instance_name, target_json),
                )
                conn.execute(
                    "UPDATE resource_permits "
                    "SET kind = ?, instance_name = ?, target_json = ? "
                    "WHERE permit_id = ? AND state = 'free'",
                    (kind, instance_name, target_json, pid),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM resource_permits "
                    f"WHERE state = 'free' AND permit_id NOT IN ({placeholders})",
                    ids,
                )
            else:
                conn.execute("DELETE FROM resource_permits WHERE state = 'free'")
        finally:
            conn.close()

    def list_permits(self) -> list[ResourcePermit]:
        conn = self._open_global_create()
        try:
            rows = conn.execute(
                "SELECT * FROM resource_permits ORDER BY kind, permit_id"
            ).fetchall()
            return [_permit_from_row(r) for r in rows]
        finally:
            conn.close()

    def count_free_permits(self, kind: str) -> int:
        conn = self._open_global_create()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM resource_permits "
                "WHERE kind = ? AND state = 'free'",
                (kind,),
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()

    def held_permits_for_task(self, task_id: str) -> list[ResourcePermit]:
        conn = self._open_global_create()
        try:
            rows = conn.execute(
                "SELECT * FROM resource_permits "
                "WHERE task_id = ? AND state = 'held' ORDER BY kind, permit_id",
                (task_id,),
            ).fetchall()
            return [_permit_from_row(r) for r in rows]
        finally:
            conn.close()

    def acquire_permits(
        self, *, kind: str, n: int, task_id: str, project_id: str, label: str | None,
    ) -> list[ResourcePermit] | None:
        """Atomically hold `n` free permits of `kind` for a task (all-or-nothing).

        Returns the held permits, or None if fewer than `n` were free (in which
        case nothing is held). A single `BEGIN IMMEDIATE` transaction makes the
        select-then-update atomic, so two concurrent grantors cannot grab the
        same permit nor over-allocate a counted resource.
        """
        if n <= 0:
            return []
        conn = self._open_global_create()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT permit_id FROM resource_permits "
                "WHERE kind = ? AND state = 'free' ORDER BY permit_id LIMIT ?",
                (kind, n),
            ).fetchall()
            if len(rows) < n:
                conn.execute("ROLLBACK")
                return None
            now = _now_rfc3339()
            chosen = [r["permit_id"] for r in rows]
            for pid in chosen:
                conn.execute(
                    "UPDATE resource_permits "
                    "SET task_id = ?, project_id = ?, state = 'held', label = ?, "
                    "leased_at = ?, released_at = NULL "
                    "WHERE permit_id = ?",
                    (task_id, project_id, label, now, pid),
                )
            placeholders = ",".join("?" for _ in chosen)
            got = conn.execute(
                f"SELECT * FROM resource_permits WHERE permit_id IN ({placeholders})",
                chosen,
            ).fetchall()
            conn.execute("COMMIT")
            return [_permit_from_row(r) for r in got]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def release_permit(self, permit_id: str) -> None:
        """Free a single permit, clearing its task binding."""
        conn = self._open_global_create()
        try:
            conn.execute(
                "UPDATE resource_permits "
                "SET task_id = NULL, project_id = NULL, state = 'free', "
                "label = NULL, released_at = ? "
                "WHERE permit_id = ?",
                (_now_rfc3339(), permit_id),
            )
        finally:
            conn.close()

    def release_permits_for_task(self, task_id: str) -> int:
        """Free every permit held by a task. Returns how many were freed."""
        conn = self._open_global_create()
        try:
            cur = conn.execute(
                "UPDATE resource_permits "
                "SET task_id = NULL, project_id = NULL, state = 'free', "
                "label = NULL, released_at = ? "
                "WHERE task_id = ? AND state = 'held'",
                (_now_rfc3339(), task_id),
            )
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    # ---- waiters (FIFO demand queue) ----

    def enqueue_waiter(
        self, *, task_id: str, project_id: str, kind: str, n: int, session_name: str | None,
    ) -> ResourceWaiter:
        """Park a task waiting for `n` permits of `kind` (idempotent per task+kind).

        Re-enqueuing the same (task, kind) refreshes `n`/`session_name` and keeps
        the *original* enqueued_at so a retrying agent doesn't lose its place.
        """
        conn = self._open_global_create()
        try:
            existing = conn.execute(
                "SELECT * FROM resource_waiters WHERE task_id = ? AND kind = ?",
                (task_id, kind),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE resource_waiters SET n = ?, session_name = ?, project_id = ? "
                    "WHERE waiter_id = ?",
                    (n, session_name, project_id, existing["waiter_id"]),
                )
                row = conn.execute(
                    "SELECT * FROM resource_waiters WHERE waiter_id = ?",
                    (existing["waiter_id"],),
                ).fetchone()
                return _waiter_from_row(row)
            waiter_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO resource_waiters "
                "(waiter_id, task_id, project_id, kind, n, session_name, enqueued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (waiter_id, task_id, project_id, kind, n, session_name, _now_rfc3339()),
            )
            row = conn.execute(
                "SELECT * FROM resource_waiters WHERE waiter_id = ?", (waiter_id,)
            ).fetchone()
            return _waiter_from_row(row)
        finally:
            conn.close()

    def list_waiters(self) -> list[ResourceWaiter]:
        """All waiters in FIFO order (oldest park first)."""
        conn = self._open_global_create()
        try:
            rows = conn.execute(
                "SELECT * FROM resource_waiters ORDER BY enqueued_at, waiter_id"
            ).fetchall()
            return [_waiter_from_row(r) for r in rows]
        finally:
            conn.close()

    def waiter_position(self, task_id: str, kind: str) -> int:
        """1-based position of (task, kind) within its kind's FIFO queue (0 if absent)."""
        conn = self._open_global_create()
        try:
            rows = conn.execute(
                "SELECT task_id FROM resource_waiters WHERE kind = ? "
                "ORDER BY enqueued_at, waiter_id",
                (kind,),
            ).fetchall()
            for idx, r in enumerate(rows, start=1):
                if r["task_id"] == task_id:
                    return idx
            return 0
        finally:
            conn.close()

    def delete_waiter(self, waiter_id: str) -> None:
        conn = self._open_global_create()
        try:
            conn.execute("DELETE FROM resource_waiters WHERE waiter_id = ?", (waiter_id,))
        finally:
            conn.close()

    def delete_waiters_for_task(self, task_id: str) -> int:
        conn = self._open_global_create()
        try:
            cur = conn.execute(
                "DELETE FROM resource_waiters WHERE task_id = ?", (task_id,)
            )
            return int(cur.rowcount or 0)
        finally:
            conn.close()


# ---- Row decoders ---------------------------------------------------------


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """sqlite3.Row.__getitem__ raises IndexError if column missing.

    Harbor's schema has migration ALTERs — older DBs may not have all columns yet.
    This wrapper tolerates that.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=_row_get(row, "description"),
        status=row["status"],
        agent=row["agent"],
        project_id=row["project_id"],
        session_name=_row_get(row, "session_name"),
        worktree_path=_row_get(row, "worktree_path"),
        branch_name=_row_get(row, "branch_name"),
        pr_number=_coerce_int(_row_get(row, "pr_number")),
        pr_url=_row_get(row, "pr_url"),
        plugin=_row_get(row, "plugin"),
        cycle=int(_row_get(row, "cycle", 1) or 1),
        referenced_tasks=_row_get(row, "referenced_tasks"),
        escalation_note=_row_get(row, "escalation_note"),
        base_branch=_row_get(row, "base_branch"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        path=row["path"],
        github_url=_row_get(row, "github_url"),
        default_agent=_row_get(row, "default_agent"),
        last_opened=row["last_opened"],
    )


def _tr_from_row(row: sqlite3.Row) -> TransitionRequest:
    return TransitionRequest(
        id=row["id"],
        task_id=row["task_id"],
        action=row["action"],
        reason=_row_get(row, "reason"),
        requested_at=row["requested_at"],
        processed_at=_row_get(row, "processed_at"),
        error=_row_get(row, "error"),
        claimed_by=_row_get(row, "claimed_by"),
    )


def _permit_from_row(row: sqlite3.Row) -> ResourcePermit:
    return ResourcePermit(
        permit_id=row["permit_id"],
        kind=row["kind"],
        instance_name=_row_get(row, "instance_name"),
        target_json=_row_get(row, "target_json"),
        task_id=_row_get(row, "task_id"),
        project_id=_row_get(row, "project_id"),
        state=row["state"],
        label=_row_get(row, "label"),
        leased_at=_row_get(row, "leased_at"),
        released_at=_row_get(row, "released_at"),
    )


def _waiter_from_row(row: sqlite3.Row) -> ResourceWaiter:
    return ResourceWaiter(
        waiter_id=row["waiter_id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        kind=row["kind"],
        n=int(_row_get(row, "n", 1) or 1),
        session_name=_row_get(row, "session_name"),
        enqueued_at=row["enqueued_at"],
    )


# ---- Schema bootstrap helper (test-only) ----------------------------------

# Harbor-owned project schema. Tests also use it to build in-memory DBs.
_PROJECT_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'backlog',
    agent TEXT NOT NULL,
    project_id TEXT NOT NULL,
    session_name TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    plugin TEXT,
    cycle INTEGER NOT NULL DEFAULT 1,
    referenced_tasks TEXT,
    escalation_note TEXT,
    base_branch TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS transition_requests (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    requested_at TEXT NOT NULL,
    processed_at TEXT,
    error TEXT,
    claimed_by TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_GLOBAL_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    github_url TEXT,
    default_agent TEXT,
    last_opened TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS running_agents (
    session_name TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS resource_permits (
    permit_id     TEXT PRIMARY KEY,   -- "<kind>/<instance>" or "<kind>#<index>"; globally unique
    kind          TEXT NOT NULL,      -- resource kind (emulator, gpu_gb, ...)
    instance_name TEXT,               -- instance name (NULL for counted permits)
    target_json   TEXT,               -- runtime-target `target` subobject (NULL ⇒ no override)
    task_id       TEXT,               -- NULL when free
    project_id    TEXT,
    state         TEXT NOT NULL,      -- 'free' | 'held'
    label         TEXT,               -- task short-id / branch holding the permit
    leased_at     TEXT,
    released_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_permits_kind_state ON resource_permits(kind, state);

CREATE TABLE IF NOT EXISTS resource_waiters (
    waiter_id    TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- kind of permit awaited
    n            INTEGER NOT NULL DEFAULT 1,
    session_name TEXT,               -- tmux wake target
    enqueued_at  TEXT NOT NULL       -- park time → FIFO ordering
);
CREATE INDEX IF NOT EXISTS idx_waiters_kind ON resource_waiters(kind, enqueued_at);
"""


def init_test_db(conn: sqlite3.Connection, *, kind: str = "project") -> None:
    """Apply Harbor's schema to a fresh sqlite3 connection. Test use only."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if kind == "project":
        conn.executescript(_PROJECT_SCHEMA_SQL)
    elif kind == "global":
        conn.executescript(_GLOBAL_SCHEMA_SQL)
    else:
        raise ValueError(f"unknown kind {kind!r}; want 'project' or 'global'")


def insert_test_task(conn: sqlite3.Connection, task: Task) -> None:
    """Insert a task using agtx's column layout. Test use only."""
    conn.execute(
        "INSERT INTO tasks (id, title, description, status, agent, project_id, "
        "session_name, worktree_path, branch_name, pr_number, pr_url, plugin, "
        "cycle, referenced_tasks, escalation_note, base_branch, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task.id, task.title, task.description, task.status, task.agent,
            task.project_id, task.session_name, task.worktree_path,
            task.branch_name, task.pr_number, task.pr_url, task.plugin,
            task.cycle, task.referenced_tasks, task.escalation_note,
            task.base_branch,
            task.created_at or _now_rfc3339(),
            task.updated_at or _now_rfc3339(),
        ),
    )
