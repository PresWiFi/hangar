"""Linking Claude, ChatGPT (Codex), and Ollama. Stand-in CLIs replace the real ones."""
import base64
import hashlib
import io
import json
import tarfile
import textwrap

import pytest

from hangar import store
from hangar.config import settings
from hangar.providers import claude, codex, ollama


def script(path, body):
    path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


# ---------- Claude ----------

@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """`auth status` reports linked once `auth login` received the code GOOD-CODE."""
    flag = tmp_path / "linked"
    exe = script(tmp_path / "claude", f"""
        if [ "$1 $2" = "auth status" ]; then
          if [ -f {flag} ]; then echo '{{"loggedIn": true, "email": "me@example.com", "subscriptionType": "max", "authMethod": "claude.ai"}}'
          else echo '{{"loggedIn": false}}'; fi
        elif [ "$1 $2" = "auth login" ]; then
          echo "Opening browser to sign in…"
          echo "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"
          printf "Paste code here if prompted > "
          read -r code
          if [ "$code" = "GOOD-CODE" ]; then touch {flag}; echo "Login successful."; else echo "Invalid code"; exit 1; fi
        elif [ "$1 $2" = "auth logout" ]; then rm -f {flag}
        fi
    """)
    monkeypatch.setattr(settings, "claude_cmd", exe)
    claude._cache = None
    yield flag
    claude.cancel_login()
    claude._cache = None


def test_claude_status_unlinked(fake_claude):
    st = claude.status(fresh=True)
    assert st["linked"] is False and st["login"] is None


def test_claude_login_flow(fake_claude, client):
    r = client.post("/api/providers/claude/login")
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://claude.com/cai/oauth/authorize?")
    r = client.post("/api/providers/claude/code", json={"code": "GOOD-CODE"})
    assert r.status_code == 200, r.text
    assert r.json()["linked"] and r.json()["email"] == "me@example.com" and r.json()["plan"] == "max"
    assert claude.status()["login"] is None
    assert client.post("/api/providers/claude/logout").json()["linked"] is False


def test_claude_wrong_code(fake_claude, client):
    client.post("/api/providers/claude/login")
    r = client.post("/api/providers/claude/code", json={"code": "BAD"})
    assert r.status_code == 409
    assert "Invalid code" in r.json()["detail"]


def test_claude_code_without_flow(fake_claude, client):
    assert client.post("/api/providers/claude/code", json={"code": "GOOD-CODE"}).status_code == 409


def test_claude_code_rejects_control_chars(fake_claude, client):
    client.post("/api/providers/claude/login")
    assert client.post("/api/providers/claude/code", json={"code": "a\nb"}).status_code == 400


def test_claude_missing_cli(monkeypatch):
    monkeypatch.setattr(settings, "claude_cmd", "/nonexistent/claude")
    claude._cache = None
    assert "isn't installed" in claude.status(fresh=True)["error"]
    claude._cache = None


# ---------- Codex install ----------

def make_package(tmp_path, version=codex.VERSION, evil=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
        add("codex-package.json", json.dumps({"version": version, "entrypoint": "bin/codex"}).encode())
        add("bin/codex", b"#!/bin/sh\necho codex-cli\n", 0o755)
        if evil:
            add("../../escape.txt", b"nope")
    name = f"codex-package-{codex.arch()}-unknown-linux-musl.tar.gz"
    (tmp_path / name).write_bytes(buf.getvalue())
    return hashlib.sha256(buf.getvalue()).hexdigest()


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "codex_home", tmp_path / "codex-home")
    monkeypatch.setattr(codex, "DOWNLOAD_BASE", f"file://{tmp_path}")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)  # ignore any real codex on PATH
    (tmp_path / "data").mkdir()
    codex._install.clear()
    codex._install["state"] = "idle"
    yield tmp_path
    codex.cancel_login()


def wait_install():
    import time
    for _ in range(200):
        if codex.install_state()["state"] in ("done", "error"):
            return codex.install_state()
        time.sleep(0.05)
    raise AssertionError("install didn't finish")


def test_codex_install(codex_env, monkeypatch, client):
    monkeypatch.setitem(codex.SHA256, codex.arch(), make_package(codex_env))
    assert client.get("/api/providers/codex").json()["installed"] is False
    client.post("/api/providers/codex/install")
    assert wait_install()["state"] == "done"
    assert codex.binary() == str(codex.install_dir() / "bin" / "codex")
    assert client.get("/api/providers/codex").json()["installed"] is True
    assert not list(codex.install_dir().parent.glob(".*"))  # no leftovers


def test_codex_install_checksum_mismatch(codex_env, monkeypatch):
    make_package(codex_env)
    monkeypatch.setitem(codex.SHA256, codex.arch(), "0" * 64)
    codex.start_install()
    st = wait_install()
    assert st["state"] == "error" and "checksum" in st["error"]
    assert codex.binary() is None


def test_codex_install_blocks_path_escape(codex_env, monkeypatch):
    monkeypatch.setitem(codex.SHA256, codex.arch(), make_package(codex_env, evil=True))
    codex.start_install()
    assert wait_install()["state"] == "error"
    assert not (codex_env / "data" / "escape.txt").exists() and not (codex_env / "escape.txt").exists()
    assert codex.binary() is None


def test_codex_install_wrong_version(codex_env, monkeypatch):
    monkeypatch.setitem(codex.SHA256, codex.arch(), make_package(codex_env, version="0.0.1"))
    codex.start_install()
    assert wait_install()["state"] == "error"


