import stat

from fastapi.testclient import TestClient

from hangar import auth
from hangar.app import app
from hangar.ideas import token

URL = "/api/ideas/projects"


def test_needs_sign_in(anon):
    assert anon.get(URL).status_code == 401
    assert anon.post(URL, json={"title": "x"}).status_code == 401


def test_cookie_write_needs_x_hangar_header(anon):
    anon.cookies.set(auth.COOKIE, auth.make_token())
    assert anon.get(URL).status_code == 200
    assert anon.post(URL, json={"title": "x"}).status_code == 403
    assert anon.post(URL, json={"title": "x"}, headers={"X-Hangar": "1"}).status_code == 200


def test_bearer_token_works_without_cookie_or_header(mcp):
    assert mcp.get(URL).status_code == 200
    assert mcp.post(URL, json={"title": "x"}).status_code == 200


def test_wrong_or_malformed_token_is_refused():
    for value in ["Bearer wrong", "Bearer ", "Basic " + token.get(), token.get(), "bearer"]:
        c = TestClient(app, headers={"Authorization": value})
        assert c.get(URL).status_code == 401, value


def test_token_only_works_for_ideas(mcp):
    assert mcp.get("/api/sessions").status_code == 401
    assert mcp.get("/api/files", params={"path": "/"}).status_code == 401
    assert mcp.post("/api/tmux/new", json={"cwd": "/"}).status_code == 401


def test_token_file_is_private():
    token.get()
    mode = stat.S_IMODE(token.path().stat().st_mode)
    assert mode == 0o600


def test_token_is_stable_and_long():
    token.reset_cache()
    a = token.get()
    token.reset_cache()
    assert token.get() == a
    assert len(a) >= 40


def test_me_lists_plugin(client):
    assert client.get("/api/me").json()["plugins"] == ["ideas"]
