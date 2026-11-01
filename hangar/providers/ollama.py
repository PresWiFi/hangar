"""Local model servers: Ollama and llama.cpp (llama-server), on this machine or elsewhere on the network.

Claude Code sessions use a server's Anthropic-compatible API (both have one at /v1/messages). Codex uses
Ollama through --oss, and llama.cpp as a custom provider on its Responses API (/v1/responses).

The module keeps its old name and storage key so servers added before llama.cpp support still load; a
record without a "kind" is an Ollama server.
"""
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
KINDS = {
    "ollama": {"label": "Ollama", "port": 11434},
    "llamacpp": {"label": "llama.cpp", "port": 9931},
}
# Claude Code's system prompt and tools take tens of thousands of tokens; less context than this fails fast.
MIN_CONTEXT = 32768
# Where servers usually are: in the same container or machine, or on the Docker host. 8080 is llama-server's
# old default port (and its Docker image's), so it's checked too, but only counts if the answer is llama.cpp's.
CANDIDATES = [
    ("http://127.0.0.1:11434", "ollama"), ("http://host.docker.internal:11434", "ollama"),
    ("http://127.0.0.1:9931", "llamacpp"), ("http://host.docker.internal:9931", "llamacpp"),
    ("http://127.0.0.1:8080", "llamacpp"), ("http://host.docker.internal:8080", "llamacpp"),
]


def label(kind: str | None) -> str:
    return KINDS.get(kind or "ollama", KINDS["ollama"])["label"]


def _kind(kind: str | None) -> str:
    kind = kind or "ollama"
    if kind not in KINDS:
        raise ValueError("The server type must be Ollama or llama.cpp.")
    return kind


def normalize(url: str, kind: str = "ollama") -> str:
    kind = _kind(kind)
    url = (url or "").strip()
    if not url:
        raise ValueError(f"Enter the server's address, for example http://192.168.1.20:{KINDS[kind]['port']}")
    if "://" not in url:
        url = "http://" + url
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("The address must start with http:// or https://")
    path = u.path.rstrip("/")
    if path.endswith("/v1"):  # that's the OpenAI-style API; Hangar needs the root
        path = path[:-3]
    # A bare http://host means the default port; https or a path usually means a reverse proxy.
    port = f":{u.port}" if u.port else (f":{KINDS[kind]['port']}" if u.scheme == "http" and not path else "")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}{port}{path}"


def servers() -> list[dict]:
    return store.kv_get(KEY, []) or []


def get(server_id: str) -> dict | None:
    return next((s for s in servers() if s["id"] == server_id), None)


def public(server: dict) -> dict:
    """A server record without its API key."""
    out = {k: v for k, v in server.items() if k != "api_key"}
    out["kind"] = server.get("kind") or "ollama"
    out["has_key"] = bool(server.get("api_key"))
    return out


def add(name: str, url: str, kind: str = "ollama", api_key: str = "") -> dict:
    kind = _kind(kind)
    url = normalize(url, kind)
    current = servers()
    if any(s["url"] == url for s in current):
        raise ValueError("That server is already added.")
    server = {"id": secrets.token_hex(4), "name": (name or "").strip()[:60] or urllib.parse.urlsplit(url).hostname,
              "url": url, "kind": kind}
    if kind == "llamacpp" and api_key.strip():
        server["api_key"] = api_key.strip()
    store.kv_set(KEY, current + [server])
    return public(server)


def remove(server_id: str) -> None:
    store.kv_set(KEY, [s for s in servers() if s["id"] != server_id])


def _get_json(url: str, timeout: float, api_key: str = "") -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def _params(n) -> str | None:
    """8030261312 -> '8B', like Ollama's parameter_size."""
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if n >= size:
            v = n / size
            return f"{v:.0f}{unit}" if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".") + unit
    return None


def probe(url: str, kind: str = "ollama", api_key: str = "", timeout: float = 4) -> dict:
    """{'ok', 'version', 'models': [{'name', 'size'}], 'error', 'warning'}"""
    try:
        if (kind or "ollama") == "llamacpp":
            return _probe_llamacpp(url, api_key, timeout)
        version = _get_json(f"{url}/api/version", timeout).get("version")
        tags = _get_json(f"{url}/api/tags", timeout).get("models") or []
        models = [{"name": m.get("name"), "size": (m.get("details") or {}).get("parameter_size")} for m in tags]
        return {"ok": True, "version": version, "models": models}
    except Exception as e:  # noqa: BLE001 - shown to the person
        return {"ok": False, "error": f"Can't reach {url}: {_why(e, kind)}"}


