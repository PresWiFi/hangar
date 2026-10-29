"""Bearer token for the Ideas MCP server. It works only for /api/ideas/*; the web UI keeps using the cookie."""
from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Request

from ..config import settings

FILE = "ideas-token"
_cached: str | None = None


def path():
    return settings.data_dir / FILE


def get() -> str:
    """The token, created on first use in the data dir (readable only by its owner)."""
    global _cached
    if _cached is None:
        p = path()
        if p.exists():
            _cached = p.read_text().strip()
        if not _cached:
            _cached = secrets.token_urlsafe(32)
            # Create with 0600 from the start, so the token is never readable by others, even briefly.
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(_cached + "\n")
            os.chmod(p, 0o600)
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None


def from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def authorize(request: Request) -> bool:
    given = from_request(request)
    return bool(given) and hmac.compare_digest(given.encode(), get().encode())
