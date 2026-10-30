"""ChatGPT plan limits for Codex, checked on a timer like Claude's usage.

This is the same read Codex's own /status does: GET <chatgpt base>/wham/usage with the ChatGPT access token
from $CODEX_HOME/auth.json. Hangar only reads that token. It never refreshes it: refreshing would rotate
Codex's refresh token and sign Codex out. When the token has expired, Hangar waits; Codex refreshes it the
next time it's used, and meanwhile the dashboard shows the limits from Codex's last reply instead.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request

from .config import settings
from .store import kv_get, kv_set

BASE_URL = "https://chatgpt.com/backend-api"


class CodexUsageError(Exception):
    pass


def _auth() -> tuple[str, str | None] | None:
    """(access token, account id) for a ChatGPT login, or None if Codex isn't signed in with ChatGPT."""
    try:
        data = json.loads((settings.codex_home / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return None
    return tokens["access_token"], tokens.get("account_id")


def _expires(jwt: str) -> float | None:
    try:
        body = jwt.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def _window(w: dict | None, key: str) -> dict | None:
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    seconds = w.get("limit_window_seconds")
    return {"key": key, "used_percent": float(w["used_percent"]),
            "window_minutes": round(seconds / 60) if seconds else None, "resets_at": w.get("reset_at")}


def fetch() -> dict:
    """Current limits in the same shape as CodexIndex.usage(), plus source="live"."""
    auth = _auth()
    if not auth:
        raise CodexUsageError("Codex isn't signed in with ChatGPT.")
    token, account = auth
    exp = _expires(token)
    if exp and exp < time.time() + 30:
        raise CodexUsageError("Codex's ChatGPT token has expired. It refreshes the next time you use Codex.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "codex-cli"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    req = urllib.request.Request(f"{BASE_URL}/wham/usage", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read(500_000))
    except urllib.error.HTTPError as e:
        err = CodexUsageError({401: "ChatGPT didn't accept Codex's token. Use Codex once to refresh it.",
                               429: "ChatGPT is rate-limiting usage checks; trying again later."}
                              .get(e.code, f"ChatGPT usage check failed (HTTP {e.code})."))
        err.retry_after = int(e.headers.get("Retry-After") or 0) if e.code == 429 else None
        raise err
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise CodexUsageError(f"Couldn't reach ChatGPT: {getattr(e, 'reason', e)}")
    rl = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    windows = [w for w in (_window(rl.get("primary_window"), "primary"),
                           _window(rl.get("secondary_window"), "secondary")) if w]
    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else None
    return {"as_of": time.time(), "plan": payload.get("plan_type"), "windows": windows, "credits": credits,
            "source": "live"}


class CodexUsagePoller:
    """Checks every HANGAR_USAGE_POLL_MINUTES while Codex is signed in with ChatGPT, with backoff on errors."""

    def __init__(self) -> None:
        self.usage: dict | None = kv_get("last_codex_usage")
        self.error: str | None = None
        self._wake = threading.Event()
        self._failures = 0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="codex-usage").start()

    def refresh_now(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        interval = settings.usage_poll_minutes * 60
        if self.usage and time.time() - (self.usage.get("as_of") or 0) < interval:
            self._wake.wait(interval - (time.time() - self.usage["as_of"]))
            self._wake.clear()
        while True:
            delay = interval
            if _auth():
                try:
                    self.usage = fetch()
                    self.error = None
                    self._failures = 0
                    kv_set("last_codex_usage", self.usage)
                except CodexUsageError as e:
                    self.error = str(e)
                    self._failures += 1
                    retry = getattr(e, "retry_after", None)
                    delay = min(3600, max(retry or 0, interval * 2 ** min(self._failures, 3)))
                except Exception as e:  # keep the thread alive no matter what
                    self.error = f"Unexpected error: {e}"
            else:
                self.error = None
            self._wake.wait(delay)
            self._wake.clear()


def newest(live: dict | None, from_logs: dict | None) -> dict | None:
    """Whichever is more recent: the timed check or the limits Codex logged with its last reply."""
    candidates = [u for u in (live, from_logs) if u and u.get("windows")]
    if not candidates:
        return None
    best = max(candidates, key=lambda u: u.get("as_of") or 0)
    return {**best, "source": best.get("source") or "logs"}


codex_poller = CodexUsagePoller()
