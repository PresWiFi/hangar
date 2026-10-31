"""Linking GitHub through the gh CLI, and cloning repos. A stand-in gh replaces the real one."""
import json
import shutil
import subprocess
import time

import pytest

from hangar.config import settings
from hangar.providers import github
from tests.test_providers import script


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """`api user` works once `linked` exists. The device flow waits for `approve`, like a person on github.com,
    and hangs at a prompt unless GH_PROMPT_DISABLED=1, like the real gh."""
    linked, approve = tmp_path / "linked", tmp_path / "approve"
    repos = [{"full_name": "me/app", "private": True, "description": "My app", "pushed_at": "2026-10-01T00:00:00Z"}]
    exe = script(tmp_path / "gh", f"""
        if [ "$1 $2" = "api user" ]; then
          [ -f {linked} ] || {{ echo "gh: To get started with GitHub CLI, please run:  gh auth login" >&2; exit 4; }}
          echo '{{"login": "octo", "id": 42, "name": "Octo Cat", "email": null}}'
        elif [ "$1" = "api" ]; then echo '{json.dumps(repos)}'
        elif [ "$1 $2" = "auth login" ] && [[ " $* " == *" --with-token "* ]]; then
          read -r tok
          if [ "$tok" = "ghp_good" ]; then touch {linked}; else echo "error validating token: HTTP 401" >&2; exit 1; fi
        elif [ "$1 $2" = "auth login" ]; then
          if [ "$GH_PROMPT_DISABLED" != 1 ]; then
            # Like gh 2.10x: asks about git, queries the cursor position, and hangs without a reply.
            printf '? Authenticate Git with your GitHub credentials? (Y/n) \033[6n'
            sleep 300
          fi
          echo "! First copy your one-time code: ABCD-1234"
          echo "Open this URL to continue in your web browser: https://github.com/login/device"
          while [ ! -f {approve} ]; do sleep 0.1; done
          echo "✓ Authentication complete."; touch {linked}
        elif [ "$1 $2" = "auth setup-git" ]; then
          git config --global credential.https://github.com.helper '!gh auth git-credential'
        elif [ "$1 $2" = "auth logout" ]; then rm -f {linked}
        fi
    """)
    monkeypatch.setattr(settings, "gh_cmd", exe)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("")
    github._cache = None
    yield {"linked": linked, "approve": approve, "gitconfig": tmp_path / "gitconfig"}
    github.cancel_login()
    github._cache = None


def git_global(key):
    return subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()


def test_not_installed(client, monkeypatch):
    monkeypatch.setattr(settings, "gh_cmd", "/nonexistent/gh")
    st = client.get("/api/providers").json()["github"]
    assert st["installed"] is False and st["linked"] is False
    assert client.post("/api/providers/github/login").status_code == 409


def test_device_flow(fake_gh, client):
    assert client.get("/api/providers/github").json()["linked"] is False
    r = client.post("/api/providers/github/login")
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "ABCD-1234" and r.json()["url"] == github.DEVICE_URL and r.json()["running"]

    fake_gh["approve"].touch()
    for _ in range(100):
        st = client.get("/api/providers/github").json()
        if st["linked"]:
            break
        github._cache = None
        time.sleep(0.05)
    assert st["linked"] and st["login"] == "octo" and st["git"] is True
    assert st["login_flow"] is None
    # Commit identity filled in from the account, with the private noreply address.
    assert git_global("user.name") == "Octo Cat"
    assert git_global("user.email") == "42+octo@users.noreply.github.com"


def test_keeps_existing_git_identity(fake_gh, client):
    subprocess.run(["git", "config", "--global", "user.email", "me@example.com"], check=True)
    assert client.post("/api/providers/github/token", json={"token": "ghp_good"}).status_code == 200
    assert git_global("user.email") == "me@example.com"


def test_token(fake_gh, client):
    r = client.post("/api/providers/github/token", json={"token": "ghp_bad"})
    assert r.status_code == 409 and "HTTP 401" in r.json()["detail"]
    r = client.post("/api/providers/github/token", json={"token": "ghp_good"})
    assert r.status_code == 200 and r.json()["linked"] and r.json()["git"]
    assert client.post("/api/providers/github/token", json={"token": "two words"}).status_code == 400


def test_logout_removes_git_helper(fake_gh, client):
    client.post("/api/providers/github/token", json={"token": "ghp_good"})
    st = client.post("/api/providers/github/logout").json()
    assert st["linked"] is False
    assert "git-credential" not in fake_gh["gitconfig"].read_text()


def test_repos(fake_gh, client):
    r = client.get("/api/providers/github/repos")
    assert r.json()["repos"] == [{"full_name": "me/app", "private": True, "description": "My app",
                                  "pushed_at": "2026-10-01T00:00:00Z"}]


@pytest.mark.parametrize("text,expect", [
    ("me/app", ("me", "app")),
    ("https://github.com/me/app.git", ("me", "app")),
    ("github.com/me/app/", ("me", "app")),
    ("git@github.com:me/my.repo.git", ("me", "my.repo")),
])
def test_parse_repo(text, expect):
    assert github.parse_repo(text) == expect


@pytest.mark.parametrize("text", ["", "app", "me/..", "-x/app", "me/app; rm -rf /", "https://gitlab.com/me/app", "a/b/c"])
def test_parse_repo_rejects(text):
    with pytest.raises(ValueError):
        github.parse_repo(text)


@pytest.fixture
def remote(tmp_path, fake_gh):
    """github.com/me/app served from a local bare repo, through git's insteadOf."""
    root = tmp_path / "remotes"
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    (src / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "me" / "app.git")], check=True)
    subprocess.run(["git", "config", "--global", f"url.file://{root}/.insteadOf", "https://github.com/"], check=True)
    return root


def test_clone(remote, client, workspace):
    r = client.post("/api/providers/github/clone", json={"repo": "https://github.com/me/app", "parent": str(workspace)})
    assert r.status_code == 200, r.text
    assert r.json() == {"path": str(workspace / "app"), "repo": "me/app"}
    try:
        assert (workspace / "app" / "README.md").read_text() == "hi\n"
        # Second time: the folder is there already.
        r = client.post("/api/providers/github/clone", json={"repo": "me/app", "parent": str(workspace)})
        assert r.status_code == 409 and "already exists" in r.json()["detail"]
    finally:
        shutil.rmtree(workspace / "app", ignore_errors=True)


def test_clone_missing_repo_cleans_up(remote, client, workspace):
    r = client.post("/api/providers/github/clone", json={"repo": "me/nope", "parent": str(workspace)})
    assert r.status_code == 409 and "Couldn't clone me/nope" in r.json()["detail"]
    assert not (workspace / "nope").exists()


def test_clone_outside_workspace(fake_gh, client, tmp_path):
    r = client.post("/api/providers/github/clone", json={"repo": "me/app", "parent": str(tmp_path)})
    assert r.status_code == 403