# ---------- Codex sign-in ----------

def test_ensure_config_prepends_once(codex_env):
    settings.codex_home.mkdir()
    (settings.codex_home / "config.toml").write_text('model = "x"\n[profiles.a]\nmodel = "y"\n')
    codex.ensure_config()
    codex.ensure_config()
    text = (settings.codex_home / "config.toml").read_text()
    assert text.startswith('cli_auth_credentials_store = "file"\n') and text.count("cli_auth_credentials_store") == 1


def test_codex_status_reads_login(codex_env):
    settings.codex_home.mkdir()
    payload = {"email": "me@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
    jwt = "x." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".sig"
    (settings.codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": jwt}}))
    st = codex.status()
    assert st["linked"] and st["email"] == "me@example.com" and st["plan"] == "plus" and st["method"] == "chatgpt"


@pytest.fixture
def fake_codex(codex_env, monkeypatch):
    exe = script(codex_env / "codex", """
        if [ "$1 $2" = "login --device-auth" ]; then
          echo "Follow these steps to sign in with ChatGPT using device code authorization:"
          echo "1. Open this link in your browser and sign in to your account"
          echo "   https://auth.openai.com/codex/device"
          echo "2. Enter this one-time code (expires in 15 minutes)"
          echo "   AB3D-EF6H9"
          sleep 30
        elif [ "$1" = "login" ]; then
          echo "If your browser did not open, navigate to this URL to authenticate:"
          echo "https://auth.openai.com/oauth/authorize?response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback&state=s"
          sleep 30
        fi
    """)
    monkeypatch.setattr(codex, "binary", lambda: exe)
    return exe


def test_codex_device_login(fake_codex, client):
    r = client.post("/api/providers/codex/login", json={"mode": "device"})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["mode"] == "device" and st["url"] == "https://auth.openai.com/codex/device" and st["code"] == "AB3D-EF6H9"
    assert client.get("/api/providers/codex").json()["login"]["running"]
    client.post("/api/providers/codex/cancel")
    assert client.get("/api/providers/codex").json()["login"] is None


def test_codex_browser_login_link(fake_codex, client):
    st = client.post("/api/providers/codex/login", json={"mode": "browser"}).json()
    assert st["mode"] == "browser" and st["url"].startswith("https://auth.openai.com/oauth/authorize?")


@pytest.mark.parametrize("url", [
    "http://evil.example.com:1455/auth/callback?code=a&state=b",
    "http://localhost:8080/auth/callback?code=a&state=b",
    "http://localhost:1455/other?code=a&state=b",
    "http://localhost:1455/auth/callback?state=b",
    "https://localhost:1455/auth/callback?code=a&state=b",
    "not a url",
])
def test_codex_callback_only_localhost_1455(fake_codex, client, url):
    client.post("/api/providers/codex/login", json={"mode": "browser"})
    assert client.post("/api/providers/codex/callback", json={"url": url}).status_code == 400


def test_codex_login_needs_install(codex_env, client):
    assert client.post("/api/providers/codex/login", json={"mode": "device"}).status_code == 409


# ---------- Ollama ----------

@pytest.mark.parametrize("given,expected", [
    ("10.0.0.5", "http://10.0.0.5:11434"),
    ("http://box:11434/", "http://box:11434"),
    ("http://box:11434/v1", "http://box:11434"),
    ("https://ai.example.com/ollama/", "https://ai.example.com/ollama"),
    ("http://[::1]", "http://[::1]:11434"),
])
def test_ollama_normalize(given, expected):
    assert ollama.normalize(given) == expected


@pytest.mark.parametrize("bad", ["", "ftp://box", "http://"])
def test_ollama_normalize_rejects(bad):
    with pytest.raises(ValueError):
        ollama.normalize(bad)


@pytest.fixture
def no_servers():
    store.kv_set(ollama.KEY, [])
    yield
    store.kv_set(ollama.KEY, [])


def test_ollama_add_list_remove(no_servers, client, monkeypatch):
    monkeypatch.setattr(ollama, "probe", lambda url, timeout=4: {"ok": True, "version": "9.9", "models": [{"name": "m"}]})
    r = client.post("/api/providers/ollama", json={"name": "Desk", "url": "10.0.0.5"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert client.post("/api/providers/ollama", json={"url": "http://10.0.0.5:11434"}).status_code == 400
    servers = client.get("/api/providers/ollama").json()["servers"]
    assert servers == [{"id": sid, "name": "Desk", "url": "http://10.0.0.5:11434", "ok": True, "version": "9.9",
                        "models": [{"name": "m"}]}]
    assert client.delete(f"/api/providers/ollama/{sid}").status_code == 200
    assert client.delete(f"/api/providers/ollama/{sid}").status_code == 404
    assert client.get("/api/providers/ollama").json()["servers"] == []


def test_ollama_unreachable(no_servers):
    st = ollama.probe("http://127.0.0.1:9", timeout=1)
    assert st["ok"] is False and "Can't reach" in st["error"]


def test_providers_need_sign_in(anon):
    assert anon.get("/api/providers").status_code == 401
    assert anon.post("/api/providers/claude/login").status_code == 401


def test_onboarding_flag(client):
    store.kv_set("onboarding_pending", True)
    assert client.get("/api/me").json()["onboarding"] is True
    client.post("/api/onboarding/done")
    assert client.get("/api/me").json()["onboarding"] is False
