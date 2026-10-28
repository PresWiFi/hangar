"""Plugin loading. "Off" is checked in a fresh Python process, because settings are read at import time."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hangar import plugins

ROOT = Path(__file__).resolve().parent.parent

CHECK_OFF = """
import json
from fastapi.testclient import TestClient
from hangar import auth
from hangar.app import app
c = TestClient(app, headers={"X-Hangar": "1"})
c.cookies.set(auth.COOKIE, auth.make_token())
print(json.dumps({
    "routes": [r.path for r in app.routes if "ideas" in r.path],
    "me": c.get("/api/me").json()["plugins"],
    "get": c.get("/api/ideas/projects").status_code,
    "bearer": TestClient(app, headers={"Authorization": "Bearer x"}).get("/api/ideas/projects").status_code,
    "modules": sorted(m for m in __import__("sys").modules if m.startswith("hangar.ideas")),
    "claude_args": "--mcp-config" in __import__("hangar.tmux").tmux.claude_command([]),
}))
"""


def _run_off(plugins_value: str) -> tuple[dict, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="hangar-off-"))
    env = {**os.environ, "HANGAR_PLUGINS": plugins_value, "HANGAR_DATA_DIR": str(tmp / "data"),
           "CLAUDE_CONFIG_DIR": str(tmp / "claude"), "HANGAR_PASSWORD": "x"}
    out = subprocess.run([sys.executable, "-c", CHECK_OFF], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1]), tmp / "data"


def test_plugin_absent_when_not_enabled():
    result, data = _run_off("")
    assert result == {"routes": [], "me": [], "get": 404, "bearer": 401, "modules": [], "claude_args": False}
    assert not (data / "ideas.db").exists()
    assert not (data / "ideas-token").exists()


def test_unknown_and_bad_names_are_skipped(capsys):
    assert plugins.load(["nope", "../etc", "Ideas!", "tmux"]) == []
    out = capsys.readouterr().out
    assert "not found" in out and "bad name" in out and "no matching PLUGIN" in out


def test_load_ideas_once():
    loaded = plugins.load(["ideas", "ideas"])
    assert [p.name for p in loaded] == ["ideas"]
    assert loaded[0].prefix == "/api/ideas/"