def _probe_llamacpp(url: str, api_key: str, timeout: float) -> dict:
    try:
        _get_json(f"{url}/health", timeout)
    except urllib.error.HTTPError as e:
        if e.code != 503:
            raise
        return {"ok": False, "error": f"{url} is still loading its model. Try again in a moment."}
    data = _get_json(f"{url}/v1/models", timeout, api_key).get("data") or []
    models = [{"name": m.get("id"), "size": _params((m.get("meta") or {}).get("n_params")),
               "owned_by": m.get("owned_by")} for m in data if m.get("id")]
    out: dict = {"ok": True, "version": None, "models": [{"name": m["name"], "size": m["size"]} for m in models],
                 "llamacpp": any(m["owned_by"] == "llamacpp" for m in models)}
    try:  # one model per server: its context size and build. Router mode wants ?model=, so this may fail.
        props = _get_json(f"{url}/props", timeout, api_key)
        out["version"] = props.get("build_info")
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        if isinstance(n_ctx, int) and 0 < n_ctx < MIN_CONTEXT:
            out["warning"] = (f"Context is {n_ctx:,} tokens; Claude Code needs at least {MIN_CONTEXT:,}. "
                              "Start llama-server with a larger -c, for example -c 65536.")
    except Exception:  # noqa: BLE001 - extra details only
        pass
    return out


def _why(e: Exception, kind: str = "ollama") -> str:
    name = label(kind)
    reason = getattr(e, "reason", None) or e
    if isinstance(reason, ConnectionRefusedError):
        if kind == "llamacpp":
            return "nothing is listening there. Is llama-server running with --host 0.0.0.0, and is the port right?"
        return "nothing is listening there. Is Ollama running, and set to listen on the network (OLLAMA_HOST=0.0.0.0)?"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
        return "no answer (timed out). Check the address and any firewall."
    if isinstance(reason, socket.gaierror):
        return "unknown host name."
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401 and kind == "llamacpp":
            return "it needs an API key (llama-server --api-key). Add the server again with the key."
        return f"it answered with HTTP {e.code}, so it may not be {name}."
    if isinstance(e, ValueError):
        return f"it answered, but not like {name} does."
    return str(reason)


def status() -> dict:
    current = servers()
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(lambda s: probe(s["url"], s.get("kind") or "ollama", s.get("api_key") or ""), current))
    return {"servers": [{**public(s), **p} for s, p in zip(current, probes)]}


def discover() -> list[dict]:
    """Servers answering at the usual addresses that aren't added yet."""
    known = {s["url"] for s in servers()}
    todo = [(u, k) for u, k in CANDIDATES if u not in known]
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = list(pool.map(lambda c: (c, probe(c[0], c[1], timeout=2)), todo))
    # Port 8080 is used by lots of things; only count it when the server says it's llama.cpp.
    return [{"url": u, "kind": k, "label": label(k), **p} for (u, k), p in found
            if p["ok"] and (k != "llamacpp" or p.get("llamacpp"))]


def context_tokens(server: dict, model: str, timeout: float = 5) -> int | None:
    """The model's context size on a llama.cpp server, or None. ?model= makes a router-mode server answer for
    that model (a single-model server ignores it)."""
    if (server.get("kind") or "ollama") != "llamacpp":
        return None
    try:
        props = _get_json(f"{server['url']}/props?model={urllib.parse.quote(model, safe='')}", timeout,
                          server.get("api_key") or "")
    except Exception:  # noqa: BLE001 - optional; Claude Code then assumes its default
        return None
    n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
    return n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None


def launch_info(server: dict, model: str) -> dict:
    """What tmux.new_session needs to run an agent on this server's model."""
    return {"server": server["id"], "url": server["url"], "model": model,
            "kind": server.get("kind") or "ollama", "api_key": server.get("api_key") or ""}
