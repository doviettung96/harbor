# harbor-workflow-template plugin

A self-contained Harbor workflow plugin: per-task acceptance criteria, three-header task descriptions, and runtime-target gating. Distributable to any project.

## Layout

```
harbor-workflow-template/
├── plugin.toml           ← workflow config (commands/prompts/artifacts/auto-dismiss)
├── install.py            ← copies skills into a target project
├── README.md             ← this file
└── skills/               ← ALL the skills the workflow needs, bundled
                              (present in an *installed* plugin only; in the
                              harbor repo the canonical copy lives at
                              <repo>/.claude/skills/ and install.py copies it in)
    ├── harbor-sweep-with-acceptance/  ← pre-task: brainstorm → tasks with 3 questions per task
    ├── harbor-task-worker/             ← per-task: planning + running phases
    ├── harbor-task-verify/             ← per-task: review phase, runs verification probes
    ├── brainstorming/                ← pre-task: explore before any task is created
    ├── build-and-test/               ← helper: discovery-based test runner
    ├── runtime-target-config/        ← project setup: configure .harbor/runtime-target.json
    ├── systematic-debugging/         ← helper: structured bug investigation
    ├── target-runtime-exec/          ← helper: route commands through runtime-target.json
    ├── verification-before-completion/ ← helper: gate "done" claims behind evidence
    └── writing-plans/                ← helper: planning rigor
```

## The four-phase task lifecycle

| Phase | What harbor types into the pane | What the agent does | Artifact that signals "done" |
|---|---|---|---|
| Planning | `/harbor-task-worker <id>` + a plan-phase prompt | Loads `harbor-task-worker` skill, parses three headers, plans the work | `.harbor/plan.md` |
| Running | `/harbor-task-worker <id>` + a run-phase prompt | Same skill, status flipped to running — implements the plan | `.harbor/execute.md` |
| Review  | `/harbor-task-verify` + a review-phase prompt | Loads `harbor-task-verify`, runs `## Verification Probes`, summarizes | `.harbor/review.md` |

The worker skill reads three headers from the task description (embedded by `harbor-sweep-with-acceptance`):
- `## Acceptance Criteria` — bullet-list success conditions
- `## Verification Probes` — shell commands run via `target-runtime-exec`
- `## Runtime Target` — local / SSH / emulator / device / game-window

## Installing into a new project

One command — copies skills, writes a starter `harbor.yml`, writes a runtime-target example:

```bash
python plugins/harbor-workflow-template/install.py /path/to/new-project
```

Flags:
- `--skills-dir <name>` — destination subdir (default: `skills`)
- `--force` — overwrite existing skill files
- `--no-harbor-yml` — skip writing the starter config
- `--no-runtime-target` — skip writing `.harbor/runtime-target.example.json`

Or do it manually:

```bash
mkdir -p /path/to/new-project/skills
# In the harbor repo the canonical skills live at .claude/skills/.
cp -r .claude/skills/* /path/to/new-project/skills/

cat > /path/to/new-project/harbor.yml <<'EOF'
harbor:
  plugin: harbor-workflow-template
  # agent_command: "codex"   # or whatever CLI you want spawned
EOF

# Then in the new project:
python -m harbor webui --project-path .
```

You also need to:
1. Make sure `harbor` is installed (`pip install -e <path-to-harbor>`).
2. Have agtx registered and the project initialized (`agtx trust && agtx`).
3. Have a working tmux on PATH (Windows: `arndawg.tmux-windows` — see `docs/WINDOWS_TMUX.md` in harbor).

## How harbor uses the plugin

When you click **Move forward** in harbor's webview, the `TransitionWorker`:

1. Reads `plugin.toml` (already loaded at startup via `harbor.plugin` in your `harbor.yml`).
2. Spawns the worktree + tmux session for the task.
3. Types `plugin.commands.<phase>` into the pane (e.g. `/harbor-task-worker abc12345`).
4. Types `plugin.prompts.<phase>` after the slash command.
5. Persists the new task status.
6. Polls `plugin.artifacts.<phase>` (e.g. `.harbor/plan.md`) to detect completion (UI hint only in v1).

agtx's TUI on Mac/Linux uses the same `plugin.toml` schema, so this bundle works there too — drop it under `~/.config/agtx/plugins/harbor-workflow-template/` or `<repo>/.harbor/plugins/harbor-workflow-template/` and set the workflow plugin in agtx's config.

## Schema reference

`plugin.toml` conforms to agtx's `WorkflowPlugin` schema (`D:/Projects/agtx/src/config/mod.rs:427-468`). Harbor's loader at `D:/Projects/harbor/harbor/plugin_loader.py` is a faithful Python port. Both consume the same TOML.

## Customizing

To tweak prompts/commands/artifacts for your project, edit this plugin's `plugin.toml` (after copying — don't change the harbor repo's bundled copy). Examples:

- **Add a research phase:** add `commands.research = "/harbor-research {task_id}"` + `artifacts.research = ".harbor/research.md"` + write a `.claude/skills/harbor-research/SKILL.md`.
- **Per-phase agent switching:** harbor v1 doesn't yet read `[agents]` from the plugin — use `--agent-command-<phase>` CLI flag instead, or wait for that feature.
- **Custom auto-dismiss patterns:** add `[[auto_dismiss]]` tables for whatever confirmation dialogs your agent shows at startup.
