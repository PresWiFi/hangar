"""Scheduled tasks: cron math, validation, the run decision (send / skip / restart), the tick loop, and the API."""
import datetime as dt
import time

import pytest

from hangar import schedules, store, tmux
from tests.test_codex_sessions import started, tmux_calls  # noqa: F401 - fixture

T = dt.datetime


# ---------- cron ----------

@pytest.mark.parametrize("expr,after,expected", [
    ("*/15 * * * *", T(2026, 11, 2, 9, 7), T(2026, 11, 2, 9, 15)),
    ("0 9 * * 1-5", T(2026, 10, 31, 12, 0), T(2026, 11, 2, 9, 0)),       # Saturday -> Monday
    ("30 8 * * 0", T(2026, 11, 2, 9, 0), T(2026, 11, 8, 8, 30)),          # Sunday as 0
    ("30 8 * * 7", T(2026, 11, 2, 9, 0), T(2026, 11, 8, 8, 30)),          # ...and as 7
    ("0 0 1 * *", T(2026, 11, 15, 0, 0), T(2026, 12, 1, 0, 0)),
    ("0 12 29 2 *", T(2026, 3, 1, 0, 0), T(2028, 2, 29, 12, 0)),          # next leap day
    ("0 6 13 * 5", T(2026, 11, 2, 0, 0), T(2026, 11, 6, 6, 0)),           # 13th OR Friday: Friday comes first
    ("5,35 14 * * *", T(2026, 11, 2, 14, 5), T(2026, 11, 2, 14, 35)),     # strictly after
    ("10/20 * * * *", T(2026, 11, 2, 9, 31), T(2026, 11, 2, 9, 50)),
])
def test_next_cron(expr, after, expected):
    assert schedules.next_cron(expr, after) == expected


@pytest.mark.parametrize("expr", ["* * * *", "61 * * * *", "* * * * 8", "a * * * *", "5-1 * * * *", "*/0 * * * *",
                                  "0 0 31 2 *"])
def test_bad_cron(expr):
    with pytest.raises(ValueError):
        schedules.next_cron(expr, T(2026, 11, 2))


@pytest.mark.parametrize("given,stored,summary", [
    ({"type": "hourly", "minute": 5}, {"type": "hourly", "minute": 5}, "Every hour at :05"),
    ({"type": "daily", "time": "7:30"}, {"type": "daily", "time": "07:30"}, "Every day at 07:30"),
    ({"type": "weekly", "time": "09:00", "days": [5, 1, 3, 1]}, {"type": "weekly", "time": "09:00", "days": [1, 3, 5]},
     "Every Mon, Wed, Fri at 09:00"),
    ({"type": "weekly", "time": "09:00", "days": [1, 2, 3, 4, 5]}, {"type": "weekly", "time": "09:00",
                                                                     "days": [1, 2, 3, 4, 5]}, "Every weekdays at 09:00"),
    ({"type": "cron", "expr": " 0  9 * * 1-5 "}, {"type": "cron", "expr": "0 9 * * 1-5"}, "Cron: 0 9 * * 1-5"),
    ({"type": "once", "at": "2030-01-02T03:04"}, {"type": "once", "at": "2030-01-02T03:04"}, "Once, Wed Jan 2 2030 at 03:04"),
])
def test_clean_schedule(given, stored, summary):
    assert schedules.clean_schedule(given) == stored
    assert schedules.describe(stored) == summary


@pytest.mark.parametrize("bad", [{}, {"type": "daily", "time": "25:00"}, {"type": "weekly", "time": "09:00", "days": []},
                                 {"type": "hourly", "minute": 60}, {"type": "once", "at": "soon"},
                                 {"type": "cron", "expr": "nope"}])
def test_clean_schedule_rejects(bad):
    with pytest.raises(ValueError):
        schedules.clean_schedule(bad)


def test_next_run_once_passed():
    s = {"type": "once", "at": "2020-01-01T00:00"}
    assert schedules.next_run(s, time.time()) is None


# ---------- tasks ----------

@pytest.fixture
def no_tasks():
    store.kv_set(schedules.KEY, [])
    store.kv_set(schedules.RUNS_KEY, {})
    yield
    store.kv_set(schedules.KEY, [])
    store.kv_set(schedules.RUNS_KEY, {})


def body(workspace, **kw):
    return {"name": "Nightly tests", "cwd": str(workspace), "agent": "claude", "prompt": "Run the tests and fix failures",
            "schedule": {"type": "daily", "time": "02:00"}, **kw}


def test_create_validates(no_tasks, workspace, tmp_path):
    with pytest.raises(ValueError):
        schedules.create(body(workspace, prompt=" "))
    with pytest.raises(PermissionError):
        schedules.create(body(workspace, cwd=str(tmp_path)))
    with pytest.raises(ValueError):
        schedules.create(body(workspace, schedule={"type": "once", "at": "2020-01-01T00:00"}))
    t = schedules.create(body(workspace))
    assert t["next_run"] > time.time() and t["session_id"] is None


