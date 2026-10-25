"""Login. Hangar gives a shell to whoever signs in, so every API call and terminal needs a valid cookie."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from .config import settings

COOKIE = "hangar_session"
MAX_AGE = 30 * 86400
_attempts: dict[str, deque] = defaultdict(deque)


def _read_or_create(name: str, make) -> str:
    path = settings.data_dir / name
    if path.exists():
        return path.read_text().strip()
    value = make()
    path.write_text(value + "\n")
    os.chmod(path, 0o600)
    return value


def password() -> str:
    if settings.password:
        return settings.password
    return _read_or_create("password", lambda: secrets.token_urlsafe(12))


def password_source() -> str:
    return "HANGAR_PASSWORD" if settings.password else str(settings.data_dir / "password")


_secret = _read_or_create("secret", lambda: secrets.token_hex(32)).encode()


def _key() -> bytes:
    # Changing the password signs everyone out.
    return hmac.new(_secret, password().encode(), hashlib.sha256).digest()


def make_token() -> str:
    payload = f"{int(time.time()) + MAX_AGE}.{secrets.token_hex(8)}"
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_token(token: str | None) -> bool:
    if not token or token.count(".") != 2:
        return False
    expires, nonce, sig = token.split(".")
    good = hmac.new(_key(), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def allow_attempt(ip: str) -> bool:
    """At most 10 login attempts per 5 minutes from one address."""
    q, now = _attempts[ip], time.time()
    while q and q[0] < now - 300:
        q.popleft()
    if len(q) >= 10:
        return False
    q.append(now)
    return True


def check_password(given: str) -> bool:
    return hmac.compare_digest(given.encode(), password().encode())
