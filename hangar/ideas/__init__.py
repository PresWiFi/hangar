"""Ideas: a project tracker that Claude fills in from any Hangar session. Turn on with HANGAR_PLUGINS=ideas."""
from __future__ import annotations

from ..plugins import Plugin
from . import api, db, token


def _startup() -> None:
    db.conn()   # create or upgrade ideas.db
    token.get()  # create the MCP token on first start


PLUGIN = Plugin(name="ideas", router=api.router, startup=_startup, authorize=token.authorize,
                nav={"label": "Ideas", "href": "#/ideas"}, script="/api/ideas/ui.js", style="/api/ideas/ui.css")
