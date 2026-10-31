"""Link GitHub through the GitHub CLI, so sessions can clone, pull, and push over HTTPS without SSH keys.

`gh auth login --web` prints a one-time code. The person enters it at github.com/login/device, and gh
keeps the token in its config folder (~/.config/gh, in the container's home volume). `gh auth setup-git`
then makes git ask gh for credentials whenever it talks to github.com. Pasting a personal access token
is the fallback.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from ..config import settings
from .pty_run import PtyRun, clean_env

HOST = "github.com"
DEVICE_URL = "https://github.com/login/device"
SCOPES = "repo,read:org,workflow"
TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo,read:org,workflow&description=Hangar"

_CODE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
_PRESS_ENTER = re.compile(r"Press Enter", re.I)
_GIT_PROMPT = re.compile(r"Authenticate Git with your GitHub credentials", re.I)
_REPO = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})$")
_HELPER = re.compile(r"gh(\.exe)?\s+auth\s+git-credential")

_lock = threading.Lock()
_flow: PtyRun | None = None
_flow_info: dict = {}
_cache: tuple[float, dict] | None = None


def _env() -> dict[str, str]:
    # Never let git or gh stop and wait for a password nobody can type.
    return clean_env({"GIT_TERMINAL_PROMPT": "0", "GH_NO_UPDATE_NOTIFIER": "1"})


def _gh(*args: str, input: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([settings.gh_cmd, *args], input=input, capture_output=True, text=True,
                          timeout=timeout, env=_env(), stdin=None if input is not None else subprocess.DEVNULL)


def _git(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, env=_env(),
                          stdin=subprocess.DEVNULL)


def installed() -> bool:
    return bool(shutil.which(settings.gh_cmd))


# ---------- status ----------

def _user() -> dict | None:
    """The signed-in account, or None."""
    try:
        r = _gh("api", "user")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("login") else None


def git_configured() -> bool:
    r = _git("config", "--global", "--get-all", f"credential.https://{HOST}.helper")
    return bool(_HELPER.search(r.stdout or ""))


def status(fresh: bool = False) -> dict:
    global _cache
    out = {"installed": installed(), "linked": False, "device_url": DEVICE_URL, "token_url": TOKEN_URL}
    # gh saves the login just before it exits, and git isn't set up until after. Until then, it's in progress.
    signing_in = bool(_flow) and "succeeded" not in _flow_info and "error" not in _flow_info
    if out["installed"] and not signing_in:
        if not fresh and _cache and time.time() - _cache[0] < 30:
            out.update(_cache[1])
        else:
            user = _user()
            found = {"linked": bool(user)}
            if user:
                found.update({"login": user["login"], "name": user.get("name"), "git": git_configured()})
            _cache = (time.time(), found)
            out.update(found)
    if out["linked"] and _flow_info.get("succeeded"):
        cancel_login()  # finished; nothing left to show
    out["login_flow"] = _flow_state()
    return out


# ---------- after sign-in ----------

def _finish() -> None:
    """Points git at gh for github.com and fills in a commit identity if there isn't one yet."""
    global _cache
    _cache = None
    r = _gh("auth", "setup-git", "--hostname", HOST)
    if r.returncode != 0:
        raise RuntimeError("Signed in, but setting up git failed: " + (r.stderr or r.stdout).strip()[-300:])
    user = _user() or {}
    if user and not _git("config", "--global", "user.name").stdout.strip():
        _git("config", "--global", "user.name", user.get("name") or user["login"])
    if user and not _git("config", "--global", "user.email").stdout.strip():
        email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
        _git("config", "--global", "user.email", email)


# ---------- device-code sign-in ----------

def _watch(flow: PtyRun) -> None:
    """Answers gh's prompts and finishes setup once it exits. Runs until the flow ends or is replaced."""
    answered: set[str] = set()
    while not flow.done and _flow is flow:
        text = flow.text
        m = _CODE.search(text)
        if m and "code" not in _flow_info:
            _flow_info["code"] = m.group(1)
        if _GIT_PROMPT.search(text) and "git" not in answered:
            flow.write("Y\n")
            answered.add("git")
        if _PRESS_ENTER.search(text) and "enter" not in answered:
            flow.write("\n")  # "Press Enter to open github.com in your browser" (BROWSER=true, so nothing opens)
            answered.add("enter")
        time.sleep(0.2)
    if _flow is not flow:
        return
    if flow.exit_code == 0:
        try:
            _finish()
            _flow_info["succeeded"] = True
        except RuntimeError as e:
            _flow_info["error"] = str(e)
    else:
        lines = " ".join(ln.strip() for ln in flow.text.splitlines() if ln.strip())
        _flow_info["error"] = lines[-400:] or "Sign-in stopped."


