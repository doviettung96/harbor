"""PTY-backed terminal bridge for embedded tmux attach clients."""
from __future__ import annotations

import os
import select
import shlex
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class TerminalError(RuntimeError):
    """Raised when a terminal session cannot be started."""


class PtySession(ABC):
    """Small cross-platform PTY process interface used by the web socket."""

    @abstractmethod
    def read(self) -> str:
        """Return the next output chunk. May block until output or process exit."""

    @abstractmethod
    def write(self, data: str) -> None:
        """Write browser input to the PTY."""

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY."""

    @abstractmethod
    def close(self) -> None:
        """Close only this attached PTY client."""


class PtyBackend(ABC):
    @abstractmethod
    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> PtySession:
        """Spawn an interactive process attached to a PTY."""


@dataclass
class WinptyBackend(PtyBackend):
    """Windows ConPTY backend via pywinpty."""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> PtySession:
        try:
            from winpty import PtyProcess
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise TerminalError(
                "pywinpty is required for embedded terminals on Windows"
            ) from exc

        cmdline = subprocess.list2cmdline([str(a) for a in argv])
        kwargs = {
            "cwd": str(cwd) if cwd is not None else None,
            "dimensions": (rows, cols),
        }
        try:
            proc = PtyProcess.spawn(cmdline, **kwargs)
        except TypeError:
            kwargs.pop("dimensions", None)
            proc = PtyProcess.spawn(cmdline, **kwargs)
            _resize_winpty_process(proc, cols, rows)
        except Exception as exc:  # pragma: no cover - depends on host tmux
            raise TerminalError(f"failed to start PTY: {exc}") from exc
        return _WinptySession(proc)


def _resize_winpty_process(proc: object, cols: int, rows: int) -> None:
    if hasattr(proc, "set_size"):
        proc.set_size(rows, cols)  # type: ignore[attr-defined]
    elif hasattr(proc, "setwinsize"):
        proc.setwinsize(rows, cols)  # type: ignore[attr-defined]
    elif hasattr(proc, "resize"):
        proc.resize(cols, rows)  # type: ignore[attr-defined]


@dataclass
class _WinptySession(PtySession):
    proc: object

    def read(self) -> str:
        try:
            return self.proc.read()  # type: ignore[attr-defined]
        except EOFError:
            return ""

    def write(self, data: str) -> None:
        self.proc.write(data)  # type: ignore[attr-defined]

    def resize(self, cols: int, rows: int) -> None:
        _resize_winpty_process(self.proc, cols, rows)

    def close(self) -> None:
        for name in ("terminate", "kill", "close"):
            fn = getattr(self.proc, name, None)
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                pass
            return


@dataclass
class PosixPtyBackend(PtyBackend):
    """POSIX stdlib pty backend."""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        cols: int = 80,
        rows: int = 24,
    ) -> PtySession:
        if os.name == "nt":  # pragma: no cover - defensive
            raise TerminalError("POSIX PTY backend is not available on Windows")
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
            proc = subprocess.Popen(
                list(argv),
                cwd=str(cwd) if cwd is not None else None,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise TerminalError(f"failed to start PTY: {exc}") from exc
        os.close(slave_fd)
        return _PosixPtySession(master_fd=master_fd, proc=proc)


@dataclass
class _PosixPtySession(PtySession):
    master_fd: int
    proc: subprocess.Popen[bytes]

    def read(self) -> str:
        while True:
            if self.proc.poll() is not None:
                return ""
            ready, _, _ = select.select([self.master_fd], [], [], 0.25)
            if not ready:
                continue
            try:
                data = os.read(self.master_fd, 4096)
            except OSError:
                return ""
            if not data:
                return ""
            return data.decode("utf-8", errors="replace")

    def write(self, data: str) -> None:
        os.write(self.master_fd, data.encode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(
            self.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        try:
            os.killpg(self.proc.pid, signal.SIGWINCH)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass
        deadline = time.monotonic() + 1.0
        while self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def default_backend() -> PtyBackend:
    return WinptyBackend() if sys.platform == "win32" else PosixPtyBackend()


def tmux_attach_argv(server: str, session: str) -> list[str]:
    """Build argv for attaching to a task tmux session."""
    return ["tmux", "-L", server, "attach", "-t", session]


def format_attach_command(server: str, session: str) -> str:
    return " ".join(shlex.quote(p) for p in tmux_attach_argv(server, session))