class FakeTmux:
    """list_sessions / send_text / kill / new_session stand-ins that record what the scheduler did."""

    def __init__(self, monkeypatch):
        self.sessions, self.sent, self.killed, self.started = [], [], [], []
        monkeypatch.setattr(tmux, "list_sessions", lambda with_live=True: self.sessions)
        monkeypatch.setattr(tmux, "send_text", lambda name, text, enter=True: self.sent.append((name, text)))
        monkeypatch.setattr(tmux, "kill", lambda name: self.killed.append(name))

        def new_session(cwd, **kw):
            self.started.append({"cwd": cwd, **kw})
            return {"name": kw["name"], "claude_id": kw.get("resume") or "new-conv-id", "agent": kw["agent"]}
        monkeypatch.setattr(tmux, "new_session", new_session)

    def live(self, task, status=None, created=0, name=None, claude_id="conv-1"):
        self.sessions = [{"name": name or schedules.session_name(task), "claude_id": claude_id, "created": created,
                          "claude": {"status": status} if status else None}]


@pytest.fixture
def fake(monkeypatch, no_tasks):
    return FakeTmux(monkeypatch)


def test_first_run_starts_session(fake, workspace):
    t = schedules.create(body(workspace))
    entry = schedules.run(t["id"])
    assert entry["result"] == "started"
    s = fake.started[0]
    assert s["name"] == f"hg-task-{t['id']}" and s["resume"] is None and s["prompt"] == "Run the tests and fix failures"
    assert s["permission_mode"] == "bypassPermissions" and s["unattended"] and s["task"] == t["id"]
    assert schedules.get(t["id"])["session_id"] == "new-conv-id"


def test_idle_session_gets_the_prompt(fake, workspace):
    t = schedules.create(body(workspace))
    fake.live(t, status="idle")
    assert schedules.run(t["id"])["result"] == "sent"
    assert fake.sent == [(f"hg-task-{t['id']}", "Run the tests and fix failures")] and not fake.started
    assert schedules.get(t["id"])["session_id"] == "conv-1"


def test_busy_session_skips(fake, workspace):
    t = schedules.create(body(workspace))
    fake.live(t, status="busy")
    entry = schedules.run(t["id"])
    assert entry["result"] == "skipped" and "previous run" in entry["detail"] and not fake.sent


def test_finds_session_resumed_from_sidebar(fake, workspace):
    t = schedules.create(body(workspace))
    schedules.run(t["id"])  # conversation new-conv-id
    fake.live(t, status="idle", name="hg-nightly-tests", claude_id="new-conv-id")
    schedules.run(t["id"])
    assert fake.sent[-1][0] == "hg-nightly-tests" and len(fake.started) == 1


def test_agent_exited_restarts(fake, workspace):
    t = schedules.create(body(workspace))
    fake.live(t, status=None, created=time.time() - 3600)
    assert schedules.run(t["id"])["result"] == "started"
    assert fake.killed == [f"hg-task-{t['id']}"] and fake.started[0]["resume"] == "conv-1"


def test_starting_session_is_left_alone(fake, workspace):
    t = schedules.create(body(workspace))
    fake.live(t, status=None, created=time.time() - 5)
    entry = schedules.run(t["id"])
    assert entry["result"] == "skipped" and "starting" in entry["detail"] and not fake.killed


def test_stopped_session_resumes_conversation(fake, workspace):
    t = schedules.create(body(workspace))
    schedules.run(t["id"])
    fake.sessions = []
    schedules.run(t["id"])
    assert fake.started[1]["resume"] == "new-conv-id" and fake.started[1]["prompt"]
    assert [r["result"] for r in schedules.runs(t["id"])] == ["started", "started"]


def test_codex_task_runs_full_access(fake, workspace):
    t = schedules.create(body(workspace, agent="codex"))
    schedules.run(t["id"])
    assert fake.started[0]["agent"] == "codex" and fake.started[0]["codex_mode"] == "full"
    assert fake.started[0]["permission_mode"] == ""


def test_tmux_error_is_logged(fake, workspace, monkeypatch):
    t = schedules.create(body(workspace))

    def boom(cwd, **kw):
        raise tmux.TmuxError("Codex isn't installed")
    monkeypatch.setattr(tmux, "new_session", boom)
    entry = schedules.run(t["id"])
    assert entry["result"] == "error" and "Codex" in entry["detail"]


def test_tick(fake, workspace):
    t = schedules.create(body(workspace, schedule={"type": "hourly", "minute": 0}))
    due = schedules.get(t["id"])["next_run"]
    assert schedules.tick(due - 1) == []
    assert schedules.tick(due + 5) == [t["id"]]
    assert schedules.get(t["id"])["next_run"] == due + 3600
    # Hangar was off for an hour past the next run: logged as missed, not run late.
    assert schedules.tick(due + 3600 + schedules.LATE + 60) == []
    assert schedules.runs(t["id"])[0]["result"] == "missed"


