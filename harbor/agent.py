"""Agent profiles: which CLI to launch with which model + reasoning effort.

Loaded from `harbor.yml` if present, else falls back to a small set of built-in
profiles. The single point of model/effort selection that the user can override
per-bead in the webview.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentProfile:
    """One named profile in harbor.yml. Maps a {profile-name} -> agent CLI invocation.

    `args_template` may contain `{model}` and `{effort}` placeholders. Anything
    else stays literal. Empty values render as empty strings — caller filters
    them out so optional flags can drop cleanly.

    `prompt_injection` controls how harbor pushes the worker prompt into the
    interactive pane after launching the agent CLI:
      - "file_ref": type `@<absolute-prompt-path>` then Enter (claude REPL).
      - "send_keys": paste the prompt body verbatim via `tmux send-keys -l` then Enter.
      - "prompt_arg": the prompt is passed as part of the launch command line —
        `launch_template` is required and must include `{prompt_path}`. Harbor
        skips the post-launch inject step entirely. Required for codex's TUI,
        which submits on Enter and so cannot accept a multi-line paste.
      - "stdin": legacy non-interactive path — the prompt goes on the agent's
        stdin via `harbor-bead-runner`. Not used by the interactive orchestrator.

    `launch_template`, if non-empty, is a literal shell command line typed into
    the pane via `send-keys` (single-line). It supports `{model}`, `{effort}`,
    and `{prompt_path}` placeholders and bypasses the argv-style command +
    args_template. This is how we get codex to accept a multi-line prompt: by
    relying on the pane's shell to substitute the prompt file contents inline,
    e.g. `codex -m {model} ... (Get-Content -Raw '{prompt_path}')` on
    PowerShell, or `codex -m {model} ... "$(cat '{prompt_path}')"` on bash.
    """

    name: str
    agent_kind: str  # "codex" | "claude" | <custom>
    command: list[str]
    args_template: list[str]
    model: str = ""
    effort: str = ""
    env: dict[str, str] = field(default_factory=dict)
    prompt_injection: str = "file_ref"
    launch_template: str = ""

    def render_argv(self, *, model: str | None = None, effort: str | None = None) -> list[str]:
        """Return the full argv to exec, with model/effort substituted."""
        m = model if model is not None else self.model
        e = effort if effort is not None else self.effort
        rendered: list[str] = []
        for tok in self.args_template:
            sub = tok.format(model=m, effort=e)
            if "{" in tok and not sub:
                # Placeholder resolved to empty — drop the flag.
                continue
            rendered.append(sub)
        return [*self.command, *rendered]


# Built-in defaults so harbor works before the user creates a harbor.yml.
# Args reflect the most common codex/claude CLI shapes; users override via
# harbor.yml when their CLIs disagree.
_BUILTIN: dict[str, dict[str, Any]] = {
    "fast": {
        "agent_kind": "codex",
        "command": ["codex"],
        "args_template": ["-m", "{model}", "--reasoning-effort", "{effort}"],
        "model": "gpt-5.3-codex",
        "effort": "low",
    },
    "balanced": {
        "agent_kind": "codex",
        "command": ["codex"],
        "args_template": ["-m", "{model}", "--reasoning-effort", "{effort}"],
        "model": "gpt-5.3-codex",
        "effort": "medium",
    },
    "thorough": {
        "agent_kind": "codex",
        "command": ["codex"],
        "args_template": ["-m", "{model}", "--reasoning-effort", "{effort}"],
        "model": "gpt-5.3-codex",
        "effort": "high",
    },
    "claude-opus": {
        "agent_kind": "claude",
        "command": ["claude"],
        "args_template": ["--model", "{model}", "--dangerously-skip-permissions"],
        "model": "claude-opus-4-7",
        "effort": "",
    },
}


@dataclass(frozen=True)
class Config:
    profiles: dict[str, AgentProfile]
    default_profile: str
    # Optional path to the shell tmux should use for harbor panes. On Windows
    # this defaults (auto-detected) to Git Bash so launch_templates can rely on
    # POSIX-style command substitution (`"$(cat 'path')"`) without fighting
    # PowerShell's native-arg-pass quirks. None means "use tmux's default".
    default_shell: str | None = None
    # Default agent invocation for the Harbor webview, read from harbor.yml's
    # `harbor.agent_command`. When set, the webview uses this when no CLI
    # `--agent-command` was passed. The CLI flag still wins over this value
    # so a one-off override is always possible. Tuple form to match
    # TransitionConfig.agent_command.
    harbor_agent_command: tuple[str, ...] | None = None
    # Per-agent worker CLI overrides for the Harbor webview, read from
    # harbor.yml's `harbor.agent_command_by_agent`. Maps a task agent name
    # (claude/codex/gemini/...) to the argv harbor launches for that agent's
    # worker session. Checked before `harbor_agent_command` when resolving a
    # task's worker, so the global command can target the manual planning
    # session while each task's worker still follows its own agent. The
    # webui's `--map-agent` CLI flag overrides any key set here.
    harbor_agent_command_by_agent: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Workflow plugin to use for phase commands/prompts/auto-dismiss. Read
    # from `harbor.plugin` in harbor.yml; the CLI `--plugin` flag overrides.
    # Can be a plain plugin name (searched in <repo>/plugins/, .harbor/plugins/,
    # ~/.config/harbor/plugins/) or a direct path to plugin.toml.
    harbor_plugin: str | None = None
    # Free-form repo-level instructions appended to every Harbor phase prompt and
    # written into each task worktree for skills to read later.
    harbor_prompt_append: str = ""

    def get(self, name: str | None) -> AgentProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise KeyError(f"unknown profile {key!r}; available: {available}")
        return self.profiles[key]


def _auto_detect_default_shell() -> str | None:
    """On Windows, prefer Git Bash so launch_templates use POSIX shell semantics.

    Returns the absolute path to bash.exe if it can be found, else None. The
    path is normalized to forward slashes — tmux (a POSIX tool ported to
    Windows) accepts forward-slash paths and silently ignores set-option calls
    that pass native backslash paths.
    """
    if os.name != "nt":
        return None
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c.replace("\\", "/")
    return None


def _profile_from_dict(name: str, raw: dict[str, Any]) -> AgentProfile:
    injection = raw.get("prompt_injection", "file_ref")
    if injection not in {"file_ref", "send_keys", "prompt_arg", "stdin"}:
        raise ValueError(
            f"profile {name!r}: prompt_injection must be one of "
            f"'file_ref', 'send_keys', 'prompt_arg', 'stdin'; got {injection!r}"
        )
    launch_template = raw.get("launch_template", "")
    if injection == "prompt_arg" and "{prompt_path}" not in launch_template:
        raise ValueError(
            f"profile {name!r}: prompt_injection='prompt_arg' requires "
            "launch_template to contain a {prompt_path} placeholder"
        )
    return AgentProfile(
        name=name,
        agent_kind=raw.get("agent_kind") or raw.get("agent") or "codex",
        command=list(raw["command"]),
        args_template=list(raw.get("args_template", [])),
        model=raw.get("model", ""),
        effort=raw.get("effort", ""),
        env=dict(raw.get("env", {})),
        prompt_injection=injection,
        launch_template=launch_template,
    )


def _parse_harbor_agent_command(raw: Any) -> tuple[str, ...] | None:
    """Accept either a shell-quoted string or a list of strings.

    `harbor.yml`:
        harbor:
          agent_command: "codex -m gpt-5.5 --reasoning-effort high"
        # OR
        agent_command: [codex, -m, gpt-5.5, --reasoning-effort, high]
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        import shlex
        argv = shlex.split(raw)
        return tuple(argv) if argv else None
    if isinstance(raw, (list, tuple)):
        argv = [str(item) for item in raw if str(item).strip()]
        return tuple(argv) if argv else None
    raise ValueError(
        f"harbor.agent_command must be a string or list of strings, got {type(raw).__name__}"
    )


