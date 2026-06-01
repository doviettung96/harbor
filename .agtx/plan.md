# Plan: bd55b605 Data ownership + migration

## Task Contract Parsed

Status: planning

Title: Data ownership + migration (harbor-owned data dir)

Acceptance Criteria:
- After launching harbor against a copy of the existing agtx config dir, `%APPDATA%\harbor\config\index.db` lists all current projects and every per-project `<hash>.db` is present and queryable.
- The spike result is recorded in the migration report: per-project DB hash is stable under the `.agtx/` -> `.harbor/` rename (or, if not, the migration rehashes and the report says so).
- A second launch performs zero migration operations (idempotent), proven by an empty migration report on the second run.
- Each migrated project's on-disk `.agtx/` directory is renamed to `.harbor/`.

Verification Probes:
- `python -m pytest tests/test_data_migration.py -q -v -s`

Runtime Target:
- Repo default `.agtx/runtime-target.json` is local.
- No runtime-target override is needed.

Worker Instructions:
- Operate on a COPY of the agtx config dir in a tempdir.
- Never mutate the live `%APPDATA%\agtx\config`.
- The test fixture should stand up a fake agtx config: global `index.db`, a couple per-project DBs, and project dirs containing `.agtx/`.
- Assert post-migration state.

Run Repo Defaults:
- yes

## Context Found

- `harbor/agtx_client.py` currently treats agtx as the owner of the SQLite data layer. Its module docstring says Harbor reads agtx DBs directly, `agtx_config_dir()` points at `%APPDATA%\agtx\config`, `global_db_path()` returns that dir's `index.db`, and `project_db_path()` returns `<agtx-config>/projects/<hash>.db`.
- The schema constants `_PROJECT_SCHEMA_SQL` and `_GLOBAL_SCHEMA_SQL` already exist in `harbor/agtx_client.py` but are documented as test-only mirrors. The task wants these promoted to Harbor's authoritative schema.
- `AgtxDb._open_global_create()` and `_open_project_create()` already execute those schema strings when creating new DBs. This is the right starting point for a lightweight Harbor-owned migration mechanism.
- `harbor/agent.py` already has `harbor_config_dir()` with the requested platform layout for `%APPDATA%\harbor\config`, `~/Library/Application Support/harbor`, and `$XDG_CONFIG_HOME/harbor`.
- `harbor/webui/server.py`, `harbor/bootstrap.py`, and `harbor/__main__.py` import `global_db_path()`, `project_db_path()`, and related helpers from `harbor/agtx_client.py`, so changing those helpers switches the web UI and bootstrap paths.
- Existing tests monkeypatch `harbor.agtx_client.agtx_config_dir()` to isolate agtx-style DB writes. The new migration tests should instead monkeypatch separate source/destination config-dir functions so the live `%APPDATA%\agtx\config` is never touched.
- `.agtx/plan.md` was stale from task `0b7d5ded`; this plan replaces it for `bd55b605`.

## Implementation Plan

1. Split data-dir ownership in `harbor/agtx_client.py`.
   - Add `harbor_data_dir()` or reuse/import `harbor_config_dir()` as the destination for Harbor-owned DBs.
   - Keep an explicit `agtx_config_dir()` only as the legacy migration source.
   - Change `global_db_path()`, `project_db_path()`, `resolve_project_db_path()`, `list_registered_projects()`, and `AgtxDb.register_project()` to use Harbor's data dir by default.
   - Avoid circular imports if reusing `harbor.agent.harbor_config_dir()` is awkward; a small local path helper in `agtx_client.py` is acceptable because this is core data-path logic.

2. Promote schema constants and add a lightweight migration layer.
   - Update comments/docstrings so `_PROJECT_SCHEMA_SQL` and `_GLOBAL_SCHEMA_SQL` are Harbor-owned schema definitions, not test-only agtx mirrors.
   - Add a migration entrypoint such as `ensure_harbor_data_migrated()` returning a structured `MigrationReport`.
   - The report should distinguish copied DBs, renamed project dirs, hash-stability result, and skipped/no-op cases.
   - Keep the first migration conservative: create Harbor config/projects directories, copy agtx `index.db`, copy agtx `projects/*.db`, and then run Harbor schema creation/migration helpers against copied DBs if needed.
   - Do not alter or delete any file under the source agtx config dir.

3. Implement the hash spike inside the migration.
   - Read project paths from the copied or source `index.db`.
   - For each project, compute the DB filename from the project path exactly as stored in the `projects.path` row.
   - Compare that filename to the existing copied per-project DB filename.
   - Record in the report that the filename is stable because the hash input is the project path, not the workflow metadata directory.
   - If a fixture demonstrates mismatch, handle it by copying/renaming to the expected Harbor filename and record that rehashing happened.

4. Rename project workflow dirs from `.agtx` to `.harbor`.
   - Iterate projects from the copied Harbor `index.db`.
   - For each project path, strip any Windows extended-length prefix before filesystem operations.
   - If `<project>/.agtx` exists and `<project>/.harbor` does not, rename `.agtx` to `.harbor` and record it.
   - If `.harbor` already exists, treat the rename as already migrated and do not overwrite it.
   - If neither exists, record no operation; do not fail migration solely because a project checkout is unavailable.

5. Wire migration into launch paths.
   - Call the migration entrypoint during app creation or global DB path resolution before the web UI lists projects.
   - Ensure startup reads from Harbor-owned `index.db` after migration, not agtx's DB.
   - Update `webui-diagnose` wording so it reports Harbor's data dir and, separately, the legacy agtx source dir if useful.
   - Keep runtime config behavior unchanged: `runtime.yml` already lives under Harbor's config dir.

6. Add focused tests in `tests/test_data_migration.py`.
   - Build a fake agtx config dir in `tmp_path` with `index.db`, `projects/<hash>.db` files, and two fake project directories each containing `.agtx/`.
   - Monkeypatch both source and destination dirs: source points at fake agtx config, destination points at fake Harbor config.
   - Run the migration once and assert Harbor `index.db` exists, all per-project DBs exist, each DB is initialized/queryable, `.agtx` was renamed to `.harbor`, and the report records hash stability.
   - Run the migration a second time and assert the report has zero operations.
   - Add a guard assertion that the source fake agtx config DBs are still present and unchanged after migration.

7. Preserve compatibility for existing tests.
   - Update tests that currently monkeypatch `agtx_config_dir()` only because it was the production DB location; after the split they should monkeypatch Harbor's data-dir helper instead.
   - Leave source-only migration tests monkeypatching `agtx_config_dir()`.
   - Keep old public names only where useful for compatibility, but their semantics should be explicit: Harbor DB path helpers use Harbor's data dir, agtx source helpers are migration-only.

## Verification Plan

Primary required probe:
- `python -m pytest tests/test_data_migration.py -q -v -s`

Repo defaults:
- Because `## Run Repo Defaults` is `yes`, the Running phase should invoke `agtx-task-verify`, which will run the required probe through `target-runtime-exec` and then repo-default tests through `build-and-test`.

Planning stop:
- Do not implement in this Planning phase.
- Wait for the task to be moved to Running before editing production or test code.
