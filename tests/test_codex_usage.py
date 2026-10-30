"""ChatGPT plan limits checked on a timer (codex_usage), and how they combine with the ones Codex logs."""
import base64
import io
import json
import time
import urllib.error

import pytest

from hangar import codex_usage
from hangar.config import settings


def jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"x.{body}.sig"


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "codex_home", tmp_path)

    def write(exp=None, account="acct-1"):
        token = jwt(exp if exp is not None else time.time() + 3600)
        (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                        "tokens": {"access_token": token, "account_id": account}}))
        return token
    return write


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PAYLOAD = {"plan_type": "plus", "rate_limit": {
    "primary_window": {"used_percent": 23, "limit_window_seconds": 18000, "reset_after_seconds": 100, "reset_at": 1795887106},
    "secondary_window": {"used_percent": 61, "limit_window_seconds": 604800, "reset_after_seconds": 9000, "reset_at": 1796000000}},
    "credits": {"has_credits": False, "unlimited": False, "balance": None}}


def test_fetch_maps_windows_and_sends_auth(signed_in, monkeypatch):
    token = signed_in()
    seen = {}

    def fake(req, timeout):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp(json.dumps(PAYLOAD).encode())
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", fake)
    u = codex_usage.fetch()
    assert seen["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert seen["headers"]["Authorization"] == f"Bearer {token}" and seen["headers"]["Chatgpt-account-id"] == "acct-1"
    assert u["plan"] == "plus" and u["source"] == "live"
    assert [(w["window_minutes"], w["used_percent"], w["resets_at"]) for w in u["windows"]] == [(300, 23.0, 1795887106), (10080, 61.0, 1796000000)]


def test_fetch_skips_expired_token(signed_in, monkeypatch):
    signed_in(exp=time.time() - 10)
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not call ChatGPT"))
    with pytest.raises(codex_usage.CodexUsageError, match="expired"):
        codex_usage.fetch()


def test_fetch_not_signed_in(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "codex_home", tmp_path)
    with pytest.raises(codex_usage.CodexUsageError, match="isn't signed in"):
        codex_usage.fetch()


@pytest.mark.parametrize("code,msg,retry", [(401, "didn't accept", None), (429, "rate-limiting", 120), (500, "HTTP 500", None)])
def test_fetch_http_errors(signed_in, monkeypatch, code, msg, retry):
    signed_in()

    def fake(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "x", {"Retry-After": "120"}, io.BytesIO(b""))
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", fake)
    with pytest.raises(codex_usage.CodexUsageError, match=msg) as e:
        codex_usage.fetch()
    assert getattr(e.value, "retry_after", None) == retry


def test_newest_prefers_recent():
    live = {"as_of": 200, "windows": [{"used_percent": 1}], "source": "live"}
    logs = {"as_of": 300, "windows": [{"used_percent": 2}]}
    assert codex_usage.newest(live, logs)["source"] == "logs"
    assert codex_usage.newest({**live, "as_of": 400}, logs)["source"] == "live"
    assert codex_usage.newest(None, None) is None
    assert codex_usage.newest({"as_of": 1, "windows": []}, None) is None


def test_overview_uses_poller(client, monkeypatch):
    from hangar import app as app_module
    monkeypatch.setattr(app_module.codex_poller, "usage", {"as_of": time.time(), "plan": "pro", "source": "live",
                                                           "windows": [{"key": "primary", "used_percent": 5, "window_minutes": 300, "resets_at": 1}]})
    monkeypatch.setattr(app_module.codex_poller, "error", None)
    o = client.get("/api/overview").json()
    assert o["codex_usage"]["plan"] == "pro" and o["codex_usage"]["source"] == "live"
