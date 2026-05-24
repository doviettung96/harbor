"""Bootstrap a project so Harbor can drive agtx-style task worktrees."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


PLUGIN_NAME = "agtx-workflow-template"


RUNTIME_TARGET_LOCAL = {
    "version": 1,
    "mode": "local",
    "target": {
        "kind": "local",
    },
}


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


def build_plan(project: str | Path) -> BootstrapPlan:
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

    plugin_dest = project_path / ".agtx" / "plugins" / PLUGIN_NAME
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
        operations.append(
            _file_operation(
                f"claude skill {name}",
                project_path / ".claude" / "skills" / name / "SKILL.md",
                content,
            )
        )
        operations.append(
            _file_operation(
                f"codex skill {name}",
                project_path / ".codex" / "skills" / f"{name}.md",
                content,
            )
        )
        operations.append(
            _file_operation(
                f"canonical skill {name}",
                project_path / ".agtx" / "skills" / name / "SKILL.md",
                content,
            )
        )

    operations.append(_harbor_yml_operation(project_path / "harbor.yml"))
    operations.append(_runtime_target_operation(project_path / ".agtx" / "runtime-target.json"))

    return BootstrapPlan(project=project_path, operations=tuple(operations))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _discover_skills(skills_dir: Path) -> tuple[tuple[str, Path], ...]:
    skills: list[tuple[str, Path]] = []
    if not skills_dir.is_dir():
        return ()
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            skills.append((child.name, skill_md))
    return tuple(skills)


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


def apply_bootstrap(project: str | Path) -> tuple[BootstrapPlan, tuple[BootstrapOperation, ...]]:
    plan = build_plan(project)
    return plan, plan.apply()


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
