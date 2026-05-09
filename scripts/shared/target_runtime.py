#!/usr/bin/env python3
"""Route project execution through the configured local, SSH, or game-RE runtime target."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".agtx" / "runtime-target.json"
PROBE_TARGET_SCRIPT = REPO_ROOT / "scripts" / "shared" / "probe_target.py"
DEFAULT_TARGET = {
    "kind": "local",
    "emulator": {"name": None, "exec_path": None, "args": [], "adb_port": None},
    "device": {"id": None, "kind": None, "transport": None},
    "game_window": {"title_pattern": None, "class_pattern": None, "pid_lookup_strategy": None},
    "probe_command": None,
}
DEFAULT_CONFIG = {
    "version": 1,
    "mode": "local",
    "ssh_host": None,
    "remote_platform": None,
    "remote_workdir": None,
    "sync_strategy": None,
    "remote_python": None,
    "target": dict(DEFAULT_TARGET),
}
EXCLUDE_DIRS = {
    ".git",
    ".agtx",
    ".beads",
    ".claude",
    ".codex",
    ".venv",
    "node_modules",
    "__pycache__",
}
EXCLUDE_FILES = {
    ".DS_Store",
    "Thumbs.db",
}
TARGET_KINDS = {"local", "emulator", "device", "game_window"}


class ConfigError(RuntimeError):
    """Raised when the runtime-target configuration is invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show the resolved runtime target")
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    configure_parser = subparsers.add_parser("configure", help="Create or update runtime-target.json")
    configure_parser.add_argument("--mode", choices=("local", "ssh"))
    configure_parser.add_argument("--ssh-host")
    configure_parser.add_argument("--remote-platform", choices=("posix", "windows"))
    configure_parser.add_argument("--remote-workdir")
    configure_parser.add_argument("--sync-strategy", choices=("rsync", "archive"))
    configure_parser.add_argument("--remote-python")
    configure_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    target_parser = subparsers.add_parser("target", help="Inspect or update the game-RE target subobject")
    target_sub = target_parser.add_subparsers(dest="target_command", required=True)

    target_sub.add_parser("status", help="Show the target.kind and its subobject")

    set_emu = target_sub.add_parser("set-emulator", help="Set target.kind=emulator and the emulator subobject")
    set_emu.add_argument("--name", required=True)
    set_emu.add_argument("--exec-path")
    set_emu.add_argument("--arg", action="append", default=[], help="Repeat for multiple args")
    set_emu.add_argument("--adb-port", type=int)
    set_emu.add_argument("--probe-command", help="Shell command that exits 0 when emulator is reachable")

    set_dev = target_sub.add_parser("set-device", help="Set target.kind=device and the device subobject")
    set_dev.add_argument("--id", required=True)
    set_dev.add_argument("--kind", choices=("adb", "frida", "raw"))
    set_dev.add_argument("--transport", choices=("usb", "tcp"))
    set_dev.add_argument("--probe-command")

    set_win = target_sub.add_parser("set-game-window", help="Set target.kind=game_window and the window subobject")
    set_win.add_argument("--title-pattern")
    set_win.add_argument("--class-pattern")
    set_win.add_argument("--pid-lookup-strategy", choices=("window-title", "process-name", "adb-focused-app"))
    set_win.add_argument("--probe-command")

    target_sub.add_parser("set-local", help="Reset target.kind=local")

    run_parser = subparsers.add_parser("run", help="Run one project command through the selected runtime")
    run_parser.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        help="Pass the command after `--`, for example: run -- pytest -q",
    )
    run_parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Skip the target reachability probe (use only for diagnostics)",
    )

    return parser.parse_args()


def load_raw_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return _deep_default()

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse {CONFIG_PATH}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} must contain a JSON object")

    merged = _deep_default()
    merged.update({k: v for k, v in data.items() if k != "target"})
    if isinstance(data.get("target"), dict):
        target = dict(merged["target"])
        for k, v in data["target"].items():
            if isinstance(v, dict) and isinstance(target.get(k), dict):
                sub = dict(target[k])
                sub.update(v)
                target[k] = sub
            else:
                target[k] = v
        merged["target"] = target
    return merged


