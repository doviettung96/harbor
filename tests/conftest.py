"""Shared pytest fixtures.

The global safety net here keeps tests from ever writing into the *real*
Harbor global registry (``index.db`` under the user's config dir). Harbor
resolves that dir from environment variables at call time
(``harbor_data_dir()`` reads ``APPDATA`` on Windows, ``XDG_CONFIG_HOME``/``HOME``
elsewhere), so redirecting those env vars per-test isolates BOTH in-process
calls and any subprocess (e.g. ``python -m harbor.bootstrap``, ``install.py``)
that a test spawns -- which a per-function ``monkeypatch.setattr`` cannot do.

Without this, a test that bootstraps/registers a project without explicitly
isolating its data dir leaves a dead row pointing at a now-deleted
``pytest-of-<user>`` temp path, and those rows pile up in the webui project
list forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_harbor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "harbor-home"
    home.mkdir()
    # Windows (APPDATA) + POSIX (XDG_CONFIG_HOME / HOME) resolution paths.
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
