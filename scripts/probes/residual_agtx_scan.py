#!/usr/bin/env python3
"""Fail if Harbor public workflow files still expose old agtx identity strings."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TRANSIENT_DOT_DIR = "." + "agtx"
SKIP_DIRS = {
    ".git",
    TRANSIENT_DOT_DIR,
    ".venv",
    "__pycache__",
    ".pytest_cache",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pyc",
    ".sqlite",
    ".db",
}
SKIP_FILES = {
    Path("scripts/probes/residual_agtx_scan.py"),
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int | None
    rule: str
    text: str


TEXT_RULES: tuple[tuple[str, str | re.Pattern[str]], ...] = (
    ("legacy MCP registration command", "agtx mcp-serve"),
    ("legacy MCP registration target", "mcp add " + "agtx"),
    ("legacy runtime bootstrap command", "agtx trust && " + "agtx"),
    ("legacy project initialization wording", "Have " + "agtx" + " registered"),
    ("mcp tool namespace", "mcp__" + "agtx__"),
    ("dot workflow path slash", "." + "agtx/"),
    ("dot workflow path backslash", "." + "agtx" + "\\"),
    ("yaml config key", re.compile(r"(?m)^\s*" + "agtx" + r":\s*(?:#.*)?$")),
    ("slash skill command", re.compile(r"(?<![\w-])/" + "agtx" + r"-[A-Za-z0-9_-]+")),
    ("backtick skill name", re.compile(r"`" + "agtx" + r"-[A-Za-z0-9_-]+`")),
)
PATH_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dot workflow path", re.compile(r"(^|[\\/])\." + "agtx" + r"([\\/]|$)")),
    ("skill filename", re.compile(r"(^|[\\/])" + "agtx" + r"-[^\\/]*")),
)


def _candidate_paths() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    out: list[Path] = []
    for raw in proc.stdout.splitlines():
        rel = Path(raw)
        if rel in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.suffix.lower() in SKIP_SUFFIXES:
            continue
        out.append(rel)
    return out


def _line_for(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan() -> list[Finding]:
    findings: list[Finding] = []
    for rel in _candidate_paths():
        rel_text = rel.as_posix()
        for rule, pattern in PATH_RULES:
            if pattern.search(rel_text):
                findings.append(Finding(rel, None, rule, rel_text))

        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for rule, pattern in TEXT_RULES:
            if isinstance(pattern, str):
                start = text.find(pattern)
                while start != -1:
                    findings.append(
                        Finding(rel, _line_for(text, start), rule, pattern)
                    )
                    start = text.find(pattern, start + 1)
                continue
            for match in pattern.finditer(text):
                findings.append(
                    Finding(rel, _line_for(text, match.start()), rule, match.group(0))
                )
    return findings


def main() -> int:
    findings = scan()
    if findings:
        for finding in findings:
            loc = str(finding.path)
            if finding.line is not None:
                loc = f"{loc}:{finding.line}"
            print(f"{loc}: {finding.rule}: {finding.text}")
        return 1
    print("residual agtx scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
