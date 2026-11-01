"""Scheduled tasks. Each task owns one agent session and sends it its prompt on a schedule.

When a run is due: if the task's session is running and idle, the prompt is typed into it; if it's still
working, the run is skipped; if it was stopped (or the agent exited to a shell), the session is started
again, resuming the task's conversation with the prompt as its first message. Tasks run unattended:
Claude Code in bypass-permissions mode, Codex with full access.

Times are the server's local time (TZ). Schedules are stored as given and turned into cron expressions,
except "once".
"""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import threading
import time

from . import tmux
from .files import inside_workspace
from .providers import ollama as local_servers
from .store import kv_get, kv_set

KEY = "schedules"
RUNS_KEY = "schedule_runs"
MAX_RUNS = 20
LATE = 600            # a recurring run this many seconds overdue (Hangar was off) is skipped, not run late
STARTUP_GRACE = 60    # a session younger than this whose agent isn't visible yet is still starting
DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_lock = threading.RLock()


# ---------- cron ----------

_CRON_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7)]


def _cron_field(text: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if not m:
            raise ValueError(f"Can't read {part!r} in the cron expression.")
        span, step = m.group(1), int(m.group(2) or 1)
        if span == "*":
            a, b = lo, hi
        elif "-" in span:
            a, b = (int(x) for x in span.split("-"))
        else:
            a = int(span)
            b = hi if m.group(2) else a  # "5/15" means from 5, every 15
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError(f"{part!r} is out of range ({lo}-{hi}) in the cron expression.")
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> dict:
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("A cron expression has 5 fields: minute hour day-of-month month day-of-week.")
    c = {name: _cron_field(p, lo, hi) for p, (name, lo, hi) in zip(parts, _CRON_FIELDS)}
    if 7 in c["weekday"]:  # 0 and 7 are both Sunday
        c["weekday"] = (c["weekday"] - {7}) | {0}
    c["any_day"], c["any_weekday"] = parts[2] == "*", parts[4] == "*"
    return c


def _day_matches(c: dict, t: dt.datetime) -> bool:
    dom, dow = t.day in c["day"], (t.isoweekday() % 7) in c["weekday"]
    if c["any_day"] and c["any_weekday"]:
        return True
    if c["any_day"]:
        return dow
    if c["any_weekday"]:
        return dom
    return dom or dow  # cron's rule: when both are restricted, either one matching is enough


def next_cron(expr: str, after: dt.datetime) -> dt.datetime:
    """The first time after `after` (naive local time) that the expression matches."""
    c = parse_cron(expr)
    t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    limit = after + dt.timedelta(days=366 * 5)
    while t < limit:
        if t.month not in c["month"]:
            t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
        elif not _day_matches(c, t):
            t = t.replace(hour=0, minute=0) + dt.timedelta(days=1)
        elif t.hour not in c["hour"]:
            t = t.replace(minute=0) + dt.timedelta(hours=1)
        elif t.minute not in c["minute"]:
            t += dt.timedelta(minutes=1)
        else:
            return t
    raise ValueError("That cron expression never runs.")


# ---------- schedules ----------

def _hhmm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("Enter the time as HH:MM, for example 09:30.")
    return int(m.group(1)), int(m.group(2))


def clean_schedule(s: dict) -> dict:
    """Validates a schedule from the browser and returns it in stored form."""
    kind = (s or {}).get("type")
    if kind == "once":
        try:
            at = dt.datetime.fromisoformat(str(s.get("at", "")).strip())
        except ValueError:
            raise ValueError("Pick the date and time to run.") from None
        return {"type": "once", "at": at.replace(second=0, microsecond=0, tzinfo=None).isoformat(timespec="minutes")}
    if kind == "hourly":
        minute = s.get("minute")
        if not isinstance(minute, int) or not 0 <= minute <= 59:
            raise ValueError("Pick the minute past the hour (0-59).")
        return {"type": "hourly", "minute": minute}
    if kind == "daily":
        h, m = _hhmm(s.get("time"))
        return {"type": "daily", "time": f"{h:02d}:{m:02d}"}
    if kind == "weekly":
        h, m = _hhmm(s.get("time"))
        days = sorted({d for d in s.get("days") or [] if isinstance(d, int) and 0 <= d <= 6})
        if not days:
            raise ValueError("Pick at least one day.")
        return {"type": "weekly", "time": f"{h:02d}:{m:02d}", "days": days}
    if kind == "cron":
        expr = " ".join(str(s.get("expr", "")).split())
        next_cron(expr, dt.datetime.now())  # raises if it's invalid or never runs
        return {"type": "cron", "expr": expr}
    raise ValueError("Pick how often to run: once, hourly, daily, weekly, or cron.")


def to_cron(s: dict) -> str | None:
    if s["type"] == "hourly":
        return f"{s['minute']} * * * *"
    if s["type"] in ("daily", "weekly"):
        h, m = (int(x) for x in s["time"].split(":"))
        return f"{m} {h} * * " + (",".join(map(str, s["days"])) if s["type"] == "weekly" else "*")
    if s["type"] == "cron":
        return s["expr"]
    return None


def next_run(s: dict, after: float) -> float | None:
    """Epoch seconds of the next run after `after`, or None (a one-time run that has passed)."""
    if s["type"] == "once":
        at = dt.datetime.fromisoformat(s["at"]).timestamp()
        return at if at > after else None
    return next_cron(to_cron(s), dt.datetime.fromtimestamp(after)).timestamp()


def describe(s: dict) -> str:
    if s["type"] == "once":
        return "Once, " + dt.datetime.fromisoformat(s["at"]).strftime("%a %b %-d %Y at %H:%M")
    if s["type"] == "hourly":
        return f"Every hour at :{s['minute']:02d}"
    if s["type"] == "daily":
        return f"Every day at {s['time']}"
    if s["type"] == "weekly":
        days = s["days"]
        names = "weekdays" if days == [1, 2, 3, 4, 5] else "weekends" if days == [0, 6] else ", ".join(DAYS[d] for d in days)
        return f"Every {names} at {s['time']}"
    return f"Cron: {s['expr']}"


# ---------- tasks ----------

def _plan(task: dict, after: float) -> None:
    """Sets the task's next run, and remembers the time zone it was worked out in."""
    task["next_run"] = next_run(task["schedule"], after) if task.get("enabled") else None
    task["next_tz"] = timezone()


def tasks() -> list[dict]:
    return kv_get(KEY, []) or []


def get(task_id: str) -> dict | None:
    return next((t for t in tasks() if t["id"] == task_id), None)


def _save(task: dict) -> None:
    with _lock:
        current = tasks()
        kv_set(KEY, [task if t["id"] == task["id"] else t for t in current]
               + ([] if any(t["id"] == task["id"] for t in current) else [task]))


def runs(task_id: str) -> list[dict]:
    return (kv_get(RUNS_KEY, {}) or {}).get(task_id, [])


def _log(task_id: str, result: str, detail: str = "") -> dict:
    entry = {"at": time.time(), "result": result, "detail": detail}
    with _lock:
        all_runs = kv_get(RUNS_KEY, {}) or {}
        all_runs[task_id] = ([entry] + all_runs.get(task_id, []))[:MAX_RUNS]
        kv_set(RUNS_KEY, all_runs)
    return entry


def clean(body: dict) -> dict:
    """Validates a task from the browser. Returns the stored fields (no id, state, or timestamps)."""
    name = " ".join(str(body.get("name", "")).split())[:60]
    if not name:
        raise ValueError("Give the task a name.")
    cwd = os.path.realpath(os.path.expanduser(str(body.get("cwd", "")).strip()))
    if not os.path.isdir(cwd):
        raise ValueError(f"Folder not found: {cwd}")
    if not inside_workspace(cwd):
        raise PermissionError("Outside the configured workspaces (HANGAR_WORKSPACES)")
    agent = body.get("agent") or "claude"
    if agent not in ("claude", "codex"):
        raise ValueError("Unknown agent")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("Write the prompt to send each time.")
    out = {"name": name, "cwd": cwd, "agent": agent, "prompt": prompt[:20_000],
           "model": str(body.get("model", "")).strip()[:200], "ollama_server": "", "ollama_model": "",
           "schedule": clean_schedule(body.get("schedule") or {}), "enabled": bool(body.get("enabled", True))}
    if body.get("ollama_server"):
        if not local_servers.get(body["ollama_server"]):
            raise ValueError("That model server isn't added any more")
        if not str(body.get("ollama_model", "")).strip():
            raise ValueError("Pick a model on the server")
        out.update(ollama_server=body["ollama_server"], ollama_model=str(body["ollama_model"]).strip(), model="")
    return out


def create(body: dict) -> dict:
    task = {"id": secrets.token_hex(4), "created": time.time(), "session_id": None, **clean(body)}
    _plan(task, time.time())
    if task["enabled"] and task["next_run"] is None:
        raise ValueError("That time has already passed.")
    _save(task)
    return task


def update(task_id: str, body: dict) -> dict:
    with _lock:
        task = get(task_id)
        if not task:
            raise KeyError(task_id)
        fields = clean({**task, **body})
        moved = task["cwd"] != fields["cwd"] or task["agent"] != fields["agent"]
        task.update(fields)
        if moved:
            task["session_id"] = None  # a new folder or agent means a new conversation
        _plan(task, time.time())
        if task["enabled"] and task["next_run"] is None:
            raise ValueError("That time has already passed.")
        _save(task)
        return task


def delete(task_id: str) -> None:
    """Removes the task. Its session and conversation stay."""
    with _lock:
        kv_set(KEY, [t for t in tasks() if t["id"] != task_id])
        all_runs = kv_get(RUNS_KEY, {}) or {}
        all_runs.pop(task_id, None)
        kv_set(RUNS_KEY, all_runs)


def session_name(task: dict) -> str:
    return f"hg-task-{task['id']}"


def find_session(task: dict, live: list[dict] | None = None) -> dict | None:
    """The task's tmux session: by its own name, or a session running the task's conversation (for example
    one you resumed from the sidebar)."""
    live = tmux.list_sessions() if live is None else live
    return (next((s for s in live if s["name"] == session_name(task)), None)
            or next((s for s in live if task.get("session_id") and s.get("claude_id") == task["session_id"]), None))


def _start_options(task: dict) -> dict:
    opts = {"agent": task["agent"], "model": task.get("model", ""), "permission_mode": "",
            "codex_mode": "full", "unattended": True}
    if task["agent"] == "claude":
        opts["permission_mode"] = "bypassPermissions"
    if task.get("ollama_server"):
        server = local_servers.get(task["ollama_server"])
        if not server:
            raise tmux.TmuxError("The task's model server isn't added any more. Edit the task.")
        opts["ollama"] = local_servers.launch_info(server, task["ollama_model"])
        opts["model"] = ""
    return opts


def run(task_id: str) -> dict:
    """Runs the task now. Returns the log entry: result is sent, started, skipped, or error."""
    task = get(task_id)
    if not task:
        raise KeyError(task_id)
    try:
        live = find_session(task)
        if live and live.get("claude_id") and live["claude_id"] != task.get("session_id"):
            task["session_id"] = live["claude_id"]  # Codex names its conversation after it starts
            _save(task)
        if live:
            agent = live.get("claude")
            if agent and agent.get("status") == "busy":
                return _log(task_id, "skipped", "Still working on the previous run.")
            if agent:
                tmux.send_text(live["name"], task["prompt"])
                return _log(task_id, "sent", f"Sent to {live['name']}.")
            if time.time() - live.get("created", 0) < STARTUP_GRACE:
                return _log(task_id, "skipped", "The session is still starting.")
            tmux.kill(live["name"])  # the agent exited and left a shell; start it again below
        if not os.path.isdir(task["cwd"]):
            return _log(task_id, "error", f"Folder not found: {task['cwd']}")
        resume = task.get("session_id")
        started = tmux.new_session(task["cwd"], label=task["name"], name=session_name(task), resume=resume,
                                   prompt=task["prompt"], task=task["id"], **_start_options(task))
        if started.get("claude_id") and started["claude_id"] != task.get("session_id"):
            task["session_id"] = started["claude_id"]
            _save(task)
        return _log(task_id, "started", "Resumed the task's conversation." if resume else "Started a new session.")
    except tmux.TmuxError as e:
        return _log(task_id, "error", str(e))


def tick(now: float | None = None) -> list[str]:
    """Runs every task that's due. Called every few seconds by the server. Returns the ids it ran."""
    now = time.time() if now is None else now
    ran = []
    for task in tasks():
        if task.get("enabled") and task.get("next_tz") != timezone():
            # The server's time zone changed (TZ): "09:00" now means a different moment.
            with _lock:
                _plan(task, now)
                _save(task)
            continue
        due = task.get("next_run")
        if not task.get("enabled") or due is None or due > now:
            continue
        recurring = task["schedule"]["type"] != "once"
        if recurring and now - due > LATE:
            _log(task["id"], "missed", "Hangar wasn't running at the scheduled time.")
        else:
            run(task["id"])
            ran.append(task["id"])
        with _lock:
            fresh = get(task["id"])
            if fresh:
                if not recurring:
                    fresh["enabled"] = False
                _plan(fresh, now)
                _save(fresh)
    return ran


def timezone() -> str:
    """The server's time zone, which schedules use: its name (TZ) if set, else the abbreviation."""
    return os.environ.get("TZ") or time.strftime("%Z")


def when(ts: float | None) -> str | None:
    """A time as the server sees it, so it matches the schedule as typed even if the browser is elsewhere."""
    return dt.datetime.fromtimestamp(ts).strftime("%a %b %-d, %H:%M") if ts else None


def view(task: dict, live: list[dict]) -> dict:
    """A task for the browser, with its schedule in words, last run, and session state."""
    s = find_session(task, live)
    agent = (s or {}).get("claude")
    state = "stopped" if not s else ("busy" if agent and agent.get("status") == "busy" else "idle" if agent else "shell")
    history = runs(task["id"])
    return {**task, "summary": describe(task["schedule"]), "next_run_text": when(task.get("next_run")), "runs": history[:5], "last_run": history[0] if history else None,
            "session": {"state": state, "name": s["name"] if s else None, "claude_id": (s or {}).get("claude_id")
                        or task.get("session_id")}}


def all_views() -> list[dict]:
    live = tmux.list_sessions()
    return [view(t, live) for t in sorted(tasks(), key=lambda t: t["name"].lower())]
