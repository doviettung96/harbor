"""Install the agtx workflow template into a target project."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harbor.bootstrap import AGENT_SKILL_LAYOUTS, apply_bootstrap  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the agtx-workflow-template plugin into a target project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="Path to the target project directory.")
    parser.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Install plugin globally at ~/.config/agtx/plugins/<name>/ "
        "instead of project-local at <target>/.agtx/plugins/<name>/.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        metavar="NAME",
        help="Deploy skills to this agent's native discovery path. Repeat "
        "for multiple agents. Default: claude + codex. Known agents: "
        + ", ".join(AGENT_SKILL_LAYOUTS),
    )
    parser.add_argument(
        "--skip-skills",
        action="store_true",
        help="Don't deploy skills to agent-native or canonical paths. The plugin's "
        "bundled skills/ dir is still installed alongside plugin.toml.",
    )
    parser.add_argument(
        "--skip-harbor-yml",
        action="store_true",
        help="Don't write or merge a starter harbor.yml.",
    )
    parser.add_argument(
        "--skip-runtime-target",
        action="store_true",
        help="Don't write .agtx/runtime-target.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Accepted for CLI compatibility; bootstrap writes only changed files.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    target = Path(args.target).resolve()
    if not target.exists() or not target.is_dir():
        print(f"error: target is not an existing directory: {target}", file=sys.stderr)
        return 2

    try:
        plan, applied = apply_bootstrap(
            target,
            global_plugin=args.global_install,
            agents=args.agent or ("claude", "codex"),
            deploy_skills=not args.skip_skills,
            write_harbor_yml=not args.skip_harbor_yml,
            write_runtime_target=not args.skip_runtime_target,
            seed_tasks=False,
        )
    except Exception as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return 2

    print(plan.render())
    print(f"Applied operations: {len(applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
