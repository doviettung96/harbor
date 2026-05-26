from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn

from harbor.agtx_client import AgtxDb, init_test_db
from harbor.webui.server import create_app


HOST = "127.0.0.1"
PORT = int(os.environ.get("HARBOR_MANUAL_SESSION_PROBE_PORT", "8765"))
PROOF_PATH = ROOT / ".agtx" / "proofs" / "manual-session-modal.png"


def _pid_command_line(pid: str) -> str:
    if os.name != "nt":
        with contextlib.suppress(Exception):
            return subprocess.check_output(
                ["ps", "-p", pid, "-o", "command="],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        return ""
    with contextlib.suppress(Exception):
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\").CommandLine",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return output.strip()
    return ""


def _listening_pids(port: int) -> set[str]:
    with contextlib.suppress(Exception):
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        pids: set[str] = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            local = parts[1]
            state = parts[3].upper()
            pid = parts[4]
            if state == "LISTENING" and local.rsplit(":", 1)[-1] == str(port):
                pids.add(pid)
        return pids
    return set()


def _stop_stale_harbor_server(port: int) -> None:
    current = str(os.getpid())
    for pid in _listening_pids(port):
        if pid == current:
            continue
        command = _pid_command_line(pid)
        lowered = command.lower()
        if "harbor" not in lowered and "uvicorn" not in lowered:
            raise RuntimeError(
                f"port {port} is already in use by pid {pid}, not a Harbor server: {command}"
            )
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], check=False, capture_output=True)
        deadline = time.time() + 5
        while pid in _listening_pids(port) and time.time() < deadline:
            time.sleep(0.1)
        if pid in _listening_pids(port):
            raise RuntimeError(f"failed to stop stale Harbor server pid {pid} on port {port}")


def _wait_ready(url: str, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Harbor did not become ready at {url}: {last_error}")


def _build_app(tmpdir: Path):
    project_dir = tmpdir / "project"
    project_dir.mkdir()
    config_path = project_dir / "harbor.yml"
    config_path.write_text(
        "agtx:\n"
        "  agent_command: \"claude --dangerously-skip-permissions\"\n"
        "  agent_command_by_agent:\n"
        "    claude: \"claude --dangerously-skip-permissions\"\n"
        "    codex: \"codex --enable goals\"\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    init_test_db(conn, kind="project")
    db = AgtxDb(project_db_p=None, connection=conn)  # type: ignore[arg-type]
    return create_app(
        project_dir,
        db=db,
        autostart_worker=False,
        runtime_config_path=config_path,
        agent_command=["claude", "--dangerously-skip-permissions"],
        agent_command_by_agent={
            "claude": ["claude", "--dangerously-skip-permissions"],
            "codex": ["codex", "--enable", "goals"],
        },
    )


def _stop_server(server: uvicorn.Server | None, thread: threading.Thread | None) -> None:
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=10)
    if thread is not None and thread.is_alive():
        raise RuntimeError("Harbor probe server did not stop")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    _stop_stale_harbor_server(PORT)
    PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)

    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="harbor-manual-session-") as raw_tmp:
            try:
                app = _build_app(Path(raw_tmp))
                config = uvicorn.Config(
                    app,
                    host=HOST,
                    port=PORT,
                    log_level="warning",
                    lifespan="on",
                )
                server = uvicorn.Server(config)
                thread = threading.Thread(target=server.run, name="harbor-probe-server", daemon=True)
                thread.start()
                url = f"http://{HOST}:{PORT}/projects/default"
                _wait_ready(url)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    try:
                        page = browser.new_page(viewport={"width": 1280, "height": 800})
                        page.goto(url, wait_until="domcontentloaded")
                        if page.locator(".manual-session-picker summary").count() == 0:
                            html = page.content()
                            idx = html.find("board-toolbar")
                            snippet = html[idx - 100:idx + 900] if idx >= 0 else html[:1000]
                            raise RuntimeError(
                                "manual session picker was not rendered at "
                                f"{page.url}; relevant html: {snippet}"
                            )
                        page.locator(".manual-session-picker summary").wait_for(
                            state="visible",
                            timeout=5000,
                        )
                        page.locator(".manual-session-picker summary").click()
                        page.locator(".manual-session-picker[open]").wait_for(
                            state="attached",
                            timeout=5000,
                        )
                        modal = page.locator('[role="dialog"][aria-label="New manual session"]')
                        modal.wait_for(state="visible", timeout=5000)
                        select = modal.locator('select[name="agent"]')
                        select.wait_for(state="visible", timeout=5000)
                        options = select.locator("option").all_text_contents()
                        if "claude" not in options or "codex" not in options:
                            raise RuntimeError(f"agent selector options were not rendered: {options}")
                        modal.screenshot(path=str(PROOF_PATH))
                    finally:
                        browser.close()
            finally:
                _stop_server(server, thread)
    finally:
        if _listening_pids(PORT):
            raise RuntimeError(f"Harbor probe left port {PORT} occupied")

    if not PROOF_PATH.exists() or PROOF_PATH.stat().st_size == 0:
        raise RuntimeError(f"screenshot was not written: {PROOF_PATH}")
    print(f"wrote {PROOF_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
