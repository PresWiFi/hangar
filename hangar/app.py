"""Hangar web server."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, auth, files, plugins, store, tmux
from .providers.api import router as providers_router
from .config import settings
from .sessions import index, transcript
from .store import all_meta, set_meta
from .usage import poller

STATIC = Path(__file__).with_name("static")
PUBLIC_API = {"/api/login", "/api/me", "/api/auth/state", "/api/setup"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    for plugin in PLUGINS:
        if plugin.startup:
            await asyncio.to_thread(plugin.startup)
    poller.start()
    await asyncio.to_thread(index.refresh, 0)
    await asyncio.to_thread(tmux.snapshot_for_restore)
    if settings.autorestore:
        started = await asyncio.to_thread(tmux.restore, _resumable)
        if started:
            print(f"Restored {len(started)} session(s)", flush=True)
    tracker = asyncio.create_task(_track_open_sessions())  # keep a reference so it is not garbage-collected
    print(f"Hangar {__version__} on http://{settings.host}:{settings.port}", flush=True)
    for line in auth.startup_lines():
        print(line, flush=True)
    yield
    tracker.cancel()


async def _track_open_sessions() -> None:
    """Keep the list of open sessions current even when no browser is polling, so restore works."""
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.to_thread(tmux.list_sessions)
        except Exception:
            pass


def _resumable(claude_id: str) -> bool:
    s = index.get(claude_id)
    return bool(s and (s.prompts or s.replies))


app = FastAPI(title="Hangar", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
PLUGINS = plugins.load(settings.plugins)
plugins.active[:] = PLUGINS
app.include_router(providers_router)
for _plugin in PLUGINS:
    app.include_router(_plugin.router)


def _authed(request_or_ws) -> bool:
    return auth.check_token(request_or_ws.cookies.get(auth.COOKIE))


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API and not _plugin_authorized(request, path):
        if not _authed(request):
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        # Custom header blocks cross-site form posts (browsers can't add it without a CORS preflight).
        if request.method not in ("GET", "HEAD") and request.headers.get("x-hangar") != "1":
            return JSONResponse({"detail": "Missing X-Hangar header"}, status_code=403)
    response = await call_next(request)
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _plugin_authorized(request: Request, path: str) -> bool:
    """A plugin's own sign-in (e.g. a bearer token), valid only under that plugin's /api/<name>/ prefix."""
    return any(p.authorize and path.startswith(p.prefix) and p.authorize(request) for p in PLUGINS)


def _err(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


# ---------- auth ----------

def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _signed_in(resp: JSONResponse, request: Request) -> JSONResponse:
    resp.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.MAX_AGE, httponly=True, samesite="lax",
                    secure=settings.secure_cookies or request.url.scheme == "https")
    return resp


@app.get("/api/auth/state")
async def auth_state():
    """What the sign-in screen should show. Public, so it must not reveal anything useful."""
    return {"setup": auth.setup_needed(), "username_hint": auth.username_hint()}


_setup_lock = asyncio.Lock()


class Setup(BaseModel):
    code: str
    username: str
    password: str


@app.post("/api/setup")
async def setup(body: Setup, request: Request):
    if not auth.setup_needed():
        raise HTTPException(409, "Hangar is already set up. Sign in instead.")
    if not auth.allow_attempt(_ip(request)):
        raise HTTPException(429, "Too many attempts. Wait a few minutes.")
    if not auth.check_setup_code(body.code):
        await asyncio.sleep(0.6)
        raise HTTPException(401, "Wrong setup code. Check Hangar's log for the current one.")
    try:
        username = auth.check_username(body.username)
        auth.check_new_password(body.password)
    except ValueError as e:
        _err(e)
    async with _setup_lock:
        if not auth.setup_needed():
            raise HTTPException(409, "Hangar is already set up. Sign in instead.")
        await asyncio.to_thread(auth.create_owner, username, body.password)
        store.kv_set("onboarding_pending", True)  # the welcome page offers to link AI providers next
    print(f'Setup done: owner account "{username}" created.', flush=True)
    return _signed_in(JSONResponse({"ok": True}), request)


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request):
    if auth.setup_needed():
        raise HTTPException(409, "Hangar isn't set up yet.")
    if not auth.allow_attempt(_ip(request)):
        raise HTTPException(429, "Too many attempts. Wait a few minutes.")
    if not await asyncio.to_thread(auth.check_login, body.username, body.password):
        await asyncio.sleep(0.6)
        raise HTTPException(401, "Wrong username or password")
    return _signed_in(JSONResponse({"ok": True}), request)


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------- account ----------

@app.get("/api/account")
async def account():
    o = auth.owner()
    return {"username": o["username"], "created": o.get("created"), "source": o.get("source"),
            "password_changed": o.get("password_changed"), "env_password": bool(settings.password),
            "min_password": auth.MIN_PASSWORD}


class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/account/password")
async def change_password(body: PasswordChange, request: Request):
    o = auth.owner()
    if not await asyncio.to_thread(auth.verify_password, body.current, o["pw_hash"]):
        await asyncio.sleep(0.6)
        raise HTTPException(401, "Current password is wrong")
    try:
        auth.check_new_password(body.new)
    except ValueError as e:
        _err(e)
    await asyncio.to_thread(auth.set_password, body.new)
    return _signed_in(JSONResponse({"ok": True}), request)  # other devices are now signed out


class UsernameChange(BaseModel):
    username: str
    password: str


@app.post("/api/account/username")
async def change_username(body: UsernameChange):
    o = auth.owner()
    if not await asyncio.to_thread(auth.verify_password, body.password, o["pw_hash"]):
        await asyncio.sleep(0.6)
        raise HTTPException(401, "Password is wrong")
    try:
        username = auth.check_username(body.username)
    except ValueError as e:
        _err(e)
    auth.set_username(username)
    return {"ok": True, "username": username}


@app.post("/api/onboarding/done")
async def onboarding_done():
    store.kv_set("onboarding_pending", False)
    return {"ok": True}


@app.post("/api/account/signout-all")
async def signout_all(request: Request):
    auth.sign_out_everywhere()
    return _signed_in(JSONResponse({"ok": True}), request)


@app.get("/api/me")
async def me(request: Request):
    if not _authed(request):
        return JSONResponse({"authed": False}, status_code=401)
    return {"authed": True, "version": __version__, "hostname": settings.hostname, "user": auth.owner()["username"],
            "onboarding": bool(store.kv_get("onboarding_pending")),
            "workspaces": settings.workspaces,
            "claude_dir": str(settings.claude_dir), "tmux_socket": settings.tmux_socket or None,
            "plugins": [p.public() for p in PLUGINS]}


# ---------- sessions ----------

def _merge(summary: dict | None, meta: dict | None, live: dict | None) -> dict:
    out = dict(summary or {})
    m = meta or {}
    out["meta"] = {"name": m.get("name"), "pinned": bool(m.get("pinned")), "archived": bool(m.get("archived")),
                   "notes": m.get("notes")}
    if m.get("name"):
        out["title"] = m["name"]
    out["tmux"] = live
    return out


@app.get("/api/sessions")
async def sessions(q: str = ""):
    summaries, live = await asyncio.gather(asyncio.to_thread(index.list), asyncio.to_thread(tmux.list_sessions))
    meta = all_meta()
    by_claude = {t["claude_id"]: t for t in live if t["claude_id"]}
    matches = await asyncio.to_thread(index.search, q) if q else None
    known = set()
    items = []
    for s in summaries:
        known.add(s["id"])
        if matches is not None and s["id"] not in matches and q.lower() not in (meta.get(s["id"], {}).get("name") or "").lower():
            continue
        items.append(_merge(s, meta.get(s["id"]), by_claude.get(s["id"])))
    # Running tmux sessions without a log yet (new sessions before the first prompt, or plain shells).
    for t in live:
        if t["claude_id"] in known:
            continue
        if q and q.lower() not in (t["label"] or t["name"]).lower():
            continue
        items.insert(0, _merge({"id": t["claude_id"], "title": t["label"] or t["name"], "cwd": t["cwd"],
                                "project": Path(t["cwd"] or "").name, "updated": t["activity"],
                                "started": t["created"], "pending": True}, meta.get(t["claude_id"] or ""), t))
    restorable = [c for c in await asyncio.to_thread(tmux.restore_candidates) if _resumable(c["claude_id"])]
    return {"sessions": items, "restorable": len(restorable)}


@app.get("/api/sessions/{sid}")
async def session(sid: str):
    s = await asyncio.to_thread(index.get, sid)
    live = await asyncio.to_thread(tmux.find_by_claude, sid)
    if not s and not live:
        raise HTTPException(404, "Session not found")
    return _merge(s.summary() if s else {"id": sid, "title": live["label"] or live["name"], "cwd": live["cwd"],
                                         "pending": True}, all_meta().get(sid), live)


@app.get("/api/sessions/{sid}/transcript")
async def session_transcript(sid: str, limit: int = 400):
    s = await asyncio.to_thread(index.get, sid)
    if not s:
        return {"items": []}
    return {"items": await asyncio.to_thread(transcript, s, max(1, min(limit, 5000)))}


class MetaIn(BaseModel):
    name: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    notes: str | None = None


@app.patch("/api/sessions/{sid}/meta")
async def session_meta(sid: str, body: MetaIn):
    return set_meta(sid, **body.model_dump(exclude_none=True))


class ResumeIn(BaseModel):
    fork: bool = False


@app.post("/api/sessions/{sid}/resume")
async def session_resume(sid: str, body: ResumeIn):
    existing = await asyncio.to_thread(tmux.find_by_claude, sid)
    if existing and not body.fork:
        return {"name": existing["name"], "claude_id": sid, "already_running": True}
    s = await asyncio.to_thread(index.get, sid)
    if not s or not s.cwd:
        raise HTTPException(404, "No log for this session, so it can't be resumed")
    label = (all_meta().get(sid) or {}).get("name") or s.custom_title or s.ai_title or Path(s.cwd).name
    try:
        return await asyncio.to_thread(tmux.new_session, s.cwd, label=label, resume=sid, fork=body.fork)
    except tmux.TmuxError as e:
        _err(e)


# ---------- tmux ----------

class NewIn(BaseModel):
    cwd: str
    label: str = ""
    model: str = ""
    permission_mode: str = ""
    shell: bool = False


@app.post("/api/tmux/new")
async def tmux_new(body: NewIn):
    if body.permission_mode and body.permission_mode not in {"default", "acceptEdits", "plan", "auto",
                                                            "bypassPermissions", "manual"}:
        raise HTTPException(400, "Unknown permission mode")
    try:
        return await asyncio.to_thread(tmux.new_session, body.cwd, label=body.label.strip()[:60],
                                       model=body.model.strip(), permission_mode=body.permission_mode,
                                       shell_only=body.shell)
    except tmux.TmuxError as e:
        _err(e)


def _check_name(name: str) -> None:
    if not tmux.valid_name(name):
        raise HTTPException(400, "Bad session name")


@app.get("/api/tmux")
async def tmux_list():
    return {"sessions": await asyncio.to_thread(tmux.list_sessions)}


@app.post("/api/tmux/{name}/kill")
async def tmux_kill(name: str):
    _check_name(name)
    try:
        await asyncio.to_thread(tmux.kill, name)
    except tmux.TmuxError as e:
        _err(e)
    return {"ok": True}


class SendIn(BaseModel):
    text: str = ""
    enter: bool = True
    key: str = ""


@app.post("/api/tmux/{name}/send")
async def tmux_send(name: str, body: SendIn):
    _check_name(name)
    try:
        if body.key:
            await asyncio.to_thread(tmux.send_key, name, body.key)
        else:
            await asyncio.to_thread(tmux.send_text, name, body.text[:100_000], body.enter)
    except tmux.TmuxError as e:
        _err(e)
    return {"ok": True}


@app.get("/api/tmux/{name}/preview")
async def tmux_preview(name: str, lines: int = 40):
    _check_name(name)
    return {"text": await asyncio.to_thread(tmux.capture, name, max(5, min(lines, 500)))}


@app.post("/api/restore")
async def restore():
    return {"started": await asyncio.to_thread(tmux.restore, _resumable)}


@app.post("/api/restore/dismiss")
async def restore_dismiss():
    tmux.dismiss_restore()
    return {"ok": True}


# ---------- folders (new session picker) ----------

_inside_workspace = files.inside_workspace


@app.get("/api/dirs")
async def dirs(path: str = ""):
    if not path:
        return {"path": "", "parent": None, "dirs": [{"name": w, "path": w} for w in settings.workspaces]}
    if not _inside_workspace(path):
        raise HTTPException(403, "Outside the configured workspaces (HANGAR_WORKSPACES)")
    try:
        entries = sorted((e for e in os.scandir(path) if e.is_dir() and not e.name.startswith(".")),
                         key=lambda e: e.name.lower())
    except OSError as e:
        _err(e)
    real = os.path.realpath(path)
    parent = os.path.dirname(real)
    return {"path": real, "parent": parent if _inside_workspace(parent) else "",
            "dirs": [{"name": e.name, "path": os.path.join(real, e.name)} for e in entries[:500]]}


# ---------- files ----------

@app.get("/api/files")
async def files_list(path: str):
    try:
        return await asyncio.to_thread(files.listing, path)
    except files.FileError as e:
        _err(e, e.code)


@app.get("/api/files/download")
async def files_download(path: str, inline: bool = False):
    try:
        real = await asyncio.to_thread(files.download_path, path)
    except files.FileError as e:
        _err(e, e.code)
    resp = FileResponse(real, filename=Path(real).name, content_disposition_type="inline" if inline else "attachment")
    resp.headers["Content-Security-Policy"] = "sandbox"  # never run uploaded HTML/JS in Hangar's origin
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.post("/api/files/upload")
async def files_upload(request: Request, dir: str, name: str, overwrite: bool = False):
    """Raw request body is the file (no multipart), so large files stream straight to disk."""
    try:
        up = await asyncio.to_thread(files.Upload, dir, name, overwrite)
    except files.FileError as e:
        _err(e, e.code)
    except OSError as e:
        _err(e)
    try:
        async for chunk in request.stream():
            if chunk:
                await asyncio.to_thread(up.write, chunk)
        return await asyncio.to_thread(up.finish)
    except files.FileError as e:
        up.abort()
        _err(e, e.code)
    except Exception:
        up.abort()
        raise


# ---------- dashboard ----------

@app.get("/api/overview")
async def overview():
    return {**poller.snapshot(), "stats": await asyncio.to_thread(index.stats)}


@app.post("/api/usage/refresh")
async def usage_refresh():
    poller.refresh_now()
    return {"ok": True}


# ---------- terminal ----------

def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return True  # non-browser client; it still needs the cookie
    return urlparse(origin).netloc == ws.headers.get("host")


@app.websocket("/ws/term/{name}")
async def term(ws: WebSocket, name: str, cols: int = 120, rows: int = 32):
    if not _authed(ws) or not _same_origin(ws) or not tmux.valid_name(name):
        await ws.close(code=4401)
        return
    live = {s["name"] for s in await asyncio.to_thread(tmux.list_sessions, False)}
    await ws.accept()
    if name not in live:
        await ws.send_text('{"type":"gone"}')
        await ws.close()
        return
    from .terminal import bridge
    await bridge(ws, tmux.attach_argv(name), cols, rows)


# ---------- static ----------

@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
