"""How Claude sessions reach the Ideas MCP server.

Hangar starts every Claude session with `--mcp-config <data dir>/ideas-mcp.json`. That file names the server
(`python -m hangar.ideas.mcp`) and tells it where Hangar is and where the token file is. The token itself is
never put in the file or on a command line.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shlex
import sys
from pathlib import Path

from ..config import settings
from . import token

SERVER = "hangar-ideas"   # MCP server name; Claude sees the tools as mcp__hangar-ideas__ideas_*
CONFIG_FILE = "ideas-mcp.json"
ROOT = Path(__file__).resolve().parents[2]  # folder that contains the hangar package


def hangar_url() -> str:
    """Where the MCP server can reach this Hangar from the same machine."""
    host = settings.host.strip()
    if host in {"", "0.0.0.0", "::", "*"}:
        host = "127.0.0.1"
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # a hostname
    return f"http://{host}:{settings.port}"


def server_entry(*, token_value: str | None = None) -> dict:
    """The stdio server entry. By default it points at the token file; pass token_value to embed the token."""
    env = {"HANGAR_URL": hangar_url(), "PYTHONPATH": str(ROOT)}
    if token_value:
        env["HANGAR_IDEAS_TOKEN"] = token_value
    else:
        env["HANGAR_IDEAS_TOKEN_FILE"] = str(token.path())
    return {"command": sys.executable, "args": ["-m", "hangar.ideas.mcp"], "env": env}


def config_path() -> Path:
    """Write the MCP config file if it's missing or out of date, and return its path."""
    path = settings.data_dir / CONFIG_FILE
    text = json.dumps({"mcpServers": {SERVER: server_entry()}}, indent=2) + "\n"
    try:
        current = path.read_text()
    except OSError:
        current = None
    if current != text:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    return path


def claude_args() -> list[str]:
    """Arguments Hangar adds to every Claude session: the server, and permission to use its tools without asking."""
    return ["--mcp-config", str(config_path()), "--allowedTools", f"mcp__{SERVER}__*"]


def add_command(url: str | None = None) -> str:
    """`claude mcp add` command for a Claude Code session started outside Hangar, on this machine."""
    env = server_entry(token_value=token.get())["env"]
    if url:
        env["HANGAR_URL"] = url
    parts = ["claude", "mcp", "add", "--scope", "user", "--transport", "stdio"]
    for key, value in env.items():
        parts += ["-e", f"{key}={value}"]
    parts += [SERVER, "--", sys.executable, "-m", "hangar.ideas.mcp"]
    return " ".join(shlex.quote(p) for p in parts)
