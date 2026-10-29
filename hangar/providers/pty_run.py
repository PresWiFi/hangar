"""Run a command in a pseudo-terminal and keep its output, so Hangar can drive an interactive sign-in.

The CLIs print a sign-in link and sometimes wait for a pasted code. A pty (rather than a pipe) makes them
behave as they do in a terminal. The pty is very wide so long links don't wrap.
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import threading
import time

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
MAX_OUTPUT = 64 * 1024


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Hangar's environment minus anything that would tie the child to an outer Claude Code session."""
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_") and not k.startswith("HANGAR_")}
    env.update({"TERM": "xterm-256color", "BROWSER": "true", "NO_COLOR": "1"})
    env.update(extra or {})
    return env


class PtyRun:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: str | None = None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                                     start_new_session=True, close_fds=True)
        os.close(slave)
        self._fd = master
        self._buf = ""
        self._lock = threading.Lock()
        self.started = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self._fd, 4096)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self._buf = (self._buf + data.decode("utf-8", "replace"))[-MAX_OUTPUT:]
        self.proc.wait()

    @property
    def text(self) -> str:
        """Output so far, without colours or cursor codes."""
        with self._lock:
            raw = self._buf
        return _ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")

    @property
    def done(self) -> bool:
        return self.proc.poll() is not None

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()

    def write(self, text: str) -> None:
        os.write(self._fd, text.encode())

    def wait_for(self, pattern: re.Pattern, timeout: float) -> re.Match | None:
        end = time.time() + timeout
        while time.time() < end:
            m = pattern.search(self.text)
            if m:
                return m
            if self.done:
                return pattern.search(self.text)
            time.sleep(0.1)
        return None

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if not self.done:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            os.close(self._fd)
        except OSError:
            pass
