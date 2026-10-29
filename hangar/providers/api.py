"""/api/providers: link and unlink Claude, ChatGPT (Codex), and Ollama. Owner only (normal cookie + header)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import claude, codex, ollama

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
    c, x, o = await asyncio.gather(asyncio.to_thread(claude.status), asyncio.to_thread(codex.status),
                                   asyncio.to_thread(ollama.status))
    return {"claude": c, "codex": x, "ollama": o}


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
