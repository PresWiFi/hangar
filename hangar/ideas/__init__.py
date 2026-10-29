"""Ideas: a project tracker that Claude fills in from any Hangar session. Turn on with HANGAR_PLUGINS=ideas.

The plugin itself lives in .plugin and is imported only when Hangar asks for PLUGIN. That keeps
`python -m hangar.ideas.mcp` (the MCP server Claude starts) light: it doesn't load FastAPI or Hangar's settings.
"""


def __getattr__(name: str):
    if name == "PLUGIN":
        from .plugin import PLUGIN
        return PLUGIN
    raise AttributeError(name)