def test_tick_once_disables(fake, workspace):
    at = dt.datetime.now().replace(second=0, microsecond=0) + dt.timedelta(minutes=5)
    t = schedules.create(body(workspace, schedule={"type": "once", "at": at.isoformat(timespec="minutes")}))
    # One-time tasks run even when late.
    assert schedules.tick(at.timestamp() + 7200) == [t["id"]]
    after = schedules.get(t["id"])
    assert after["enabled"] is False and after["next_run"] is None


def test_disabled_never_runs(fake, workspace):
    t = schedules.create(body(workspace, enabled=False))
    assert t["next_run"] is None and schedules.tick(time.time() + 86400 * 2) == []


def test_edit_folder_starts_new_conversation(fake, workspace):
    t = schedules.create(body(workspace))
    schedules.run(t["id"])
    (workspace / "other").mkdir(exist_ok=True)
    assert schedules.update(t["id"], {"cwd": str(workspace / "other")})["session_id"] is None
    assert schedules.update(t["id"], {"prompt": "new prompt"})["prompt"] == "new prompt"


# ---------- API ----------

def test_api(fake, client, workspace):
    r = client.post("/api/schedules", json=body(workspace, schedule={"type": "weekly", "time": "08:00", "days": [1]}))
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["summary"] == "Every Mon at 08:00" and task["session"]["state"] == "stopped"
    assert task["next_run_text"].endswith("08:00") and task["next_run_text"].startswith("Mon ")
    assert client.get("/api/schedules").json()["timezone"]
    assert client.post("/api/schedules", json=body(workspace, name="")).status_code == 400
    r = client.post(f"/api/schedules/{task['id']}/run").json()
    assert r["run"]["result"] == "started" and r["task"]["last_run"]["result"] == "started"
    r = client.post(f"/api/schedules/{task['id']}/enabled", json={"enabled": False}).json()
    assert r["enabled"] is False and r["next_run"] is None
    r = client.put(f"/api/schedules/{task['id']}", json=body(workspace, name="Weekly review",
                                                              schedule={"type": "cron", "expr": "0 8 * * 1"}))
    assert r.json()["name"] == "Weekly review" and r.json()["summary"] == "Cron: 0 8 * * 1"
    assert len(client.get("/api/schedules").json()["tasks"]) == 1
    assert client.delete(f"/api/schedules/{task['id']}").status_code == 200
    assert client.post(f"/api/schedules/{task['id']}/run").status_code == 404


def test_api_needs_sign_in(anon):
    assert anon.get("/api/schedules").status_code == 401


# ---------- the session it starts ----------

def test_unattended_claude_skips_bypass_prompt(tmux_calls, workspace):  # noqa: F811
    tmux.new_session(str(workspace), name="hg-task-abc", task="abc", permission_mode="bypassPermissions",
                     unattended=True, prompt="go")
    cmd, _, opts = started(tmux_calls)
    assert "--permission-mode bypassPermissions" in cmd and "skipDangerousModePermissionPrompt" in cmd
    assert '"task": "abc"' in opts["@hangar_launch"]
    new = next(c for c in tmux_calls if c[0] == "new-session")
    assert new[new.index("-s") + 1] == "hg-task-abc"


def test_normal_bypass_session_still_asks(tmux_calls, workspace):  # noqa: F811
    tmux.new_session(str(workspace), permission_mode="bypassPermissions")
    cmd, _, _ = started(tmux_calls)
    assert "skipDangerousModePermissionPrompt" not in cmd


def test_codex_resume_with_prompt(tmux_calls, workspace):  # noqa: F811
    tmux.new_session(str(workspace), agent="codex", codex_mode="full", resume="conv-9", prompt="do it")
    cmd, _, _ = started(tmux_calls)
    assert " resume " in cmd and " -- conv-9 'do it';" in cmd


def test_task_on_local_model(fake, workspace):
    from hangar.providers import ollama
    store.kv_set(ollama.KEY, [{"id": "l1", "name": "GPU", "url": "http://10.0.0.7:9931", "kind": "llamacpp",
                               "api_key": "k"}])
    try:
        t = schedules.create(body(workspace, ollama_server="l1", ollama_model="qwen3-coder"))
        assert schedules.run(t["id"])["result"] == "started"
        assert fake.started[0]["ollama"] == {"server": "l1", "url": "http://10.0.0.7:9931", "model": "qwen3-coder",
                                             "kind": "llamacpp", "api_key": "k"}
        store.kv_set(ollama.KEY, [])  # server removed later: the run fails with a clear message
        fake.sessions = []
        entry = schedules.run(t["id"])
        assert entry["result"] == "error" and "isn't added any more" in entry["detail"]
    finally:
        store.kv_set(ollama.KEY, [])


def test_time_zone_change_replans(fake, workspace, monkeypatch):
    t = schedules.create(body(workspace, schedule={"type": "daily", "time": "09:00"}))
    assert schedules.get(t["id"])["next_tz"] == schedules.timezone()
    monkeypatch.setattr(schedules, "timezone", lambda: "Somewhere/Else")
    assert schedules.tick(t["next_run"] + 5) == []  # re-planned instead of firing at the old moment
    assert schedules.get(t["id"])["next_tz"] == "Somewhere/Else" and not schedules.runs(t["id"])
