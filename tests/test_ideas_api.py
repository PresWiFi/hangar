import os
import uuid

import pytest

BASE = "/api/ideas"
MILESTONES = [
    {"title": "Spike: water sensor reads", "done_criteria": "A reading shows up on a laptop"},
    {"title": "Schedule from forecast", "done_criteria": "Skips watering when rain is due"},
    {"title": "Ship or stop", "done_criteria": "Decide after two weeks of use"},
]


def create(client, title="Garden watering planner", **extra):
    r = client.post(f"{BASE}/projects", json={"title": title, "summary": "Waters the garden.", **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- projects ----------

def test_create_with_milestones(client):
    out = create(client, milestones=MILESTONES, tags=["home", "home", "iot"])
    assert out["created"] is True
    p = out["project"]
    assert p["slug"] == "garden-watering-planner"
    assert p["status"] == "idea"
    assert p["tags"] == ["home", "iot"]
    assert [m["position"] for m in p["milestones"]] == [1, 2, 3]
    assert p["milestones"][0]["done_criteria"] == "A reading shows up on a laptop"
    assert p["milestones"][0]["created_by"] == "user"
    assert p["progress"] == {"done": 0, "total": 3}
    assert p["next_milestone"]["title"] == MILESTONES[0]["title"]
    assert [e["type"] for e in p["events"]][::-1] == ["created", "milestone_added"]


def test_get_by_id_or_slug(client):
    p = create(client)["project"]
    assert client.get(f"{BASE}/projects/{p['id']}").json()["title"] == p["title"]
    assert client.get(f"{BASE}/projects/{p['slug']}").json()["id"] == p["id"]
    assert client.get(f"{BASE}/projects/nope").status_code == 404
    assert client.get(f"{BASE}/projects/9999").status_code == 404


def test_slugs_stay_unique(client):
    a = create(client, "Same name")["project"]
    b = create(client, "Same name", force=True)["project"]
    assert (a["slug"], b["slug"]) == ("same-name", "same-name-2")


def test_list_and_filter(client):
    create(client, "Garden watering planner")
    p = create(client, "Office declutter helper")["project"]
    client.patch(f"{BASE}/projects/{p['id']}", json={"status": "active"})
    all_ = client.get(f"{BASE}/projects").json()["projects"]
    assert [x["title"] for x in all_] == ["Office declutter helper", "Garden watering planner"]  # recent first
    active = client.get(f"{BASE}/projects", params={"status": "active"}).json()["projects"]
    assert [x["title"] for x in active] == ["Office declutter helper"]
    assert client.get(f"{BASE}/projects", params={"status": "bogus"}).status_code == 422


def test_search_by_keywords(client):
    create(client, "Garden watering planner", tags=["iot"])
    create(client, "Office declutter helper", summary="Clear one desk zone at a time.")
    hits = client.get(f"{BASE}/projects", params={"q": "watering garden"}).json()["projects"]
    assert [h["title"] for h in hits] == ["Garden watering planner"]
    assert hits[0]["score"] == 1.0
    assert client.get(f"{BASE}/projects", params={"q": "iot"}).json()["projects"][0]["title"] == "Garden watering planner"
    assert client.get(f"{BASE}/projects", params={"q": "spaceship"}).json()["projects"] == []


def test_patch_project(client):
    p = create(client)["project"]
    r = client.patch(f"{BASE}/projects/{p['slug']}", json={"title": "Smart garden", "status": "killed",
                                                         "note": "Too much hardware"})
    assert r.status_code == 200
    out = r.json()
    assert out["title"] == "Smart garden" and out["status"] == "killed"
    assert out["slug"] == p["slug"]  # slugs don't change
    assert out["notes"][0]["text"] == "Too much hardware"
    assert {"type": "status_changed", "detail": {"from": "idea", "to": "killed"}}.items() <= next(
        e for e in out["events"] if e["type"] == "status_changed").items()


def test_patch_null_title_is_ignored(client):
    p = create(client)["project"]
    r = client.patch(f"{BASE}/projects/{p['id']}", json={"title": None})
    assert r.status_code == 200 and r.json()["title"] == p["title"]


# ---------- dedupe ----------

def test_duplicate_returns_existing(client):
    first = create(client, "Garden watering planner")["project"]
    out = create(client, "Garden Watering Planner App")
    assert out["created"] is False and out["reason"] == "duplicate"
    assert out["project"]["id"] == first["id"]
    assert len(client.get(f"{BASE}/projects").json()["projects"]) == 1


def test_force_creates_anyway(client):
    create(client, "Garden watering planner")
    assert create(client, "Garden watering planner", force=True)["created"] is True


def test_killed_projects_are_not_duplicates(client):
    p = create(client, "Garden watering planner")["project"]
    client.patch(f"{BASE}/projects/{p['id']}", json={"status": "killed"})
    assert create(client, "Garden watering planner")["created"] is True


def test_different_idea_is_created(client):
    create(client, "Garden watering planner")
    assert create(client, "Garden lighting planner")["created"] is True


# ---------- milestones ----------

def test_add_and_update_milestones(client):
    p = create(client, milestones=MILESTONES[:1])["project"]
    r = client.post(f"{BASE}/projects/{p['id']}/milestones", json={"milestones": MILESTONES[1:]})
    ms = r.json()["milestones"]
    assert [m["position"] for m in ms] == [1, 2, 3]

    first = ms[0]["id"]
    out = client.patch(f"{BASE}/milestones/{first}", json={"status": "doing"}).json()
    assert out["next_milestone"]["id"] == first
    out = client.patch(f"{BASE}/milestones/{first}", json={"status": "done", "note": "Works"}).json()
    m = out["milestones"][0]
    assert m["status"] == "done" and m["completed_at"] and m["note"] == "Works"
    assert out["progress"] == {"done": 1, "total": 3}
    assert out["next_milestone"]["id"] == ms[1]["id"]

    out = client.patch(f"{BASE}/milestones/{ms[2]['id']}", json={"status": "dropped"}).json()
    assert out["progress"] == {"done": 1, "total": 2}

    out = client.patch(f"{BASE}/milestones/{first}", json={"status": "todo"}).json()
    assert out["milestones"][0]["completed_at"] is None


def test_done_keeps_first_completion_time(client):
    p = create(client, milestones=MILESTONES[:1])["project"]
    mid = p["milestones"][0]["id"]
    t1 = client.patch(f"{BASE}/milestones/{mid}", json={"status": "done"}).json()["milestones"][0]["completed_at"]
    t2 = client.patch(f"{BASE}/milestones/{mid}", json={"status": "done", "note": "again"}).json()["milestones"][0]["completed_at"]
    assert t1 == t2


def test_reorder_milestones(client):
    p = create(client, milestones=MILESTONES)["project"]
    ids = [m["id"] for m in p["milestones"]]
    out = client.patch(f"{BASE}/milestones/{ids[2]}", json={"position": 1}).json()
    assert [m["id"] for m in out["milestones"]] == [ids[2], ids[0], ids[1]]
    assert [m["position"] for m in out["milestones"]] == [1, 2, 3]
    out = client.patch(f"{BASE}/milestones/{ids[2]}", json={"position": 99}).json()
    assert [m["id"] for m in out["milestones"]] == [ids[0], ids[1], ids[2]]


def test_unknown_milestone(client):
    assert client.patch(f"{BASE}/milestones/9999", json={"status": "done"}).status_code == 404


# ---------- notes and sessions ----------

def test_notes(client):
    p = create(client)["project"]
    out = client.post(f"{BASE}/projects/{p['id']}/notes", json={"text": "Line one\nLine two"}).json()
    assert out["notes"][0]["text"] == "Line one\nLine two"


def test_session_header_links_project(mcp):
    sid = str(uuid.uuid4())
    r = mcp.post(f"{BASE}/projects", json={"title": "Garden watering planner", "milestones": MILESTONES},
                 headers={"X-Claude-Session": sid})
    p = r.json()["project"]
    assert p["source_session_id"] == sid
    assert [s["session_id"] for s in p["sessions"]] == [sid]
    assert all(m["created_by"] == "claude" for m in p["milestones"])

    other = str(uuid.uuid4())
    out = mcp.patch(f"{BASE}/milestones/{p['milestones'][0]['id']}", json={"status": "done"},
                    headers={"X-Claude-Session": other}).json()
    assert [s["session_id"] for s in out["sessions"]] == [sid, other]
    # Linking the same session twice adds nothing.
    out = mcp.post(f"{BASE}/projects/{p['id']}/notes", json={"text": "x"}, headers={"X-Claude-Session": other}).json()
    assert len(out["sessions"]) == 2
    assert sum(e["type"] == "session_linked" for e in out["events"]) == 2


def test_bad_session_header(client):
    r = client.post(f"{BASE}/projects", json={"title": "X"}, headers={"X-Claude-Session": "not-a-uuid"})
    assert r.status_code == 400


# ---------- limits and validation ----------

@pytest.mark.parametrize("body", [
    {"title": ""},
    {"title": "   "},
    {"title": "x" * 121},
    {"title": "ok", "summary": "x" * 2001},
    {"title": "ok", "milestones": [{"title": f"m{i}"} for i in range(13)]},
    {"title": "ok", "tags": [f"t{i}" for i in range(13)]},
    {"title": "bad\x00title"},
    {"title": "two\nlines"},
    {"title": "ok", "summary": "bell\x07"},
    {"title": "ok", "milestones": [{"title": "m", "done_criteria": "x" * 501}]},
    {"title": "ok", "status": "someday"},
])
def test_rejects_bad_input(client, body):
    assert client.post(f"{BASE}/projects", json=body).status_code == 422


def test_accepts_limits_exactly(client):
    out = create(client, "x" * 120, milestones=[{"title": f"step {i}"} for i in range(12)])
    assert out["created"] and len(out["project"]["milestones"]) == 12


def test_milestone_cap_per_project(client):
    p = create(client)["project"]
    for _ in range(4):
        client.post(f"{BASE}/projects/{p['id']}/milestones", json={"milestones": [{"title": "m"}] * 12})
    r = client.post(f"{BASE}/projects/{p['id']}/milestones", json={"milestones": [{"title": "m"}] * 12})
    assert r.status_code == 400 and "at most" in r.json()["detail"]


# ---------- repo_path ----------

def test_repo_path_inside_workspace(client, workspace):
    folder = workspace / "garden"
    folder.mkdir(exist_ok=True)
    p = create(client, repo_path=str(folder))["project"]
    assert p["repo_path"] == os.path.realpath(folder)
    out = client.patch(f"{BASE}/projects/{p['id']}", json={"repo_path": ""}).json()
    assert out["repo_path"] is None


@pytest.mark.parametrize("path", ["/etc", "relative/path", "/"])
def test_repo_path_outside_workspace_rejected(client, path):
    r = client.post(f"{BASE}/projects", json={"title": "X", "repo_path": path})
    assert r.status_code == 400
    assert "workspace" in r.json()["detail"] or "absolute" in r.json()["detail"]


def test_repo_path_symlink_escape_rejected(client, workspace):
    link = workspace / "escape"
    if not link.exists():
        link.symlink_to("/etc")
    r = client.post(f"{BASE}/projects", json={"title": "X", "repo_path": str(link)})
    assert r.status_code == 400


def test_repo_path_dotdot_escape_rejected(client, workspace):
    r = client.post(f"{BASE}/projects", json={"title": "X", "repo_path": f"{workspace}/../.."})
    assert r.status_code == 400
