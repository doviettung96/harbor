#!/usr/bin/env python3
"""Probe the configured runtime target. Exit 0 if reachable, non-zero otherwise.

Reads `.harbor/runtime-target.json`. The user-defined `target.probe_command` is
the source of truth for non-local targets. Prints a single-line structured
summary so callers (target_runtime.py, harbor-task-verify, CI) can grep it.

Exit codes:
  0 = target is local OR probe_command exited 0
  1 = target is non-local but no probe_command is set
  2 = config could not be loaded
  N = exit code of target.probe_command if non-zero
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".harbor" / "runtime-target.json"


def _summary(kind: str, probe: str | None, exit_code: int) -> str:
    probe_repr = shlex.quote(probe) if probe else "none"
    return f"target={kind} probe={probe_repr} exit={exit_code}"


def _load_target() -> dict:
    if not CONFIG_PATH.exists():
        return {"kind": "local", "probe_command": None}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"probe_target: failed to parse {CONFIG_PATH}: {exc}\n")
        sys.exit(2)
    target = data.get("target") or {}
    if not isinstance(target, dict):
        sys.stderr.write(f"probe_target: {CONFIG_PATH} 'target' must be an object\n")
        sys.exit(2)
    return target


def _target_env(target: dict) -> dict[str, str]:
    kind = target.get("kind", "local")
    env = {"AWT_TARGET_KIND": kind}
    if kind == "emulator":
        emu = target.get("emulator") or {}
        if emu.get("name"):
            env["AWT_TARGET_EMULATOR_NAME"] = str(emu["name"])
        if emu.get("adb_port"):
            env["AWT_TARGET_EMULATOR_ADB_PORT"] = str(emu["adb_port"])
    elif kind == "device":
        dev = target.get("device") or {}
        if dev.get("id"):
            env["AWT_TARGET_DEVICE_ID"] = str(dev["id"])
        if dev.get("kind"):
            env["AWT_TARGET_DEVICE_KIND"] = str(dev["kind"])
    elif kind == "game_window":
        win = target.get("game_window") or {}
        if win.get("title_pattern"):
            env["AWT_TARGET_GAME_WINDOW_TITLE"] = str(win["title_pattern"])
        if win.get("class_pattern"):
            env["AWT_TARGET_GAME_WINDOW_CLASS"] = str(win["class_pattern"])
    return env


def main() -> int:
    target = _load_target()
    kind = target.get("kind", "local")
    probe = (target.get("probe_command") or "").strip() or None

    if kind == "local":
        print(_summary(kind, probe, 0))
        return 0

    if not probe:
        print(_summary(kind, None, 1), file=sys.stderr)
        sys.stderr.write(
            "probe_target: target.kind is non-local but target.probe_command is not set. "
            "Add a probe_command to .harbor/runtime-target.json (e.g. an `adb shell pidof <pkg>` "
            "or a window-enumerator) so the worker can verify the target is reachable.\n"
        )
        return 1

    env = {**os.environ, **_target_env(target)}
    result = subprocess.run(probe, cwd=REPO_ROOT, shell=True, env=env, check=False)
    print(_summary(kind, probe, result.returncode))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
