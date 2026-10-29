"""Owner account: setup, sign-in, account changes, migration from HANGAR_PASSWORD, reset-password."""
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from hangar import auth, store
from hangar.app import app
from hangar.config import settings

PW = "test-password"  # HANGAR_PASSWORD from conftest, migrated to owner "admin"


@pytest.fixture(autouse=True)
def keep_owner():
    """Each test may change the owner; put it back afterwards."""
    saved, fp, env = auth.owner(), store.kv_get("env_password_fp"), settings.password
    auth._attempts.clear()
    yield
    store.kv_set("owner", saved)
    store.kv_set("env_password_fp", fp)
    settings.password = env
    auth.setup_code = None
    auth._attempts.clear()


@pytest.fixture
def fresh():
    """A brand-new install: no owner, a setup code."""
    store.kv_set("owner", None)
    auth.new_setup_code()
    return TestClient(app, headers={"X-Hangar": "1"})


def signed_in(c: TestClient) -> bool:
    return c.get("/api/me").status_code == 200


# ---------- hashing ----------

def test_hash_roundtrip_and_salted():
    a, b = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert a != b and a.startswith("scrypt$")
    assert auth.verify_password("correct horse", a)
    assert not auth.verify_password("wrong horse", a)
    assert not auth.verify_password("x", "garbage")


# ---------- setup ----------

def test_fresh_install_needs_setup(fresh):
    assert fresh.get("/api/auth/state").json() == {"setup": True, "username_hint": None, "password": True, "oidc": None}
    assert fresh.get("/api/sessions").status_code == 401
    assert fresh.post("/api/login", json={"username": "admin", "password": PW}).status_code == 409


def test_setup_creates_owner_and_signs_in(fresh):
    r = fresh.post("/api/setup", json={"code": auth.setup_code.lower().replace("-", " "), "username": "blake",
                                       "password": "a-long-password"})
    assert r.status_code == 200, r.text
    assert signed_in(fresh)
    assert fresh.get("/api/me").json()["user"] == "blake"
    assert auth.setup_code is None
    assert fresh.get("/api/auth/state").json()["setup"] is False


def test_setup_rejects_wrong_code(fresh):
    r = fresh.post("/api/setup", json={"code": "AAAA-BBBB-CCCC", "username": "blake", "password": "a-long-password"})
    assert r.status_code == 401
    assert auth.setup_needed()


@pytest.mark.parametrize("username,password", [("blake", "short"), ("", "a-long-password"),
                                               ("bad name", "a-long-password"), ("-x", "a-long-password")])
def test_setup_validates(fresh, username, password):
    r = fresh.post("/api/setup", json={"code": auth.setup_code, "username": username, "password": password})
    assert r.status_code == 400
    assert auth.setup_needed()


def test_setup_only_once(fresh):
    code = auth.setup_code
    assert fresh.post("/api/setup", json={"code": code, "username": "one", "password": "a-long-password"}).status_code == 200
    other = TestClient(app, headers={"X-Hangar": "1"})
    r = other.post("/api/setup", json={"code": code, "username": "two", "password": "a-long-password"})
    assert r.status_code == 409
    assert auth.owner()["username"] == "one"


def test_setup_rate_limited(fresh):
    for _ in range(10):
        fresh.post("/api/setup", json={"code": "nope", "username": "x", "password": "a-long-password"})
    r = fresh.post("/api/setup", json={"code": auth.setup_code, "username": "x", "password": "a-long-password"})
    assert r.status_code == 429


def test_startup_lines_show_code(fresh):
    lines = "\n".join(auth.startup_lines())
    assert auth.setup_code in lines


# ---------- sign-in ----------

def test_login_with_username(anon):
    assert anon.get("/api/auth/state").json() == {"setup": False, "username_hint": "admin", "password": True, "oidc": None}
    r = anon.post("/api/login", json={"username": "ADMIN", "password": PW})
    assert r.status_code == 200
    assert signed_in(anon)


