"""/api/providers: link and unlink Claude, ChatGPT (Codex), Ollama, and GitHub. Owner only (normal cookie + header)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import files
from . import claude, codex, github, ollama

router = APIRouter(prefix="/api/providers")


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("")
async def all_status():
    c, x, o, g = await asyncio.gather(asyncio.to_thread(claude.status), asyncio.to_thread(codex.status),
                                      asyncio.to_thread(ollama.status), asyncio.to_thread(github.status))
    return {"claude": c, "codex": x, "ollama": o, "github": g}


# ---------- Claude ----------

class Code(BaseModel):
    code: str


@router.post("/claude/login")
async def claude_login():
    return await _run(claude.start_login)


@router.post("/claude/code")
async def claude_code(body: Code):
    return await _run(claude.submit_code, body.code)


@router.post("/claude/cancel")
async def claude_cancel():
    await _run(claude.cancel_login)
    return {"ok": True}


@router.post("/claude/logout")
async def claude_logout():
    return await _run(claude.logout)


# ---------- ChatGPT (Codex) ----------

@router.get("/codex")
async def codex_status():
    return await _run(codex.status)


@router.post("/codex/install")
async def codex_install():
    return await _run(codex.start_install)


class CodexLogin(BaseModel):
    mode: str = "device"


@router.post("/codex/login")
async def codex_login(body: CodexLogin):
    return await _run(codex.start_login, body.mode)


class Callback(BaseModel):
    url: str


@router.post("/codex/callback")
async def codex_callback(body: Callback):
    return await _run(codex.finish_browser_login, body.url)


@router.post("/codex/cancel")
async def codex_cancel():
    await _run(codex.cancel_login)
    return {"ok": True}


@router.post("/codex/logout")
async def codex_logout():
    return await _run(codex.logout)


# ---------- Ollama ----------

class Server(BaseModel):
    name: str = ""
    url: str


@router.get("/ollama")
async def ollama_status():
    return await _run(ollama.status)


@router.get("/ollama/discover")
async def ollama_discover():
    return {"found": await _run(ollama.discover)}


@router.post("/ollama/test")
async def ollama_test(body: Server):
    url = await _run(ollama.normalize, body.url)
    return {"url": url, **await _run(ollama.probe, url)}


@router.post("/ollama")
async def ollama_add(body: Server):
    return await _run(ollama.add, body.name, body.url)


@router.delete("/ollama/{server_id}")
async def ollama_remove(server_id: str):
    if not ollama.get(server_id):
        raise HTTPException(404, "No such server")
    await _run(ollama.remove, server_id)
    return {"ok": True}


# ---------- GitHub ----------

@router.get("/github")
async def github_status():
    return await _run(github.status)


@router.post("/github/login")
async def github_login():
    return await _run(github.start_login)


class Token(BaseModel):
    token: str


@router.post("/github/token")
async def github_token(body: Token):
    return await _run(github.login_with_token, body.token)


@router.post("/github/cancel")
async def github_cancel():
    await _run(github.cancel_login)
    return {"ok": True}


@router.post("/github/logout")
async def github_logout():
    return await _run(github.logout)


@router.get("/github/repos")
async def github_repos():
    return {"repos": await _run(github.repos)}


class Clone(BaseModel):
    repo: str
    parent: str


@router.post("/github/clone")
async def github_clone(body: Clone):
    if not body.parent or not files.inside_workspace(body.parent):
        raise HTTPException(403, "Outside the configured workspaces (HANGAR_WORKSPACES)")
    return await _run(github.clone, body.repo, body.parent)
