"""Test setup. Hangar reads its settings from env vars at import time, so set them before importing it."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hangar-test-"))
WORKSPACE = _TMP / "workspace"
WORKSPACE.mkdir()
os.environ.update({
    "HANGAR_DATA_DIR": str(_TMP / "data"),
    "CLAUDE_CONFIG_DIR": str(_TMP / "claude"),
    "HANGAR_PASSWORD": "test-password",
    "HANGAR_WORKSPACES": str(WORKSPACE),
    "HANGAR_TMUX_SOCKET": str(_TMP / "tmux.sock"),
    "HANGAR_PLUGINS": "ideas",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hangar import auth  # noqa: E402
from hangar.app import app  # noqa: E402
from hangar.ideas import db, token  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.open_db(tmp_path / "ideas.db")
    yield


# Clients are not used as context managers, so Hangar's startup (usage poller, tmux restore) never runs.

@pytest.fixture
def anon():
    return TestClient(app)


@pytest.fixture
def client():
    """The web UI: signed-in cookie plus the X-Hangar header."""
    c = TestClient(app, headers={"X-Hangar": "1"})
    c.cookies.set(auth.COOKIE, auth.make_token())
    return c


@pytest.fixture
def mcp():
    """The MCP server: bearer token only."""
    return TestClient(app, headers={"Authorization": f"Bearer {token.get()}"})


@pytest.fixture
def workspace():
    return WORKSPACE
