"""Bootstrap a project so Harbor can drive agtx-style task worktrees."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from . import agtx_client


PLUGIN_NAME = "agtx-workflow-template"
CONFIGURE_RUNTIME_TITLE = "Configure runtime target"
WORKER_SMOKE_TITLE = "Worker smoke test"


AGENT_SKILL_LAYOUTS: dict[str, tuple[str, str, str, str]] = {
    "claude": (".claude/skills", "", "dir", "SKILL.md"),
    "gemini": (".gemini/commands", "agtx", "file", ".md"),
    "opencode": (".opencode/command", "", "file", ".md"),
    "codex": (".codex/skills", "", "file", ".md"),
    "cursor": (".cursor/skills", "", "file", ".md"),
    "copilot": (".github/agents", "agtx", "file", ".md"),
}


RUNTIME_TARGET_LOCAL = {
    "version": 1,
    "mode": "local",
    "target": {
        "kind": "local",
    },
}


CONFIGURE_RUNTIME_DESCRIPTION = """Point `.agtx/runtime-target.json` at the real runtime this repo should use: local, ssh, emulator, device, or game_window. Use `python scripts/shared/target_runtime.py target set-*` commands so the schema is validated.

## Acceptance Criteria
- `.agtx/runtime-target.json` reflects the target runtime the user chose.
- `python scripts/shared/target_runtime.py target show` exits 0.
- If the target is not local, the configured probe command proves the target is reachable.

## Verification Probes
- python scripts/shared/target_runtime.py target show

## Runtime Target
local

## Worker Instructions
Ask the user which runtime target to configure before changing `.agtx/runtime-target.json`; do not guess emulator, device, SSH, or game-window details.

## Run Repo Defaults
no
"""


WORKER_SMOKE_DESCRIPTION = """Prove a task worker can edit its own worktree and satisfy an explicit file probe.

## Acceptance Criteria
- Create `SMOKE_WORKER.md` in the task worktree.
- The file contains exactly `harbor worker smoke ok`.
- The verification probe exits 0 through `target-runtime-exec`.

## Verification Probes
- python -c "from pathlib import Path; p=Path('SMOKE_WORKER.md'); raise SystemExit(0 if p.is_file() and p.read_text(encoding='utf-8').strip() == 'harbor worker smoke ok' else 1)"

## Runtime Target
local

## Worker Instructions
Only edit `SMOKE_WORKER.md` for this smoke task.

