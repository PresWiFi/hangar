"""Optional plugins, turned on by name with HANGAR_PLUGINS (for example HANGAR_PLUGINS=ideas).

A plugin is a package `hangar.<name>` that defines `PLUGIN = Plugin(...)`. When no plugin is turned on,
nothing here is imported and Hangar behaves exactly as without this module.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Request


@dataclass
class Plugin:
    name: str
    router: APIRouter                                  # routes under /api/<name>/
    startup: Callable[[], None] | None = None         # runs once when Hangar starts (in a thread)
    # Extra way to sign in, checked only for requests under /api/<name>/ (for example a bearer token).
    # Returning False falls back to the normal cookie and X-Hangar checks.
    authorize: Callable[[Request], bool] | None = None
    nav: dict | None = None        # sidebar link, e.g. {"label": "Ideas", "href": "#/ideas"}
    script: str | None = None      # front-end script the UI loads at startup
    style: str | None = None       # and its stylesheet

    def public(self) -> dict:
        """What the web UI needs to know (sent in /api/me)."""
        return {"name": self.name, "nav": self.nav, "script": self.script, "style": self.style}

    @property
    def prefix(self) -> str:
        return f"/api/{self.name}/"


def load(names: list[str]) -> list[Plugin]:
    plugins: list[Plugin] = []
    for name in dict.fromkeys(names):  # keep order, drop repeats
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            print(f"Plugin {name!r} skipped: bad name", flush=True)
            continue
        module_name = f"{__package__.rpartition('.')[0]}.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            if e.name != module_name:
                raise  # the plugin exists but is broken; fail loudly
            print(f"Plugin {name!r} skipped: not found", flush=True)
            continue
        plugin = getattr(module, "PLUGIN", None)
        if not isinstance(plugin, Plugin) or plugin.name != name:
            print(f"Plugin {name!r} skipped: {module_name} has no matching PLUGIN", flush=True)
            continue
        plugins.append(plugin)
    return plugins
