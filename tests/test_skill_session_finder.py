"""The standalone session-finder skill script, run against fake Claude Code and Codex logs."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "session-finder" / "scripts" / "sessions.py"
CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
CODEX_ID = "019a0000-aaaa-bbbb-cccc-dddddddddddd"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_logs(tmp_path: Path) -> dict:
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    reply = {"type": "assistant", "requestId": "r1", "timestamp": "2026-11-01T10:00:05Z",
             "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "Fixed the login redirect."}]}}
    write_jsonl(claude / "projects" / "-work-api" / f"{CLAUDE_ID}.jsonl", [
        {"type": "user", "cwd": "/work/api", "gitBranch": "fix-login", "timestamp": "2026-11-01T10:00:00Z",
         "message": {"content": "the login page redirects forever"}},
        {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>ignore</system-reminder>"}},
        {**reply, "message": {**reply["message"], "usage": {"input_tokens": 10, "output_tokens": 1}}},
        reply,  # streamed copy with the final counts; must replace the first, not add to it
        {"type": "assistant", "requestId": "r2", "timestamp": "2026-11-01T10:00:06Z",
         "message": {"id": "m2", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "cwd": "/work/api/sub", "timestamp": "2026-11-01T10:01:00Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "ai-title", "aiTitle": "Fix login redirect loop"},
    ])
    write_jsonl(codex / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T09-00-00-{CODEX_ID}.jsonl", [
        {"type": "session_meta", "timestamp": "2026-11-02T09:00:00Z",
         "payload": {"id": CODEX_ID, "cwd": "/work/web", "git": {"branch": "main"}}},
        {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        {"type": "response_item", "timestamp": "2026-11-02T09:00:01Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nx"}]}},
        {"type": "response_item", "timestamp": "2026-11-02T09:00:02Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "add dark mode"}]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 300},
            "last_token_usage": {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 100}}}},
    ])
    write_jsonl(codex / "session_index.jsonl", [{"id": CODEX_ID, "thread_name": "Dark mode"}])
    return {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex), "PATH": "/usr/bin:/bin"}


def run(env: dict, *args: str) -> str:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, check=True).stdout


def test_list_both_agents(tmp_path):
    rows = json.loads(run(make_logs(tmp_path), "list", "--json"))
    assert [r["agent"] for r in rows] == ["codex", "claude"]  # newest first
    codex, claude = rows
    assert claude["display_title"] == "Fix login redirect loop"
    assert claude["cwd"] == "/work/api"  # starting folder, not a later cd
    assert claude["branch"] == "fix-login"
    assert claude["prompts"] == 1 and claude["replies"] == 2
    assert claude["tokens_in"] + claude["tokens_out"] == 17
    assert claude["resume"] == f"cd /work/api && claude --resume {CLAUDE_ID}"
    assert claude["fork"].endswith("--fork-session")
    assert codex["display_title"] == "Dark mode"
    assert codex["prompts"] == 1  # AGENTS.md injection isn't a prompt
    assert (codex["tokens_in"], codex["tokens_out"]) == (150, 100)
    assert codex["resume"] == f"cd /work/web && codex resume {CODEX_ID}"


def test_search_and_filters(tmp_path):
    env = make_logs(tmp_path)
    ids = lambda *a: [r["id"] for r in json.loads(run(env, *a, "--json"))]
    assert ids("search", "login redirects") == [CLAUDE_ID]
    assert ids("search", "dark") == [CODEX_ID]
    assert ids("search", "nothing like this") == []
    assert ids("list", "--agent", "claude") == [CLAUDE_ID]
    assert ids("list", "--project", "web") == [CODEX_ID]


def test_show_transcript(tmp_path):
    env = make_logs(tmp_path)
    out = json.loads(run(env, "show", CLAUDE_ID[:8], "--json"))
    assert [i["role"] for i in out["items"]] == ["user", "assistant", "tool"]
    assert out["items"][2]["text"] == "pytest -q"
    text = run(env, "show", CODEX_ID[:8])
    assert "Resume: cd /work/web && codex resume" in text and "add dark mode" in text


def test_script_uses_only_stdlib():
    spec = importlib.util.spec_from_file_location("skill_sessions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = SCRIPT.read_text()
    for banned in ("urllib", "socket", "http.client", "requests", "shell=True", "os.system"):
        assert banned not in source
