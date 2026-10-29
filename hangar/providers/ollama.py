"""Ollama servers, on this machine or elsewhere on the network. Claude Code and Codex sessions can use their
models (Claude Code through Ollama's Anthropic-compatible API, Codex with --oss)."""
from __future__ import annotations

import json
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .. import store

KEY = "ollama_servers"
# Where Ollama usually is: in the same container or machine, or on the Docker host.
CANDIDATES = ["http://127.0.0.1:11434", "http://host.docker.internal:11434"]


def normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("Enter the server's address, for example http://192.168.1.20:11434")
    if "://" not in url:
        url = "http://" + url
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("The address must start with http:// or https://")
    path = u.path.rstrip("/")
    if path.endswith("/v1"):  # that's Ollama's OpenAI-style API; Hangar needs the root
        path = path[:-3]
    # A bare http://host means Ollama's default port; https or a path usually means a reverse proxy.
    port = f":{u.port}" if u.port else (":11434" if u.scheme == "http" and not path else "")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}{port}{path}"


def servers() -> list[dict]:
    return store.kv_get(KEY, []) or []


def get(server_id: str) -> dict | None:
    return next((s for s in servers() if s["id"] == server_id), None)


def add(name: str, url: str) -> dict:
    url = normalize(url)
    current = servers()
    if any(s["url"] == url for s in current):
        raise ValueError("That server is already added.")
    server = {"id": secrets.token_hex(4), "name": (name or "").strip()[:60] or urllib.parse.urlsplit(url).hostname,
              "url": url}
    store.kv_set(KEY, current + [server])
    return server


def remove(server_id: str) -> None:
    store.kv_set(KEY, [s for s in servers() if s["id"] != server_id])


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def probe(url: str, timeout: float = 4) -> dict:
    """{'ok', 'version', 'models': [{'name', 'size'}], 'error'}"""
    try:
        version = _get_json(f"{url}/api/version", timeout).get("version")
        tags = _get_json(f"{url}/api/tags", timeout).get("models") or []
        models = [{"name": m.get("name"), "size": (m.get("details") or {}).get("parameter_size")} for m in tags]
        return {"ok": True, "version": version, "models": models}
    except Exception as e:  # noqa: BLE001 - shown to the person
        return {"ok": False, "error": f"Can't reach {url}: {_why(e)}"}


def _why(e: Exception) -> str:
    reason = getattr(e, "reason", None) or e
    if isinstance(reason, ConnectionRefusedError):
        return "nothing is listening there. Is Ollama running, and set to listen on the network (OLLAMA_HOST=0.0.0.0)?"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
        return "no answer (timed out). Check the address and any firewall."
    if isinstance(reason, socket.gaierror):
        return "unknown host name."
    if isinstance(e, urllib.error.HTTPError):
        return f"it answered with HTTP {e.code}, so it may not be Ollama."
    if isinstance(e, ValueError):
        return "it answered, but not like Ollama does."
    return str(reason)


def status() -> dict:
    current = servers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(lambda s: probe(s["url"]), current))
    return {"servers": [{**s, **p} for s, p in zip(current, probes)]}


def discover() -> list[dict]:
    """Ollama servers answering at the usual addresses that aren't added yet."""
    known = {s["url"] for s in servers()}
    todo = [u for u in CANDIDATES if u not in known]
    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(lambda u: (u, probe(u, timeout=2)), todo))
    return [{"url": u, **p} for u, p in found if p["ok"]]