@pytest.mark.parametrize("username,password", [("admin", "wrong"), ("someone", PW), ("", "")])
def test_login_rejects(anon, username, password):
    assert anon.post("/api/login", json={"username": username, "password": password}).status_code == 401
    assert not signed_in(anon)


def test_old_password_only_login_is_rejected(anon):
    assert anon.post("/api/login", json={"password": PW}).status_code == 422


# ---------- account ----------

def test_change_password_signs_out_other_devices(client):
    other = TestClient(app, headers={"X-Hangar": "1"})
    other.cookies.set(auth.COOKIE, auth.make_token())
    assert signed_in(other)
    r = client.post("/api/account/password", json={"current": PW, "new": "brand-new-password"})
    assert r.status_code == 200
    assert signed_in(client)       # got a fresh cookie
    assert not signed_in(other)
    assert auth.check_login("admin", "brand-new-password")
    assert not auth.check_login("admin", PW)


def test_change_password_checks_current_and_length(client):
    assert client.post("/api/account/password", json={"current": "wrong", "new": "brand-new-password"}).status_code == 401
    assert client.post("/api/account/password", json={"current": PW, "new": "short"}).status_code == 400
    assert auth.check_login("admin", PW)


def test_change_username(client, anon):
    assert client.post("/api/account/username", json={"username": "blake", "password": "wrong"}).status_code == 401
    r = client.post("/api/account/username", json={"username": "blake", "password": PW})
    assert r.json()["username"] == "blake"
    assert signed_in(client)
    assert anon.get("/api/auth/state").json()["username_hint"] is None
    assert auth.check_login("blake", PW)


def test_sign_out_everywhere(client):
    other = TestClient(app, headers={"X-Hangar": "1"})
    other.cookies.set(auth.COOKIE, auth.make_token())
    assert client.post("/api/account/signout-all").status_code == 200
    assert signed_in(client)
    assert not signed_in(other)


def test_account_needs_sign_in_and_header(anon, client):
    assert anon.get("/api/account").status_code == 401
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.make_token())
    assert c.post("/api/account/signout-all").status_code == 403
    assert client.get("/api/account").json()["username"] == "admin"


# ---------- migration ----------

def test_migrate_from_env_password():
    store.kv_set("owner", None)
    store.kv_set("env_password_fp", None)
    settings.password = "from-the-env"
    auth.migrate()
    o = auth.owner()
    assert o["username"] == "admin" and o["source"] == "env"
    assert auth.check_login("admin", "from-the-env")
    assert auth.setup_code is None


def test_editing_env_password_updates_owner():
    settings.password = "edited-in-env-file"
    auth.migrate()
    assert auth.check_login("admin", "edited-in-env-file")
    assert any("changed" in n for n in auth.notices)
    auth.notices.clear()


def test_unchanged_env_password_keeps_ui_password(client):
    client.post("/api/account/password", json={"current": PW, "new": "changed-in-the-ui"})
    auth.migrate()  # restart with the same HANGAR_PASSWORD
    assert auth.check_login("admin", "changed-in-the-ui")


def test_migrate_from_legacy_password_file():
    store.kv_set("owner", None)
    settings.password = ""
    legacy = settings.data_dir / "password"
    legacy.write_text("generated-long-ago\n")
    auth.migrate()
    assert auth.check_login("admin", "generated-long-ago")
    assert auth.owner()["source"] == "file"
    assert not legacy.exists()


def test_no_password_anywhere_means_setup():
    store.kv_set("owner", None)
    settings.password = ""
    auth.migrate()
    assert auth.setup_needed() and auth.setup_code


# ---------- reset-password command ----------

def test_reset_password_command():
    env = {"HANGAR_DATA_DIR": str(settings.data_dir), "HANGAR_PASSWORD": settings.password,
           "HANGAR_WORKSPACES": settings.workspaces[0], "CLAUDE_CONFIG_DIR": str(settings.claude_dir),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-m", "hangar", "reset-password", "--username", "owner2"],
                         capture_output=True, text=True, env=env, check=True).stdout
    password = out.split("Password: ")[1].split()[0]
    assert auth.check_login("owner2", password)
