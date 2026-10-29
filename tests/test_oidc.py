"""Sign in with an OIDC provider. A mock provider signs real ID tokens with an RSA key."""
import base64
import hashlib
import json
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from hangar import auth, oidc, store
from hangar.app import app

ISS = "https://idp.example.com"
CLIENT = "hangar-client"
REDIRECT = "http://testserver/api/auth/oidc/callback"


class Provider:
    """Answers discovery, JWKS, and token requests the way an OIDC provider would."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.codes = {}            # code -> (nonce, challenge, claims overrides)
        self.sub = "user-123"
        self.token_requests = []
        self.overrides = {}
        self.header_overrides = {}

    def discovery(self):
        return {"issuer": ISS, "authorization_endpoint": f"{ISS}/authorize", "token_endpoint": f"{ISS}/token",
                "jwks_uri": f"{ISS}/jwks", "token_endpoint_auth_methods_supported": ["client_secret_basic"]}

    def fetch(self, url, timeout=10):
        if url == f"{ISS}/.well-known/openid-configuration":
            return self.discovery()
        if url == f"{ISS}/jwks":
            return self.jwks
        raise oidc.OIDCError(f"unexpected {url}")

    def authorize(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["client_id"] == CLIENT and q["redirect_uri"] == REDIRECT and q["code_challenge_method"] == "S256"
        assert "openid" in q["scope"].split()
        code = f"code-{len(self.codes)}"
        self.codes[code] = (q["nonce"], q["code_challenge"])
        return q["state"], code

    def post(self, url, data, auth_pair, timeout=15):
        self.token_requests.append((data, auth_pair))
        nonce, challenge = self.codes.pop(data["code"])
        verifier_hash = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        if verifier_hash != challenge:
            raise oidc.OIDCError("PKCE failed")
        now = int(time.time())
        claims = {"iss": ISS, "sub": self.sub, "aud": CLIENT, "iat": now, "exp": now + 300, "nonce": nonce,
                  "email": "owner@example.com", **self.overrides}
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1", **self.header_overrides})
        return {"id_token": token, "access_token": "at", "token_type": "Bearer"}


@pytest.fixture
def idp(monkeypatch):
    p = Provider()
    monkeypatch.setattr(oidc, "_fetch_json", p.fetch)
    monkeypatch.setattr(oidc, "_post_form", p.post)
    oidc._discovery.clear()
    oidc._jwks.clear()
    oidc._pending.clear()
    auth._attempts.clear()
    store.kv_set(oidc.KEY, None)
    yield p
    store.kv_set(oidc.KEY, None)


def configure(client):
    r = client.put("/api/oidc", json={"preset": "authentik", "issuer": ISS, "client_id": CLIENT,
                                      "client_secret": "s3cret", "redirect_uri": REDIRECT})
    assert r.status_code == 200, r.text
    return r.json()


def run_flow(c: TestClient, idp: Provider, start_path: str):
    r = c.get(start_path, follow_redirects=False)
    assert r.status_code == 303, r.text
    state, code = idp.authorize(r.headers["location"])
    return c.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)


def link(client, idp):
    configure(client)
    r = run_flow(client, idp, "/api/oidc/link")
    assert r.headers["location"] == "/?oidc=linked#/settings", r.headers["location"]


def query(r):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.headers["location"]).query))


# ---------- setup ----------

def test_configure_checks_issuer(client, idp, monkeypatch):
    for bad in ("http://idp.example.com", "https://<tenant>.example.com"):
        r = client.put("/api/oidc", json={"issuer": bad, "client_id": CLIENT, "redirect_uri": REDIRECT})
        assert r.status_code == 400
    monkeypatch.setattr(idp, "discovery", lambda: {**Provider.discovery(idp), "issuer": "https://other"})
    r = client.put("/api/oidc", json={"issuer": ISS, "client_id": CLIENT, "redirect_uri": REDIRECT})
    assert r.status_code == 400 and "https://other" in r.json()["detail"]


def test_configure_hides_secret(client, idp):
    cfg = configure(client)
    assert cfg["has_secret"] and "client_secret" not in cfg and cfg["label"] == "Authentik"
    assert cfg["linked"] is None and cfg["configured"]
    # saving again without a secret keeps it
    client.put("/api/oidc", json={"preset": "authentik", "issuer": ISS, "client_id": CLIENT, "redirect_uri": REDIRECT})
    assert oidc.config()["client_secret"] == "s3cret"


def test_oidc_settings_need_owner(anon, idp):
    assert anon.get("/api/oidc").status_code == 401
    assert anon.get("/api/oidc/link", follow_redirects=False).status_code == 401
    assert anon.put("/api/oidc", json={"issuer": ISS, "client_id": CLIENT, "redirect_uri": REDIRECT}).status_code == 401


# ---------- linking ----------

def test_link_records_identity(client, idp):
    link(client, idp)
    linked = oidc.config()["linked"]
    assert linked["sub"] == "user-123" and linked["iss"] == ISS and linked["email"] == "owner@example.com"
    data, basic = idp.token_requests[-1]
    assert basic == (CLIENT, "s3cret") and "client_secret" not in data  # client_secret_basic


def test_login_offered_only_when_linked(anon, client, idp):
    configure(client)
    assert anon.get("/api/auth/state").json()["oidc"] is None
    r = anon.get("/api/auth/oidc/start", follow_redirects=False)
    assert "oidc_error" in query(r)
    run_flow(client, idp, "/api/oidc/link")
    assert anon.get("/api/auth/state").json()["oidc"] == {"label": "Authentik"}


# ---------- signing in ----------

def test_login_with_linked_identity(client, idp):
    link(client, idp)
    browser = TestClient(app)
    r = run_flow(browser, idp, "/api/auth/oidc/start")
    assert r.headers["location"] == "/"
    assert browser.get("/api/me").status_code == 200


def test_login_rejects_other_identity(client, idp):
    link(client, idp)
    idp.sub = "someone-else"
    browser = TestClient(app)
    r = run_flow(browser, idp, "/api/auth/oidc/start")
    assert "isn't the identity linked" in query(r)["oidc_error"]
    assert browser.get("/api/me").status_code == 401


@pytest.mark.parametrize("override,reason", [
    ({"aud": "other-client"}, "didn't check out"),
    ({"iss": "https://evil.example.com"}, "didn't check out"),
    ({"exp": int(time.time()) - 3600}, "didn't check out"),
    ({"nonce": "wrong"}, "nonce"),
])
def test_login_rejects_bad_tokens(client, idp, override, reason):
    link(client, idp)
    idp.overrides = override
    browser = TestClient(app)
    r = run_flow(browser, idp, "/api/auth/oidc/start")
    assert reason in query(r)["oidc_error"]
    assert browser.get("/api/me").status_code == 401


def test_login_rejects_wrong_signing_key(client, idp):
    link(client, idp)
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # JWKS still has the old key
    browser = TestClient(app)
    r = run_flow(browser, idp, "/api/auth/oidc/start")
    assert "didn't check out" in query(r)["oidc_error"]


def test_login_rejects_alg_none(client, idp, monkeypatch):
    link(client, idp)
    browser = TestClient(app)
    r = browser.get("/api/auth/oidc/start", follow_redirects=False)
    state, code = idp.authorize(r.headers["location"])
    nonce = idp.codes[code][0]
    unsigned = jwt.encode({"iss": ISS, "sub": "user-123", "aud": CLIENT, "iat": int(time.time()),
                           "exp": int(time.time()) + 300, "nonce": nonce}, key=None, algorithm="none")
    monkeypatch.setattr(oidc, "_post_form", lambda *a, **k: {"id_token": unsigned})
    r = browser.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    assert "Unsupported" in query(r)["oidc_error"]
    assert browser.get("/api/me").status_code == 401


def test_callback_needs_same_browser(client, idp):
    link(client, idp)
    starter, victim = TestClient(app), TestClient(app)
    r = starter.get("/api/auth/oidc/start", follow_redirects=False)
    state, code = idp.authorize(r.headers["location"])
    r = victim.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    assert "different browser" in query(r)["oidc_error"]
    assert victim.get("/api/me").status_code == 401


def test_state_is_single_use(client, idp):
    link(client, idp)
    browser = TestClient(app)
    r = browser.get("/api/auth/oidc/start", follow_redirects=False)
    state, code = idp.authorize(r.headers["location"])
    browser.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    browser.cookies.clear()
    browser.cookies.set(oidc.STATE_COOKIE, state, path="/api/auth/oidc")
    r = browser.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    assert "expired or was already used" in query(r)["oidc_error"]


def test_provider_error_is_shown(client, idp):
    link(client, idp)
    r = TestClient(app).get("/api/auth/oidc/callback?state=x&error=access_denied&error_description=User+said+no",
                            follow_redirects=False)
    assert query(r)["oidc_error"] == "The provider said: User said no"


def test_link_callback_needs_signed_in_owner(client, idp):
    configure(client)
    r = client.get("/api/oidc/link", follow_redirects=False)
    state, code = idp.authorize(r.headers["location"])
    client.cookies.delete(auth.COOKIE)  # signed out before coming back
    r = client.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    assert "Sign in to Hangar first" in query(r)["oidc_error"]
    assert oidc.config()["linked"] is None


def test_hs256_tokens_use_client_secret(client, idp, monkeypatch):
    link(client, idp)
    browser = TestClient(app)
    r = browser.get("/api/auth/oidc/start", follow_redirects=False)
    state, code = idp.authorize(r.headers["location"])
    nonce = idp.codes[code][0]
    now = int(time.time())
    tok = jwt.encode({"iss": ISS, "sub": "user-123", "aud": CLIENT, "iat": now, "exp": now + 300, "nonce": nonce},
                     "s3cret", algorithm="HS256")
    monkeypatch.setattr(oidc, "_post_form", lambda *a, **k: {"id_token": tok})
    r = browser.get(f"/api/auth/oidc/callback?state={state}&code={code}", follow_redirects=False)
    assert r.headers["location"] == "/" and browser.get("/api/me").status_code == 200


# ---------- password sign-in switch ----------

def test_turn_off_password_login(client, anon, idp):
    configure(client)
    assert client.post("/api/oidc/password-login", json={"enabled": False}).status_code == 400  # not linked yet
    run_flow(client, idp, "/api/oidc/link")
    assert client.post("/api/oidc/password-login", json={"enabled": False}).json()["password_login"] is False
    st = anon.get("/api/auth/state").json()
    assert st["password"] is False and st["oidc"] == {"label": "Authentik"}
    r = anon.post("/api/login", json={"username": "admin", "password": "test-password"})
    assert r.status_code == 403 and "Sign in with Authentik" in r.json()["detail"]


def test_unlink_turns_password_back_on(client, idp):
    link(client, idp)
    client.post("/api/oidc/password-login", json={"enabled": False})
    cfg = client.post("/api/oidc/unlink").json()
    assert cfg["linked"] is None and cfg["password_login"] is True


def test_changing_provider_drops_link(client, idp):
    link(client, idp)
    client.put("/api/oidc", json={"issuer": ISS, "client_id": "another-client", "redirect_uri": REDIRECT})
    assert oidc.config()["linked"] is None and oidc.config()["client_secret"] == ""


def test_reset_password_reenables_password_login(client, idp):
    link(client, idp)
    client.post("/api/oidc/password-login", json={"enabled": False})
    from hangar.__main__ import reset_password
    owner = auth.owner()
    try:
        reset_password(None)
        assert oidc.password_login_enabled()
    finally:
        store.kv_set("owner", owner)


# ---------- redirect address ----------

def test_suggested_redirect_uses_forwarded_host(client, idp):
    r = client.get("/api/oidc", headers={"X-Forwarded-Host": "hangar.tail1234.ts.net", "X-Forwarded-Proto": "https"})
    assert r.json()["suggested_redirect_uri"] == "https://hangar.tail1234.ts.net/api/auth/oidc/callback"


def test_public_url_wins(client, idp, monkeypatch):
    from hangar.config import settings
    monkeypatch.setattr(settings, "public_url", "https://hangar.example.com")
    j = client.get("/api/oidc", headers={"X-Forwarded-Host": "other"}).json()
    assert j["suggested_redirect_uri"] == "https://hangar.example.com/api/auth/oidc/callback" and j["public_url"]


@pytest.mark.parametrize("given,saved", [
    ("http://10.10.0.5:7878/api/auth/oidc/callback", "http://10.10.0.5:7878/api/auth/oidc/callback"),
    ("https://hangar.example.com", "https://hangar.example.com/api/auth/oidc/callback"),
    ("hangar.lan:7878", "http://hangar.lan:7878/api/auth/oidc/callback"),
])
def test_redirect_uri_can_be_entered(client, idp, given, saved):
    r = client.put("/api/oidc", json={"issuer": ISS, "client_id": CLIENT, "redirect_uri": given})
    assert r.status_code == 200 and oidc.config()["redirect_uri"] == saved


@pytest.mark.parametrize("bad", ["ftp://x/api/auth/oidc/callback", "https://x/elsewhere", "https://x/api/auth/oidc/callback?a=1"])
def test_redirect_uri_rejects_bad(client, idp, bad):
    assert client.put("/api/oidc", json={"issuer": ISS, "client_id": CLIENT, "redirect_uri": bad}).status_code == 400


def test_start_moves_to_registered_address_once(client, idp):
    link(client, idp)
    client.put("/api/oidc", json={"preset": "authentik", "issuer": ISS, "client_id": CLIENT,
                                  "redirect_uri": "https://hangar.example.com/api/auth/oidc/callback"})
    browser = TestClient(app)  # arrives as http://testserver
    r = browser.get("/api/auth/oidc/start", follow_redirects=False)
    assert r.headers["location"] == "https://hangar.example.com/api/auth/oidc/start?here=1"
    r = browser.get("/api/auth/oidc/start?here=1", follow_redirects=False)  # proxy hid the address: no loop
    assert r.headers["location"].startswith(f"{ISS}/authorize?")
