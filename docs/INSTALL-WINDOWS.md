# Windows install notes

## tmux

This repo expects a working `tmux` binary on PATH. Earlier work in the bead-era harbor template flagged severe issues with `marlocarlo.psmux`:

- `tmux new-window` silently dropped trailing commands
- `tmux attach` failed with "no server running" even after a successful detached `new-session` on the same socket
- `tmux kill-window -t target` ignored the target argument

Those issues are documented in `WINDOWS_TMUX.md` for historical reference. **Do not use psmux.** Install a working build (the current Windows setup uses an alternative tmux that operates correctly with `new-session`, `new-window`, attach, and `kill-window -t <target>`).

Verify your install before trusting the Harbor workflow:

```powershell
tmux -V
tmux new-session -d -s probe sh -c 'echo TMUX_OK; sleep 1'
tmux capture-pane -t probe -p
tmux kill-session -t probe
```

You should see `TMUX_OK` in the captured output. If you don't, your tmux is broken — fix that before continuing.

## Python

Python 3.10+ on PATH. Recommended: a virtualenv per repo so harbor's editable install doesn't pollute the global site-packages.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
```

## Harbor MCP

Register Harbor's MCP server with your agent after the editable install:

```powershell
claude mcp add harbor -- python -m harbor mcp-serve
```

Then start Harbor's webui when you want the Windows-friendly board and transition worker:

```powershell
cd D:\Projects\harbor
python -m harbor webui --project-path .
```

## ADB / emulators (game-RE work only)

If your tasks target an emulator or ADB device, install:

- `platform-tools` (`adb`) on PATH.
- The emulator of your choice (LDPlayer, MuMu, BlueStacks). Note the emulator's ADB port (default LDPlayer: 5555) — you'll set it via `target_runtime.py target set-emulator --adb-port=...`.

Verify ADB works before configuring `target.probe_command`:

```powershell
adb devices
adb -s 127.0.0.1:5555 get-state
```

Both should succeed. If they don't, the emulator isn't booted or the port is wrong — fix that before configuring the runtime target.

## Frida (optional, advanced game-RE)

Frida tooling lives outside this repo. If a task uses Frida, the per-task `## Verification Probes` should call your Frida scripts directly via `target-runtime-exec`. The runtime target config doesn't bundle a Frida client — you provide your own and reference it from the probe command.
