"""Codex sessions: the rollout index, transcripts, usage, and how sessions are started and resumed."""
import json
import shutil
import subprocess

import pytest

from hangar import store, tmux
from hangar.codex_sessions import CodexIndex, transcript
from hangar.config import settings
from hangar.providers import ollama

SID = "01a1890a-382c-7ec1-96c9-84a5f35cde21"


def line(ts, type_, payload):
    return json.dumps({"timestamp": f"2026-10-29T{ts}Z", "type": type_, "payload": payload})


def rollout(cwd, sid=SID, rate_limits=None, finished=True):
    usage = lambda total, inp, cached, out: {  # noqa: E731
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}
    rows = [
        line("13:01:50.000", "session_meta", {"id": sid, "cwd": cwd, "cli_version": "0.162.1", "git": {"branch": "main"}}),
        line("13:01:50.100", "turn_context", {"model": "gpt-6", "cwd": cwd}),
        line("13:01:50.200", "event_msg", {"type": "task_started"}),
        line("13:01:50.300", "response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Codex"}]}),
        line("13:01:50.400", "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}),
        line("13:01:50.500", "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "List the files"}]}),
        line("13:01:51.000", "response_item", {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls -1 ."}), "call_id": "c1"}),
        line("13:01:51.500", "response_item", {"type": "function_call_output", "call_id": "c1",
                                               "output": "Chunk ID: 1\nProcess exited with code 0\nOutput:\na.txt\nb.txt\n"}),
        line("13:01:52.000", "response_item", {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "cat nope"}), "call_id": "c2"}),
        line("13:01:52.500", "response_item", {"type": "function_call_output", "call_id": "c2",
                                               "output": "Chunk ID: 2\nProcess exited with code 1\nOutput:\nNo such file\n"}),
        line("13:01:53.000", "event_msg", {"type": "token_count", "info": usage(1000, 900, 400, 100), "rate_limits": rate_limits}),
        line("13:01:53.100", "event_msg", {"type": "token_count", "info": usage(1000, 900, 400, 100), "rate_limits": rate_limits}),  # repeat
        line("13:01:54.000", "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Two files."}]}),
    ]
    if finished:
        rows.append(line("13:01:55.000", "event_msg", {"type": "task_complete"}))
    return "\n".join(rows) + "\n"


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True)
    monkeypatch.setattr(settings, "codex_home", home)
    return home, day


def write(day, text, sid=SID, name=None):
    path = day / (name or f"rollout-2026-10-29T13-01-50-{sid}.jsonl")
    path.write_text(text)
    return path


def test_index_reads_a_session(codex_home, tmp_path):
    home, day = codex_home
    write(day, rollout(str(tmp_path / "proj")))
    (home / "session_index.jsonl").write_text(json.dumps({"id": SID, "thread_name": "List files"}) + "\n")
    idx = CodexIndex()
    [s] = idx.list()
    assert s["agent"] == "codex" and s["title"] == "List files" and s["project"] == "proj"
    assert s["prompts"] == 1 and s["replies"] == 1 and s["first_prompt"] == "List the files"
    assert s["git_branch"] == "main" and s["version"] == "0.162.1" and s["models"] == ["gpt-6"]
    assert s["tokens"] == {"input": 500, "output": 100, "cache_write": 0, "cache_read": 400}  # repeat skipped
    assert len(idx.token_records()) == 1
    assert idx.get(SID).task_open is False
    assert idx.search("list the") == {SID} and idx.search("nothing like it") == set()


def test_index_busy_while_turn_open(codex_home, tmp_path):
    _, day = codex_home
    write(day, rollout(str(tmp_path), finished=False))
    idx = CodexIndex()
    assert idx.get(SID).task_open is True


def test_index_reads_appended_lines(codex_home, tmp_path):
    _, day = codex_home
    path = write(day, rollout(str(tmp_path)))
    idx = CodexIndex()
    idx.refresh(0)
    with path.open("a") as f:
        f.write(line("13:02:00.000", "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Again"}]}) + "\n")
    idx.refresh(0)
    assert idx.get(SID).prompts == 2 and idx.get(SID).last_prompt == "Again"


@pytest.mark.skipif(not shutil.which("zstd"), reason="needs the zstd tool")
def test_index_reads_compressed(codex_home, tmp_path):
    _, day = codex_home
    path = write(day, rollout(str(tmp_path)))
    subprocess.run(["zstd", "-q", "--rm", str(path)], check=True)
    idx = CodexIndex()
    assert idx.get(SID).prompts == 1
    assert [i["role"] for i in transcript(idx.get(SID))] == ["user", "tool", "tool", "assistant"]