def _parse_harbor_agent_command_map(raw: Any) -> dict[str, tuple[str, ...]]:
    """Parse `harbor.agent_command_by_agent` — a mapping from a task agent
    name (claude/codex/gemini/...) to the CLI invocation harbor launches for
    that agent's worker session. Each value is a shell-quoted string or a list
    of strings, the same shapes `agent_command` accepts.

    `harbor.yml`:
        harbor:
          agent_command_by_agent:
            codex: "codex --yolo"
            claude: [claude, --dangerously-skip-permissions]
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"harbor.agent_command_by_agent must be a mapping, got {type(raw).__name__}"
        )
    out: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        argv = _parse_harbor_agent_command(value)
        if argv:
            out[str(key)] = argv
    return out


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load harbor.yml.

    Behavior:
      - `path` is an existing file → load it.
      - `path` is given but doesn't exist → return built-in defaults (no cwd lookup).
      - `path` is None → look at ./harbor.yml (legacy convenience); if absent,
        built-in defaults.

    The "path given but missing → built-ins" branch is important for callers
    like `create_app(repo_root)` that always pass a `<repo>/harbor.yml` path
    — we don't want them to silently pick up an unrelated harbor.yml from
    the user's current working directory.
    """
    if path is not None:
        p = Path(path)
        if not p.exists():
            p = None  # type: ignore[assignment]
    else:
        candidate = Path.cwd() / "harbor.yml"
        p = candidate if candidate.exists() else None  # type: ignore[assignment]

    if p is None:
        # Pure built-in defaults.
        profiles = {name: _profile_from_dict(name, raw) for name, raw in _BUILTIN.items()}
        return Config(
            profiles=profiles,
            default_profile="balanced",
            default_shell=_auto_detect_default_shell(),
        )

    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    raw_profiles = data.get("profiles") or {}
    profiles: dict[str, AgentProfile] = {
        name: _profile_from_dict(name, raw) for name, raw in raw_profiles.items()
    }
    # Fill in any built-ins the user didn't override (so `fast` etc. always exist).
    for name, raw in _BUILTIN.items():
        profiles.setdefault(name, _profile_from_dict(name, raw))

    default = data.get("default_profile") or "balanced"
    if default not in profiles:
        raise ValueError(
            f"default_profile {default!r} not found among profiles {sorted(profiles)}"
        )
    # Top-level default_shell: explicit user override beats auto-detection.
    default_shell = data.get("default_shell")
    if default_shell is None:
        default_shell = _auto_detect_default_shell()
    harbor_section = data.get("harbor") or {}
    if not isinstance(harbor_section, dict):
        raise ValueError(
            f"harbor must be a YAML mapping when present, got {type(harbor_section).__name__}"
        )
    harbor_agent_command = _parse_harbor_agent_command(harbor_section.get("agent_command"))
    harbor_agent_command_by_agent = _parse_harbor_agent_command_map(
        harbor_section.get("agent_command_by_agent")
    )
    harbor_plugin = harbor_section.get("plugin")
    if harbor_plugin is not None and not isinstance(harbor_plugin, str):
        raise ValueError(
            f"harbor.plugin must be a string (plugin name or path), got {type(harbor_plugin).__name__}"
        )
    harbor_prompt_append = harbor_section.get("prompt_append") or ""
    if not isinstance(harbor_prompt_append, str):
        raise ValueError(
            f"harbor.prompt_append must be a string, got {type(harbor_prompt_append).__name__}"
        )
    return Config(
        profiles=profiles,
        default_profile=default,
        default_shell=default_shell,
        harbor_agent_command=harbor_agent_command,
        harbor_agent_command_by_agent=harbor_agent_command_by_agent,
        harbor_plugin=harbor_plugin,
        harbor_prompt_append=harbor_prompt_append,
    )


