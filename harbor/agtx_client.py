"""SQLite client for agtx's per-project task database.

agtx (https://github.com/fynnfluegge/agtx) stores each project's kanban state in
a SQLite file under its config directory. The webui needs to read and mutate
that file directly because, on Windows, the agtx ratatui TUI — which is the
process that normally executes transition_requests side effects — is unusable.

The path layout (mirrored from `D:/Projects/agtx/src/db/schema.rs`):
- Config dir: resolved via the same rules as Rust's `directories::ProjectDirs::from("","","agtx")`.
- Per-project DB: `<config_dir>/projects/<sha256_8>.db`, where `<sha256_8>` is
  the first 8 bytes of SHA-256(project_path_str) rendered as 16 hex chars.
- Global index DB: `<config_dir>/index.db` — holds the `projects` and
  `running_agents` tables.

The schema is whatever agtx wrote (we do NOT call CREATE TABLE; agtx owns
migration). We only read documented columns and write back the small set
listed in `ALLOWED_TASK_UPDATE_COLUMNS`.
"""
from __future__ import annotations

import hashlib
import os
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
    """Return the agtx config directory for the current OS.

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
    """Path to agtx's SQLite for `project_path` using the literal string supplied.

    Note: this does NOT consult agtx's global index.db. On Windows agtx
    canonicalizes paths through Rust's `std::fs::canonicalize`, which returns
    a `\\\\?\\` extended-length form — so the literal hash often won't match
    what agtx actually wrote. Use `resolve_project_db_path()` instead for the
    end-to-end lookup; this function is kept for tests and as a fallback.
    """
    s = str(project_path) if isinstance(project_path, str) else str(project_path)
    return agtx_config_dir() / "projects" / f"{hash_project_path(s)}.db"


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
    """Find the per-project SQLite agtx actually uses for `project_path`.

    Strategy:
      1. Look up agtx's global `index.db` `projects` table for any row whose
         `path` matches `project_path` (or one of its common Windows variants).
         If found, use the *stored* path string for hashing — that's the byte
         string agtx itself hashed.
      2. Otherwise fall back to hashing the input path verbatim.

    Returns (db_path, canonical_path_str_or_None). If `canonical_path_str` is
    None, the project is NOT registered in agtx's global index — caller may
    want to surface that to the user.
    """
    # Keep the literal input string for the fallback hash so behavior is
    # predictable when the project isn't in agtx's index. We still construct
    # a Path for the variant generator.
    input_str = str(project_path)
    input_path_obj = project_path if isinstance(project_path, Path) else Path(input_str)
    candidates = _windows_path_variants(input_path_obj)
    candidate_set = {c for c in candidates}
    # Always include the raw input string as a possible match.
    candidate_set.add(input_str)

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
                if stored in candidate_set:
                    return agtx_config_dir() / "projects" / f"{hash_project_path(stored)}.db", stored
        except sqlite3.Error:
            pass

    # Fallback — caller will probably get a "schema not found" error and we
    # surface that with a helpful message. Hash the literal input string for
    # consistency with how `project_db_path()` behaves on its own.
    return project_db_path(input_str), None


def list_registered_projects() -> list[tuple[str, str]]:
    """Return [(name, path), ...] from agtx's global index.db. Empty if absent."""
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
    """Path to agtx's global index.db (projects + running_agents)."""
    return agtx_config_dir() / "index.db"


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


# ---- Dataclasses (mirror D:/Projects/agtx/src/db/models.rs) ---------------


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
                f"agtx project DB does not exist: {self.project_db_p}. "
                "agtx has not been opened on this project yet, or the project path "
                "doesn't match what agtx registered. Run `python -m harbor webui-diagnose` "
                "for available projects."
            )
        conn = sqlite3.connect(str(self.project_db_p), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Don't run CREATE TABLE — agtx owns the schema. Just set busy_timeout
        # for safety; agtx already enables WAL via the file's pragma.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def is_initialized(self) -> bool:
        """True iff the per-project DB exists AND has agtx's required tables.

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
        """Register a project in agtx's global index and initialize its DB.

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

        db_path = agtx_config_dir() / "projects" / f"{hash_project_path(stored_path)}.db"
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


# ---- Row decoders ---------------------------------------------------------


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    return int(v)


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """sqlite3.Row.__getitem__ raises IndexError if column missing.

    agtx's schema has migration ALTERs — older DBs may not have all columns yet.
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


# ---- Schema bootstrap helper (test-only) ----------------------------------

# Mirror of the agtx project schema, kept here ONLY so tests can build an
# in-memory DB matching what the production agtx binary would have created.
# We never run this against a real on-disk DB — agtx owns those.
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
"""


def init_test_db(conn: sqlite3.Connection, *, kind: str = "project") -> None:
    """Apply agtx's schema to a fresh sqlite3 connection. Test use only."""
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