def test_transcript(codex_home, tmp_path):
    _, day = codex_home
    write(day, rollout(str(tmp_path)))
    items = transcript(CodexIndex().get(SID))
    assert [i["role"] for i in items] == ["user", "tool", "tool", "assistant"]
    ls, cat = items[1], items[2]
    assert ls["summary"] == "ls -1 ." and ls["result"] == "a.txt\nb.txt\n" and not ls["error"]
    assert cat["error"] and cat["result"].startswith("[exit 1]")
    assert items[3]["text"] == "Two files."


def test_usage_from_rate_limits(codex_home, tmp_path):
    _, day = codex_home
    rl = {"limit_id": "codex", "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1795887106},
          "secondary": {"used_percent": 40, "window_minutes": 10080, "resets_at": 1796000000}, "plan_type": "plus"}
    write(day, rollout(str(tmp_path), rate_limits=rl))
    u = CodexIndex().usage()
    assert u["plan"] == "plus" and [w["window_minutes"] for w in u["windows"]] == [300, 10080]
    assert u["windows"][0]["used_percent"] == 12.5


def test_usage_none_without_limits(codex_home, tmp_path):
    _, day = codex_home
    write(day, rollout(str(tmp_path), rate_limits={"limit_id": "codex", "primary": None, "secondary": None}))
    assert CodexIndex().usage() is None


# ---------- API ----------

@pytest.fixture
def api_codex(codex_home, workspace, monkeypatch):
    from hangar import app as app_module
    _, day = codex_home
    write(day, rollout(str(workspace)))
    idx = CodexIndex()
    monkeypatch.setattr(app_module, "codex_index", idx)
    monkeypatch.setattr(tmux, "codex_index", idx)
    return idx


def test_api_lists_and_reads_codex(api_codex, client):
    items = client.get("/api/sessions").json()["sessions"]
    codex = [s for s in items if s.get("id") == SID]
    assert codex and codex[0]["agent"] == "codex"
    assert client.get(f"/api/sessions/{SID}").json()["agent"] == "codex"
    assert [i["role"] for i in client.get(f"/api/sessions/{SID}/transcript").json()["items"]] == ["user", "tool", "tool", "assistant"]
    assert [s["id"] for s in client.get("/api/sessions?q=list the").json()["sessions"] if s.get("id")] == [SID]


# ---------- starting sessions ----------

@pytest.fixture
def tmux_calls(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, input=None, check=True):
        calls.append(list(args))
        return ""
    monkeypatch.setattr(tmux, "run", fake_run)
    from hangar.providers import codex
    monkeypatch.setattr(codex, "binary", lambda: "/opt/codex/bin/codex")
    store.kv_set("session_launch", {})
    store.kv_set(ollama.KEY, [{"id": "s1", "name": "GPU", "url": "http://10.0.0.5:11434"}])
    yield calls
    store.kv_set("session_launch", {})
    store.kv_set(ollama.KEY, [])


def started(calls):
    new = next(c for c in calls if c[0] == "new-session")
    env = {new[i + 1].split("=", 1)[0]: new[i + 1].split("=", 1)[1] for i, a in enumerate(new) if a == "-e"}
    options = {c[3]: c[4] for c in calls if c[0] == "set-option" and len(c) > 4}
    return new[-1], env, options


def test_codex_session_ask_mode(tmux_calls, workspace):
    r = tmux.new_session(str(workspace), agent="codex", codex_mode="ask")
    cmd, env, opts = started(tmux_calls)
    assert cmd.startswith("/opt/codex/bin/codex --no-daemon -s workspace-write -a on-request;")
    assert env["CODEX_HOME"] == str(settings.codex_home)
    assert opts["@hangar_agent"] == "codex" and json.loads(opts["@hangar_launch"]) == {"codex_mode": "ask"}
    assert r["claude_id"] is None  # learned later from the log the process opens


def test_codex_label_becomes_name_once_linked(tmux_calls, workspace, monkeypatch):
    tmux.new_session(str(workspace), agent="codex", label="My task")
    _, _, opts = started(tmux_calls)
    launch = json.loads(opts["@hangar_launch"])
    assert launch["label"] == "My task"
    store.set_meta(SID, name=None)
    s = {"name": "hg-my-task", "claude_id": None, "launch": launch, "claude": None}

    class P:
        pid = 42
        def name(self): return "codex"
        def children(self, recursive=True): return []
        def open_files(self):
            return [type("F", (), {"path": str(settings.codex_home / "sessions" / f"rollout-x-{SID}.jsonl")})()]
    monkeypatch.setattr(tmux.psutil, "Process", lambda pid: P())
    monkeypatch.setattr(tmux.codex_index, "get", lambda sid: None)
    tmux._codex_live(s, 1)
    assert s["claude_id"] == SID
    assert store.all_meta()[SID]["name"] == "My task"
    assert tmux.launch_for(SID) == {"agent": "codex", "codex_mode": "ask"}
    store.set_meta(SID, name=None)


