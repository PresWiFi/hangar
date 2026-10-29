"""Sign in with an OpenID Connect provider: Authentik, Microsoft Entra ID, Google, Keycloak, Duo, or any other.

Hangar has one owner, so OIDC doesn't create accounts. The owner links one identity at the provider
(issuer + subject) from Settings, and after that only that identity can sign in this way.

Flow: authorization code with PKCE. `state` is kept server-side for 10 minutes and also bound to a cookie
in the browser that started the flow, so a callback can't be replayed into someone else's browser. The ID
token is checked for signature (provider's JWKS, or the client secret for HS256), issuer, audience,
expiry, and nonce.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt

from . import store

KEY = "oidc"
STATE_COOKIE = "hangar_oidc"
STATE_TTL = 600
ASYMMETRIC = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]

# Shown in Settings. `issuer` is a template the person fills in.
PRESETS = {
    "authentik": {"label": "Authentik", "issuer": "https://auth.example.com/application/o/hangar/",
                  "help": "In Authentik, create an OAuth2/OpenID provider and an application. Use its "
                          "\"OpenID Configuration Issuer\" as the issuer."},
    "entra": {"label": "Microsoft", "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
              "help": "In Entra ID, register an app (Web platform). Use your tenant ID in the issuer, not "
                      "\"common\". Create a client secret under Certificates & secrets."},
    "google": {"label": "Google", "issuer": "https://accounts.google.com",
               "help": "In Google Cloud, create an OAuth client ID of type \"Web application\"."},
    "keycloak": {"label": "Keycloak", "issuer": "https://keycloak.example.com/realms/<realm>",
                 "help": "In Keycloak, create an OpenID Connect client with client authentication on."},
    "duo": {"label": "Duo", "issuer": "https://<sso-subdomain>.sso.duosecurity.com/oidc/<client-id>",
            "help": "In Duo, add a \"Generic OIDC Relying Party\" application and copy its issuer."},
    "generic": {"label": "SSO", "issuer": "https://idp.example.com",
                "help": "Any OpenID Connect provider. Hangar reads <issuer>/.well-known/openid-configuration."},
}

_pending: dict[str, dict] = {}
_lock = threading.Lock()
_discovery: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, tuple[float, dict]] = {}


class OIDCError(Exception):
    pass


# ---------- config ----------

def config() -> dict:
    return store.kv_get(KEY) or {}


def _save(cfg: dict) -> None:
    store.kv_set(KEY, cfg)


def public_config() -> dict:
    c = config()
    return {"configured": bool(c.get("issuer") and c.get("client_id")), "preset": c.get("preset") or "generic",
            "issuer": c.get("issuer") or "", "client_id": c.get("client_id") or "",
            "has_secret": bool(c.get("client_secret")), "label": label(), "redirect_uri": c.get("redirect_uri"),
            "linked": c.get("linked"), "password_login": password_login_enabled(), "presets": PRESETS}


def label() -> str:
    c = config()
    return c.get("label") or PRESETS.get(c.get("preset") or "generic", PRESETS["generic"])["label"]


def ready() -> bool:
    """Configured and linked, so the sign-in page can offer it."""
    c = config()
    return bool(c.get("issuer") and c.get("client_id") and c.get("linked"))


def password_login_enabled() -> bool:
    return not (config().get("password_login") is False and ready())


def set_password_login(enabled: bool) -> None:
    c = config()
    if not enabled and not ready():
        raise OIDCError("Link a sign-in provider first, so you can still get in.")
    c["password_login"] = enabled
    _save(c)


def save(preset: str, issuer: str, client_id: str, client_secret: str | None, redirect_uri: str,
         button_label: str = "") -> dict:
    preset = preset if preset in PRESETS else "generic"
    issuer = (issuer or "").strip()
    client_id = (client_id or "").strip()
    if not issuer.startswith("https://") and not issuer.startswith("http://localhost") \
            and not issuer.startswith("http://127.0.0.1"):
        raise OIDCError("The issuer must be an https:// address.")
    if "<" in issuer or ">" in issuer:
        raise OIDCError("Fill in the parts of the issuer shown in <angle brackets>.")
    if not client_id:
        raise OIDCError("Enter the client ID.")
    u = urllib.parse.urlsplit(redirect_uri or "")
    if u.scheme not in ("http", "https") or not u.netloc or u.path != "/api/auth/oidc/callback":
        raise OIDCError("Bad redirect address.")
    disc = discover(issuer, fresh=True)  # fails loudly if the issuer is wrong
    old = config()
    same_provider = old.get("issuer") == issuer and old.get("client_id") == client_id
    cfg = {"preset": preset, "issuer": issuer, "client_id": client_id,
           "client_secret": client_secret if client_secret else (old.get("client_secret") if same_provider else ""),
           "redirect_uri": redirect_uri, "label": (button_label or "").strip()[:40],
           # A different provider or client means a different identity; link again.
           "linked": old.get("linked") if same_provider else None,
           "password_login": old.get("password_login", True) if same_provider else True}
    _save(cfg)
    return {"issuer": disc.get("issuer")}


def unlink() -> None:
    c = config()
    c["linked"] = None
    c["password_login"] = True
    _save(c)


def remove() -> None:
    store.kv_set(KEY, None)


# ---------- HTTP ----------

def _fetch_json(url: str, timeout: float = 10) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Hangar"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(1_000_000))
    except urllib.error.HTTPError as e:
        raise OIDCError(f"{url} answered HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OIDCError(f"Couldn't read {url}: {getattr(e, 'reason', e)}")


def _post_form(url: str, data: dict, auth: tuple[str, str] | None, timeout: float = 15) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Hangar"}
    if auth:
        pair = f"{urllib.parse.quote(auth[0], safe='')}:{urllib.parse.quote(auth[1], safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(1_000_000))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(10_000))
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            detail = ""
        raise OIDCError(f"The provider refused the sign-in (HTTP {e.code}{': ' + detail if detail else ''}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OIDCError(f"Couldn't reach the provider's token endpoint: {getattr(e, 'reason', e)}")


def discover(issuer: str, fresh: bool = False) -> dict:
    hit = _discovery.get(issuer)
    if hit and not fresh and time.time() - hit[0] < 3600:
        return hit[1]
    d = _fetch_json(issuer.rstrip("/") + "/.well-known/openid-configuration")
    for key in ("issuer", "authorization_endpoint", "token_endpoint"):
        if not d.get(key):
            raise OIDCError(f"The provider's configuration has no {key}.")
    if d["issuer"].rstrip("/") != issuer.rstrip("/"):
        raise OIDCError(f"The provider says its issuer is {d['issuer']}. Use exactly that.")
    _discovery[issuer] = (time.time(), d)
    return d


def _jwk_set(uri: str, fresh: bool = False) -> dict:
    hit = _jwks.get(uri)
    if hit and not fresh and time.time() - hit[0] < 3600:
        return hit[1]
    keys = _fetch_json(uri)
    _jwks[uri] = (time.time(), keys)
    return keys


# ---------- the flow ----------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def start(mode: str) -> tuple[str, str]:
    """Returns (authorization URL, state). mode is "login" or "link"."""
    c = config()
    if not (c.get("issuer") and c.get("client_id") and c.get("redirect_uri")):
        raise OIDCError("Sign-in with a provider isn't set up.")
    if mode == "login" and not c.get("linked"):
        raise OIDCError("No identity is linked yet.")
    d = discover(c["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    with _lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if v["at"] < now - STATE_TTL]:
            del _pending[k]
        if len(_pending) > 200:
            raise OIDCError("Too many sign-ins in progress. Try again in a few minutes.")
        _pending[state] = {"at": now, "nonce": nonce, "verifier": verifier, "mode": mode}
    params = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
              "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    sep = "&" if "?" in d["authorization_endpoint"] else "?"
    return d["authorization_endpoint"] + sep + urllib.parse.urlencode(params), state


def finish(state: str, cookie_state: str | None, code: str) -> tuple[str, dict]:
    """Checks the callback and returns (mode, identity). Raises OIDCError with a reason to show."""
    with _lock:
        pending = _pending.pop(state or "", None)
    if not pending or time.time() - pending["at"] > STATE_TTL:
        raise OIDCError("That sign-in expired or was already used. Try again.")
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        raise OIDCError("This sign-in was started in a different browser. Try again from here.")
    if not code:
        raise OIDCError("The provider didn't send a sign-in code.")
    c = config()
    d = discover(c["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": c["redirect_uri"],
            "code_verifier": pending["verifier"], "client_id": c["client_id"]}
    methods = d.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    auth = None
    if c.get("client_secret"):
        if "client_secret_basic" in methods or "client_secret_post" not in methods:
            auth = (c["client_id"], c["client_secret"])
        else:
            data["client_secret"] = c["client_secret"]
    tokens = _post_form(d["token_endpoint"], data, auth)
    raw = tokens.get("id_token")
    if not raw:
        raise OIDCError("The provider didn't return an ID token.")
    claims = _verify(raw, c, d, pending["nonce"])
    ident = {"iss": claims["iss"], "sub": claims["sub"],
             "email": claims.get("email"), "name": claims.get("name") or claims.get("preferred_username")}
    return pending["mode"], ident


def _verify(raw: str, c: dict, d: dict, nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(raw)
    except jwt.PyJWTError as e:
        raise OIDCError(f"The ID token is malformed: {e}")
    alg = header.get("alg")
    if alg == "HS256":
        if not c.get("client_secret"):
            raise OIDCError("The provider signs with the client secret, but none is set.")
        key = c["client_secret"]
    elif alg in ASYMMETRIC:
        if not d.get("jwks_uri"):
            raise OIDCError("The provider publishes no signing keys (jwks_uri).")
        key = _signing_key(d["jwks_uri"], header.get("kid"), alg)
    else:
        raise OIDCError(f"Unsupported ID token algorithm: {alg}")
    try:
        claims = jwt.decode(raw, key=key, algorithms=[alg], audience=c["client_id"], issuer=d["issuer"],
                            options={"require": ["iss", "sub", "aud", "exp", "iat"]}, leeway=60)
    except jwt.PyJWTError as e:
        raise OIDCError(f"The ID token didn't check out: {e}")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OIDCError("The ID token is for a different sign-in (nonce mismatch).")
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != c["client_id"]:
        raise OIDCError("The ID token was issued to a different client.")
    return claims


def _signing_key(uri: str, kid: str | None, alg: str):
    for fresh in (False, True):  # refetch once in case the provider rotated its keys
        for k in _jwk_set(uri, fresh).get("keys", []):
            if kid and k.get("kid") != kid:
                continue
            if k.get("use", "sig") != "sig":
                continue
            try:
                return jwt.PyJWK(k, algorithm=alg).key
            except jwt.PyJWTError:
                continue
    raise OIDCError("No matching signing key at the provider.")


def link(ident: dict) -> None:
    c = config()
    c["linked"] = {"iss": ident["iss"], "sub": ident["sub"], "email": ident.get("email"), "name": ident.get("name"),
                   "at": time.time()}
    _save(c)


def matches_linked(ident: dict) -> bool:
    linked = config().get("linked") or {}
    return bool(linked) and secrets.compare_digest(linked.get("iss", ""), ident["iss"]) \
        and secrets.compare_digest(linked.get("sub", ""), ident["sub"])