## Run Repo Defaults
no
"""

BOOTSTRAP_TASKS = (
    (CONFIGURE_RUNTIME_TITLE, CONFIGURE_RUNTIME_DESCRIPTION),
    (WORKER_SMOKE_TITLE, WORKER_SMOKE_DESCRIPTION),
)


@dataclass(frozen=True)
class BootstrapOperation:
    """A single file write the bootstrapper may apply."""

    label: str
    path: Path
    content: bytes
    status: str

    @property
    def pending(self) -> bool:
        return self.status in {"create", "update"}


@dataclass(frozen=True)
class BootstrapPlan:
    project: Path
    operations: tuple[BootstrapOperation, ...]

    @property
    def pending_operations(self) -> tuple[BootstrapOperation, ...]:
        return tuple(op for op in self.operations if op.pending)

    def apply(self) -> tuple[BootstrapOperation, ...]:
        applied: list[BootstrapOperation] = []
        for op in self.pending_operations:
            op.path.parent.mkdir(parents=True, exist_ok=True)
            op.path.write_bytes(op.content)
            applied.append(op)
        return tuple(applied)

    def render(self) -> str:
        lines = [f"Bootstrap plan for {self.project}:"]
        for op in self.operations:
            rel = _display_path(op.path, self.project)
            lines.append(f"- {op.status}: {rel} ({op.label})")
        lines.append(f"Pending operations: {len(self.pending_operations)}")
        return "\n".join(lines)


def build_plan(
    project: str | Path,
    *,
    global_plugin: bool = False,
    agents: Iterable[str] | None = ("claude", "codex"),
    deploy_skills: bool = True,
    write_harbor_yml: bool = True,
    write_runtime_target: bool = True,
) -> BootstrapPlan:
    """Return the file-level bootstrap plan for *project* without applying it."""

    project_path = Path(project).resolve()
    root = _repo_root()
    plugin_src = root / "plugins" / PLUGIN_NAME
    skills_src = root / ".claude" / "skills"

    if not (plugin_src / "plugin.toml").is_file():
        raise FileNotFoundError(f"plugin.toml not found under {plugin_src}")
    skills = _discover_skills(skills_src)
    if not skills:
        raise FileNotFoundError(f"no skills found under {skills_src}")

    operations: list[BootstrapOperation] = []

    plugin_dest = (
        _global_plugin_root() / PLUGIN_NAME
        if global_plugin
        else project_path / ".agtx" / "plugins" / PLUGIN_NAME
    )
    operations.append(
        _file_operation(
            "plugin manifest",
            plugin_dest / "plugin.toml",
            (plugin_src / "plugin.toml").read_bytes(),
        )
    )

    for name, skill_md in skills:
        content = skill_md.read_bytes()
        operations.append(
            _file_operation(
                f"plugin skill {name}",
                plugin_dest / "skills" / name / "SKILL.md",
                content,
            )
        )
        if deploy_skills:
            for op in _agent_skill_operations(project_path, name, content, agents):
                operations.append(op)
            operations.append(
                _file_operation(
                    f"canonical skill {name}",
                    project_path / ".agtx" / "skills" / name / "SKILL.md",
                    content,
                )
            )

    if write_harbor_yml:
        operations.append(_harbor_yml_operation(project_path / "harbor.yml"))
    if write_runtime_target:
        operations.append(_runtime_target_operation(project_path / ".agtx" / "runtime-target.json"))

    return BootstrapPlan(project=project_path, operations=tuple(operations))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _global_plugin_root() -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "agtx" / "plugins"


def _discover_skills(skills_dir: Path) -> tuple[tuple[str, Path], ...]:
    skills: list[tuple[str, Path]] = []
    if not skills_dir.is_dir():
        return ()
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            skills.append((child.name, skill_md))
    return tuple(skills)


def _agent_skill_operations(
    project_path: Path,
    name: str,
    content: bytes,
    agents: Iterable[str] | None,
) -> tuple[BootstrapOperation, ...]:
    operations: list[BootstrapOperation] = []
    for agent in agents or ():
        layout = AGENT_SKILL_LAYOUTS.get(agent)
        if layout is None:
            continue
        base, namespace, kind, suffix = layout
        native_dir = project_path / base / namespace if namespace else project_path / base
        path = (
            native_dir / name / suffix
            if kind == "dir"
            else native_dir / f"{name}{suffix}"
        )
        operations.append(_file_operation(f"{agent} skill {name}", path, content))
    return tuple(operations)


def _file_operation(label: str, path: Path, content: bytes) -> BootstrapOperation:
    if not path.exists():
        status = "create"
    elif path.read_bytes() == content:
        status = "skip"
    else:
        status = "update"
    return BootstrapOperation(label=label, path=path, content=content, status=status)


def _harbor_yml_operation(path: Path) -> BootstrapOperation:
    data: dict[str, object]
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
        data = dict(loaded)
        agtx = data.get("agtx") or {}
        if not isinstance(agtx, dict):
            raise ValueError(f"{path}: agtx must be a YAML mapping when present")
        agtx = dict(agtx)
    else:
        data = {}
        agtx = {}

    agtx["plugin"] = PLUGIN_NAME
    data["agtx"] = agtx
    content = yaml.safe_dump(data, sort_keys=False, allow_unicode=False).encode("utf-8")
    return _file_operation("harbor.yml agtx plugin", path, content)


def _runtime_target_operation(path: Path) -> BootstrapOperation:
    content = (json.dumps(RUNTIME_TARGET_LOCAL, indent=2) + "\n").encode("utf-8")
    if path.exists():
        return BootstrapOperation(
            label="runtime target local default",
            path=path,
            content=path.read_bytes(),
            status="skip",
        )
    return BootstrapOperation(
        label="runtime target local default",
        path=path,
        content=content,
        status="create",
    )


def _display_path(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def apply_bootstrap(
    project: str | Path,
    *,
    global_plugin: bool = False,
    agents: Iterable[str] | None = ("claude", "codex"),
    deploy_skills: bool = True,
    write_harbor_yml: bool = True,
    write_runtime_target: bool = True,
    seed_tasks: bool = True,
) -> tuple[BootstrapPlan, tuple[BootstrapOperation, ...]]:
    plan = build_plan(
        project,
        global_plugin=global_plugin,
        agents=agents,
        deploy_skills=deploy_skills,
        write_harbor_yml=write_harbor_yml,
        write_runtime_target=write_runtime_target,
    )
    applied = plan.apply()
    if seed_tasks:
        seed_bootstrap_tasks(plan.project)
    return plan, applied


def seed_bootstrap_tasks(project: str | Path) -> tuple[tuple[agtx_client.Task, bool], ...]:
    """Register *project* with agtx and ensure Harbor's starter tasks exist."""

    project_record = agtx_client.AgtxDb(
        project_db_p=None,  # type: ignore[arg-type]
        global_db_p=agtx_client.global_db_path(),
    ).register_project(project)
    project_db = (
        agtx_client.agtx_config_dir()
        / "projects"
        / f"{agtx_client.hash_project_path(project_record.path)}.db"
    )
    db = agtx_client.AgtxDb(project_db_p=project_db, global_db_p=agtx_client.global_db_path())
    seeded: list[tuple[agtx_client.Task, bool]] = []
    for title, description in BOOTSTRAP_TASKS:
        seeded.append(
            db.create_task_if_title_missing(
                title=title,
                description=description,
                project_id=project_record.id,
                agent="codex",
                status="backlog",
            )
        )
    return tuple(seeded)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", metavar="PROJECT", help="print planned operations without applying")
    mode.add_argument("--apply", metavar="PROJECT", help="apply bootstrap operations")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.plan:
            plan = build_plan(args.plan)
            print(plan.render())
            return 0

        plan, applied = apply_bootstrap(args.apply)
        print(plan.render())
        print(f"Applied operations: {len(applied)}")
        return 0
    except Exception as exc:
        print(f"harbor.bootstrap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
