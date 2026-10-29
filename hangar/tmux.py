"""tmux control. Every Claude or Codex session Hangar starts lives in its own tmux session, so it keeps
running when the browser closes and any device can attach to it.

tmux user options link a tmux session to its conversation: @hangar_claude holds the conversation ID (for
Codex too, once Hangar has seen which log the Codex process writes), @hangar_agent is "claude" or "codex",
and @hangar_launch holds how it was started (Codex mode, Ollama server and model) so resume can repeat it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

import psutil

from . import plugins
from .config import settings
from .codex_sessions import codex_index
from .sessions import live_claude_processes
from .store import all_meta, kv_get, kv_set, set_meta

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
                    "#{@hangar_claude}", "#{@hangar_label}", "#{session_path}", "#{@hangar_agent}", "#{@hangar_launch}"])
    out = run("list-sessions", "-F", fmt, check=False)
    sessions = {}
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) < 9:
            continue
        name, created, attached, activity, claude_id, label, path, agent, launch = parts[:9]
        sessions[name] = {
            "name": name, "created": int(created or 0), "attached": int(attached or 0),
            "activity": int(activity or 0), "claude_id": claude_id or None, "label": label or None,
            "cwd": path, "hangar": bool(claude_id or label), "claude": None, "command": None,
            "agent": agent or ("claude" if claude_id else None), "launch": _launch(launch),
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
        if s["agent"] == "codex":
            if parts[1].isdigit():
                _codex_live(s, int(parts[1]))
            continue
        info = _find_claude(int(parts[1]), live) if parts[1].isdigit() else None
        if info:
            # The running process is the source of truth: /clear or /resume inside Claude changes the ID.
            s["claude"] = {"status": info.get("status"), "name": info.get("name"), "pid": info.get("pid")}
            if info.get("sessionId") and info["sessionId"] != s["claude_id"]:
                s["claude_id"] = info["sessionId"]
                run("set-option", "-t", f"={s['name']}:", "@hangar_claude", s["claude_id"], check=False)
    _remember_open(list(sessions.values()))
    return list(sessions.values())


def _launch(raw: str) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _codex_live(s: dict, pane_pid: int) -> None:
    """Finds the Codex process under the pane and the rollout log it has open, which names the session."""
    try:
        proc = psutil.Process(pane_pid)
        procs = [proc] + proc.children(recursive=True)
    except psutil.Error:
        return
    root = str(settings.codex_home / "sessions")
    for p in procs:
        try:
            if "codex" not in p.name():
                continue
            files = [f.path for f in p.open_files() if f.path.startswith(root) and "/rollout-" in f.path]
        except psutil.Error:
            continue
        sid = codex_index.session_for_file(files[0]) if files else None
        sess = codex_index.get(sid) if sid else None
        s["claude"] = {"status": "busy" if sess and sess.task_open else "idle", "name": None, "pid": p.pid}
        if sid and sid != s["claude_id"]:
            s["claude_id"] = sid
            run("set-option", "-t", f"={s['name']}:", "@hangar_claude", sid, check=False)
            launch = {k: v for k, v in s["launch"].items() if k != "label"}
            remember_launch(sid, {"agent": "codex", **launch})
            # Codex has no --name, so the name typed in Hangar becomes the session's display name.
            if s["launch"].get("label") and not (all_meta().get(sid) or {}).get("name"):
                set_meta(sid, name=s["launch"]["label"])
        return


def remember_launch(session_id: str, launch: dict) -> None:
    """How a session was started (agent, Codex mode, Ollama), so Resume and restore can start it the same way."""
    saved = kv_get("session_launch", {}) or {}
    if saved.get(session_id) != launch:
        saved[session_id] = launch
        kv_set("session_launch", saved)


def launch_for(session_id: str) -> dict:
    return (kv_get("session_launch", {}) or {}).get(session_id, {})


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
        ({"claude_id": s["claude_id"], "cwd": s["cwd"], "name": s["name"], "agent": s.get("agent") or "claude"}
         for s in sessions if s["claude_id"]),
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
    args = shlex.split(settings.claude_base_args) + plugins.claude_args() + shlex.split(settings.claude_args) + extra
    claude = " ".join([shlex.quote(settings.claude_cmd)] + [shlex.quote(a) for a in args])
    note = "Claude exited. This is a shell in the same folder. Type exit to close the session."
    return f"{claude}; printf '\\n\\033[2m%s\\033[0m\\n' {shlex.quote(note)}; exec \"${{SHELL:-/bin/bash}}\" -l"


CODEX_MODES = {
    # Codex's sandbox can't run in Docker (no user namespaces). "ask": commands fail in the sandbox and
    # Codex then asks to run them outside it. "full": no sandbox, commands run without asking.
    "ask": ["-s", "workspace-write", "-a", "on-request"],
    "full": ["-s", "danger-full-access", "-a", "on-request"],
}


def codex_command(args: list[str]) -> str:
    from .providers import codex
    exe = codex.binary()
    if not exe:
        raise TmuxError("Codex isn't installed. Link ChatGPT in Settings first.")
    cmd = " ".join(shlex.quote(a) for a in [exe, *args])
    note = "Codex exited. This is a shell in the same folder. Type exit to close the session."
    return f"{cmd}; printf '\\n\\033[2m%s\\033[0m\\n' {shlex.quote(note)}; exec \"${{SHELL:-/bin/bash}}\" -l"


def new_session(cwd: str, *, label: str = "", resume: str | None = None, fork: bool = False,
                model: str = "", permission_mode: str = "", shell_only: bool = False, prompt: str = "",
                agent: str = "claude", codex_mode: str = "ask", ollama: dict | None = None) -> dict:
    """Start Claude, Codex, or a plain shell in a new tmux session. `prompt` is sent as the opening message.
    `ollama` is {"server": id, "url": base url, "model": name} to run the agent on an Ollama model."""
    cwd = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        raise TmuxError(f"Folder not found: {cwd}")
    if agent not in ("claude", "codex"):
        raise TmuxError(f"Unknown agent: {agent}")
    if codex_mode not in CODEX_MODES:
        raise TmuxError(f"Unknown Codex mode: {codex_mode}")
    name = _unique_name("hg-" + _slug(label or Path(cwd).name))
    env = {"TERM": "xterm-256color", "COLORTERM": "truecolor", "CODEX_HOME": str(settings.codex_home)}
    launch: dict = {"agent": agent}
    if ollama:
        launch["ollama"] = {"server": ollama.get("server"), "model": ollama["model"]}
        model = ollama["model"]
    claude_id = ""

    if shell_only:
        command = 'exec "${SHELL:-/bin/bash}" -l'
        launch = {}
    elif agent == "codex":
        launch["codex_mode"] = codex_mode
        if label and not resume:
            launch["label"] = label
        flags = ["--no-daemon", *CODEX_MODES[codex_mode]]
        if ollama:
            env["CODEX_OSS_BASE_URL"] = ollama["url"].rstrip("/") + "/v1"
            flags += ["-c", 'model_provider="ollama"']
        if model:
            flags += ["-m", model]
        if resume:
            args = ["fork" if fork else "resume", *flags, resume]
            claude_id = "" if fork else resume
        else:
            args = flags + (["--", prompt] if prompt else [])
        command = codex_command(args)
    else:
        if ollama:
            env.update({"ANTHROPIC_BASE_URL": ollama["url"].rstrip("/"), "ANTHROPIC_AUTH_TOKEN": "ollama",
                        "ANTHROPIC_API_KEY": ""})
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
        command = claude_command(extra)

    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    run("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50", *env_args, "bash", "-lc", command)
    target = f"={name}:"  # set-option takes a pane target; "=" means exact name match
    if claude_id:
        run("set-option", "-t", target, "@hangar_claude", claude_id, check=False)
        if agent == "claude" and not fork:
            remember_launch(claude_id, launch)
    if not shell_only:
        run("set-option", "-t", target, "@hangar_agent", agent, check=False)
        run("set-option", "-t", target, "@hangar_launch", json.dumps({k: v for k, v in launch.items() if k != "agent"}),
            check=False)
    run("set-option", "-t", target, "@hangar_label", label or Path(cwd).name, check=False)
    # Hangar draws its own header, so hide tmux's status bar in sessions it owns.
    run("set-option", "-t", target, "status", "off", check=False)
    list_sessions(with_live=False)  # record it as open right away, for restore after a restart
    return {"name": name, "claude_id": claude_id or None, "agent": None if shell_only else agent}


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
            new_session(cwd, label=item.get("name", "").removeprefix("hg-"), resume=cid,
                        **resume_options(cid, item.get("agent") or "claude"))
            started.append(cid)
        except TmuxError:
            continue
    dismiss_restore()
    return started


def resume_options(session_id: str, agent: str) -> dict:
    """new_session arguments that start a session the way it was started before."""
    launch = launch_for(session_id)
    opts: dict = {"agent": launch.get("agent") or agent}
    if opts["agent"] == "codex":
        opts["codex_mode"] = launch.get("codex_mode") or "ask"
    o = launch.get("ollama")
    if o and o.get("model"):
        from .providers import ollama
        server = ollama.get(o.get("server") or "")
        if server:
            opts["ollama"] = {"server": server["id"], "url": server["url"], "model": o["model"]}
    return opts