def _deep_default() -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    out["target"] = {k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
                     for k, v in DEFAULT_TARGET.items()}
    return out


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    mode = config.get("mode") or "local"
    if mode not in {"local", "ssh"}:
        raise ConfigError("runtime-target mode must be `local` or `ssh`")

    remote_platform = config.get("remote_platform")
    sync_strategy = config.get("sync_strategy")
    remote_python = (config.get("remote_python") or "").strip() or None
    resolved = dict(config)
    resolved["mode"] = mode
    resolved["target"] = _validate_target(config.get("target") or dict(DEFAULT_TARGET))

    if mode == "local":
        resolved["remote_platform"] = remote_platform
        resolved["sync_strategy"] = sync_strategy or "local"
        resolved["remote_python"] = remote_python
        return resolved

    ssh_host = (config.get("ssh_host") or "").strip()
    remote_workdir = (config.get("remote_workdir") or "").strip()
    if not ssh_host:
        raise ConfigError("runtime-target ssh mode requires `ssh_host`")
    if remote_platform not in {"posix", "windows"}:
        raise ConfigError("runtime-target ssh mode requires `remote_platform` set to `posix` or `windows`")
    if not remote_workdir:
        raise ConfigError("runtime-target ssh mode requires `remote_workdir`")
    if sync_strategy is None:
        sync_strategy = "rsync" if remote_platform == "posix" else "archive"
    if sync_strategy not in {"rsync", "archive"}:
        raise ConfigError("runtime-target sync_strategy must be `rsync` or `archive`")

    resolved["ssh_host"] = ssh_host
    resolved["remote_platform"] = remote_platform
    resolved["remote_workdir"] = remote_workdir
    resolved["sync_strategy"] = sync_strategy
    resolved["remote_python"] = remote_python
    return resolved


def _validate_target(target: dict[str, Any]) -> dict[str, Any]:
    kind = (target.get("kind") or "local").strip()
    if kind not in TARGET_KINDS:
        raise ConfigError(f"target.kind must be one of {sorted(TARGET_KINDS)}")
    out = dict(target)
    out["kind"] = kind

    if kind == "emulator":
        emu = out.get("emulator") or {}
        if not (emu.get("name") or "").strip():
            raise ConfigError("target.kind=emulator requires target.emulator.name")
    elif kind == "device":
        dev = out.get("device") or {}
        if not (dev.get("id") or "").strip():
            raise ConfigError("target.kind=device requires target.device.id")
    elif kind == "game_window":
        win = out.get("game_window") or {}
        if not ((win.get("title_pattern") or "").strip() or (win.get("class_pattern") or "").strip()):
            raise ConfigError("target.kind=game_window requires target.game_window.title_pattern or class_pattern")
    return out


def get_resolved_config() -> dict[str, Any]:
    raw = load_raw_config()
    resolved = validate_config(raw)
    resolved["config_path"] = str(CONFIG_PATH)
    resolved["config_exists"] = CONFIG_PATH.exists()
    return resolved


