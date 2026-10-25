"""Bridges a browser terminal (xterm.js over a WebSocket) to `tmux attach` running in a pty.

Closing the browser only detaches this tmux client; the session keeps running.
Protocol: binary frames carry keystrokes and output; text frames are JSON control messages
({"type": "resize", "cols": n, "rows": n}).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import struct
import subprocess
import termios

from starlette.websockets import WebSocket, WebSocketDisconnect


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(10, min(cols, 500)), max(5, min(rows, 300))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_controlling_tty() -> None:
    # Runs in the child after setsid(): make the pty our controlling terminal so resizes send SIGWINCH.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def bridge(ws: WebSocket, argv: list[str], cols: int, rows: int) -> None:
    master, slave = os.openpty()
    _set_size(master, cols, rows)
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    env.pop("TMUX", None)
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                            start_new_session=True, preexec_fn=_make_controlling_tty)
    os.close(slave)
    os.set_blocking(master, False)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def readable() -> None:
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master)
            queue.put_nowait(None)
        else:
            queue.put_nowait(data)

    loop.add_reader(master, readable)

    async def pump_out() -> None:
        while True:
            data = await queue.get()
            if data is None:
                break
            # Coalesce bursts (Claude redraws a lot) into one frame.
            while not queue.empty() and len(data) < 256 * 1024:
                more = queue.get_nowait()
                if more is None:
                    await ws.send_bytes(data)
                    return
                data += more
            await ws.send_bytes(data)

    async def pump_in() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                _write_all(master, msg["bytes"])
            elif msg.get("text"):
                try:
                    ctl = json.loads(msg["text"])
                except ValueError:
                    continue
                if ctl.get("type") == "resize":
                    _set_size(master, int(ctl.get("cols", 80)), int(ctl.get("rows", 24)))
                elif ctl.get("type") == "input" and isinstance(ctl.get("data"), str):
                    _write_all(master, ctl["data"].encode())

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        try:
            loop.remove_reader(master)
        except Exception:
            pass
        try:
            os.killpg(proc.pid, signal.SIGHUP)  # detaches the tmux client; the session lives on
        except ProcessLookupError:
            pass
        os.close(master)
        await asyncio.get_running_loop().run_in_executor(None, _reap, proc)
        try:
            await ws.close()
        except Exception:
            pass


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            import select
            select.select([], [fd], [], 1)


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
