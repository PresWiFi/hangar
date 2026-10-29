"""Link Claude: Claude Code's own subscription sign-in (no API key), stored in Hangar's Claude folder.

`claude auth login` prints a link. The person signs in on claude.ai, which shows a code, and pastes it
back here; Hangar types it into the waiting CLI.
"""
from __future__ import annotations

import json
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


def status(fresh: bool = False) -> dict:
    """{'linked': bool, 'email', 'plan', 'org', 'method', 'folder', 'login': {...} | None}"""
    global _cache
    if not fresh and _cache and time.time() - _cache[0] < 15:
        out = dict(_cache[1])
    else:
        out = {"linked": False, "folder": str(settings.claude_dir)}
        try:
            r = subprocess.run([settings.claude_cmd, "auth", "status", "--json"], capture_output=True, text=True,
                               timeout=20, env=_env())
            data = json.loads(r.stdout or "{}")
            out.update({"linked": bool(data.get("loggedIn")), "email": data.get("email"),
                        "plan": data.get("subscriptionType"), "org": data.get("orgName"),
                        "method": data.get("authMethod")})
        except FileNotFoundError:
            out["error"] = f"Claude Code isn't installed ({settings.claude_cmd} not found)."
        except (subprocess.TimeoutExpired, ValueError) as e:
            out["error"] = f"Couldn't read Claude's sign-in status: {e}"
        _cache = (time.time(), dict(out))
    out["login"] = _flow_state()
    return out


def _flow_state() -> dict | None:
    if not _flow:
        return None
    return {"url": _flow_url, "running": not _flow.done}


def start_login() -> dict:
    """Starts `claude auth login` and returns the sign-in link."""
    global _flow, _flow_url
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
        _cache = None
        st = status(fresh=True)
        if st["linked"]:
            cancel_login()
            return status()
        if not _flow.done:
            cancel_login()
        raise RuntimeError(f"Sign-in didn't finish. {_tail(output)}".strip())


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
