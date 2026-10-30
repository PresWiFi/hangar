"""Claude plan usage (the same numbers `/usage` shows), read with Claude Code's own login.

The OAuth token is only ever read, never refreshed or written: refreshing it would rotate
Claude Code's refresh token and sign it out.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import settings
from .store import kv_get, kv_set

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
STATUS_URL = "https://status.claude.com/api/v2/summary.json"

LABELS = {
    "five_hour": "Current session (5h)",
    "seven_day": "Weekly (all models)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_oauth_apps": "Weekly (OAuth apps)",
    "iguana_necktie": "Cloud session credit",  # promo credit; it expires instead of resetting
}
EXPIRING = {"iguana_necktie"}
WINDOW_HOURS = {"five_hour": 5}


class UsageError(Exception):
    pass


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _token() -> tuple[str, str | None]:
    path = settings.claude_dir / ".credentials.json"
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError("No Claude Code login found. Sign in with `claude` (on the host or in a Hangar terminal).")
    except (OSError, ValueError) as e:
        raise UsageError(f"Could not read Claude Code login: {e}")
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise UsageError("Claude Code is signed in with an API key; plan usage needs a Claude subscription login.")
    expires_ms = oauth.get("expiresAt")
    if expires_ms and expires_ms / 1000 < time.time():
        raise UsageError("Login token expired. Run Claude Code once and it will refresh it.")
    return token, oauth.get("subscriptionType")


def _window_hours(key: str) -> float | None:
    if key in WINDOW_HOURS:
        return WINDOW_HOURS[key]
    return 168 if key.startswith("seven_day") else None


def _pace(key: str, utilization: float, resets_at: datetime | None) -> float | None:
    """Projected utilization at reset if use continues at the current average rate."""
    hours = _window_hours(key)
    if not hours or not resets_at:
        return None
    window = timedelta(hours=hours)
    elapsed = window - (resets_at - datetime.now(timezone.utc))
    if elapsed <= window * 0.05:
        return None
    return utilization * (window / elapsed)


def fetch_usage() -> dict:
    token, plan = _token()
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "Hangar/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise UsageError("Login rejected (401). Run Claude Code once to refresh the login.") from e
        if e.code == 429:
            retry = e.headers.get("Retry-After", "")
            err = UsageError("Rate limited by the usage endpoint; backing off.")
            err.retry_after = int(retry) if retry.isdigit() else None  # type: ignore[attr-defined]
            raise err from e
        raise UsageError(f"Usage request failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise UsageError(f"Network error: {e.reason}") from e

    limits = []
    for key, value in data.items():
        if key == "extra_usage" or not isinstance(value, dict) or value.get("utilization") is None:
            continue
        resets = parse_time(value.get("resets_at"))
        util = float(value["utilization"])
        limits.append({
            "key": key,
            "label": LABELS.get(key, key.replace("_", " ").capitalize()),
            "utilization": util,
            "resets_at": resets.isoformat() if resets else None,
            "ends": "expires" if key in EXPIRING else "resets",
            "pace": _pace(key, util, resets),
        })
    order = list(LABELS)
    limits.sort(key=lambda l: order.index(l["key"]) if l["key"] in order else 99)
    extra = data.get("extra_usage")
    return {
        "limits": limits,
        "extra_usage": extra if isinstance(extra, dict) and extra.get("is_enabled") else None,
        "plan": plan,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_status() -> dict:
    req = urllib.request.Request(STATUS_URL, headers={"User-Agent": "Hangar/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    return {
        "indicator": (data.get("status") or {}).get("indicator", "none"),
        "description": (data.get("status") or {}).get("description", ""),
        "incidents": [{"name": i.get("name"), "status": i.get("status"), "url": i.get("shortlink")}
                      for i in data.get("incidents") or []],
    }


def _mtime(path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def wait_or_login_change(wake: threading.Event, delay: float, login_file, failing: bool) -> None:
    """Sleeps `delay` seconds, or less: a refresh request wakes it, and after a failure so does a change
    to the login file (the CLI refreshed its token or someone signed in), so usage recovers right away."""
    start = _mtime(login_file)
    end = time.time() + delay
    while (left := end - time.time()) > 0:
        if wake.wait(min(left, 15 if failing else left)):
            break
        if failing and _mtime(login_file) != start:
            break
    wake.clear()


class UsagePoller:
    """Polls usage in the background with backoff. The endpoint rate-limits after a few quick calls."""

    def __init__(self) -> None:
        self.usage: dict | None = kv_get("last_usage")
        self.error: str | None = None
        self.status: dict | None = None
        self._wake = threading.Event()
        self._failures = 0
        self._logged_ok = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="usage").start()
        threading.Thread(target=self._run_status, daemon=True, name="status").start()

    def refresh_now(self) -> None:
        self._wake.set()

    def snapshot(self) -> dict:
        return {"usage": self.usage, "error": self.error, "status": self.status}

    def _run(self) -> None:
        interval = settings.usage_poll_minutes * 60
        # Reuse a recent saved result after a restart instead of spending a request.
        if self.usage and (fetched := parse_time(self.usage.get("fetched_at"))):
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if age < interval:
                self._wake.wait(interval - age)
                self._wake.clear()
        while True:
            delay = interval
            try:
                self.usage = fetch_usage()
                if self.error or not self._logged_ok:
                    print("Claude usage check working.", flush=True)
                    self._logged_ok = True
                self.error = None
                self._failures = 0
                kv_set("last_usage", self.usage)
            except UsageError as e:
                if str(e) != self.error:
                    print(f"Claude usage check failed: {e}", flush=True)
                self._logged_ok = False
                self.error = str(e)
                self._failures += 1
                retry = getattr(e, "retry_after", None)
                delay = min(3600, max(retry or 0, interval * 2 ** min(self._failures, 3)))
            except Exception as e:  # keep the thread alive no matter what
                self.error = f"Unexpected error: {e}"
                delay = interval
            wait_or_login_change(self._wake, delay, settings.claude_dir / ".credentials.json", bool(self.error))

    def _run_status(self) -> None:
        while True:
            try:
                self.status = fetch_status()
            except Exception:
                pass
            time.sleep(120)


poller = UsagePoller()
