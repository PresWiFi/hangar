"""Settings, read once from environment variables."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str) -> list[str]:
    return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = "0.0.0.0"
    hostname: str = ""             # name shown in the UI
    port: int = 7878
    data_dir: Path = Path.home() / ".local" / "share" / "hangar"
    claude_dir: Path = Path.home() / ".claude"
    # Folders new sessions may start in. The first one is the default.
    workspaces: list[str] = field(default_factory=list)
    password: str = ""
    tmux_socket: str = ""             # empty = tmux's default socket
    claude_cmd: str = "claude"
    claude_args: str = ""             # extra arguments for every new or resumed session
    claude_base_args: str = ""        # set by the Docker image (permission rules, container notes)
    autorestore: bool = False         # reopen sessions that were running when Hangar last stopped
    usage_poll_minutes: float = 5
    secure_cookies: bool = False      # set when served over HTTPS only
    max_upload_mb: int = 512
    plugins: list[str] = field(default_factory=list)  # optional plugins to turn on, e.g. ["ideas"]

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        s = cls()
        s.host = e.get("HANGAR_HOST", s.host)
        s.hostname = e.get("HANGAR_HOSTNAME") or socket.gethostname()
        s.port = int(e.get("HANGAR_PORT", s.port))
        s.data_dir = Path(e.get("HANGAR_DATA_DIR") or s.data_dir).expanduser()
        s.claude_dir = Path(e.get("CLAUDE_CONFIG_DIR") or s.claude_dir).expanduser()
        s.workspaces = _list(e.get("HANGAR_WORKSPACES", "")) or [str(Path.home())]
        s.password = e.get("HANGAR_PASSWORD", "")
        s.tmux_socket = e.get("HANGAR_TMUX_SOCKET", "")
        s.claude_cmd = e.get("HANGAR_CLAUDE_CMD", s.claude_cmd)
        s.claude_args = e.get("HANGAR_CLAUDE_ARGS", "")
        s.claude_base_args = e.get("HANGAR_CLAUDE_BASE_ARGS", "")
        s.autorestore = _bool(e.get("HANGAR_AUTORESTORE"))
        s.usage_poll_minutes = max(1.0, float(e.get("HANGAR_USAGE_POLL_MINUTES", s.usage_poll_minutes)))
        s.secure_cookies = _bool(e.get("HANGAR_SECURE_COOKIES"))
        s.max_upload_mb = max(1, int(e.get("HANGAR_MAX_UPLOAD_MB") or s.max_upload_mb))
        s.plugins = [p.lower() for p in _list(e.get("HANGAR_PLUGINS", ""))]
        s.data_dir.mkdir(parents=True, exist_ok=True)
        return s


settings = Settings.from_env()
