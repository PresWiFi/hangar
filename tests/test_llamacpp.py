"""llama.cpp servers: probing a stand-in llama-server, storing them, and running sessions on their models."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hangar import store, tmux
from hangar.providers import ollama
from tests.test_codex_sessions import SID, started, tmux_calls  # noqa: F401 - fixture


class FakeLlamaServer(BaseHTTPRequestHandler):
    api_key = ""
    n_ctx = 65536
    loading = False

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server's name
        if self.path == "/health":  # public, like the real one
            return self._send(503, {"error": {"code": 503, "message": "Loading model"}}) if self.loading \
                else self._send(200, {"status": "ok"})
        if self.api_key and self.headers.get("Authorization") != f"Bearer {self.api_key}":
            return self._send(401, {"error": {"code": 401, "message": "Invalid API Key"}})
        if self.path == "/v1/models":
            return self._send(200, {"object": "list", "data": [{
                "id": "qwen3-coder-30b", "object": "model", "owned_by": "llamacpp",
                "meta": {"n_params": 30_532_122_624, "n_ctx_train": 262144}}]})
        if self.path.split("?")[0] == "/props":
            return self._send(200, {"build_info": "b7001-abc1234",
                                    "default_generation_settings": {"n_ctx": self.n_ctx}})
        self._send(404, {"error": "not found"})


@pytest.fixture
def llama(monkeypatch):
    for k, v in {"api_key": "", "n_ctx": 65536, "loading": False}.items():
        monkeypatch.setattr(FakeLlamaServer, k, v)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store.kv_set(ollama.KEY, [])
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    store.kv_set(ollama.KEY, [])


@pytest.mark.parametrize("given,expected", [
    ("10.0.0.7", "http://10.0.0.7:9931"),
    ("http://gpu:8080/v1", "http://gpu:8080"),
    ("https://llama.example.com", "https://llama.example.com"),
])
def test_normalize_llamacpp(given, expected):
    assert ollama.normalize(given, "llamacpp") == expected


def test_unknown_kind():
    with pytest.raises(ValueError):
        ollama.normalize("10.0.0.7", "vllm")


def test_probe(llama):
    st = ollama.probe(llama, "llamacpp")
    assert st["ok"] and st["version"] == "b7001-abc1234" and st["llamacpp"]
    assert st["models"] == [{"name": "qwen3-coder-30b", "size": "31B"}]
    assert "warning" not in st


def test_probe_small_context_warns(llama, monkeypatch):
    monkeypatch.setattr(FakeLlamaServer, "n_ctx", 4096)
    st = ollama.probe(llama, "llamacpp")
    assert st["ok"] and "4,096" in st["warning"] and "-c 65536" in st["warning"]


def test_probe_loading(llama, monkeypatch):
    monkeypatch.setattr(FakeLlamaServer, "loading", True)
    st = ollama.probe(llama, "llamacpp")
    assert st["ok"] is False and "still loading" in st["error"]


def test_probe_needs_key(llama, monkeypatch):
    monkeypatch.setattr(FakeLlamaServer, "api_key", "sekrit")
    st = ollama.probe(llama, "llamacpp")
    assert st["ok"] is False and "needs an API key" in st["error"]
    assert ollama.probe(llama, "llamacpp", "sekrit")["ok"]


def test_add_with_key_never_returns_it(llama, monkeypatch, client):
    monkeypatch.setattr(FakeLlamaServer, "api_key", "sekrit")
    body = {"name": "GPU box", "url": llama, "kind": "llamacpp", "api_key": "sekrit"}
    t = client.post("/api/providers/ollama/test", json=body).json()
    assert t["ok"] and t["models"][0]["name"] == "qwen3-coder-30b"
    added = client.post("/api/providers/ollama", json=body).json()
    assert added["kind"] == "llamacpp" and added["has_key"] and "api_key" not in added
    listed = client.get("/api/providers/ollama").json()["servers"]
    assert listed[0]["ok"] and listed[0]["kind"] == "llamacpp" and "sekrit" not in json.dumps(listed)
    assert ollama.servers()[0]["api_key"] == "sekrit"


def test_ollama_server_ignores_key(llama):
    added = ollama.add("", "10.0.0.9", "ollama", "x")
    assert added["kind"] == "ollama" and not added["has_key"] and "api_key" not in ollama.servers()[0]


def test_discover_needs_llamacpp_answer(llama, monkeypatch):
    monkeypatch.setattr(ollama, "CANDIDATES", [(llama, "llamacpp"), ("http://127.0.0.1:9", "ollama")])
    found = ollama.discover()
    assert [(f["url"], f["kind"], f["label"]) for f in found] == [(llama, "llamacpp", "llama.cpp")]
    monkeypatch.setattr(ollama, "probe", lambda url, kind="ollama", api_key="", timeout=4:
                        {"ok": kind == "llamacpp", "models": [], "llamacpp": False})
    assert ollama.discover() == []  # something else answering on 8080 isn't llama.cpp


# ---------- sessions ----------

LLAMA = {"id": "l1", "name": "GPU", "url": "http://10.0.0.7:9931", "kind": "llamacpp", "api_key": "sekrit"}


def test_claude_session_on_llamacpp(tmux_calls, workspace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ollama, "context_tokens", lambda server, model, timeout=5: None)
    store.kv_set(ollama.KEY, [LLAMA])
    r = tmux.new_session(str(workspace), ollama=ollama.launch_info(LLAMA, "qwen3-coder-30b"))
    cmd, env, _ = started(tmux_calls)
    assert env["ANTHROPIC_BASE_URL"] == "http://10.0.0.7:9931" and env["ANTHROPIC_AUTH_TOKEN"] == "sekrit"
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "qwen3-coder-30b"
    assert "--model qwen3-coder-30b" in cmd
    launch = tmux.launch_for(r["claude_id"])
    assert launch == {"agent": "claude", "ollama": {"server": "l1", "model": "qwen3-coder-30b", "kind": "llamacpp"}}
    assert "sekrit" not in json.dumps(launch)


def test_codex_session_on_llamacpp(tmux_calls, workspace):  # noqa: F811
    tmux.new_session(str(workspace), agent="codex", codex_mode="ask",
                     ollama=ollama.launch_info(LLAMA, "qwen3-coder-30b"))
    cmd, env, opts = started(tmux_calls)
    assert "-c 'model_provider=\"llamacpp\"'" in cmd
    assert "-c 'model_providers.llamacpp.base_url=\"http://10.0.0.7:9931/v1\"'" in cmd
    assert "-c 'model_providers.llamacpp.env_key=\"LLAMACPP_API_KEY\"'" in cmd and env["LLAMACPP_API_KEY"] == "sekrit"
    assert "-m qwen3-coder-30b" in cmd and "CODEX_OSS_BASE_URL" not in env
    assert "sekrit" not in cmd and "sekrit" not in opts["@hangar_launch"]


def test_codex_on_llamacpp_without_key(tmux_calls, workspace):  # noqa: F811
    server = {**LLAMA, "api_key": ""}
    tmux.new_session(str(workspace), agent="codex", ollama=ollama.launch_info(server, "m"))
    cmd, env, _ = started(tmux_calls)
    assert "env_key" not in cmd and "LLAMACPP_API_KEY" not in env


def test_resume_on_llamacpp(tmux_calls, workspace):  # noqa: F811
    store.kv_set(ollama.KEY, [LLAMA])
    tmux.remember_launch(SID, {"agent": "claude", "ollama": {"server": "l1", "model": "m", "kind": "llamacpp"}})
    opts = tmux.resume_options(SID, "claude")
    assert opts["ollama"] == {"server": "l1", "url": "http://10.0.0.7:9931", "model": "m", "kind": "llamacpp",
                              "api_key": "sekrit"}


def test_session_list_tags_llamacpp(tmux_calls, client, workspace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ollama, "context_tokens", lambda server, model, timeout=5: None)
    store.kv_set(ollama.KEY, [LLAMA])
    r = client.post("/api/tmux/new", json={"cwd": str(workspace), "ollama_server": "l1", "ollama_model": "m"}).json()
    from hangar import app as app_module
    out = app_module._merge({"id": r["claude_id"], "cost": 1.23}, None, None)
    assert out["ollama"] == "m" and out["local_kind"] == "llama.cpp" and out["cost"] is None


def test_context_tokens(llama, monkeypatch):
    server = {"id": "x", "url": llama, "kind": "llamacpp"}
    assert ollama.context_tokens(server, "qwen3-coder-30b") == 65536
    monkeypatch.setattr(FakeLlamaServer, "n_ctx", 0)  # router mode without ?model= answers 0
    assert ollama.context_tokens(server, "qwen3-coder-30b") is None
    assert ollama.context_tokens({"url": llama, "kind": "ollama"}, "m") is None


def test_claude_on_llamacpp_gets_context_size(tmux_calls, workspace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ollama, "context_tokens", lambda server, model, timeout=5: 92672)
    tmux.new_session(str(workspace), ollama=ollama.launch_info(LLAMA, "m"))
    _, env, _ = started(tmux_calls)
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "92672"
