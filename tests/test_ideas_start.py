"""Starting a session from a project. tmux is replaced with a stub, so nothing is launched."""
import shlex

import pytest

from hangar import tmux
from hangar.ideas import api

BASE = "/api/ideas"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def started(monkeypatch):
    calls = []

    def fake_new_session(cwd, **kw):
        calls.append({"cwd": cwd, **kw})
        return {"name": "hg-test", "claude_id": SID}
    monkeypatch.setattr(api.tmux, "new_session", fake_new_session)
    return calls


def project(client, workspace, folder=True, milestones=True):
    body = {"title": "Garden watering planner", "summary": "Waters the garden when it's dry."}
    if folder:
        (workspace / "garden").mkdir(exist_ok=True)
        body["repo_path"] = str(workspace / "garden")
    if milestones:
        body["milestones"] = [{"title": "Read the soil sensor", "done_criteria": "A reading shows on a laptop"},
                              {"title": "Ship or stop", "done_criteria": "Decide after two weeks"}]
    return client.post(f"{BASE}/projects", json=body).json()["project"]


def test_starts_in_project_folder_with_next_milestone(client, workspace, started):
    p = project(client, workspace)
    r = client.post(f"{BASE}/projects/{p['id']}/start-session", json={})
    assert r.status_code == 200 and r.json() == {"name": "hg-test", "claude_id": SID}
    call = started[0]
    assert call["cwd"] == str((workspace / "garden").resolve())
    assert call["label"] == "Garden watering planner"
    prompt = call["prompt"]
    assert "Garden watering planner" in prompt and "Waters the garden" in prompt
    assert "Next milestone (1 of 2): Read the soil sensor" in prompt
    assert "Done when: A reading shows on a laptop" in prompt

    after = client.get(f"{BASE}/projects/{p['id']}").json()
    assert after["status"] == "active"
    assert after["milestones"][0]["status"] == "doing"
    assert [s["session_id"] for s in after["sessions"]] == [SID]


def test_no_folder_asks_for_one(client, workspace, started):
    p = project(client, workspace, folder=False)
    r = client.post(f"{BASE}/projects/{p['id']}/start-session", json={})
    assert r.status_code == 409 and "no folder" in r.json()["detail"]
    assert not started


def test_picked_folder_can_be_saved(client, workspace, started):
    p = project(client, workspace, folder=False)
    (workspace / "picked").mkdir(exist_ok=True)
    r = client.post(f"{BASE}/projects/{p['id']}/start-session",
                    json={"cwd": str(workspace / "picked"), "save_folder": True})
    assert r.status_code == 200
    assert client.get(f"{BASE}/projects/{p['id']}").json()["repo_path"] == str((workspace / "picked").resolve())


def test_folder_outside_workspace_rejected(client, workspace, started):
    p = project(client, workspace, folder=False)
    r = client.post(f"{BASE}/projects/{p['id']}/start-session", json={"cwd": "/etc"})
    assert r.status_code == 400 and not started


def test_missing_folder_rejected(client, workspace, started):
    p = project(client, workspace, folder=False)
    r = client.post(f"{BASE}/projects/{p['id']}/start-session", json={"cwd": str(workspace / "nope")})
    assert r.status_code == 400 and "not found" in r.json()["detail"]


def test_token_cannot_start_sessions(mcp, client, workspace, started):
    p = project(client, workspace)
    assert mcp.post(f"{BASE}/projects/{p['id']}/start-session", json={}).status_code == 403
    assert not started


def test_no_open_milestones_prompt(client, workspace, started):
    p = project(client, workspace, milestones=False)
    client.post(f"{BASE}/projects/{p['id']}/start-session", json={})
    assert "no open milestones" in started[0]["prompt"]


def test_shipped_project_keeps_its_status(client, workspace, started):
    p = project(client, workspace)
    client.patch(f"{BASE}/projects/{p['id']}", json={"status": "shipped"})
    client.post(f"{BASE}/projects/{p['id']}/start-session", json={})
    assert client.get(f"{BASE}/projects/{p['id']}").json()["status"] == "shipped"  # only idea/planned/paused move


def test_tmux_passes_prompt_after_double_dash(monkeypatch, tmp_path):
    """The opening prompt is the last argument, after "--", so multi-value options can't swallow it."""
    ran = []
    monkeypatch.setattr(tmux, "run", lambda *a, **k: ran.append(a) or "")
    prompt = "Line one\nIt's got 'quotes' and $(dollar)"
    out = tmux.new_session(str(tmp_path), label="Demo", model="sonnet", prompt=prompt)
    command = next(a for a in ran if a[0] == "new-session")[-1]
    claude_part = command.split("; printf")[0]
    args = shlex.split(claude_part)
    assert args[-2:] == ["--", prompt]
    assert args[args.index("--session-id") + 1] == out["claude_id"]


def test_tmux_without_prompt_has_no_double_dash(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(tmux, "run", lambda *a, **k: ran.append(a) or "")
    tmux.new_session(str(tmp_path), label="Demo")
    command = next(a for a in ran if a[0] == "new-session")[-1]
    assert " -- " not in command.split("; printf")[0]