def print_status(config: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        json.dump(config, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"Repo root: {REPO_ROOT}")
    print(f"Config path: {CONFIG_PATH}")
    print(f"Config exists: {'yes' if config['config_exists'] else 'no'}")
    print(f"Mode: {config['mode']}")
    if config["mode"] == "ssh":
        print(f"SSH host: {config['ssh_host']}")
        print(f"Remote platform: {config['remote_platform']}")
        print(f"Remote workdir: {config['remote_workdir']}")
        print(f"Sync strategy: {config['sync_strategy']}")
        if config.get("remote_python"):
            print(f"Remote python: {config['remote_python']}")
    else:
        print("Remote target: local default")

    target = config.get("target") or {}
    kind = target.get("kind", "local")
    print(f"Target kind: {kind}")
    if kind == "emulator":
        emu = target.get("emulator") or {}
        print(f"  emulator.name: {emu.get('name')}")
        if emu.get("exec_path"):
            print(f"  emulator.exec_path: {emu.get('exec_path')}")
        if emu.get("adb_port"):
            print(f"  emulator.adb_port: {emu.get('adb_port')}")
    elif kind == "device":
        dev = target.get("device") or {}
        print(f"  device.id: {dev.get('id')}")
        if dev.get("kind"):
            print(f"  device.kind: {dev.get('kind')}")
    elif kind == "game_window":
        win = target.get("game_window") or {}
        if win.get("title_pattern"):
            print(f"  game_window.title_pattern: {win.get('title_pattern')}")
        if win.get("class_pattern"):
            print(f"  game_window.class_pattern: {win.get('class_pattern')}")
    if target.get("probe_command"):
        print(f"Probe command: {target.get('probe_command')}")
    return 0


def write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def configure_runtime(args: argparse.Namespace) -> int:
    existing = load_raw_config()
    updated = dict(existing)

    for key in ("mode", "ssh_host", "remote_platform", "remote_workdir", "sync_strategy", "remote_python"):
        value = getattr(args, key.replace("-", "_"), None)
        if value is not None:
            updated[key] = value

    validated = validate_config(updated)
    persisted = _persist_shape(validated)
    write_config(persisted)
    validated["config_path"] = str(CONFIG_PATH)
    validated["config_exists"] = True
    return print_status(validated, as_json=args.json)


def _persist_shape(validated: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": validated.get("version", 1),
        "mode": validated["mode"],
        "ssh_host": validated.get("ssh_host"),
        "remote_platform": validated.get("remote_platform"),
        "remote_workdir": validated.get("remote_workdir"),
        "sync_strategy": validated.get("sync_strategy"),
        "remote_python": validated.get("remote_python"),
        "target": validated.get("target") or dict(DEFAULT_TARGET),
    }


def configure_target(args: argparse.Namespace) -> int:
    existing = load_raw_config()
    target = dict(existing.get("target") or DEFAULT_TARGET)
    sub = args.target_command

    if sub == "status":
        return print_status(get_resolved_config(), as_json=False)

    if sub == "set-local":
        target["kind"] = "local"
    elif sub == "set-emulator":
        target["kind"] = "emulator"
        emu = dict(target.get("emulator") or {})
        emu["name"] = args.name
        if args.exec_path is not None:
            emu["exec_path"] = args.exec_path
        if args.arg:
            emu["args"] = list(args.arg)
        if args.adb_port is not None:
            emu["adb_port"] = args.adb_port
        target["emulator"] = emu
        if args.probe_command is not None:
            target["probe_command"] = args.probe_command
    elif sub == "set-device":
        target["kind"] = "device"
        dev = dict(target.get("device") or {})
        dev["id"] = args.id
        if args.kind is not None:
            dev["kind"] = args.kind
        if args.transport is not None:
            dev["transport"] = args.transport
        target["device"] = dev
        if args.probe_command is not None:
            target["probe_command"] = args.probe_command
    elif sub == "set-game-window":
        target["kind"] = "game_window"
        win = dict(target.get("game_window") or {})
        if args.title_pattern is not None:
            win["title_pattern"] = args.title_pattern
        if args.class_pattern is not None:
            win["class_pattern"] = args.class_pattern
        if args.pid_lookup_strategy is not None:
            win["pid_lookup_strategy"] = args.pid_lookup_strategy
        target["game_window"] = win
        if args.probe_command is not None:
            target["probe_command"] = args.probe_command
    else:
        raise ConfigError(f"Unsupported target subcommand: {sub}")

    existing["target"] = target
    validated = validate_config(existing)
    write_config(_persist_shape(validated))
    validated["config_path"] = str(CONFIG_PATH)
    validated["config_exists"] = True
    return print_status(validated, as_json=False)


def should_exclude(path: Path) -> bool:
    relative_parts = path.relative_to(REPO_ROOT).parts
    if any(part in EXCLUDE_DIRS for part in relative_parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    return False


def target_env(config: dict[str, Any]) -> dict[str, str]:
    """Build the AWT_TARGET_* env vars to inject into the spawned command."""
    target = config.get("target") or {}
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


def probe_target(config: dict[str, Any]) -> None:
    """Run the configured probe; raise ConfigError if it fails."""
    target = config.get("target") or {}
    kind = target.get("kind", "local")
    if kind == "local":
        return

    probe_cmd = (target.get("probe_command") or "").strip()
    if probe_cmd:
        env = {**os.environ, **target_env(config)}
        result = subprocess.run(probe_cmd, cwd=REPO_ROOT, shell=True, env=env, check=False)
        if result.returncode != 0:
            raise ConfigError(
                f"target probe_command failed (exit {result.returncode}): {probe_cmd}"
            )
        return

    # Fall back to probe_target.py if it exists; otherwise refuse.
    if PROBE_TARGET_SCRIPT.exists():
        env = {**os.environ, **target_env(config)}
        result = subprocess.run(
            [sys.executable, str(PROBE_TARGET_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise ConfigError(
                f"probe_target.py failed (exit {result.returncode}); "
                "set target.probe_command in .agtx/runtime-target.json or fix the target"
            )
        return

    raise ConfigError(
        f"target.kind={kind} requires either target.probe_command or scripts/shared/probe_target.py"
    )


def run_local(command: str, *, env: dict[str, str] | None = None) -> int:
    full_env = None if env is None else {**os.environ, **env}
    result = subprocess.run(command, cwd=REPO_ROOT, shell=True, env=full_env, check=False)
    return result.returncode


def check_command(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise ConfigError(f"Required command not found: {name}")
    return resolved


def run_checked(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def ssh_run(host: str, remote_command: str) -> subprocess.CompletedProcess[str]:
    check_command("ssh")
    return subprocess.run(
        ["ssh", host, remote_command],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def ensure_remote_directory(config: dict[str, Any]) -> None:
    remote_workdir = config["remote_workdir"]
    if config["remote_platform"] == "posix":
        command = f"mkdir -p {posix_shell_path(remote_workdir)}"
    else:
        quoted = ps_single_quote(remote_workdir)
        command = (
            "powershell -NoProfile -NonInteractive -Command "
            f"\"New-Item -ItemType Directory -Force -Path {quoted} | Out-Null\""
        )
    result = ssh_run(config["ssh_host"], command)
    if result.returncode != 0:
        raise ConfigError(result.stderr.strip() or "Failed to create the remote workdir")


def sync_with_rsync(config: dict[str, Any]) -> None:
    rsync = check_command("rsync")
    destination = f"{config['ssh_host']}:{config['remote_workdir'].rstrip('/')}/"
    args = [
        rsync,
        "-az",
        "--exclude=.git/",
        "--exclude=.agtx/",
        "--exclude=.beads/",
        "--exclude=.claude/",
        "--exclude=.codex/",
        "--exclude=.venv/",
        "--exclude=node_modules/",
        "--exclude=__pycache__/",
        f"{REPO_ROOT}{os.sep}",
        destination,
    ]
    result = run_checked(args)
    if result.returncode != 0:
        raise ConfigError(result.stderr.strip() or "rsync failed")


def build_archive(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file():
                continue
            if should_exclude(path):
                continue
            handle.write(path, path.relative_to(REPO_ROOT))


def ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def posix_shell_path(value: str) -> str:
    if value == "~":
        return "$HOME"
    if value.startswith("~/"):
        return "$HOME/" + shlex.quote(value[2:])
    return shlex.quote(value)


def remote_python_shell_lines(config: dict[str, Any], *, error_message: str) -> list[str]:
    lines = ["REMOTE_PYTHON=''"]
    preferred = (config.get("remote_python") or "").strip()
    if preferred:
        lines.append(f'if [ -x {shlex.quote(preferred)} ]; then REMOTE_PYTHON={shlex.quote(preferred)}; fi')
    lines.append('if [ -z "$REMOTE_PYTHON" ] && command -v python >/dev/null 2>&1; then REMOTE_PYTHON="$(command -v python)"; fi')
    lines.append('if [ -z "$REMOTE_PYTHON" ] && command -v python3 >/dev/null 2>&1; then REMOTE_PYTHON="$(command -v python3)"; fi')
    lines.append(f'if [ -z "$REMOTE_PYTHON" ]; then echo {shlex.quote(error_message)} >&2; exit 1; fi')
    return lines


def sync_with_archive(config: dict[str, Any]) -> None:
    check_command("ssh")
    scp = check_command("scp")
    ensure_remote_directory(config)

    with tempfile.TemporaryDirectory(prefix="target-runtime-") as tmp_dir:
        archive_path = Path(tmp_dir) / "repo-sync.zip"
        build_archive(archive_path)
        remote_base = config["remote_workdir"].rstrip("/\\")
        remote_archive = f"{config['ssh_host']}:{remote_base}/repo-sync.zip"
        copy_result = run_checked([scp, str(archive_path), remote_archive])
        if copy_result.returncode != 0:
            raise ConfigError(copy_result.stderr.strip() or "scp failed")

        remote_workdir = config["remote_workdir"]
        if config["remote_platform"] == "windows":
            remote_command = (
                "powershell -NoProfile -NonInteractive -Command "
                "\""
                f"$archive = Join-Path {ps_single_quote(remote_workdir)} 'repo-sync.zip'; "
                f"if (Test-Path {ps_single_quote(remote_workdir)}) {{ "
                f"Get-ChildItem -LiteralPath {ps_single_quote(remote_workdir)} -Force | "
                "Where-Object { $_.Name -ne 'repo-sync.zip' } | Remove-Item -Recurse -Force }; "
                "Expand-Archive -LiteralPath $archive -DestinationPath "
                f"{ps_single_quote(remote_workdir)} -Force; "
                "Remove-Item -LiteralPath $archive -Force"
                "\""
            )
        else:
            quoted_workdir = posix_shell_path(remote_workdir)
            remote_command = (
                "\n".join(remote_python_shell_lines(config, error_message="python is required for archive sync"))
                + "\n"
                + "\"${REMOTE_PYTHON}\" - <<'PY'\n"
                "import pathlib\n"
                "import shutil\n"
                "import zipfile\n"
                "root = pathlib.Path("
                + repr(remote_workdir)
                + ").expanduser()\n"
                "archive = root / 'repo-sync.zip'\n"
                "root.mkdir(parents=True, exist_ok=True)\n"
                "for child in list(root.iterdir()):\n"
                "    if child.name == 'repo-sync.zip':\n"
                "        continue\n"
                "    if child.is_dir():\n"
                "        shutil.rmtree(child)\n"
                "    else:\n"
                "        child.unlink()\n"
                "with zipfile.ZipFile(archive) as handle:\n"
                "    handle.extractall(root)\n"
                "archive.unlink()\n"
                "PY"
            )
            remote_command = f"cd {quoted_workdir} && {remote_command}"
        extract_result = ssh_run(config["ssh_host"], remote_command)
        if extract_result.returncode != 0:
            raise ConfigError(extract_result.stderr.strip() or "Remote archive extraction failed")


def sync_repo(config: dict[str, Any]) -> None:
    ensure_remote_directory(config)
    strategy = config["sync_strategy"]
    if strategy == "rsync":
        sync_with_rsync(config)
        return
    if strategy == "archive":
        sync_with_archive(config)
        return
    raise ConfigError(f"Unsupported sync strategy: {strategy}")


def build_remote_command(config: dict[str, Any], command: str) -> str:
    remote_workdir = config["remote_workdir"]
    if config["remote_platform"] == "posix":
        wrapped = f"cd {posix_shell_path(remote_workdir)} && {command}"
        return "bash -lc " + shlex.quote(wrapped)

    command_text = f"Set-Location -LiteralPath {ps_single_quote(remote_workdir)}; {command}"
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "
        + ps_single_quote(command_text)
    )


def run_remote(config: dict[str, Any], command: str) -> int:
    sync_repo(config)
    remote_command = build_remote_command(config, command)
    result = subprocess.run(["ssh", config["ssh_host"], remote_command], cwd=REPO_ROOT, check=False)
    return result.returncode


def build_command_text(config: dict[str, Any], command_args: list[str]) -> str:
    if (
        config["mode"] == "ssh"
        and config["remote_platform"] == "posix"
        and command_args
        and command_args[0] in {"python", "python3"}
    ):
        resolver_lines = remote_python_shell_lines(config, error_message="python is required on the remote target")
        tail = " ".join(shlex.quote(arg) for arg in command_args[1:])
        exec_command = '"$REMOTE_PYTHON"' if not tail else f'"$REMOTE_PYTHON" {tail}'
        return "\n".join(resolver_lines + [exec_command])

    return " ".join(command_args)


def run_command(args: argparse.Namespace) -> int:
    command_args = list(args.command_args)
    if command_args and command_args[0] == "--":
        command_args = command_args[1:]
    if not command_args:
        raise ConfigError("No command provided. Use: target_runtime.py run -- <command>")

    config = get_resolved_config()
    if not getattr(args, "skip_probe", False):
        probe_target(config)

    command = build_command_text(config, command_args)
    env = target_env(config)
    if config["mode"] == "local":
        return run_local(command, env=env)
    return run_remote(config, command)


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            return print_status(get_resolved_config(), as_json=args.json)
        if args.command == "configure":
            return configure_runtime(args)
        if args.command == "target":
            return configure_target(args)
        if args.command == "run":
            return run_command(args)
        raise ConfigError(f"Unsupported command: {args.command}")
    except ConfigError as exc:
        sys.stderr.write(f"target-runtime: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
