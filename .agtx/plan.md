# Plan: cbdb2404 harbor/bootstrap.py file-level bootstrap core

## Task Contract Parsed

Status: planning

Title: harbor/bootstrap.py - file-level bootstrap core

Acceptance Criteria:
- Applying bootstrap to a blank temp project creates:
  - `.agtx/plugins/agtx-workflow-template/plugin.toml`
  - `.agtx/plugins/agtx-workflow-template/skills/<name>/SKILL.md` for every repo skill under `harbor/.claude/skills/`
  - `.claude/skills/<name>/SKILL.md` for every skill
  - `.codex/skills/<name>.md` for every skill
  - `.agtx/skills/<name>/SKILL.md` for every skill
  - `harbor.yml` containing `agtx.plugin: agtx-workflow-template`
  - `.agtx/runtime-target.json` with `target.kind: local`
- Re-applying bootstrap is a no-op.
- `python -m harbor.bootstrap --plan <project>` prints operations without applying.
- Existing `harbor.yml` keys are preserved while `agtx.plugin` is added or updated.
- Existing `.agtx/runtime-target.json` is preserved and never overwritten.

Verification Probes:
- `python -m pytest tests/test_bootstrap.py -q -v -s`
- `python -m harbor.bootstrap --plan tests/fixtures/blank-project`

Runtime Target:
- `local`
- Repo default `.agtx/runtime-target.json` is already local, so no worktree runtime override is needed.

Worker Instructions:
- none

Run Repo Defaults:
- yes

## Context Found

- The repo has no `harbor/bootstrap.py` or `tests/test_bootstrap.py` yet.
- `plugins/agtx-workflow-template/install.py` has related copy/deploy logic, but it deploys Claude skills to `.claude/commands/agtx/<name>.md`; this task explicitly requires skill-dir layout at `.claude/skills/<name>/SKILL.md`.
- `harbor/agent.py` already imports PyYAML and owns config serialization helpers, but bootstrap needs a lighter non-destructive merge that preserves unrelated `harbor.yml` keys.
- `plugins/agtx-workflow-template/` currently contains `plugin.toml`, `README.md`, and `install.py`; the bootstrap destination only needs a self-contained plugin directory with `plugin.toml` and materialized `skills/`.

## Implementation Plan

1. Add `harbor/bootstrap.py`.
   - Define small structured operation records, likely with fields such as action, source, destination, reason, and whether the operation is pending.
   - Resolve the Harbor repo root from `Path(__file__).resolve().parent.parent`.
   - Resolve canonical sources:
     - plugin source: `<repo>/plugins/agtx-workflow-template`
     - skills source: `<repo>/.claude/skills`
   - Enumerate skills by directories containing `SKILL.md`, sorted for deterministic output.

2. Implement plan computation without side effects.
   - Plugin install operations:
     - Copy `plugin.toml` to `<project>/.agtx/plugins/agtx-workflow-template/plugin.toml`.
     - Copy each skill `SKILL.md` to `<project>/.agtx/plugins/agtx-workflow-template/skills/<name>/SKILL.md`.
   - Agent-native skill operations:
     - Copy each skill to `<project>/.claude/skills/<name>/SKILL.md`.
     - Copy each skill to `<project>/.codex/skills/<name>.md`.
     - Copy each skill to `<project>/.agtx/skills/<name>/SKILL.md`.
   - Config operations:
     - Plan a `harbor.yml` YAML merge so `agtx.plugin` becomes `agtx-workflow-template`, preserving unrelated keys.
     - Plan `.agtx/runtime-target.json` creation only when absent.
   - Treat a destination with identical content as no-op so a second apply reports zero pending file changes.

3. Implement apply behavior.
   - Create parent directories as needed.
   - Copy only when content differs or destination is absent.
   - Write YAML through `yaml.safe_load` / `yaml.safe_dump(sort_keys=False, allow_unicode=False)` while preserving existing top-level keys semantically.
   - Write the local runtime target JSON only if absent, with version/mode local and `target.kind: local`.
   - Never overwrite existing `.agtx/runtime-target.json`.

4. Implement CLI for `python -m harbor.bootstrap`.
   - Support `--plan <project>`: compute and print deterministic operations without applying; exit zero.
   - Support `--apply <project>`: compute, apply pending operations, print applied/skipped summary; exit zero.
   - Validate exactly one of `--plan` or `--apply`.

5. Add `tests/test_bootstrap.py`.
   - Use `tmp_path` for blank projects.
   - Assert all required plugin, Claude, Codex, and canonical skill files exist after apply.
   - Compare against the current skill list under repo `.claude/skills` so the test tracks all skills.
   - Snapshot file contents after first apply and assert second apply leaves the tree unchanged and reports no pending operations.
   - Assert plan mode prints operations but leaves a blank project untouched.
   - Assert `harbor.yml` merge preserves unrelated keys and sets `agtx.plugin`.
   - Assert existing `.agtx/runtime-target.json` content remains byte-for-byte unchanged.

## Verification Plan For Running Phase

After implementation, invoke `agtx-task-verify`, which must run the task probes through `target-runtime-exec`:
- `python -m pytest tests/test_bootstrap.py -q -v -s`
- `python -m harbor.bootstrap --plan tests/fixtures/blank-project`

Because `## Run Repo Defaults` is `yes`, the verification step should also run the repo default build/test path after probes pass.

## Notes / Risks

- The task's Claude destination conflicts with older installer comments and helper mappings. The bootstrap implementation should follow the task's explicit `.claude/skills/<name>/SKILL.md` acceptance criterion.
- The second verification probe references `tests/fixtures/blank-project`; if it does not exist yet, the implementation or tests should add that fixture directory without depending on generated state.
- "YAML round-trip, never clobber" is interpreted as preserving existing data keys and only changing `agtx.plugin`, not preserving comments or formatting byte-for-byte.
