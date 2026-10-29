"""Link Claude: Claude Code's own subscription sign-in (no API key), stored in Hangar's Claude folder.

`claude auth login` prints a link. The person signs in on claude.ai, which shows a code, and pastes it
back here; Hangar types it into the waiting CLI.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from ..config import settings
from .pty_run import PtyRun, clean_env

_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
_lock = threading.Lock()
_flow: PtyRun | None = None
_flow_url: str | None = None
_cache: tuple[float, dict] | None = None


def _env() -> dict[str, str]:
    return clean_env({"CLAUDE_CONFIG_DIR": str(settings.claude_dir)})


def _credentials() -> dict:
    """The subscription login Claude Code keeps in its folder (the same file the usage card reads)."""
    try:
        data = json.loads((settings.claude_dir / ".credentials.json").read_text())
    except (OSError, ValueError):
        return {}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else {}


def status(fresh: bool = False) -> dict:
    """{'linked': bool, 'email', 'plan', 'org', 'method', 'folder', 'login': {...} | None}

    Linked means Claude Code's credentials file has a subscription login. `claude auth status` adds the
    account details; if it fails, the file still decides."""
    global _cache
    if not fresh and _cache and time.time() - _cache[0] < 15:
        out = dict(_cache[1])
    else:
        creds = _credentials()
        out = {"linked": bool(creds), "folder": str(settings.claude_dir), "plan": creds.get("subscriptionType")}
        try:
            r = subprocess.run([settings.claude_cmd, "auth", "status", "--json"], capture_output=True, text=True,
                               timeout=60, env=_env())
            text = r.stdout or ""
            data = json.loads(text[text.index("{"):]) if "{" in text else {}
            out.update({"linked": bool(data.get("loggedIn")) or out["linked"], "email": data.get("email"),
                        "plan": data.get("subscriptionType") or out["plan"], "org": data.get("orgName"),
                        "method": data.get("authMethod")})
            if not data:
                out["status_output"] = (text + r.stderr).strip()[-300:]
        except FileNotFoundError:
            out["error"] = f"Claude Code isn't installed ({settings.claude_cmd} not found)."
        except (subprocess.TimeoutExpired, ValueError) as e:
            out["status_output"] = f"claude auth status failed: {e}"
        _cache = (time.time(), dict(out))
    problem = folder_problem()
    if problem:
        out["error"] = problem
    out["login"] = _flow_state()
    return out


def folder_problem() -> str | None:
    """Claude Code can't save a login into a folder it can't write (it still says "Login successful")."""
    d = settings.claude_dir
    target = d if d.exists() else d.parent
    if os.access(target, os.W_OK):
        return None
    try:
        owner = target.owner()
    except (KeyError, OSError):
        owner = "another user"
    return (f"Hangar can't write to {target} (it belongs to {owner}), so Claude can't save a sign-in there. "
            f"Fix it with: docker exec -u root <container> chown {os.getuid()}:{os.getgid()} {target}  "
            "(restarting the container also fixes it if sudo is allowed).")


def _flow_state() -> dict | None:
    if not _flow:
        return None
    return {"url": _flow_url, "running": not _flow.done}


def start_login() -> dict:
    """Starts `claude auth login` and returns the sign-in link."""
    global _flow, _flow_url
    problem = folder_problem()
    if problem:
        raise RuntimeError(problem)
    with _lock:
        cancel_login()
        _flow = PtyRun([settings.claude_cmd, "auth", "login", "--claudeai"], _env(), cwd=str(settings.data_dir))
        m = _flow.wait_for(_URL, 30)
        if not m:
            tail = _tail(_flow.text)
            cancel_login()
            raise RuntimeError(f"Claude didn't print a sign-in link. {tail}".strip())
        _flow_url = m.group(0)
        return {"url": _flow_url}


def submit_code(code: str) -> dict:
    """Types the code from claude.ai into the waiting CLI, then waits for it to finish."""
    global _cache
    code = (code or "").strip()
    if not code or len(code) > 512 or any(c in code for c in "\r\n\x03\x04"):
        raise ValueError("Paste the code exactly as claude.ai shows it.")
    with _lock:
        if not _flow or _flow.done:
            raise RuntimeError("The sign-in expired. Start again.")
        _flow.write(code + "\r")
        _flow.wait(90)
        output = _flow.text
        said_ok = _flow.exit_code == 0 or bool(re.search(r"login successful", output, re.I))
        st = {"linked": False}
        for _ in range(5 if said_ok else 1):  # give the CLI a moment to finish writing its files
            _cache = None
            st = status(fresh=True)
            if st["linked"]:
                break
            time.sleep(2)
        cancel_login()  # finished or failed, this attempt's link can't be used again
        if st["linked"]:
            return status()
        if said_ok:
            raise RuntimeError(
                f"Claude Code says the sign-in worked, but Hangar can't find the login in {settings.claude_dir}. "
                f"Details: {st.get('status_output') or st.get('error') or 'claude auth status reports signed out'}")
        raise RuntimeError(explain(output))


def explain(output: str) -> str:
    """Turns the CLI's last words into something a person can act on."""
    tail = _tail(output)
    if re.search(r"status code 4\d\d|invalid[_ ]grant|invalid code", output, re.I):
        return ("Claude didn't accept that code. A code works once, only with the sign-in link from the same "
                "attempt, and only for a few minutes. Click Sign in with Claude and use the new link. "
                f"(Claude Code said: {tail})")
    if re.search(r"status code 5\d\d|ECONNRESET|ETIMEDOUT|ENOTFOUND|network", output, re.I):
        return f"Couldn't reach Claude to finish signing in. Check this machine's internet access. ({tail})"
    return f"Sign-in didn't finish. Claude Code said: {tail or 'nothing'}"


def cancel_login() -> None:
    global _flow, _flow_url
    if _flow:
        _flow.stop()
    _flow, _flow_url = None, None


def logout() -> dict:
    global _cache
    subprocess.run([settings.claude_cmd, "auth", "logout"], capture_output=True, text=True, timeout=30, env=_env())
    _cache = None
    return status(fresh=True)


def _tail(text: str, lines: int = 4) -> str:
    kept = [ln.strip() for ln in text.splitlines() if ln.strip() and not _URL.search(ln)]
    return " ".join(kept[-lines:])[:400]
