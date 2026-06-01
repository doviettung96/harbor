---
name: target-runtime-exec
description: "Use when project execution should follow the repo's selected runtime target instead of assuming the local machine. This covers build, test, run, deploy, migration, codegen, environment-bootstrap, or game-RE probe commands that should route through `scripts/shared/target_runtime.py` when `.harbor/runtime-target.json` selects SSH or pins a specific emulator/device/game window."
---

# Target Runtime Exec

Route environment-dependent project commands through the repo's selected runtime target.

Keep local exploration local. This skill is only for commands that execute repo code or depend on the target runtime environment.

## Use This For

- builds
- tests
- app or service launch commands
- migrations
- code generation tied to the project runtime
- Docker, Conda, or similar repo bootstrap commands
- game-RE probes that touch an emulator, ADB device, or specific game window

## Do Not Use This For

- reading files
- searching the repo
- `git status`, `git diff`, or similar inspection
- checking what tools or files exist locally

## Steps

1. Inspect the runtime target:

   ```bash
   python scripts/shared/target_runtime.py status
   ```

2. If the status reports `local` and `target.kind` is `local`, run the project command through the helper anyway so the execution path stays explicit:

   ```bash
   python scripts/shared/target_runtime.py run -- <exact command>
   ```

3. If the status reports `ssh`, use the same helper command. It will:
   - sync the repo to the configured remote workdir
   - execute the command on the configured SSH host
   - fail if the SSH target, sync step, or remote command fails

4. If `target.kind` is `emulator`, `device`, or `game_window`, the helper will first run `scripts/shared/probe_target.py` (or `target.probe_command` if set) to confirm the target is reachable. If the probe fails, the helper exits non-zero and the command never runs.

5. Preserve the repo's exact command string.
   - Do not substitute another command because it seems more convenient.
   - Prefer repo-owned wrapper scripts when the repo already provides them.

## Command Shape

Always pass the exact project command after `--`.

Examples:

```bash
python scripts/shared/target_runtime.py run -- pytest tests/api/test_health.py -q
python scripts/shared/target_runtime.py run -- bash scripts/verify.sh
python scripts/shared/target_runtime.py run -- pwsh -File .\scripts\verify.ps1
python scripts/shared/target_runtime.py run -- docker compose up --build --detach
python scripts/shared/target_runtime.py run -- adb -s $env:AWT_TARGET_DEVICE_ID logcat -d
```

The helper injects these env vars into the child process when `target.kind` is non-local:
- `AWT_TARGET_KIND`
- `AWT_TARGET_EMULATOR_NAME`
- `AWT_TARGET_DEVICE_ID`
- `AWT_TARGET_GAME_WINDOW_TITLE`

## Hard Rules

- Do not silently bypass the helper for runtime-dependent commands.
- Do not silently fall back to local execution if SSH mode fails.
- Do not silently fall back to local execution if the emulator/device/game-window probe fails. The probe is a hard gate.
- If the configured target is invalid, stop and report the exact config or connectivity problem.
- If the surrounding task is `Configure target runtime for this repo`, ask the user to choose `local`, `ssh`, `emulator`, `device`, or `game_window` before treating the current config as the answer.
- If the user chooses `ssh`, collect or confirm `ssh_host`, `remote_platform`, and `remote_workdir` before proceeding.
- If the user chooses `emulator`, `device`, or `game_window`, collect or confirm the corresponding subobject in `.harbor/runtime-target.json` and define `target.probe_command` so probes are reproducible.
- If Python-based commands should run under a specific remote interpreter, collect or confirm `remote_python` too.
