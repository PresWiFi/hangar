"""Sign-in. Hangar gives a shell to whoever signs in, so every API call and terminal needs a valid cookie.

There is one owner account (username + scrypt password hash) in hangar.db. A fresh install has no
owner: the setup page creates it, and needs a one-time code that Hangar prints to its log, so nobody
else on the network can claim the install first.

Older installs signed in with HANGAR_PASSWORD (or a generated password file). They are migrated to an
owner named "admin" with that password. Editing HANGAR_PASSWORD later still changes the password:
Hangar notices the new value on its next start.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

from . import store
from .config import settings

COOKIE = "hangar_session"
MAX_AGE = 30 * 86400
MIN_PASSWORD = 10
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
MIGRATED_USERNAME = "admin"

_attempts: dict[str, deque] = defaultdict(deque)
notices: list[str] = []          # printed once at startup (migrations and the like)
setup_code: str | None = None    # one-time code for the setup page; None once an owner exists


def _read_or_create(name: str, make) -> str:
    path = settings.data_dir / name
    if path.exists():
        return path.read_text().strip()
    value = make()
    path.write_text(value + "\n")
    os.chmod(path, 0o600)
    return value


_secret = _read_or_create("secret", lambda: secrets.token_hex(32)).encode()


# ---------- password hashing ----------

_N, _R, _P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # keeps a wrong username as slow as a wrong password


# ---------- the owner account ----------

def owner() -> dict | None:
    return store.kv_get("owner")


def setup_needed() -> bool:
    return owner() is None


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ValueError("Username: 1 to 64 letters, digits, or . _ @ -, starting with a letter or digit")
    return username


def check_new_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    return password


def _save_owner(o: dict) -> None:
    store.kv_set("owner", o)


def create_owner(username: str, password: str, source: str = "setup") -> dict:
    global setup_code
    o = {"username": username, "pw_hash": hash_password(password), "created": time.time(), "source": source,
         "epoch": secrets.token_hex(8)}
    _save_owner(o)
    setup_code = None
    return o


def set_password(password: str) -> None:
    """Also signs out every device, since the cookie key depends on the hash."""
    o = owner()
    o["pw_hash"] = hash_password(password)
    o["password_changed"] = time.time()
    _save_owner(o)


def set_username(username: str) -> None:
    o = owner()
    o["username"] = username
    _save_owner(o)


def sign_out_everywhere() -> None:
    o = owner()
    o["epoch"] = secrets.token_hex(8)
    _save_owner(o)


def check_login(username: str, password: str) -> bool:
    o = owner()
    if not o:
        return False
    name_ok = hmac.compare_digest((username or "").strip().lower().encode(), o["username"].lower().encode())
    pw_ok = verify_password(password or "", o["pw_hash"] if name_ok else _DUMMY_HASH)
    return name_ok and pw_ok


def username_hint() -> str | None:
    """Shown on the sign-in page only for migrated installs that still use the default name."""
    o = owner()
    if o and o.get("source") in ("env", "file") and o["username"] == MIGRATED_USERNAME:
        return MIGRATED_USERNAME
    return None


# ---------- setup code ----------

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I


def new_setup_code() -> str:
    global setup_code
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    setup_code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    return setup_code


def check_setup_code(given: str) -> bool:
    if not setup_code:
        return False
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", (s or "").upper())  # noqa: E731
    return hmac.compare_digest(norm(given).encode(), norm(setup_code).encode())


# ---------- cookies ----------

def _key() -> bytes | None:
    o = owner()
    if not o:
        return None
    # Changing the password or signing out everywhere changes the key, which signs every device out.
    return hmac.new(_secret, f"{o['pw_hash']}|{o.get('epoch', '')}".encode(), hashlib.sha256).digest()


def make_token() -> str:
    key = _key()
    if key is None:
        raise RuntimeError("No owner account yet")
    payload = f"{int(time.time()) + MAX_AGE}.{secrets.token_hex(8)}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_token(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    key = _key()
    if key is None:
        return False
    expires, nonce, sig = token.split(".")
    good = hmac.new(key, f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def allow_attempt(ip: str) -> bool:
    """At most 10 sign-in or setup attempts per 5 minutes from one address."""
    q, now = _attempts[ip], time.time()
    while q and q[0] < now - 300:
        q.popleft()
    if len(q) >= 10:
        return False
    q.append(now)
    return True


# ---------- migration from the password-only versions ----------

def _env_fingerprint(password: str) -> str:
    return hmac.new(_secret, b"env-password:" + password.encode(), hashlib.sha256).hexdigest()


def migrate() -> None:
    """Runs at import. Creates or updates the owner from HANGAR_PASSWORD or the old password file."""
    o = owner()
    legacy = settings.data_dir / "password"
    if settings.password:
        fp = _env_fingerprint(settings.password)
        if o is None:
            create_owner(MIGRATED_USERNAME, settings.password, source="env")
            notices.append(f'Created owner account "{MIGRATED_USERNAME}" with the password from HANGAR_PASSWORD.')
        elif store.kv_get("env_password_fp") not in (None, fp):
            set_password(settings.password)
            notices.append("HANGAR_PASSWORD changed, so the owner password was updated to match.")
        store.kv_set("env_password_fp", fp)
    elif o is None and legacy.exists():
        create_owner(MIGRATED_USERNAME, legacy.read_text().strip(), source="file")
        legacy.unlink()
        notices.append(f'Created owner account "{MIGRATED_USERNAME}" with the password from {legacy} '
                       "(file removed).")
    if setup_needed():
        new_setup_code()


def startup_lines() -> list[str]:
    lines = list(notices)
    notices.clear()
    if setup_code:
        bar = "=" * 60
        lines += [bar, "  Hangar needs setting up. Open it in a browser and enter this code:",
                  f"      {setup_code}", "  (A new code is printed each time Hangar starts until setup is done.)", bar]
    return lines


migrate()