def _profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
    data: dict[str, Any] = {
        "agent_kind": profile.agent_kind,
        "command": list(profile.command),
        "args_template": list(profile.args_template),
    }
    if profile.model:
        data["model"] = profile.model
    if profile.effort:
        data["effort"] = profile.effort
    if profile.env:
        data["env"] = dict(profile.env)
    if profile.prompt_injection != "file_ref":
        data["prompt_injection"] = profile.prompt_injection
    if profile.launch_template:
        data["launch_template"] = profile.launch_template
    return data


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Serialize the editable harbor.yml surface.

    `load_config` keeps built-in profiles available at runtime even when a
    user file only overrides one profile. Writing intentionally emits the
    current effective profile set so custom/user-only entries survive UI saves.
    """
    data: dict[str, Any] = {
        "default_profile": cfg.default_profile,
        "profiles": {
            name: _profile_to_dict(profile)
            for name, profile in sorted(cfg.profiles.items())
        },
    }
    if cfg.default_shell is not None:
        data["default_shell"] = cfg.default_shell
    harbor: dict[str, Any] = {}
    if cfg.harbor_agent_command:
        harbor["agent_command"] = list(cfg.harbor_agent_command)
    if cfg.harbor_agent_command_by_agent:
        harbor["agent_command_by_agent"] = {
            k: list(v) for k, v in sorted(cfg.harbor_agent_command_by_agent.items())
        }
    if cfg.harbor_plugin:
        harbor["plugin"] = cfg.harbor_plugin
    if cfg.harbor_prompt_append:
        harbor["prompt_append"] = cfg.harbor_prompt_append
    if harbor:
        data["harbor"] = harbor
    return data


def write_config(path: str | os.PathLike[str], cfg: Config, *, backup: bool = True) -> None:
    """Atomically write harbor.yml and validate that it round-trips."""
    p = Path(path)
    data = config_to_dict(cfg)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)

    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)
    if backup and p.exists():
        (parent / f"{p.name}.bak").write_bytes(p.read_bytes())

    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        load_config(tmp)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def harbor_config_dir() -> Path:
    """Return Harbor's global user config directory."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "harbor" / "config"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "harbor"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "harbor"


def global_runtime_config_path() -> Path:
    """Path to the shared live Harbor runtime config."""
    return harbor_config_dir() / "runtime.yml"


def load_runtime_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load the shared live runtime config.

    The global config wins on startup. If it is absent, return built-in defaults
    without looking at the current working directory or any project harbor.yml.
    """
    p = Path(path) if path is not None else global_runtime_config_path()
    if p.exists():
        return load_config(p)
    return load_config(p)


def write_runtime_config(
    cfg: Config,
    path: str | os.PathLike[str] | None = None,
    *,
    backup: bool = True,
) -> Path:
    """Persist the shared live runtime config and return its path."""
    p = Path(path) if path is not None else global_runtime_config_path()
    write_config(p, cfg, backup=backup)
    return p


def load_issue_prefix(repo_root: str | os.PathLike[str]) -> str | None:
    """Read Beads' local issue_prefix from .beads/config.yaml if configured."""
    path = Path(repo_root) / ".beads" / "config.yaml"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    prefix = data.get("issue_prefix") if isinstance(data, dict) else None
    if prefix is None:
        return None
    prefix_s = str(prefix).strip()
    return prefix_s or None