def test_codex_session_full_mode_on_ollama(tmux_calls, workspace):
    tmux.new_session(str(workspace), agent="codex", codex_mode="full",
                     ollama={"server": "s1", "url": "http://10.0.0.5:11434", "model": "qwen3.5:9b"})
    cmd, env, opts = started(tmux_calls)
    assert "-s danger-full-access" in cmd and "-c 'model_provider=\"ollama\"'" in cmd and "-m qwen3.5:9b" in cmd
    assert env["CODEX_OSS_BASE_URL"] == "http://10.0.0.5:11434/v1"
    assert json.loads(opts["@hangar_launch"]) == {"codex_mode": "full", "ollama": {"server": "s1", "model": "qwen3.5:9b"}}


def test_claude_session_on_ollama(tmux_calls, workspace):
    r = tmux.new_session(str(workspace), ollama={"server": "s1", "url": "http://10.0.0.5:11434", "model": "qwen3.5:9b"})
    cmd, env, _ = started(tmux_calls)
    assert env["ANTHROPIC_BASE_URL"] == "http://10.0.0.5:11434" and env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    assert env["ANTHROPIC_API_KEY"] == "" and "--model qwen3.5:9b" in cmd
    assert tmux.launch_for(r["claude_id"]) == {"agent": "claude", "ollama": {"server": "s1", "model": "qwen3.5:9b"}}


def test_resume_repeats_launch(tmux_calls, workspace):
    tmux.remember_launch(SID, {"agent": "codex", "codex_mode": "full", "ollama": {"server": "s1", "model": "m"}})
    opts = tmux.resume_options(SID, "codex")
    assert opts == {"agent": "codex", "codex_mode": "full",
                    "ollama": {"server": "s1", "url": "http://10.0.0.5:11434", "model": "m", "kind": "ollama",
                               "api_key": ""}}
    tmux.new_session(str(workspace), resume=SID, **opts)
    cmd, env, _ = started(tmux_calls)
    assert cmd.startswith(f"/opt/codex/bin/codex resume --no-daemon -s danger-full-access")
    assert f" {SID};" in cmd and env["CODEX_OSS_BASE_URL"] == "http://10.0.0.5:11434/v1"


def test_resume_fork_codex(tmux_calls, workspace):
    r = tmux.new_session(str(workspace), resume=SID, fork=True, agent="codex")
    cmd, _, _ = started(tmux_calls)
    assert cmd.startswith("/opt/codex/bin/codex fork --no-daemon") and r["claude_id"] is None


def test_resume_without_launch_defaults(tmux_calls):
    assert tmux.resume_options("unknown", "claude") == {"agent": "claude"}
    assert tmux.resume_options("unknown", "codex") == {"agent": "codex", "codex_mode": "ask"}


def test_codex_needs_install(tmux_calls, workspace, monkeypatch):
    from hangar.providers import codex
    monkeypatch.setattr(codex, "binary", lambda: None)
    with pytest.raises(tmux.TmuxError, match="isn't installed"):
        tmux.new_session(str(workspace), agent="codex")


def test_api_new_session_checks(tmux_calls, client, workspace, tmp_path):
    ok = client.post("/api/tmux/new", json={"cwd": str(workspace), "agent": "codex", "codex_mode": "ask"})
    assert ok.status_code == 200 and ok.json()["agent"] == "codex"
    assert client.post("/api/tmux/new", json={"cwd": str(workspace), "agent": "gemini"}).status_code == 400
    assert client.post("/api/tmux/new", json={"cwd": str(workspace), "codex_mode": "yolo"}).status_code == 400
    assert client.post("/api/tmux/new", json={"cwd": str(tmp_path)}).status_code == 403
    assert client.post("/api/tmux/new", json={"cwd": str(workspace), "ollama_server": "gone", "ollama_model": "m"}).status_code == 400
    assert client.post("/api/tmux/new", json={"cwd": str(workspace), "ollama_server": "s1"}).status_code == 400
    r = client.post("/api/tmux/new", json={"cwd": str(workspace), "ollama_server": "s1", "ollama_model": "m"})
    assert r.status_code == 200


def test_ollama_session_hides_cost(tmux_calls, client, workspace):
    r = client.post("/api/tmux/new", json={"cwd": str(workspace), "ollama_server": "s1", "ollama_model": "m"}).json()
    from hangar import app as app_module
    out = app_module._merge({"id": r["claude_id"], "cost": 1.23}, None, None)
    assert out["cost"] is None and out["ollama"] == "m" and out["agent"] == "claude"
