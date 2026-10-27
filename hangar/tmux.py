"""tmux control. Every Claude session Hangar starts lives in its own tmux session, so it keeps running
when the browser closes and any device can attach to it."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

import psutil

from .config import settings
from .sessions import live_claude_processes
from .store import kv_get, kv_set

CONF = Path(__file__).with_name("tmux.conf")
SEP = "|~|"  # tmux escapes control characters in format output, so use printable text


class TmuxError(Exception):
    pass


def base_cmd() -> list[str]:
    cmd = ["tmux"]
    if settings.tmux_socket:
        cmd += ["-S", settings.tmux_socket]
    return cmd + ["-f", str(CONF)]


def run(*args: str, input: str | None = None, check: bool = True) -> str:
    try:
        r = subprocess.run(base_cmd() + list(args), capture_output=True, text=True, input=input, timeout=10)
    except FileNotFoundError:
        raise TmuxError("tmux is not installed")
    if check and r.returncode != 0:
        err = r.stderr.strip()
        if "no server running" in err or "error connecting" in err:
            return ""
        raise TmuxError(err or f"tmux {args[0]} failed")
    return r.stdout


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name or ""))


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:28] or "session"


def _unique_name(base: str) -> str:
    existing = {s["name"] for s in list_sessions(with_live=False)}
    name, i = base, 2
    while name in existing:
        name, i = f"{base}-{i}", i + 1
    return name


def list_sessions(with_live: bool = True) -> list[dict]:
    fmt = SEP.join(["#{session_name}", "#{session_created}", "#{session_attached}", "#{session_activity}",
                    "#{@hangar_claude}", "#{@hangar_label}", "#{session_path}"])
    out = run("list-sessions", "-F", fmt, check=False)
    sessions = {}
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) < 7:
            continue
        name, created, attached, activity, claude_id, label, path = parts[:7]
        sessions[name] = {
            "name": name, "created": int(created or 0), "attached": int(attached or 0),
            "activity": int(activity or 0), "claude_id": claude_id or None, "label": label or None,
            "cwd": path, "hangar": bool(claude_id or label), "claude": None, "command": None,
        }
    if not sessions or not with_live:
        _remember_open(list(sessions.values()))
        return list(sessions.values())

    panes = run("list-panes", "-a", "-F", SEP.join(["#{session_name}", "#{pane_pid}", "#{pane_current_command}",
                                                    "#{pane_current_path}"]), check=False)
    live = live_claude_processes()
    for line in panes.splitlines():
        parts = line.split(SEP)
        if len(parts) < 4 or parts[0] not in sessions:
            continue
        s = sessions[parts[0]]
        s["command"] = s["command"] or parts[2]
        s["cwd"] = parts[3] or s["cwd"]
        info = _find_claude(int(parts[1]), live) if parts[1].isdigit() else None
        if info:
            # The running process is the source of truth: /clear or /resume inside Claude changes the ID.
            s["claude"] = {"status": info.get("status"), "name": info.get("name"), "pid": info.get("pid")}
            if info.get("sessionId") and info["sessionId"] != s["claude_id"]:
                s["claude_id"] = info["sessionId"]
                run("set-option", "-t", f"={s['name']}:", "@hangar_claude", s["claude_id"], check=False)
    _remember_open(list(sessions.values()))
    return list(sessions.values())


def _find_claude(pane_pid: int, live: dict[int, dict]) -> dict | None:
    if not live:
        return None
    try:
        proc = psutil.Process(pane_pid)
        procs = [proc] + proc.children(recursive=True)
    except psutil.Error:
        return None
    for p in procs:
        if p.pid in live:
            return live[p.pid]
    return None


def _remember_open(sessions: list[dict]) -> None:
    """Track which Claude sessions are open right now. Sessions that close while Hangar is running drop off."""
    current = sorted(
        ({"claude_id": s["claude_id"], "cwd": s["cwd"], "name": s["name"]} for s in sessions if s["claude_id"]),
        key=lambda s: s["name"])
    if current != kv_get("open_sessions", []):
        kv_set("open_sessions", current)


def snapshot_for_restore() -> None:
    """At startup: whatever was open when Hangar last stopped becomes the list it can restore."""
    previous = kv_get("open_sessions", [])
    if previous:
        kv_set("restore_candidates", previous)


def restore_candidates() -> list[dict]:
    running = {s["claude_id"] for s in list_sessions(with_live=False)}
    return [c for c in kv_get("restore_candidates", []) if c.get("claude_id") and c["claude_id"] not in running]


def dismiss_restore() -> None:
    kv_set("restore_candidates", [])


def claude_command(extra: list[str]) -> str:
    """Shell command that runs Claude and then leaves a shell behind, so the session stays usable."""
    args = shlex.split(settings.claude_base_args) + shlex.split(settings.claude_args) + extra
    claude = " ".join([shlex.quote(settings.claude_cmd)] + [shlex.quote(a) for a in args])
    note = "Claude exited. This is a shell in the same folder. Type exit to close the session."
    return f"{claude}; printf '\\n\\033[2m%s\\033[0m\\n' {shlex.quote(note)}; exec \"${{SHELL:-/bin/bash}}\" -l"


def new_session(cwd: str, *, label: str = "", resume: str | None = None, fork: bool = False,
                model: str = "", permission_mode: str = "", shell_only: bool = False, prompt: str = "") -> dict:
    """Start Claude (or a plain shell) in a new tmux session. `prompt` is sent as the opening message."""
    cwd = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        raise TmuxError(f"Folder not found: {cwd}")
    name = _unique_name("hg-" + _slug(label or Path(cwd).name))
    if resume:
        claude_id = resume
        extra = ["--resume", resume] + (["--fork-session"] if fork else [])
    else:
        claude_id = str(uuid.uuid4())
        extra = ["--session-id", claude_id]
        if label:
            extra += ["--name", label]
    if model:
        extra += ["--model", model]
    if permission_mode:
        extra += ["--permission-mode", permission_mode]
    if prompt:
        extra += ["--", prompt]  # "--" so options that take several values can't swallow the prompt

    if shell_only:
        claude_id = ""
        command = 'exec "${SHELL:-/bin/bash}" -l'
    else:
        command = claude_command(extra)
    env = ["-e", "TERM=xterm-256color", "-e", "COLORTERM=truecolor"]
    run("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50", *env, "bash", "-lc", command)
    target = f"={name}:"  # set-option takes a pane target; "=" means exact name match
    if claude_id:
        run("set-option", "-t", target, "@hangar_claude", claude_id, check=False)
    run("set-option", "-t", target, "@hangar_label", label or Path(cwd).name, check=False)
    # Hangar draws its own header, so hide tmux's status bar in sessions it owns.
    run("set-option", "-t", target, "status", "off", check=False)
    list_sessions(with_live=False)  # record it as open right away, for restore after a restart
    return {"name": name, "claude_id": claude_id or None}


def find_by_claude(claude_id: str) -> dict | None:
    return next((s for s in list_sessions() if s["claude_id"] == claude_id), None)


def kill(name: str) -> None:
    run("kill-session", "-t", f"={name}")


def capture(name: str, lines: int = 60) -> str:
    return run("capture-pane", "-p", "-J", "-t", f"={name}:", "-S", f"-{lines}", check=False)


def send_text(name: str, text: str, enter: bool = True) -> None:
    """Paste text into the session as one bracketed paste (multi-line safe), then press Enter."""
    if text:
        run("load-buffer", "-b", "hangar-send", "-", input=text)
        run("paste-buffer", "-p", "-d", "-b", "hangar-send", "-t", f"={name}:")
    if enter:
        run("send-keys", "-t", f"={name}:", "Enter")


def send_key(name: str, key: str) -> None:
    allowed = {"Escape", "Tab", "BTab", "Up", "Down", "Left", "Right", "Enter", "C-c", "C-d", "C-l", "C-o",
               "C-r", "C-t", "C-b", "PageUp", "PageDown", "Space", "y", "n", "1", "2", "3"}
    if key not in allowed:
        raise TmuxError(f"Key not allowed: {key}")
    run("send-keys", "-t", f"={name}:", key)


def attach_argv(name: str) -> list[str]:
    return base_cmd() + ["attach-session", "-t", f"={name}"]


def restore(resumable=None) -> list[str]:
    """Resume the Claude sessions that were open when Hangar last stopped and aren't running now.
    `resumable` filters out sessions without a conversation log (they have nothing to resume)."""
    started = []
    for item in restore_candidates():
        cid, cwd = item["claude_id"], item.get("cwd")
        if not cwd or not os.path.isdir(cwd) or (resumable and not resumable(cid)):
            continue
        try:
            new_session(cwd, label=item.get("name", "").removeprefix("hg-"), resume=cid)
            started.append(cid)
        except TmuxError:
            continue
    dismiss_restore()
    return started
