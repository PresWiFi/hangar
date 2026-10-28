"""The Ideas plugin definition, loaded by Hangar when HANGAR_PLUGINS includes ideas."""
from __future__ import annotations

from ..plugins import Plugin
from . import api, connect, db, token


def _startup() -> None:
    db.conn()            # create or upgrade ideas.db
    token.get()          # create the MCP token on first start
    connect.config_path()  # write the MCP config that sessions are started with


PLUGIN = Plugin(name="ideas", router=api.router, startup=_startup, authorize=token.authorize,
                nav={"label": "Ideas", "href": "#/ideas"}, script="/api/ideas/ui.js", style="/api/ideas/ui.css",
                claude_args=connect.claude_args)