def _flow_state() -> dict | None:
    if not _flow:
        return None
    info = dict(_flow_info)
    info["running"] = not _flow.done or not ("succeeded" in info or "error" in info)
    info.setdefault("url", DEVICE_URL)
    return info


def start_login() -> dict:
    global _flow
    if not installed():
        raise RuntimeError("The GitHub CLI (gh) isn't installed here. Rebuild the Hangar image, or install gh.")
    with _lock:
        cancel_login()
        args = [settings.gh_cmd, "auth", "login", "--hostname", HOST, "--git-protocol", "https", "--web",
                "--scopes", SCOPES]
        _flow = PtyRun(args, _env())
        threading.Thread(target=_watch, args=(_flow,), daemon=True).start()
        m = _flow.wait_for(_CODE, 30)
        if not m:
            text = _flow.text
            cancel_login()
            raise RuntimeError("GitHub CLI didn't print a sign-in code. " + " ".join(text.split())[-300:])
        _flow_info["code"] = m.group(1)
        return _flow_state()


def cancel_login() -> None:
    global _flow
    if _flow:
        _flow.stop()
    _flow = None
    _flow_info.clear()


def login_with_token(token: str) -> dict:
    token = (token or "").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Paste the whole token.")
    if not installed():
        raise RuntimeError("The GitHub CLI (gh) isn't installed here.")
    with _lock:
        cancel_login()
        r = _gh("auth", "login", "--hostname", HOST, "--git-protocol", "https", "--with-token", input=token + "\n")
        if r.returncode != 0:
            raise RuntimeError("GitHub didn't accept that token: " + (r.stderr or r.stdout).strip()[-300:])
        _finish()
    return status(fresh=True)


def logout() -> dict:
    global _cache
    with _lock:
        cancel_login()
        if installed():
            _gh("auth", "logout", "--hostname", HOST)
        for key in (f"credential.https://{HOST}.helper", "credential.https://gist.github.com.helper"):
            _git("config", "--global", "--unset-all", key)
        _cache = None
    return status(fresh=True)


# ---------- repos ----------

def repos() -> list[dict]:
    """Repos the account owns, collaborates on, or can see through its orgs, most recently pushed first."""
    r = _gh("api", "user/repos?per_page=100&sort=pushed&affiliation=owner,collaborator,organization_member")
    if r.returncode != 0:
        raise RuntimeError("Couldn't list your repos: " + (r.stderr or r.stdout).strip()[-300:])
    return [{"full_name": x["full_name"], "private": x.get("private", False), "description": x.get("description") or "",
             "pushed_at": x.get("pushed_at")} for x in json.loads(r.stdout)]


def parse_repo(text: str) -> tuple[str, str]:
    """owner/name from 'owner/name' or a github.com address (https or git@)."""
    t = (text or "").strip()
    t = re.sub(r"^(https?://)?(www\.)?github\.com[/:]", "", t)
    t = re.sub(r"^git@github\.com:", "", t)
    t = re.sub(r"(\.git)?/?$", "", t)
    m = _REPO.match(t)
    if not m or m.group(2) in (".", ".."):
        raise ValueError("Enter a GitHub repo as owner/name or its github.com address.")
    return m.group(1), m.group(2)


def clone(repo: str, parent: str) -> dict:
    """Clones into <parent>/<name>. The caller checks that parent is inside a workspace."""
    owner, name = parse_repo(repo)
    parent = os.path.realpath(parent)
    if not os.path.isdir(parent):
        raise ValueError("That folder doesn't exist.")
    dest = os.path.join(parent, name)
    if os.path.exists(dest):
        raise RuntimeError(f"{dest} already exists. Pick another folder, or start a session in that one.")
    r = _git("clone", "--", f"https://{HOST}/{owner}/{name}.git", dest, timeout=900)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        msg = (r.stderr or r.stdout).strip()
        low = msg.lower()
        if any(t in low for t in ("could not read username", "authentication failed", "not found",
                                  "could not read from remote")):
            msg = f"Couldn't clone {owner}/{name}. Check the name, and link GitHub in Settings for private repos. ({msg[-200:]})"
        raise RuntimeError(msg[-400:])
    return {"path": dest, "repo": f"{owner}/{name}"}
